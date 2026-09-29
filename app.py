#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import math
import mimetypes
import os
import random
import re
import secrets
import string
import textwrap
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from cfshot.db import (
    aliases_for,
    db_path,
    dump_json,
    get_db,
    init_db,
    load_json,
    matching_question_ids,
    parse_filters,
    public_user,
)


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
SESSION_DAYS = 30
ROUND_SECONDS = 60
REVEAL_SECONDS = 4
RATED_MIN_CONTEST_SPAN = 200
RATED_MIN_YEAR_SPAN = 2
RATED_MIN_POOL_SIZE = 8
RATED_SOLO_SECONDS = 120
QUIZ_RETRY_SECONDS = 5
QUIZ_MAX_ATTEMPTS = 10
RECENT_EXPOSURE_LIMIT = 12
RECENT_CORRECT_LIMIT = 24
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
CONTEST_ANSWER_RE = re.compile(r"^\s*(\d{1,6})\s*[-_/ ]?\s*([A-Za-z][A-Za-z0-9]?)\s*$")
INDEX_RE = re.compile(r"^[A-Z][A-Z0-9]?")
RATE_BUCKETS = defaultdict(deque)
RATE_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = 3 * 1024 * 1024
IMAGE_FORMATS = {
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8\xff"),
    "image/webp": (".webp", b"RIFF"),
}
OPEN_MATCH_THRESHOLD = 0.9
OPEN_VERIFY_TIMEOUT = 6
DISTANCE_MAX_SCORE = 5000
DISTANCE_ACCURACY_SCORE = 4500
DISTANCE_TIME_SCORE = 500
DISTANCE_SIGMA = 75
DISTANCE_INDEX_WEIGHT = 75
DAILY_QUESTION_COUNT = 5
DAILY_SECONDS = 120
CHINA_TIMEZONE = timezone(timedelta(hours=8))


def now():
    return time.time()


def bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def parse_contest_answer(value):
    match = CONTEST_ANSWER_RE.fullmatch(str(value or ""))
    if not match:
        return None
    return int(match.group(1)), match.group(2).upper()


def problem_index_rank(problem_index):
    match = re.fullmatch(r"([A-Z])(\d*)", str(problem_index or "").upper())
    if not match:
        return None
    base = ord(match.group(1)) - ord("A")
    suffix = int(match.group(2)) if match.group(2) else 1
    return base + (suffix - 1) * 0.35


def distance_score(question_id, contest_id, problem_index, elapsed_seconds, time_limit):
    guessed_rank = problem_index_rank(problem_index)
    if guessed_rank is None:
        raise ValueError("题号请使用 A、C 或 E2 这样的格式")
    candidates = []
    for alias in aliases_for(question_id):
        rank = problem_index_rank(alias["problem_index"])
        if rank is None:
            continue
        contest_gap = abs(contest_id - alias["contest_id"])
        index_gap = abs(guessed_rank - rank)
        distance = contest_gap + DISTANCE_INDEX_WEIGHT * index_gap
        candidates.append((distance, contest_gap, index_gap, alias))
    if not candidates:
        raise ValueError("这道题还没有可用于计分的标准答案")
    distance, contest_gap, index_gap, nearest = min(candidates, key=lambda item: item[0])
    accuracy = math.exp(-0.5 * (distance / DISTANCE_SIGMA) ** 2)
    remaining_ratio = 1.0 if time_limit <= 0 else max(0.0, min(1.0, (time_limit - elapsed_seconds) / time_limit))
    score = round(accuracy * (DISTANCE_ACCURACY_SCORE + DISTANCE_TIME_SCORE * math.sqrt(remaining_ratio)))
    if score < 5:
        score = 0
    return {
        "score": min(DISTANCE_MAX_SCORE, score),
        "distance": round(distance, 2),
        "contestGap": contest_gap,
        "indexGap": round(index_gap, 2),
        "exact": contest_gap == 0 and index_gap == 0,
        "nearest": f'{nearest["contest_id"]}{nearest["problem_index"]}',
    }


def daily_day(timestamp=None):
    return datetime.fromtimestamp(timestamp or now(), CHINA_TIMEZONE).date().isoformat()


def record_question_exposure(user_id, question_id, source, source_key):
    get_db().execute(
        """
        INSERT INTO question_exposures(user_id,question_id,source,source_key,outcome,seen_at)
        VALUES(?,?,?,?, 'seen', ?)
        ON CONFLICT(user_id,source,source_key) DO NOTHING
        """,
        (user_id, question_id, source, str(source_key), now()),
    )


def settle_question_exposure(user_id, source, source_key, outcome):
    get_db().execute(
        "UPDATE question_exposures SET outcome=? WHERE user_id=? AND source=? AND source_key=?",
        (outcome, user_id, source, str(source_key)),
    )


def select_fresh_questions(candidates, count, user_ids):
    """Prefer unseen questions, then oldest seen; only reuse recent questions as a last resort."""
    candidates = list(dict.fromkeys(candidates))
    user_ids = [user_id for user_id in dict.fromkeys(user_ids) if user_id]
    if not candidates or not user_ids:
        return random.sample(candidates, min(count, len(candidates)))
    db = get_db()
    placeholders = ",".join("?" for _ in user_ids)
    last_seen = {
        row["question_id"]: row["last_seen"]
        for row in db.execute(
            f"SELECT question_id,MAX(seen_at) last_seen FROM question_exposures "
            f"WHERE user_id IN ({placeholders}) GROUP BY question_id",
            user_ids,
        )
    }
    protected = set()
    for user_id in user_ids:
        protected.update(
            row["question_id"] for row in db.execute(
                "SELECT question_id FROM question_exposures WHERE user_id=? ORDER BY seen_at DESC LIMIT ?",
                (user_id, RECENT_EXPOSURE_LIMIT),
            )
        )
        protected.update(
            row["question_id"] for row in db.execute(
                """
                SELECT question_id FROM question_exposures
                WHERE user_id=? AND outcome='correct' ORDER BY seen_at DESC LIMIT ?
                """,
                (user_id, RECENT_CORRECT_LIMIT),
            )
        )
    unseen = [question_id for question_id in candidates if question_id not in last_seen]
    reusable = [question_id for question_id in candidates if question_id in last_seen and question_id not in protected]
    recent = [question_id for question_id in candidates if question_id in protected]
    random.shuffle(unseen)
    random.shuffle(reusable)
    random.shuffle(recent)
    reusable.sort(key=last_seen.get)
    recent.sort(key=last_seen.get)
    return (unseen + reusable + recent)[:min(count, len(candidates))]


def hash_password(password, salt=None):
    salt_bytes = base64.b64decode(salt) if salt else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 260_000)
    return base64.b64encode(digest).decode(), base64.b64encode(salt_bytes).decode()


def verify_password(password, encoded, salt):
    actual, _ = hash_password(password, salt)
    return hmac.compare_digest(actual, encoded)


def rate_limited(key, limit=30, window=60):
    current = now()
    with RATE_LOCK:
        bucket = RATE_BUCKETS[key]
        while bucket and bucket[0] < current - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return True
        bucket.append(current)
        return False


def normalize_division(value):
    raw = (value or "").lower().replace("division", "div").replace(".", "").replace(" ", "")
    if raw in {"12", "1+2", "div12", "div1+2", "div1+div2"}:
        return "Div. 1 + Div. 2"
    if raw in {"div1", "1"}:
        return "Div. 1"
    if raw in {"div2", "2"}:
        return "Div. 2"
    if raw in {"div3", "3"}:
        return "Div. 3"
    if raw in {"div4", "4"}:
        return "Div. 4"
    if raw in {"e", "edu", "educational"}:
        return "Edu"
    return value or ""


def check_answer(question_id, payload):
    aliases = aliases_for(question_id)
    answer_mode = payload.get("answerMode", "contest")
    if answer_mode == "contest":
        raw = str(payload.get("contestAnswer", ""))
        match = CONTEST_ANSWER_RE.match(raw)
        if not match:
            return False, raw.strip(), "请使用 2269E 这样的格式"
        contest_id, index = int(match.group(1)), match.group(2).upper()
        correct = any(a["contest_id"] == contest_id and a["problem_index"].upper() == index for a in aliases)
        return correct, f"{contest_id}{index}", None

    try:
        round_number = int(payload.get("roundNumber"))
    except (TypeError, ValueError):
        return False, "", "Round 编号必须是数字"
    index = str(payload.get("roundIndex", "")).strip().upper()
    if not INDEX_RE.fullmatch(index):
        return False, f"Round {round_number} {index}", "题号应为 A、C 或 E2 这样的格式"
    division = normalize_division(payload.get("division"))
    valid_divisions = {"Div. 1", "Div. 2", "Div. 3", "Div. 4", "Div. 1 + Div. 2", "Edu"}
    if division and division not in valid_divisions:
        return False, f"Round {round_number} {division} {index}", "组别请输入 1、2、3、4、12 或 E"
    round_divisions = {
        row["division"] for row in get_db().execute(
            "SELECT DISTINCT division FROM aliases WHERE round_number=? AND division!='Open'",
            (round_number,),
        )
    }
    if not division and len(round_divisions) > 1:
        return False, f"Round {round_number} {index}", "这个 Round 有多个组别，请补填 1、2、3、4、12 或 E"
    correct = any(
        a["round_number"] == round_number
        and a["problem_index"].upper() == index
        and (not division or a["division"] == division)
        for a in aliases
    )
    shown = f"Round {round_number} {division} {index}".replace("  ", " ").strip()
    return correct, shown, None


class ProblemTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skipped = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skipped += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skipped:
            self.skipped -= 1

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)


def normalize_problem_text(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))


def problem_text_similarity(reference, candidate):
    reference_tokens = normalize_problem_text(reference).split()
    candidate_tokens = normalize_problem_text(candidate).split()
    if len(reference_tokens) < 12 or len(candidate_tokens) < 12:
        return 0.0
    reference_text = " ".join(reference_tokens)
    candidate_text = " ".join(candidate_tokens)
    if reference_text in candidate_text:
        return 1.0
    size = 5 if len(reference_tokens) >= 20 else 3
    reference_shingles = {tuple(reference_tokens[i:i + size]) for i in range(len(reference_tokens) - size + 1)}
    candidate_shingles = {tuple(candidate_tokens[i:i + size]) for i in range(len(candidate_tokens) - size + 1)}
    return len(reference_shingles & candidate_shingles) / max(1, len(reference_shingles))


def fetch_codeforces_problem_text(contest_id, problem_index):
    url = f"https://codeforces.com/contest/{contest_id}/problem/{problem_index}"
    request = Request(url, headers={"User-Agent": "CF-Snap/1.0 (+https://cf-tuxun.wannafly.cn)"})
    with urlopen(request, timeout=OPEN_VERIFY_TIMEOUT) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("题面页面过大")
        charset = response.headers.get_content_charset() or "utf-8"
    parser = ProblemTextExtractor()
    parser.feed(data.decode(charset, errors="replace"))
    return " ".join(parser.parts)


def parse_contest_answers(value, limit=20):
    answers = []
    for raw in re.split(r"[,，;；\s]+", str(value or "")):
        if not raw:
            continue
        match = CONTEST_ANSWER_RE.fullmatch(raw)
        if not match:
            raise ValueError(f"无法识别题号：{raw}")
        answer = (int(match.group(1)), match.group(2).upper())
        if answer not in answers:
            answers.append(answer)
        if len(answers) > limit:
            raise ValueError(f"最多填写 {limit} 个题号")
    return answers


def resolve_open_answer(question, user_id, payload):
    if not question["open_mode"] or payload.get("answerMode", "contest") != "contest":
        return None
    match = CONTEST_ANSWER_RE.fullmatch(str(payload.get("contestAnswer", "")))
    if not match:
        return None
    contest_id, problem_index = int(match.group(1)), match.group(2).upper()
    shown = f"{contest_id}{problem_index}"
    db = get_db()
    existing = db.execute(
        """
        SELECT * FROM open_answer_candidates
        WHERE question_id=? AND contest_id=? AND problem_index=?
        """,
        (question["id"], contest_id, problem_index),
    ).fetchone()
    if existing:
        db.execute("UPDATE open_answer_candidates SET hit_count=hit_count+1 WHERE id=?", (existing["id"],))
        if existing["status"] in {"approved", "auto_approved"}:
            db.execute(
                "INSERT OR IGNORE INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
                (question["id"], contest_id, problem_index, 0, "Open"),
            )
            return {"status": "correct", "shown": shown, "similarity": existing["similarity"]}
        if existing["status"] == "pending":
            return {"status": "pending", "shown": shown, "candidateId": existing["id"]}
        return None

    similarity = 0.0
    verification_text = question["verification_text"].strip()
    if verification_text:
        try:
            candidate_text = fetch_codeforces_problem_text(contest_id, problem_index)
            similarity = problem_text_similarity(verification_text, candidate_text)
        except Exception:
            similarity = 0.0
    status = "auto_approved" if similarity >= OPEN_MATCH_THRESHOLD else "pending"
    cursor = db.execute(
        """
        INSERT INTO open_answer_candidates(
            question_id,requester_id,contest_id,problem_index,status,similarity,created_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (question["id"], user_id, contest_id, problem_index, status, similarity, int(now())),
    )
    if status == "auto_approved":
        db.execute(
            "INSERT OR IGNORE INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
            (question["id"], contest_id, problem_index, 0, "Open"),
        )
        return {"status": "correct", "shown": shown, "similarity": similarity}
    return {"status": "pending", "shown": shown, "candidateId": cursor.lastrowid}


def solution_for(question):
    aliases = aliases_for(question["id"])
    return {
        "title": question["title"],
        "rating": question["rating"],
        "sourceUrl": question["source_url"],
        "answers": [
            {
                "contest": f'{a["contest_id"]}{a["problem_index"]}',
                "round": f'Round {a["round_number"]} {a["division"]} {a["problem_index"]}' if a["round_number"] else None,
            }
            for a in aliases
        ],
    }


def question_public(question, token, difficulty):
    aliases = aliases_for(question["id"])
    divisions = sorted({a["division"] for a in aliases if a["round_number"]})
    return {
        "token": token,
        "clueUrl": f"/api/clues/{token}",
        "difficulty": difficulty,
        "needsDivision": True,
        "divisions": divisions,
        "timeLimit": ROUND_SECONDS,
        "openMode": bool(question["open_mode"]),
    }


def make_clue_svg(question, difficulty="medium"):
    clue = question["clue"].strip()
    widths = {"easy": 80, "medium": 75, "hard": 70, "all": 75, "brain": 70}
    lines = []
    for paragraph in clue.split("\n"):
        lines.extend(textwrap.wrap(paragraph, width=widths.get(difficulty, 75), break_long_words=False) or [""])
    line_height = 42
    start_y = 232
    escaped_lines = []
    for i, line in enumerate(lines[:10]):
        escaped_lines.append(
            f'<text x="108" y="{start_y + i * line_height}" class="body">{html.escape(line)}</text>'
        )
    label = {"easy": "EASY", "medium": "MEDIUM", "hard": "HARD", "all": "MIXED", "brain": "BRAIN"}.get(difficulty, "MEDIUM")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="760" viewBox="0 0 1280 760">
<rect width="1280" height="760" fill="#e8ebed"/>
<rect x="58" y="48" width="1164" height="664" rx="4" fill="#ffffff" stroke="#c8cdd1"/>
<rect x="58" y="48" width="10" height="664" fill="#c53232"/>
<text x="108" y="108" class="eyebrow">CODEFORCES  /  PROBLEM STATEMENT</text>
<text x="1132" y="108" text-anchor="end" class="level">{label}</text>
<line x1="108" y1="137" x2="1172" y2="137" stroke="#d8dcdf"/>
<text x="108" y="188" class="section">Statement</text>
{''.join(escaped_lines)}
<line x1="108" y1="652" x2="1172" y2="652" stroke="#e1e4e6"/>
<text x="108" y="684" class="foot">Title, contest number and problem index are hidden.</text>
<style>
text {{ font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; fill: #202528; }}
.eyebrow {{ font-size: 18px; font-weight: 700; letter-spacing: 1px; fill: #5c646a; }}
.level {{ font-size: 16px; font-weight: 700; fill: #087f74; }}
.section {{ font-size: 25px; font-weight: 700; }}
.body {{ font-family: Georgia, "Times New Roman", serif; font-size: 27px; }}
.foot {{ font-size: 15px; fill: #7a8287; }}
</style></svg>"""


def random_room_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def decode_image_data(data_url):
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)", str(data_url))
    if not match or match.group(1) not in IMAGE_FORMATS:
        raise ValueError("只支持 PNG、JPEG 或 WebP 图片")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except ValueError as exc:
        raise ValueError("图片编码无效") from exc
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("图片必须小于 3 MB")
    mime = match.group(1)
    suffix, signature = IMAGE_FORMATS[mime]
    if not data.startswith(signature) or (mime == "image/webp" and data[8:12] != b"WEBP"):
        raise ValueError("图片内容与文件格式不符")
    return data, mime, suffix


def ensure_daily_questions(day):
    db = get_db()
    rows = db.execute(
        "SELECT position,question_id FROM daily_questions WHERE day=? ORDER BY position", (day,)
    ).fetchall()
    if len(rows) == DAILY_QUESTION_COUNT:
        return [row["question_id"] for row in rows]
    candidates = [row["id"] for row in db.execute(
        """
        SELECT id FROM questions
        WHERE active=1 AND unique_checked=1 AND brain=0 AND open_mode=0
        ORDER BY id
        """
    )]
    if len(candidates) < DAILY_QUESTION_COUNT:
        raise ValueError("每日挑战题库暂时不足 5 题")
    recent = {row["question_id"] for row in db.execute(
        """
        SELECT question_id FROM daily_questions
        WHERE day < ? ORDER BY day DESC,position LIMIT ?
        """,
        (day, DAILY_QUESTION_COUNT * 7),
    )}
    fresh = [question_id for question_id in candidates if question_id not in recent]
    pool = fresh if len(fresh) >= DAILY_QUESTION_COUNT else candidates
    pool.sort(key=lambda question_id: hashlib.sha256(f"{day}:{question_id}".encode()).digest())
    selected = pool[:DAILY_QUESTION_COUNT]
    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT question_id FROM daily_questions WHERE day=? ORDER BY position", (day,)
        ).fetchall()
        if len(existing) != DAILY_QUESTION_COUNT:
            db.execute("DELETE FROM daily_questions WHERE day=?", (day,))
            for position, question_id in enumerate(selected):
                db.execute(
                    "INSERT INTO daily_questions(day,position,question_id) VALUES(?,?,?)",
                    (day, position, question_id),
                )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return [row["question_id"] for row in db.execute(
        "SELECT question_id FROM daily_questions WHERE day=? ORDER BY position", (day,)
    )]


class Handler(BaseHTTPRequestHandler):
    server_version = "CFSnap/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def json(self, status=200, **payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size > 5 * 1024 * 1024:
            raise ValueError("请求内容过大")
        try:
            return json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 格式无效") from exc

    def current_session(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("cf_session")
        if not token:
            return None
        row = get_db().execute(
            """
            SELECT s.token, s.csrf, s.expires_at, u.* FROM sessions s
            JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?
            """,
            (token.value, int(now())),
        ).fetchone()
        return row

    def require_user(self, csrf=False):
        session = self.current_session()
        if not session:
            self.json(401, error="请先登录")
            return None
        if csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
            self.json(403, error="会话校验失败，请刷新页面后重试")
            return None
        return session

    def require_admin(self, csrf=False):
        session = self.require_user(csrf=csrf)
        if not session:
            return None
        if not (session["is_admin"] or session["is_super_admin"]):
            self.json(403, error="需要管理员权限")
            return None
        return session

    def require_super_admin(self, csrf=False):
        session = self.require_user(csrf=csrf)
        if not session:
            return None
        if not session["is_super_admin"]:
            self.json(403, error="需要超级管理员权限")
            return None
        return session

    def client_key(self, suffix):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return f"{forwarded or self.client_address[0]}:{suffix}"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.json(ok=True, time=int(now()))
        if path == "/api/me":
            return self.get_me()
        if path == "/api/config":
            return self.get_config()
        if path == "/api/leaderboard":
            return self.get_leaderboard()
        if path == "/api/daily":
            return self.get_daily()
        if path == "/api/daily/leaderboard":
            return self.get_daily_leaderboard()
        if path.startswith("/api/daily/clue/"):
            return self.get_daily_clue(path)
        if path.startswith("/api/clues/"):
            return self.get_clue(path)
        if path == "/api/submissions/mine":
            return self.get_my_submissions()
        if path == "/api/admin/submissions":
            return self.get_admin_submissions()
        if path == "/api/admin/open-candidates":
            return self.get_open_candidates()
        if path == "/api/admin/users":
            return self.get_admin_users()
        if path.startswith("/api/submissions/") and path.endswith("/image"):
            return self.get_submission_image(path)
        if path.startswith("/api/matches/") and path.endswith("/clue.svg"):
            return self.get_match_clue(path)
        if path.startswith("/api/matches/"):
            return self.get_match(path)
        return self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
        except ValueError as exc:
            return self.json(400, error=str(exc))
        if path == "/api/auth/register":
            return self.register(payload)
        if path == "/api/auth/login":
            return self.login(payload)
        if path == "/api/auth/logout":
            return self.logout()
        if path == "/api/quiz/next":
            return self.quiz_next(payload)
        if path == "/api/quiz/answer":
            return self.quiz_answer(payload)
        if path == "/api/quiz/abandon":
            return self.quiz_abandon(payload)
        if path == "/api/quiz/timeout":
            return self.quiz_timeout(payload)
        if path == "/api/daily/start":
            return self.start_daily(payload)
        if path == "/api/daily/answer":
            return self.answer_daily(payload)
        if path == "/api/daily/timeout":
            return self.timeout_daily(payload)
        if path == "/api/submissions":
            return self.create_submission(payload)
        if path.startswith("/api/admin/submissions/") and path.endswith("/review"):
            return self.review_submission(path, payload)
        if path.startswith("/api/admin/open-candidates/") and path.endswith("/review"):
            return self.review_open_candidate(path, payload)
        if path.startswith("/api/admin/users/") and path.endswith("/role"):
            return self.change_admin_role(path, payload)
        if path == "/api/matches":
            return self.create_match(payload)
        if path == "/api/matches/join":
            return self.join_match(payload)
        if path.startswith("/api/matches/") and path.endswith("/start"):
            return self.start_match(path, payload)
        if path.startswith("/api/matches/") and path.endswith("/leave"):
            return self.leave_match(path, payload)
        if path.startswith("/api/matches/") and path.endswith("/abandon"):
            return self.abandon_match_round(path, payload)
        if path.startswith("/api/matches/") and path.endswith("/answer"):
            return self.answer_match(path, payload)
        return self.json(404, error="接口不存在")

    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        if path.startswith("/static/"):
            relative = path[len("/static/"):]
        else:
            relative = path.lstrip("/")
        relative = unquote(relative)
        if ".." in Path(relative).parts:
            return self.send_error(404)
        target = (STATIC / relative).resolve()
        if STATIC not in target.parents and target != STATIC:
            return self.send_error(404)
        if not target.is_file():
            target = STATIC / "index.html"
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if target.name == "index.html" else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def create_session(self, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        expires = int(now()) + SESSION_DAYS * 86400
        db = get_db()
        db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.execute("INSERT INTO sessions(token, csrf, user_id, expires_at) VALUES(?,?,?,?)", (token, csrf, user_id, expires))
        secure = "; Secure" if os.environ.get("COOKIE_SECURE") == "1" else ""
        self.send_response(200)
        self.send_header(
            "Set-Cookie",
            f"cf_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}{secure}",
        )
        return csrf

    def register(self, payload):
        if rate_limited(self.client_key("auth"), 12, 600):
            return self.json(429, error="尝试次数过多，请稍后再试")
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if not USERNAME_RE.fullmatch(username):
            return self.json(400, error="用户名需为 3-20 位字母、数字或下划线")
        if len(password) < 8 or len(password) > 128:
            return self.json(400, error="密码长度需为 8-128 位")
        encoded, salt = hash_password(password)
        db = get_db()
        try:
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,salt,created_at) VALUES(?,?,?,?)",
                (username, encoded, salt, int(now())),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                return self.json(409, error="这个用户名已经被注册")
            raise
        csrf = self.create_session(cursor.lastrowid)
        user = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
        data = json.dumps({"user": public_user(user), "csrf": csrf}, ensure_ascii=False).encode()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def login(self, payload):
        if rate_limited(self.client_key("auth"), 12, 600):
            return self.json(429, error="尝试次数过多，请稍后再试")
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        user = get_db().execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if not user or not verify_password(password, user["password_hash"], user["salt"]):
            return self.json(401, error="用户名或密码不正确")
        csrf = self.create_session(user["id"])
        data = json.dumps({"user": public_user(user), "csrf": csrf}, ensure_ascii=False).encode()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def logout(self):
        session = self.require_user(csrf=True)
        if not session:
            return
        get_db().execute("DELETE FROM sessions WHERE token=?", (session["token"],))
        self.send_response(200)
        self.send_header("Set-Cookie", "cf_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        data = b'{"ok":true}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def get_me(self):
        session = self.current_session()
        if not session:
            return self.json(user=None)
        return self.json(user=public_user(session), csrf=session["csrf"])

    def get_config(self):
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) count, MIN(contest_time) min_time, MAX(contest_time) max_time FROM questions WHERE active=1"
        ).fetchone()
        ids = db.execute("SELECT MIN(contest_id) lo, MAX(contest_id) hi FROM aliases").fetchone()
        return self.json(
            questionCount=row["count"],
            yearMin=datetime.fromtimestamp(row["min_time"], timezone.utc).year,
            yearMax=datetime.fromtimestamp(row["max_time"], timezone.utc).year,
            contestMin=ids["lo"],
            contestMax=ids["hi"],
            roundTypes=["Div. 1", "Div. 2", "Div. 1 + Div. 2", "Div. 3", "Div. 4", "Edu"],
        )

    def get_leaderboard(self):
        rows = get_db().execute(
            "SELECT * FROM users ORDER BY rating DESC, total_score DESC, id ASC LIMIT 20"
        ).fetchall()
        return self.json(players=[public_user(row) for row in rows])

    def daily_leaderboard(self, day):
        rows = get_db().execute(
            """
            SELECT u.username,dr.total_score,totals.elapsed_ms
            FROM daily_runs dr JOIN users u ON u.id=dr.user_id
            JOIN (
                SELECT day,user_id,SUM(elapsed_ms) elapsed_ms
                FROM daily_answers GROUP BY day,user_id
            ) totals ON totals.day=dr.day AND totals.user_id=dr.user_id
            WHERE dr.day=? AND dr.finished_at IS NOT NULL
            ORDER BY dr.total_score DESC,totals.elapsed_ms ASC,u.id ASC LIMIT 100
            """,
            (day,),
        ).fetchall()
        return [{
            "rank": index + 1,
            "username": row["username"],
            "score": row["total_score"],
            "elapsedMs": row["elapsed_ms"],
        } for index, row in enumerate(rows)]

    def daily_payload(self, session, day):
        question_ids = ensure_daily_questions(day)
        db = get_db()
        run = db.execute(
            "SELECT * FROM daily_runs WHERE day=? AND user_id=?", (day, session["id"])
        ).fetchone()
        payload = {
            "day": day,
            "questionCount": DAILY_QUESTION_COUNT,
            "timeLimit": DAILY_SECONDS,
            "status": "ready",
            "position": 0,
            "totalScore": 0,
        }
        if not run:
            return payload
        payload.update({
            "position": min(run["current_index"], DAILY_QUESTION_COUNT),
            "totalScore": run["total_score"],
        })
        if run["finished_at"] is not None or run["current_index"] >= DAILY_QUESTION_COUNT:
            payload["status"] = "finished"
            payload["leaderboard"] = self.daily_leaderboard(day)
            return payload
        if run["question_started_at"] is None:
            payload["status"] = "between"
            return payload
        position = run["current_index"]
        question = db.execute("SELECT * FROM questions WHERE id=?", (question_ids[position],)).fetchone()
        elapsed = max(0, now() - run["question_started_at"])
        public_question = question_public(question, f"daily-{day}-{position}", "all")
        public_question.update({
            "clueUrl": f"/api/daily/clue/{position}?day={day}",
            "position": position + 1,
            "timeLimit": DAILY_SECONDS,
            "secondsLeft": max(0, int(DAILY_SECONDS - elapsed + 0.999)),
        })
        payload.update({"status": "playing", "question": public_question})
        return payload

    def get_daily(self):
        session = self.require_user()
        if not session:
            return
        day = daily_day()
        try:
            payload = self.daily_payload(session, day)
        except ValueError as exc:
            return self.json(503, error=str(exc))
        return self.json(challenge=payload)

    def get_daily_leaderboard(self):
        session = self.require_user()
        if not session:
            return
        day = daily_day()
        return self.json(day=day, players=self.daily_leaderboard(day))

    def start_daily(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        day = daily_day()
        try:
            question_ids = ensure_daily_questions(day)
        except ValueError as exc:
            return self.json(503, error=str(exc))
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO daily_runs(day,user_id) VALUES(?,?)", (day, session["id"])
        )
        run = db.execute(
            "SELECT * FROM daily_runs WHERE day=? AND user_id=?", (day, session["id"])
        ).fetchone()
        if run["finished_at"] is None and run["current_index"] < DAILY_QUESTION_COUNT and run["question_started_at"] is None:
            started_at = now()
            db.execute(
                """
                UPDATE daily_runs SET question_started_at=?
                WHERE day=? AND user_id=? AND question_started_at IS NULL AND finished_at IS NULL
                """,
                (started_at, day, session["id"]),
            )
            record_question_exposure(
                session["id"], question_ids[run["current_index"]], "daily", f"{day}:{run['current_index']}"
            )
        return self.json(challenge=self.daily_payload(session, day))

    def get_daily_clue(self, path):
        session = self.require_user()
        if not session:
            return
        try:
            position = int(path.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            return self.send_error(404)
        day = daily_day()
        run = get_db().execute(
            "SELECT * FROM daily_runs WHERE day=? AND user_id=?", (day, session["id"])
        ).fetchone()
        if not run or run["question_started_at"] is None or run["current_index"] != position:
            return self.send_error(404)
        question_ids = ensure_daily_questions(day)
        if position < 0 or position >= len(question_ids):
            return self.send_error(404)
        question = get_db().execute("SELECT * FROM questions WHERE id=?", (question_ids[position],)).fetchone()
        return self.send_question_clue(question, "all")

    def settle_daily_answer(self, session, day, run, answer, parsed, timed_out=False):
        question_ids = ensure_daily_questions(day)
        position = run["current_index"]
        question = get_db().execute("SELECT * FROM questions WHERE id=?", (question_ids[position],)).fetchone()
        elapsed = max(0, now() - run["question_started_at"])
        result = {
            "score": 0,
            "distance": None,
            "contestGap": None,
            "indexGap": None,
            "exact": False,
            "nearest": None,
        }
        if parsed and not timed_out:
            result = distance_score(question["id"], parsed[0], parsed[1], elapsed, DAILY_SECONDS)
        elapsed_ms = min(DAILY_SECONDS * 1000, int(elapsed * 1000))
        next_index = position + 1
        finished_at = now() if next_index >= DAILY_QUESTION_COUNT else None
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            inserted = db.execute(
                """
                INSERT OR IGNORE INTO daily_answers(
                    day,user_id,position,question_id,answer,contest_id,problem_index,
                    distance,score,elapsed_ms,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    day, session["id"], position, question["id"], answer,
                    parsed[0] if parsed else None, parsed[1] if parsed else None,
                    result["distance"], result["score"], elapsed_ms, int(now()),
                ),
            ).rowcount
            if not inserted:
                db.execute("ROLLBACK")
                return self.json(409, error="这道每日题已经提交")
            changed = db.execute(
                """
                UPDATE daily_runs SET current_index=?,question_started_at=NULL,
                    total_score=total_score+?,finished_at=?
                WHERE day=? AND user_id=? AND current_index=? AND question_started_at IS NOT NULL
                """,
                (next_index, result["score"], finished_at, day, session["id"], position),
            ).rowcount
            if not changed:
                raise RuntimeError("每日挑战状态已经变化")
            user = db.execute("SELECT * FROM users WHERE id=?", (session["id"],)).fetchone()
            streak = user["streak"] + 1 if result["exact"] else 0
            db.execute(
                """
                UPDATE users SET total_score=total_score+?,attempts=attempts+1,
                    correct=correct+?,streak=?,best_streak=MAX(best_streak,?) WHERE id=?
                """,
                (result["score"], int(result["exact"]), streak, streak, session["id"]),
            )
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        settle_question_exposure(
            session["id"], "daily", f"{day}:{position}",
            "timeout" if timed_out else "correct" if result["exact"] else "scored",
        )
        user = db.execute("SELECT * FROM users WHERE id=?", (session["id"],)).fetchone()
        response = {
            **result,
            "settled": True,
            "timedOut": timed_out,
            "elapsedMs": elapsed_ms,
            "position": position + 1,
            "completed": finished_at is not None,
            "solution": solution_for(question),
            "user": public_user(user),
            "challenge": self.daily_payload(session, day),
        }
        return self.json(result=response)

    def answer_daily(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        day = daily_day()
        run = get_db().execute(
            "SELECT * FROM daily_runs WHERE day=? AND user_id=?", (day, session["id"])
        ).fetchone()
        if not run or run["finished_at"] is not None or run["question_started_at"] is None:
            return self.json(409, error="当前没有正在作答的每日题")
        if now() - run["question_started_at"] >= DAILY_SECONDS:
            return self.settle_daily_answer(session, day, run, "超时", None, timed_out=True)
        parsed = parse_contest_answer(payload.get("contestAnswer"))
        if not parsed:
            return self.json(400, error="请使用 2269E 这样的格式")
        return self.settle_daily_answer(session, day, run, f"{parsed[0]}{parsed[1]}", parsed)

    def timeout_daily(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        day = daily_day()
        run = get_db().execute(
            "SELECT * FROM daily_runs WHERE day=? AND user_id=?", (day, session["id"])
        ).fetchone()
        if not run or run["finished_at"] is not None or run["question_started_at"] is None:
            return self.json(409, error="当前没有正在作答的每日题")
        elapsed = now() - run["question_started_at"]
        if elapsed < DAILY_SECONDS:
            return self.json(409, error="倒计时尚未结束", secondsLeft=max(1, int(DAILY_SECONDS - elapsed)))
        return self.settle_daily_answer(session, day, run, "超时", None, timed_out=True)

    def submission_public(self, row):
        return {
            "id": row["id"],
            "username": row["username"] if "username" in row.keys() else None,
            "answer": f'{row["contest_id"]}{row["problem_index"]}',
            "clueKind": row["clue_kind"],
            "imageUrl": f'/api/submissions/{row["id"]}/image' if row["image_path"] else None,
            "textClue": row["clue_text"],
            "note": row["note"],
            "suggestedBrain": bool(row["suggested_brain"]),
            "suggestedOpen": bool(row["suggested_open"]),
            "acceptedAnswers": row["accepted_answers"],
            "verificationText": row["verification_text"],
            "status": row["status"],
            "reviewNote": row["review_note"],
            "createdAt": row["created_at"],
        }

    def create_submission(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        if rate_limited(f'user:{session["id"]}:submission', 10, 3600):
            return self.json(429, error="投稿过于频繁，请稍后再试")
        match = CONTEST_ANSWER_RE.fullmatch(str(payload.get("answer", "")))
        if not match:
            return self.json(400, error="题号请使用 2257D 这样的格式")
        contest_id, problem_index = int(match.group(1)), match.group(2).upper()
        clue_kind = str(payload.get("clueKind", "image"))
        if clue_kind not in {"image", "fragment"}:
            return self.json(400, error="线索类型无效")
        note = str(payload.get("note", "")).strip()[:1000]
        clue_text = str(payload.get("textClue", "")).strip()[:3000]
        suggested_open = int(bool(payload.get("suggestedOpen")))
        accepted_answers = str(payload.get("acceptedAnswers", "")).strip()[:500]
        verification_text = str(payload.get("verificationText", "")).strip()[:5000]
        try:
            parse_contest_answers(accepted_answers)
        except ValueError as exc:
            return self.json(400, error=str(exc))
        image_path = None
        if payload.get("imageData"):
            try:
                image_data, _, suffix = decode_image_data(payload["imageData"])
            except ValueError as exc:
                return self.json(400, error=str(exc))
            upload_dir = Path(db_path()).parent / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{int(now())}-{secrets.token_hex(12)}{suffix}"
            (upload_dir / filename).write_bytes(image_data)
            image_path = f"uploads/{filename}"
        if not image_path and len(clue_text) < 12:
            return self.json(400, error="请上传图片，或填写至少 12 个字符的文字线索")
        cursor = get_db().execute(
            """
            INSERT INTO submissions(
                user_id,contest_id,problem_index,clue_kind,image_path,clue_text,note,
                suggested_brain,suggested_open,accepted_answers,verification_text,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session["id"], contest_id, problem_index, clue_kind, image_path, clue_text,
                note, int(bool(payload.get("suggestedBrain"))), suggested_open,
                accepted_answers, verification_text, "pending", int(now()),
            ),
        )
        return self.json(submission={"id": cursor.lastrowid, "status": "pending"})

    def get_my_submissions(self):
        session = self.require_user()
        if not session:
            return
        rows = get_db().execute(
            """
            SELECT s.*,u.username FROM submissions s JOIN users u ON u.id=s.user_id
            WHERE s.user_id=? ORDER BY s.created_at DESC LIMIT 100
            """,
            (session["id"],),
        ).fetchall()
        return self.json(submissions=[self.submission_public(row) for row in rows])

    def get_admin_submissions(self):
        session = self.require_admin()
        if not session:
            return
        rows = get_db().execute(
            """
            SELECT s.*,u.username FROM submissions s JOIN users u ON u.id=s.user_id
            ORDER BY CASE s.status WHEN 'pending' THEN 0 ELSE 1 END,s.created_at DESC LIMIT 200
            """
        ).fetchall()
        return self.json(submissions=[self.submission_public(row) for row in rows])

    def get_open_candidates(self):
        session = self.require_admin()
        if not session:
            return
        rows = get_db().execute(
            """
            SELECT c.*,q.title,q.canonical_key,u.username
            FROM open_answer_candidates c
            JOIN questions q ON q.id=c.question_id
            JOIN users u ON u.id=c.requester_id
            ORDER BY CASE c.status WHEN 'pending' THEN 0 ELSE 1 END,c.created_at DESC
            LIMIT 200
            """
        ).fetchall()
        return self.json(candidates=[{
            "id": row["id"],
            "questionTitle": row["title"],
            "questionKey": row["canonical_key"],
            "answer": f'{row["contest_id"]}{row["problem_index"]}',
            "username": row["username"],
            "status": row["status"],
            "similarity": round(row["similarity"], 3),
            "hitCount": row["hit_count"],
            "createdAt": row["created_at"],
        } for row in rows])

    def review_open_candidate(self, path, payload):
        session = self.require_admin(csrf=True)
        if not session:
            return
        try:
            candidate_id = int(path.rstrip("/").split("/")[-2])
        except ValueError:
            return self.json(404, error="候选答案不存在")
        db = get_db()
        candidate = db.execute("SELECT * FROM open_answer_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            return self.json(404, error="候选答案不存在")
        if candidate["status"] != "pending":
            return self.json(409, error="这个候选答案已经处理")
        action = payload.get("action")
        if action not in {"approve", "reject"}:
            return self.json(400, error="审核操作无效")
        review_note = str(payload.get("reviewNote", "")).strip()[:1000]
        if action == "approve":
            round_number = bounded_int(payload.get("roundNumber"), 0, 0, 100000)
            division = str(payload.get("division", "Open")).strip()[:40] or "Open"
            db.execute(
                "INSERT OR IGNORE INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
                (candidate["question_id"], candidate["contest_id"], candidate["problem_index"], round_number, division),
            )
        status = "approved" if action == "approve" else "rejected"
        db.execute(
            """
            UPDATE open_answer_candidates
            SET status=?,reviewer_id=?,review_note=?,reviewed_at=? WHERE id=?
            """,
            (status, session["id"], review_note, int(now()), candidate_id),
        )
        return self.json(ok=True, candidateStatus=status)

    def get_admin_users(self):
        session = self.require_super_admin()
        if not session:
            return
        rows = get_db().execute(
            """
            SELECT * FROM users
            ORDER BY is_super_admin DESC,is_admin DESC,username COLLATE NOCASE ASC LIMIT 500
            """
        ).fetchall()
        return self.json(users=[{
            "id": row["id"],
            "username": row["username"],
            "isAdmin": bool(row["is_admin"] or row["is_super_admin"]),
            "isSuperAdmin": bool(row["is_super_admin"]),
            "createdAt": row["created_at"],
        } for row in rows])

    def change_admin_role(self, path, payload):
        session = self.require_super_admin(csrf=True)
        if not session:
            return
        try:
            user_id = int(path.rstrip("/").split("/")[-2])
        except ValueError:
            return self.json(404, error="用户不存在")
        db = get_db()
        target = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            return self.json(404, error="用户不存在")
        if target["is_super_admin"]:
            return self.json(400, error="不能在网页中修改超级管理员权限")
        is_admin = int(bool(payload.get("isAdmin")))
        db.execute("UPDATE users SET is_admin=? WHERE id=?", (is_admin, user_id))
        return self.json(ok=True, user={
            "id": target["id"],
            "username": target["username"],
            "isAdmin": bool(is_admin),
            "isSuperAdmin": False,
        })

    def get_submission_image(self, path):
        session = self.require_user()
        if not session:
            return
        try:
            submission_id = int(path.split("/")[-2])
        except ValueError:
            return self.send_error(404)
        row = get_db().execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not row or not row["image_path"] or (row["user_id"] != session["id"] and not (session["is_admin"] or session["is_super_admin"])):
            return self.send_error(404)
        return self.send_image_path(row["image_path"])

    def review_submission(self, path, payload):
        session = self.require_admin(csrf=True)
        if not session:
            return
        try:
            submission_id = int(path.rstrip("/").split("/")[-2])
        except ValueError:
            return self.json(404, error="投稿不存在")
        db = get_db()
        submission = db.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        if not submission:
            return self.json(404, error="投稿不存在")
        if submission["status"] != "pending":
            return self.json(409, error="这条投稿已经审核")
        action = payload.get("action")
        review_note = str(payload.get("reviewNote", "")).strip()[:1000]
        if action == "reject":
            db.execute(
                "UPDATE submissions SET status='rejected',reviewer_id=?,review_note=?,reviewed_at=? WHERE id=?",
                (session["id"], review_note, int(now()), submission_id),
            )
            return self.json(ok=True, submissionStatus="rejected")
        if action != "approve":
            return self.json(400, error="审核操作无效")
        title = str(payload.get("title", "")).strip()[:200]
        division = str(payload.get("division", "Div. 2")).strip()[:40]
        try:
            rating = int(payload.get("rating"))
            round_number = int(payload.get("roundNumber"))
            contest_time = int(payload.get("contestTime") or now())
        except (TypeError, ValueError):
            return self.json(400, error="rating、Round 和比赛时间必须是数字")
        if not title or not 800 <= rating <= 4000 or round_number < 1:
            return self.json(400, error="请填写有效的题名、rating 和 Round")
        key = f'{submission["contest_id"]}{submission["problem_index"]}@submission{submission_id}'
        clue_text = submission["clue_text"] or submission["note"] or "User-submitted visual clue reviewed for unique recognition."
        brain = int(bool(payload.get("brain", submission["suggested_brain"])))
        open_mode = int(bool(payload.get("openMode", submission["suggested_open"])))
        verification_text = str(payload.get("verificationText", submission["verification_text"])).strip()[:5000]
        try:
            accepted_answers = parse_contest_answers(payload.get("acceptedAnswers", submission["accepted_answers"]))
        except ValueError as exc:
            return self.json(400, error=str(exc))
        if not open_mode:
            accepted_answers = []
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute(
                """
                INSERT INTO questions(
                    canonical_key,title,clue,rating,contest_time,round_type,source_url,
                    clue_kind,image_path,brain,open_mode,verification_text,active,unique_checked
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,1)
                """,
                (
                    key, title, clue_text, rating, contest_time, division,
                    f'https://codeforces.com/contest/{submission["contest_id"]}/problem/{submission["problem_index"]}',
                    submission["clue_kind"], submission["image_path"], brain, open_mode, verification_text,
                ),
            )
            question_id = cursor.lastrowid
            db.execute(
                "INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
                (question_id, submission["contest_id"], submission["problem_index"], round_number, division),
            )
            for contest_id, problem_index in accepted_answers:
                db.execute(
                    "INSERT OR IGNORE INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
                    (question_id, contest_id, problem_index, 0, "Open"),
                )
            db.execute(
                """
                UPDATE submissions SET status='approved',reviewer_id=?,review_note=?,reviewed_at=?,question_id=? WHERE id=?
                """,
                (session["id"], review_note, int(now()), question_id, submission_id),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return self.json(ok=True, submissionStatus="approved", questionId=question_id)

    def resolve_image_path(self, stored_path):
        if not stored_path:
            return None
        if stored_path.startswith("static/questions/"):
            base = (ROOT / "static" / "questions").resolve()
            target = (ROOT / stored_path).resolve()
        else:
            base = Path(db_path()).parent.resolve()
            target = (base / stored_path).resolve()
        if target != base and base not in target.parents:
            return None
        return target if target.is_file() else None

    def send_image_path(self, stored_path):
        target = self.resolve_image_path(stored_path)
        if not target:
            return self.send_error(404)
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_question_clue(self, question, difficulty):
        if question["image_path"]:
            return self.send_image_path(question["image_path"])
        svg = make_clue_svg(question, difficulty).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(svg)

    def quiz_next(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        filters = parse_filters(payload.get("filters"))
        rated = bool(payload.get("rated"))
        scoring_mode = str(payload.get("scoringMode", "classic"))
        if scoring_mode not in {"classic", "distance"}:
            return self.json(400, error="未知计分方式")
        if scoring_mode == "distance" and filters["questionMode"] == "open":
            return self.json(400, error="开放多解只支持传统对错")
        if scoring_mode == "distance" and filters["difficulty"] == "brain":
            return self.json(400, error="最强大脑不支持距离积分，以免分数泄露答案范围")
        timed = rated or bool(payload.get("timed"))
        try:
            requested_time_limit = int(payload.get("timeLimit", RATED_SOLO_SECONDS))
        except (TypeError, ValueError):
            requested_time_limit = RATED_SOLO_SECONDS
        time_limit = RATED_SOLO_SECONDS if rated else min(600, max(30, requested_time_limit)) if timed else 0
        max_attempts = 1 if scoring_mode == "distance" else QUIZ_MAX_ATTEMPTS if timed else 1
        if rated:
            filters["difficulty"] = "all"
        candidates = matching_question_ids(filters, 5000)
        if not candidates:
            return self.json(404, error="这个筛选范围暂时没有题目，请放宽条件")
        if rated:
            ranges = get_db().execute(
                """
                SELECT MIN(a.contest_id) contest_min, MAX(a.contest_id) contest_max,
                    MIN(CAST(strftime('%Y',q.contest_time,'unixepoch') AS INTEGER)) year_min,
                    MAX(CAST(strftime('%Y',q.contest_time,'unixepoch') AS INTEGER)) year_max
                FROM aliases a JOIN questions q ON q.id=a.question_id
                WHERE q.active=1 AND q.unique_checked=1 AND q.open_mode=?
                """,
                (int(filters["questionMode"] == "open"),),
            ).fetchone()
            contest_min = filters["contestMin"] if filters["contestMin"] is not None else ranges["contest_min"]
            contest_max = filters["contestMax"] if filters["contestMax"] is not None else ranges["contest_max"]
            year_min = filters["yearMin"] if filters["yearMin"] is not None else ranges["year_min"]
            year_max = filters["yearMax"] if filters["yearMax"] is not None else ranges["year_max"]
            if contest_max - contest_min < RATED_MIN_CONTEST_SPAN:
                return self.json(400, error=f"Rating 模式的 Contest ID 范围至少需要跨度 {RATED_MIN_CONTEST_SPAN}")
            if year_max - year_min < RATED_MIN_YEAR_SPAN:
                return self.json(400, error="Rating 模式的时间范围至少需要覆盖 3 个自然年")
            if len(candidates) < RATED_MIN_POOL_SIZE:
                return self.json(400, error=f"Rating 模式筛选后至少需要 {RATED_MIN_POOL_SIZE} 道题")
        qid = select_fresh_questions(candidates, 1, [session["id"]])[0]
        token = secrets.token_urlsafe(24)
        difficulty = filters["difficulty"]
        db = get_db()
        db.execute(
            """
            INSERT INTO quiz_rounds(
                token,user_id,question_id,difficulty,rated,scoring_mode,time_limit,max_attempts,started_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (token, session["id"], qid, difficulty, int(rated), scoring_mode, time_limit, max_attempts, now()),
        )
        record_question_exposure(session["id"], qid, "solo", token)
        question = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        public_question = question_public(question, token, difficulty)
        public_question.update({
            "timed": bool(time_limit),
            "timeLimit": time_limit,
            "maxAttempts": max_attempts,
            "retrySeconds": QUIZ_RETRY_SECONDS,
            "scoringMode": scoring_mode,
        })
        return self.json(question=public_question, rated=rated)

    def get_clue(self, path):
        session = self.require_user()
        if not session:
            return
        token = path.rsplit("/", 1)[-1].split(".", 1)[0]
        row = get_db().execute(
            """
            SELECT qr.difficulty, q.* FROM quiz_rounds qr
            JOIN questions q ON q.id=qr.question_id
            WHERE qr.token=? AND qr.user_id=?
            """,
            (token, session["id"]),
        ).fetchone()
        if not row:
            return self.send_error(404)
        return self.send_question_clue(row, row["difficulty"])

    def quiz_answer(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        token = str(payload.get("token", ""))
        db = get_db()
        round_row = db.execute(
            "SELECT * FROM quiz_rounds WHERE token=? AND user_id=?", (token, session["id"])
        ).fetchone()
        if not round_row:
            return self.json(404, error="这道题已失效，请换一题")
        if round_row["answered_at"] is not None:
            return self.json(409, error="这道题已经作答")
        question = db.execute("SELECT * FROM questions WHERE id=?", (round_row["question_id"],)).fetchone()
        if round_row["scoring_mode"] == "distance" and question["open_mode"]:
            return self.json(409, error="开放多解只支持传统对错，请结束本题后重新选择")
        elapsed = now() - round_row["started_at"]
        if round_row["time_limit"] and elapsed >= round_row["time_limit"]:
            elapsed_ms = min(180_000, max(0, int(elapsed * 1000)))
            return self.finish_quiz_round(session, round_row, question, "超时", False, elapsed_ms, reason="timeout")
        if round_row["scoring_mode"] == "distance":
            if payload.get("answerMode", "contest") != "contest":
                return self.json(400, error="距离积分请使用 Contest ID + 题号作答")
            parsed = parse_contest_answer(payload.get("contestAnswer"))
            if not parsed:
                return self.json(400, error="请使用 2269E 这样的格式")
            elapsed_ms = min(180_000, max(0, int(elapsed * 1000)))
            score_result = distance_score(
                question["id"], parsed[0], parsed[1], elapsed, round_row["time_limit"]
            )
            return self.finish_quiz_round(
                session, round_row, question, f"{parsed[0]}{parsed[1]}", score_result["exact"],
                elapsed_ms, count_attempt=True, reason="answered", score_result=score_result,
            )
        if round_row["last_attempt_at"]:
            retry_after = QUIZ_RETRY_SECONDS - (now() - round_row["last_attempt_at"])
            if retry_after > 0:
                return self.json(429, error="两次尝试至少间隔 5 秒", retryAfter=max(1, int(retry_after + 0.999)))
        correct, shown, error = check_answer(question["id"], payload)
        if error:
            return self.json(400, error=error)
        if not correct:
            open_resolution = resolve_open_answer(question, session["id"], payload)
            if open_resolution and open_resolution["status"] == "correct":
                correct, shown = True, open_resolution["shown"]
            elif open_resolution and open_resolution["status"] == "pending":
                db.execute(
                    "UPDATE quiz_rounds SET last_attempt_at=? WHERE token=? AND answered_at IS NULL",
                    (now(), round_row["token"]),
                )
                return self.json(
                    correct=False,
                    settled=False,
                    pendingReview=True,
                    candidateId=open_resolution["candidateId"],
                    attemptsUsed=round_row["attempt_count"],
                    attemptsLeft=max(1, round_row["max_attempts"] - round_row["attempt_count"]),
                    retryAfter=QUIZ_RETRY_SECONDS,
                )
        elapsed_ms = min(180_000, max(0, int((now() - round_row["started_at"]) * 1000)))
        next_attempt_count = round_row["attempt_count"] + 1
        if not correct and round_row["time_limit"] and next_attempt_count < round_row["max_attempts"]:
            db.execute(
                """
                UPDATE quiz_rounds SET attempt_count=attempt_count+1,last_attempt_at=?
                WHERE token=? AND answered_at IS NULL
                """,
                (now(), round_row["token"]),
            )
            seconds_left = max(0, int(round_row["time_limit"] - (now() - round_row["started_at"])))
            return self.json(
                correct=False,
                settled=False,
                attemptsUsed=next_attempt_count,
                attemptsLeft=round_row["max_attempts"] - next_attempt_count,
                retryAfter=QUIZ_RETRY_SECONDS,
                secondsLeft=seconds_left,
            )
        reason = "attempts" if not correct and round_row["time_limit"] else "answered"
        return self.finish_quiz_round(session, round_row, question, shown, correct, elapsed_ms, count_attempt=True, reason=reason)

    def quiz_abandon(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        token = str(payload.get("token", ""))
        db = get_db()
        round_row = db.execute(
            "SELECT * FROM quiz_rounds WHERE token=? AND user_id=?", (token, session["id"])
        ).fetchone()
        if not round_row:
            return self.json(404, error="这道题已失效，请换一题")
        if round_row["answered_at"] is not None:
            return self.json(409, error="这道题已经结算")
        question = db.execute("SELECT * FROM questions WHERE id=?", (round_row["question_id"],)).fetchone()
        elapsed_ms = min(180_000, max(0, int((now() - round_row["started_at"]) * 1000)))
        return self.finish_quiz_round(session, round_row, question, "放弃", False, elapsed_ms, reason="abandoned")

    def quiz_timeout(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        token = str(payload.get("token", ""))
        db = get_db()
        round_row = db.execute(
            "SELECT * FROM quiz_rounds WHERE token=? AND user_id=?", (token, session["id"])
        ).fetchone()
        if not round_row:
            return self.json(404, error="这道题已失效，请换一题")
        if round_row["answered_at"] is not None:
            return self.json(409, error="这道题已经结算")
        if not round_row["time_limit"]:
            return self.json(409, error="这道题没有倒计时")
        elapsed = now() - round_row["started_at"]
        if elapsed < round_row["time_limit"]:
            return self.json(409, error="倒计时尚未结束", secondsLeft=max(1, int(round_row["time_limit"] - elapsed)))
        question = db.execute("SELECT * FROM questions WHERE id=?", (round_row["question_id"],)).fetchone()
        elapsed_ms = min(180_000, max(0, int(elapsed * 1000)))
        return self.finish_quiz_round(session, round_row, question, "超时", False, elapsed_ms, reason="timeout")

    def finish_quiz_round(
        self, session, round_row, question, shown, correct, elapsed_ms, count_attempt=False,
        reason="answered", score_result=None,
    ):
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id=?", (session["id"],)).fetchone()
        streak = user["streak"] + 1 if correct else 0
        multiplier = {"easy": 0.8, "medium": 1.0, "hard": 1.2, "brain": 1.5}.get(round_row["difficulty"], 1.0)
        base = max(150, 1000 - elapsed_ms // 45)
        brain_protected = bool(question["brain"] and score_result and not correct)
        points = 0 if brain_protected else score_result["score"] if score_result else int((base + min(streak, 10) * 30) * multiplier) if correct else 0
        rating_delta = 0
        if round_row["rated"]:
            expected = 1 / (1 + 10 ** ((question["rating"] - user["rating"]) / 400))
            raw_delta = round(24 * ((1 if correct else 0) - expected))
            proposed = max(1, raw_delta) if correct else min(-1, raw_delta)
            rating_delta = max(0, user["rating"] + proposed) - user["rating"]
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute(
                """
                UPDATE quiz_rounds SET answered_at=?,answer=?,correct=?,points=?,
                    attempt_count=attempt_count+? WHERE token=? AND answered_at IS NULL
                """,
                (now(), shown, int(correct), points, int(count_attempt), round_row["token"]),
            ).rowcount
            if not changed:
                db.execute("ROLLBACK")
                return self.json(409, error="这道题已经作答")
            db.execute(
                "INSERT INTO attempts(user_id,question_id,mode,answer,correct,elapsed_ms,points,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (session["id"], question["id"], "solo-rated" if round_row["rated"] else "solo", shown, int(correct), elapsed_ms, points, int(now())),
            )
            settle_question_exposure(
                session["id"], "solo", round_row["token"],
                "correct" if correct else reason if reason in {"abandoned", "timeout"} else "incorrect",
            )
            db.execute(
                """
                UPDATE users SET total_score=total_score+?, rating=rating+?, attempts=attempts+1,
                    correct=correct+?, streak=?, best_streak=MAX(best_streak,?) WHERE id=?
                """,
                (points, rating_delta, int(correct), streak, streak, session["id"]),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        user = db.execute("SELECT * FROM users WHERE id=?", (session["id"],)).fetchone()
        solution_withheld = round_row["difficulty"] == "brain" and not correct
        return self.json(
            correct=correct,
            points=points,
            scoringMode=round_row["scoring_mode"],
            distance=score_result["distance"] if score_result and not brain_protected else None,
            contestGap=score_result["contestGap"] if score_result and not brain_protected else None,
            indexGap=score_result["indexGap"] if score_result and not brain_protected else None,
            rated=bool(round_row["rated"]),
            ratingDelta=rating_delta,
            settled=True,
            terminalReason=reason,
            attemptsUsed=round_row["attempt_count"] + int(count_attempt),
            elapsedMs=elapsed_ms,
            solution=None if solution_withheld else solution_for(question),
            solutionWithheld=solution_withheld,
            user=public_user(user),
        )

    def create_match(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        filters = parse_filters(payload.get("filters"))
        rounds = min(10, max(3, int(payload.get("rounds", 5))))
        round_seconds = bounded_int(payload.get("roundSeconds"), ROUND_SECONDS, 20, 300)
        abandon_seconds = bounded_int(payload.get("abandonSeconds"), 20, 5, 60)
        penalty_enabled = int(bool(payload.get("penaltyEnabled", True)))
        penalty_first = bounded_int(payload.get("penaltyFirst"), 3, 1, 30)
        penalty_second = bounded_int(payload.get("penaltySecond"), 5, 1, 30)
        penalty_repeat = bounded_int(payload.get("penaltyRepeat"), 10, 1, 30)
        scoring_mode = str(payload.get("scoringMode", "classic"))
        if scoring_mode not in {"classic", "distance"}:
            return self.json(400, error="未知赛制")
        if scoring_mode == "distance" and filters["questionMode"] == "open":
            return self.json(400, error="开放多解只支持传统对错")
        if scoring_mode == "distance" and filters["difficulty"] == "brain":
            return self.json(400, error="最强大脑不支持距离积分，以免分数泄露答案范围")
        candidates = matching_question_ids(filters, 100)
        if len(candidates) < min(rounds, 3):
            return self.json(400, error="这个筛选范围题目不足，请放宽条件")
        db = get_db()
        for _ in range(8):
            code = random_room_code()
            try:
                db.execute(
                    """
                    INSERT INTO matches(
                        code,host_id,rated,filters_json,rounds,round_seconds,abandon_seconds,
                        penalty_enabled,penalty_first,penalty_second,penalty_repeat,scoring_mode,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        code, session["id"], int(bool(payload.get("rated"))), dump_json(filters), rounds,
                        round_seconds, abandon_seconds, penalty_enabled, penalty_first, penalty_second,
                        penalty_repeat, scoring_mode, int(now()),
                    ),
                )
                return self.json(code=code)
            except Exception as exc:
                if "UNIQUE" not in str(exc):
                    raise
        return self.json(500, error="房间码生成失败，请重试")

    def join_match(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        code = str(payload.get("code", "")).strip().upper()
        db = get_db()
        match = db.execute("SELECT * FROM matches WHERE code=?", (code,)).fetchone()
        if not match:
            return self.json(404, error="没有找到这个房间")
        if match["host_id"] == session["id"]:
            return self.json(code=code)
        if match["status"] != "waiting":
            return self.json(409, error="对战已经开始")
        if match["guest_id"] and match["guest_id"] != session["id"]:
            return self.json(409, error="房间已经满员")
        db.execute("UPDATE matches SET guest_id=? WHERE id=?", (session["id"], match["id"]))
        return self.json(code=code)

    def match_from_path(self, path, suffix=""):
        clean = path[:-len(suffix)] if suffix and path.endswith(suffix) else path
        code = clean.rstrip("/").rsplit("/", 1)[-1].upper()
        return get_db().execute("SELECT * FROM matches WHERE code=?", (code,)).fetchone()

    def participant(self, match, user_id):
        return user_id in {match["host_id"], match["guest_id"]}

    def start_match(self, path, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        match = self.match_from_path(path, "/start")
        if not match:
            return self.json(404, error="房间不存在")
        if match["host_id"] != session["id"]:
            return self.json(403, error="只有房主可以开始")
        if not match["guest_id"]:
            return self.json(409, error="等待另一位玩家加入")
        if match["status"] != "waiting":
            return self.json(409, error="对战已经开始")
        candidates = matching_question_ids(load_json(match["filters_json"], {}), 100)
        if len(candidates) < min(match["rounds"], 3):
            return self.json(409, error="题库不足，请重建房间并放宽筛选")
        selected = select_fresh_questions(
            candidates, min(match["rounds"], len(candidates)), [match["host_id"], match["guest_id"]]
        )
        started_at = now()
        db = get_db()
        db.execute(
            """
            UPDATE matches SET status='active',phase='playing',question_ids_json=?,rounds=?,
                round_index=0,phase_started_at=?,round_deadline=? WHERE id=? AND status='waiting'
            """,
            (dump_json(selected), len(selected), started_at, started_at + match["round_seconds"], match["id"]),
        )
        started = db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()
        self.record_match_round_exposures(started)
        return self.json(ok=True)

    def leave_match(self, path, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        match = self.match_from_path(path, "/leave")
        if not match:
            return self.json(404, error="房间不存在")
        if not self.participant(match, session["id"]):
            return self.json(403, error="你不在这个房间中")

        db = get_db()
        if match["status"] == "waiting":
            if match["host_id"] == session["id"]:
                db.execute("DELETE FROM matches WHERE id=? AND status='waiting'", (match["id"],))
                return self.json(ok=True, roomClosed=True)
            db.execute(
                "UPDATE matches SET guest_id=NULL WHERE id=? AND status='waiting' AND guest_id=?",
                (match["id"], session["id"]),
            )
            return self.json(ok=True, roomClosed=False)

        if match["status"] == "active":
            if match["host_id"] == session["id"]:
                db.execute(
                    "UPDATE matches SET guest_score=MAX(guest_score,host_score+1),status='finished',phase='finished' "
                    "WHERE id=? AND status='active'",
                    (match["id"],),
                )
            else:
                db.execute(
                    "UPDATE matches SET host_score=MAX(host_score,guest_score+1),status='finished',phase='finished' "
                    "WHERE id=? AND status='active'",
                    (match["id"],),
                )
            self.apply_match_result(match["id"])
            return self.json(ok=True, forfeited=True)

        return self.json(ok=True)

    def advance_match(self, match):
        if match["status"] != "active":
            return match
        db = get_db()
        elapsed = now() - (match["phase_started_at"] or now())
        settled_count = db.execute(
            "SELECT COUNT(*) n FROM match_answers WHERE match_id=? AND round_index=? AND settled=1",
            (match["id"], match["round_index"]),
        ).fetchone()["n"]
        deadline = match["round_deadline"] or ((match["phase_started_at"] or now()) + match["round_seconds"])
        if match["phase"] == "playing" and (settled_count >= 2 or now() >= deadline):
            self.settle_match_round_exposures(match)
            db.execute("UPDATE matches SET phase='reveal',phase_started_at=? WHERE id=?", (now(), match["id"]))
        elif match["phase"] == "reveal" and elapsed >= REVEAL_SECONDS:
            if match["round_index"] + 1 >= match["rounds"]:
                db.execute("UPDATE matches SET status='finished',phase='finished' WHERE id=?", (match["id"],))
                self.apply_match_result(match["id"])
            else:
                started_at = now()
                db.execute(
                    """
                    UPDATE matches SET round_index=round_index+1,phase='playing',phase_started_at=?,round_deadline=?
                    WHERE id=?
                    """,
                    (started_at, started_at + match["round_seconds"], match["id"]),
                )
                next_round = db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()
                self.record_match_round_exposures(next_round)
        return db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()

    def record_match_round_exposures(self, match):
        question_ids = load_json(match["question_ids_json"], [])
        if match["round_index"] >= len(question_ids):
            return
        source_key = f'{match["id"]}:{match["round_index"]}'
        for user_id in (match["host_id"], match["guest_id"]):
            if user_id:
                record_question_exposure(user_id, question_ids[match["round_index"]], "battle", source_key)

    def settle_match_round_exposures(self, match):
        db = get_db()
        source_key = f'{match["id"]}:{match["round_index"]}'
        for user_id in (match["host_id"], match["guest_id"]):
            if not user_id:
                continue
            answer = db.execute(
                "SELECT correct,abandoned,pending_review,attempt_count,points FROM match_answers WHERE match_id=? AND round_index=? AND user_id=?",
                (match["id"], match["round_index"], user_id),
            ).fetchone()
            outcome = "timeout"
            if answer:
                outcome = "correct" if answer["correct"] else "abandoned" if answer["abandoned"] else "pending" if answer["pending_review"] else "scored" if answer["points"] else "incorrect"
            settle_question_exposure(user_id, "battle", source_key, outcome)

    def apply_match_result(self, match_id):
        db = get_db()
        match = db.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        if not match or match["rated_applied"] or not match["guest_id"]:
            return
        host = db.execute("SELECT * FROM users WHERE id=?", (match["host_id"],)).fetchone()
        guest = db.execute("SELECT * FROM users WHERE id=?", (match["guest_id"],)).fetchone()
        if match["host_score"] > match["guest_score"]:
            host_result, guest_result = 1.0, 0.0
        elif match["host_score"] < match["guest_score"]:
            host_result, guest_result = 0.0, 1.0
        else:
            host_result = guest_result = 0.5
        host_delta = guest_delta = 0
        if match["rated"]:
            expected_host = 1 / (1 + 10 ** ((guest["rating"] - host["rating"]) / 400))
            k_host = 40 if host["games"] < 10 else 24
            k_guest = 40 if guest["games"] < 10 else 24
            host_delta = round(k_host * (host_result - expected_host))
            guest_delta = round(k_guest * (guest_result - (1 - expected_host)))
        db.execute("BEGIN IMMEDIATE")
        try:
            claimed = db.execute(
                "UPDATE matches SET rated_applied=1 WHERE id=? AND rated_applied=0", (match_id,)
            ).rowcount
            if claimed:
                db.execute(
                    "UPDATE users SET rating=rating+?,games=games+1,wins=wins+? WHERE id=?",
                    (host_delta, int(host_result == 1), host["id"]),
                )
                db.execute(
                    "UPDATE users SET rating=rating+?,games=games+1,wins=wins+? WHERE id=?",
                    (guest_delta, int(guest_result == 1), guest["id"]),
                )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise

    def get_match(self, path):
        session = self.require_user()
        if not session:
            return
        match = self.match_from_path(path)
        if not match:
            return self.json(404, error="房间不存在")
        if not self.participant(match, session["id"]):
            return self.json(403, error="你不在这个房间中")
        match = self.advance_match(match)
        db = get_db()
        host = db.execute("SELECT * FROM users WHERE id=?", (match["host_id"],)).fetchone()
        guest = db.execute("SELECT * FROM users WHERE id=?", (match["guest_id"],)).fetchone() if match["guest_id"] else None
        deadline = match["round_deadline"] or ((match["phase_started_at"] or now()) + match["round_seconds"])
        payload = {
            "code": match["code"],
            "status": match["status"],
            "phase": match["phase"],
            "rated": bool(match["rated"]),
            "scoringMode": match["scoring_mode"],
            "questionMode": load_json(match["filters_json"], {}).get("questionMode", "standard"),
            "isHost": session["id"] == match["host_id"],
            "round": min(match["round_index"] + 1, match["rounds"]),
            "rounds": match["rounds"],
            "secondsLeft": max(0, int(deadline - now() + 0.999)) if match["phase"] == "playing" else 0,
            "rules": {
                "roundSeconds": match["round_seconds"],
                "abandonSeconds": match["abandon_seconds"],
                "penaltyEnabled": bool(match["penalty_enabled"]),
                "penalties": [match["penalty_first"], match["penalty_second"], match["penalty_repeat"]],
                "scoringMode": match["scoring_mode"],
            },
            "players": [
                {"username": host["username"], "rating": host["rating"], "score": match["host_score"], "you": host["id"] == session["id"]},
                {"username": guest["username"], "rating": guest["rating"], "score": match["guest_score"], "you": guest and guest["id"] == session["id"]} if guest else None,
            ],
        }
        if match["status"] == "active":
            question_ids = load_json(match["question_ids_json"], [])
            qid = question_ids[match["round_index"]]
            aliases = aliases_for(qid)
            divisions = sorted({a["division"] for a in aliases if a["round_number"]})
            own = db.execute(
                "SELECT * FROM match_answers WHERE match_id=? AND round_index=? AND user_id=?",
                (match["id"], match["round_index"], session["id"]),
            ).fetchone()
            opponent_id = match["guest_id"] if session["id"] == match["host_id"] else match["host_id"]
            opponent = db.execute(
                "SELECT * FROM match_answers WHERE match_id=? AND round_index=? AND user_id=?",
                (match["id"], match["round_index"], opponent_id),
            ).fetchone()
            cooldown_left = max(0, int((own["cooldown_until"] or 0) - now() + 0.999)) if own else 0
            own_status = "playing"
            if own and own["settled"]:
                own_status = "abandoned" if own["abandoned"] else "correct" if own["correct"] else "scored" if match["scoring_mode"] == "distance" else "settled"
            payload.update({
                "clueUrl": f'/api/matches/{match["code"]}/clue.svg?r={match["round_index"]}',
                "needsDivision": True,
                "divisions": divisions,
                "answered": own_status != "playing",
                "ownStatus": own_status,
                "attempts": own["attempt_count"] if own else 0,
                "cooldownLeft": cooldown_left,
                "canSubmit": own_status == "playing" and cooldown_left == 0 and match["phase"] == "playing",
                "opponentAbandoned": bool(opponent and opponent["abandoned"]),
                "openMode": bool(db.execute("SELECT open_mode FROM questions WHERE id=?", (qid,)).fetchone()["open_mode"]),
            })
            if match["phase"] == "reveal":
                question = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
                answers = db.execute(
                    "SELECT ma.*,u.username FROM match_answers ma JOIN users u ON u.id=ma.user_id WHERE match_id=? AND round_index=? ORDER BY created_at",
                    (match["id"], match["round_index"]),
                ).fetchall()
                outcomes = {row["user_id"]: row for row in answers}
                difficulty = load_json(match["filters_json"], {}).get("difficulty", "medium")
                viewer_knows_solution = difficulty != "brain" or bool(own and own["correct"])
                reveal_rows = []
                for player in (host, guest):
                    if not player:
                        continue
                    outcome = outcomes.get(player["id"])
                    if not outcome:
                        status = "timeout"
                    elif outcome["correct"]:
                        status = "correct"
                    elif outcome["abandoned"]:
                        status = "abandoned"
                    elif outcome["pending_review"]:
                        status = "pending"
                    elif match["scoring_mode"] == "distance":
                        status = "scored"
                    else:
                        status = "miss"
                    reveal_rows.append({
                        "username": player["username"],
                        "status": status,
                        "attempts": outcome["attempt_count"] if outcome else 0,
                        "points": outcome["points"] if outcome else 0,
                        "distance": outcome["distance"] if outcome and viewer_knows_solution else None,
                        "answer": outcome["answer"] if outcome and viewer_knows_solution else None,
                    })
                solution_withheld = difficulty == "brain" and not (own and own["correct"])
                payload["reveal"] = {
                    "solution": None if solution_withheld else solution_for(question),
                    "solutionWithheld": solution_withheld,
                    "answers": reveal_rows,
                }
        if match["status"] == "finished":
            payload["winner"] = "draw"
            if match["host_score"] > match["guest_score"]:
                payload["winner"] = host["username"]
            elif guest and match["guest_score"] > match["host_score"]:
                payload["winner"] = guest["username"]
        return self.json(match=payload)

    def get_match_clue(self, path):
        session = self.require_user()
        if not session:
            return
        match = self.match_from_path(path, "/clue.svg")
        if not match or not self.participant(match, session["id"]) or match["status"] != "active":
            return self.send_error(404)
        question_ids = load_json(match["question_ids_json"], [])
        if match["round_index"] >= len(question_ids):
            return self.send_error(404)
        question = get_db().execute("SELECT * FROM questions WHERE id=?", (question_ids[match["round_index"]],)).fetchone()
        difficulty = load_json(match["filters_json"], {}).get("difficulty", "medium")
        return self.send_question_clue(question, difficulty)

    def match_penalty_seconds(self, match, wrong_attempt):
        if not match["penalty_enabled"]:
            return 0
        if wrong_attempt == 1:
            return match["penalty_first"]
        if wrong_attempt == 2:
            return match["penalty_second"]
        return match["penalty_repeat"]

    def abandon_match_round(self, path, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        match = self.match_from_path(path, "/abandon")
        if not match or not self.participant(match, session["id"]):
            return self.json(404, error="房间不存在")
        match = self.advance_match(match)
        if match["status"] != "active" or match["phase"] != "playing":
            return self.json(409, error="当前不在作答阶段")
        db = get_db()
        own = db.execute(
            "SELECT * FROM match_answers WHERE match_id=? AND round_index=? AND user_id=?",
            (match["id"], match["round_index"], session["id"]),
        ).fetchone()
        if own and own["settled"]:
            return self.json(409, error="本题已经结算")
        elapsed_ms = max(0, int((now() - match["phase_started_at"]) * 1000))
        attempt_count = own["attempt_count"] if own else 0
        if own:
            db.execute(
                """
                UPDATE match_answers SET answer='放弃',correct=0,points=0,elapsed_ms=?,created_at=?,
                    settled=1,abandoned=1,pending_review=0,cooldown_until=NULL
                WHERE id=? AND settled=0
                """,
                (elapsed_ms, now(), own["id"]),
            )
        else:
            db.execute(
                """
                INSERT INTO match_answers(
                    match_id,round_index,user_id,answer,correct,elapsed_ms,points,created_at,
                    settled,abandoned,attempt_count
                ) VALUES(?,?,?,?,?,?,?,?,1,1,0)
                """,
                (match["id"], match["round_index"], session["id"], "放弃", 0, elapsed_ms, 0, now()),
            )
        settle_question_exposure(
            session["id"], "battle", f'{match["id"]}:{match["round_index"]}', "abandoned"
        )
        current_deadline = match["round_deadline"] or (now() + match["round_seconds"])
        shortened_deadline = min(current_deadline, now() + match["abandon_seconds"])
        db.execute("UPDATE matches SET round_deadline=? WHERE id=? AND phase='playing'", (shortened_deadline, match["id"]))
        refreshed = db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()
        refreshed = self.advance_match(refreshed)
        return self.json(
            ok=True,
            settled=True,
            attempts=attempt_count,
            secondsLeft=max(0, int((refreshed["round_deadline"] or 0) - now() + 0.999)) if refreshed["phase"] == "playing" else 0,
        )

    def answer_match(self, path, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        match = self.match_from_path(path, "/answer")
        if not match or not self.participant(match, session["id"]):
            return self.json(404, error="房间不存在")
        match = self.advance_match(match)
        if match["status"] != "active" or match["phase"] != "playing":
            return self.json(409, error="当前不在作答阶段")
        db = get_db()
        own = db.execute(
            "SELECT * FROM match_answers WHERE match_id=? AND round_index=? AND user_id=?",
            (match["id"], match["round_index"], session["id"]),
        ).fetchone()
        if own and own["settled"]:
            return self.json(409, error="本题已经结算")
        if own and own["cooldown_until"] and own["cooldown_until"] > now():
            retry_after = max(1, int(own["cooldown_until"] - now() + 0.999))
            return self.json(429, error=f"错答罚时中，还需等待 {retry_after} 秒", retryAfter=retry_after)
        question_ids = load_json(match["question_ids_json"], [])
        question = db.execute("SELECT * FROM questions WHERE id=?", (question_ids[match["round_index"]],)).fetchone()
        if match["scoring_mode"] == "distance" and question["open_mode"]:
            return self.json(409, error="开放多解只支持传统对错，请退出本场后重新创建房间")
        if match["scoring_mode"] == "distance":
            if payload.get("answerMode", "contest") != "contest":
                return self.json(400, error="积分赛只接受 2269E 这样的 Contest 写法")
            parsed = parse_contest_answer(payload.get("contestAnswer"))
            if not parsed:
                return self.json(400, error="请使用 2269E 这样的格式")
            elapsed_seconds = max(0, now() - match["phase_started_at"])
            result = distance_score(
                question["id"], parsed[0], parsed[1], elapsed_seconds, match["round_seconds"]
            )
            if question["brain"] and not result["exact"]:
                result.update(score=0, distance=None, contestGap=None, indexGap=None)
            shown = f"{parsed[0]}{parsed[1]}"
            elapsed_ms = int(elapsed_seconds * 1000)
            try:
                db.execute(
                    """
                    INSERT INTO match_answers(
                        match_id,round_index,user_id,answer,correct,elapsed_ms,points,created_at,
                        settled,abandoned,pending_review,distance,attempt_count
                    ) VALUES(?,?,?,?,?,?,?,?,1,0,0,?,1)
                    """,
                    (
                        match["id"], match["round_index"], session["id"], shown,
                        int(result["exact"]), elapsed_ms, result["score"], now(), result["distance"],
                    ),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc):
                    return self.json(409, error="本题已经锁定答案")
                raise
            score_field = "host_score" if session["id"] == match["host_id"] else "guest_score"
            db.execute(f"UPDATE matches SET {score_field}={score_field}+? WHERE id=?", (result["score"], match["id"]))
            settle_question_exposure(
                session["id"], "battle", f'{match["id"]}:{match["round_index"]}',
                "correct" if result["exact"] else "scored",
            )
            self.advance_match(db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone())
            return self.json(
                correct=result["exact"],settled=True,points=result["score"],attempts=1,cooldown=0,
                distance=result["distance"],contestGap=result["contestGap"],indexGap=result["indexGap"],
            )
        correct, shown, error = check_answer(question["id"], payload)
        if error:
            return self.json(400, error=error)
        if not correct:
            open_resolution = resolve_open_answer(question, session["id"], payload)
            if open_resolution and open_resolution["status"] == "correct":
                correct, shown = True, open_resolution["shown"]
            elif open_resolution and open_resolution["status"] == "pending":
                cooldown_until = now() + QUIZ_RETRY_SECONDS
                if own:
                    db.execute(
                        """
                        UPDATE match_answers SET answer=?,created_at=?,pending_review=1,cooldown_until=?
                        WHERE id=? AND settled=0
                        """,
                        (open_resolution["shown"], now(), cooldown_until, own["id"]),
                    )
                else:
                    db.execute(
                        """
                        INSERT INTO match_answers(
                            match_id,round_index,user_id,answer,correct,elapsed_ms,points,created_at,
                            settled,abandoned,pending_review,attempt_count,cooldown_until
                        ) VALUES(?,?,?,?,0,0,0,?,0,0,1,0,?)
                        """,
                        (match["id"], match["round_index"], session["id"], open_resolution["shown"], now(), cooldown_until),
                    )
                return self.json(
                    correct=False,settled=False,pendingReview=True,
                    candidateId=open_resolution["candidateId"],points=0,
                    attempts=own["attempt_count"] if own else 0,cooldown=QUIZ_RETRY_SECONDS,
                )
        elapsed_ms = max(0, int((now() - match["phase_started_at"]) * 1000))
        first_correct = db.execute(
            "SELECT COUNT(*) n FROM match_answers WHERE match_id=? AND round_index=? AND correct=1 AND settled=1",
            (match["id"], match["round_index"]),
        ).fetchone()["n"] == 0
        points = (1000 if first_correct else 600) + max(0, 300 - elapsed_ms // 100) if correct else 0
        attempt_count = (own["attempt_count"] if own else 0) + 1
        if correct:
            if own:
                db.execute(
                    """
                    UPDATE match_answers SET answer=?,correct=1,elapsed_ms=?,points=?,created_at=?,
                        settled=1,abandoned=0,pending_review=0,attempt_count=?,cooldown_until=NULL
                    WHERE id=? AND settled=0
                    """,
                    (shown, elapsed_ms, points, now(), attempt_count, own["id"]),
                )
            else:
                db.execute(
                    """
                    INSERT INTO match_answers(
                        match_id,round_index,user_id,answer,correct,elapsed_ms,points,created_at,
                        settled,abandoned,attempt_count
                    ) VALUES(?,?,?,?,?,?,?,?,1,0,?)
                    """,
                    (match["id"], match["round_index"], session["id"], shown, 1, elapsed_ms, points, now(), attempt_count),
                )
            score_field = "host_score" if session["id"] == match["host_id"] else "guest_score"
            db.execute(f"UPDATE matches SET {score_field}={score_field}+? WHERE id=?", (points, match["id"]))
            settle_question_exposure(
                session["id"], "battle", f'{match["id"]}:{match["round_index"]}', "correct"
            )
            refreshed = db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()
            self.advance_match(refreshed)
            return self.json(correct=True, settled=True, points=points, attempts=attempt_count, cooldown=0)

        penalty = self.match_penalty_seconds(match, attempt_count)
        cooldown_until = now() + penalty if penalty else None
        if own:
            db.execute(
                """
                UPDATE match_answers SET answer=?,correct=0,elapsed_ms=?,points=0,created_at=?,
                    settled=0,abandoned=0,pending_review=0,attempt_count=?,cooldown_until=? WHERE id=? AND settled=0
                """,
                (shown, elapsed_ms, now(), attempt_count, cooldown_until, own["id"]),
            )
        else:
            db.execute(
                """
                INSERT INTO match_answers(
                    match_id,round_index,user_id,answer,correct,elapsed_ms,points,created_at,
                    settled,abandoned,attempt_count,cooldown_until
                ) VALUES(?,?,?,?,?,?,?,?,0,0,?,?)
                """,
                (match["id"], match["round_index"], session["id"], shown, 0, elapsed_ms, 0, now(), attempt_count, cooldown_until),
            )
        settle_question_exposure(
            session["id"], "battle", f'{match["id"]}:{match["round_index"]}', "incorrect"
        )
        return self.json(correct=False, settled=False, points=0, attempts=attempt_count, cooldown=penalty)


def main():
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CF Snap running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
