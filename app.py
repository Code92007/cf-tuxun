#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
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
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from cfshot.db import (
    aliases_for,
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
ROUND_SECONDS = 30
REVEAL_SECONDS = 4
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
CONTEST_ANSWER_RE = re.compile(r"^\s*(\d{1,6})\s*[-_/ ]?\s*([A-Za-z][A-Za-z0-9]?)\s*$")
INDEX_RE = re.compile(r"^[A-Z][A-Z0-9]?")
RATE_BUCKETS = defaultdict(deque)
RATE_LOCK = threading.Lock()


def now():
    return time.time()


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
    if raw in {"div1", "1"}:
        return "Div. 1"
    if raw in {"div2", "2"}:
        return "Div. 2"
    if raw in {"div3", "3"}:
        return "Div. 3"
    if raw in {"edu", "educational"}:
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
    divisions = sorted({a["division"] for a in aliases})
    division = normalize_division(payload.get("division"))
    if len(divisions) > 1 and not division:
        return False, f"Round {round_number} {index}", "这是一道共享题，请选择 Div. 1 或 Div. 2"
    correct = any(
        a["round_number"] == round_number
        and a["problem_index"].upper() == index
        and (len(divisions) == 1 or a["division"] == division)
        for a in aliases
    )
    shown = f"Round {round_number} {division} {index}".replace("  ", " ").strip()
    return correct, shown, None


def solution_for(question):
    aliases = aliases_for(question["id"])
    return {
        "title": question["title"],
        "rating": question["rating"],
        "sourceUrl": question["source_url"],
        "answers": [
            {
                "contest": f'{a["contest_id"]}{a["problem_index"]}',
                "round": f'Round {a["round_number"]} {a["division"]} {a["problem_index"]}',
            }
            for a in aliases
        ],
    }


def question_public(question, token, difficulty):
    aliases = aliases_for(question["id"])
    divisions = sorted({a["division"] for a in aliases})
    return {
        "token": token,
        "clueUrl": f"/api/clues/{token}.svg",
        "difficulty": difficulty,
        "needsDivision": len(divisions) > 1,
        "divisions": divisions if len(divisions) > 1 else [],
        "timeLimit": ROUND_SECONDS,
    }


def make_clue_svg(question, difficulty="medium"):
    clue = question["clue"].strip()
    widths = {"easy": 80, "medium": 75, "hard": 70, "all": 75}
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
    label = {"easy": "EASY", "medium": "MEDIUM", "hard": "HARD", "all": "MIXED"}.get(difficulty, "MEDIUM")
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
        if size > 64 * 1024:
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
        if path.startswith("/api/clues/") and path.endswith(".svg"):
            return self.get_clue(path)
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
        if path == "/api/matches":
            return self.create_match(payload)
        if path == "/api/matches/join":
            return self.join_match(payload)
        if path.startswith("/api/matches/") and path.endswith("/start"):
            return self.start_match(path, payload)
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
            roundTypes=["Div. 1", "Div. 2", "Div. 3", "Edu"],
        )

    def get_leaderboard(self):
        rows = get_db().execute(
            "SELECT * FROM users ORDER BY rating DESC, total_score DESC, id ASC LIMIT 20"
        ).fetchall()
        return self.json(players=[public_user(row) for row in rows])

    def quiz_next(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        filters = parse_filters(payload.get("filters"))
        candidates = matching_question_ids(filters, 50)
        if not candidates:
            return self.json(404, error="这个筛选范围暂时没有题目，请放宽条件")
        recent = {
            row["question_id"] for row in get_db().execute(
                "SELECT question_id FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 6", (session["id"],)
            )
        }
        fresh = [qid for qid in candidates if qid not in recent]
        qid = random.choice(fresh or candidates)
        token = secrets.token_urlsafe(24)
        difficulty = filters["difficulty"] if filters["difficulty"] != "all" else "medium"
        db = get_db()
        db.execute(
            "INSERT INTO quiz_rounds(token,user_id,question_id,difficulty,started_at) VALUES(?,?,?,?,?)",
            (token, session["id"], qid, difficulty, now()),
        )
        question = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        return self.json(question=question_public(question, token, difficulty))

    def get_clue(self, path):
        session = self.require_user()
        if not session:
            return
        token = path.rsplit("/", 1)[-1][:-4]
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
        svg = make_clue_svg(row, row["difficulty"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(svg)

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
        correct, shown, error = check_answer(question["id"], payload)
        if error:
            return self.json(400, error=error)
        elapsed_ms = min(180_000, max(0, int((now() - round_row["started_at"]) * 1000)))
        streak = session["streak"] + 1 if correct else 0
        multiplier = {"easy": 0.8, "medium": 1.0, "hard": 1.2}.get(round_row["difficulty"], 1.0)
        base = max(150, 1000 - elapsed_ms // 45)
        points = int((base + min(streak, 10) * 30) * multiplier) if correct else 0
        db.execute("BEGIN IMMEDIATE")
        try:
            changed = db.execute(
                "UPDATE quiz_rounds SET answered_at=?,answer=?,correct=?,points=? WHERE token=? AND answered_at IS NULL",
                (now(), shown, int(correct), points, token),
            ).rowcount
            if not changed:
                db.execute("ROLLBACK")
                return self.json(409, error="这道题已经作答")
            db.execute(
                "INSERT INTO attempts(user_id,question_id,mode,answer,correct,elapsed_ms,points,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (session["id"], question["id"], "solo", shown, int(correct), elapsed_ms, points, int(now())),
            )
            db.execute(
                """
                UPDATE users SET total_score=total_score+?, attempts=attempts+1,
                    correct=correct+?, streak=?, best_streak=MAX(best_streak,?) WHERE id=?
                """,
                (points, int(correct), streak, streak, session["id"]),
            )
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        user = db.execute("SELECT * FROM users WHERE id=?", (session["id"],)).fetchone()
        return self.json(
            correct=correct,
            points=points,
            elapsedMs=elapsed_ms,
            solution=solution_for(question),
            user=public_user(user),
        )

    def create_match(self, payload):
        session = self.require_user(csrf=True)
        if not session:
            return
        filters = parse_filters(payload.get("filters"))
        rounds = min(10, max(3, int(payload.get("rounds", 5))))
        candidates = matching_question_ids(filters, 100)
        if len(candidates) < min(rounds, 3):
            return self.json(400, error="这个筛选范围题目不足，请放宽条件")
        db = get_db()
        for _ in range(8):
            code = random_room_code()
            try:
                db.execute(
                    """
                    INSERT INTO matches(code,host_id,rated,filters_json,rounds,created_at)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (code, session["id"], int(bool(payload.get("rated"))), dump_json(filters), rounds, int(now())),
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
        selected = random.sample(candidates, min(match["rounds"], len(candidates)))
        get_db().execute(
            """
            UPDATE matches SET status='active',phase='playing',question_ids_json=?,rounds=?,
                round_index=0,phase_started_at=? WHERE id=? AND status='waiting'
            """,
            (dump_json(selected), len(selected), now(), match["id"]),
        )
        return self.json(ok=True)

    def advance_match(self, match):
        if match["status"] != "active":
            return match
        db = get_db()
        elapsed = now() - (match["phase_started_at"] or now())
        answer_count = db.execute(
            "SELECT COUNT(*) n FROM match_answers WHERE match_id=? AND round_index=?",
            (match["id"], match["round_index"]),
        ).fetchone()["n"]
        if match["phase"] == "playing" and (answer_count >= 2 or elapsed >= ROUND_SECONDS):
            db.execute("UPDATE matches SET phase='reveal',phase_started_at=? WHERE id=?", (now(), match["id"]))
        elif match["phase"] == "reveal" and elapsed >= REVEAL_SECONDS:
            if match["round_index"] + 1 >= match["rounds"]:
                db.execute("UPDATE matches SET status='finished',phase='finished' WHERE id=?", (match["id"],))
                self.apply_match_result(match["id"])
            else:
                db.execute(
                    "UPDATE matches SET round_index=round_index+1,phase='playing',phase_started_at=? WHERE id=?",
                    (now(), match["id"]),
                )
        return db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()

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
        payload = {
            "code": match["code"],
            "status": match["status"],
            "phase": match["phase"],
            "rated": bool(match["rated"]),
            "isHost": session["id"] == match["host_id"],
            "round": min(match["round_index"] + 1, match["rounds"]),
            "rounds": match["rounds"],
            "secondsLeft": max(0, int(ROUND_SECONDS - (now() - (match["phase_started_at"] or now())))) if match["phase"] == "playing" else 0,
            "players": [
                {"username": host["username"], "rating": host["rating"], "score": match["host_score"], "you": host["id"] == session["id"]},
                {"username": guest["username"], "rating": guest["rating"], "score": match["guest_score"], "you": guest and guest["id"] == session["id"]} if guest else None,
            ],
        }
        if match["status"] == "active":
            question_ids = load_json(match["question_ids_json"], [])
            qid = question_ids[match["round_index"]]
            aliases = aliases_for(qid)
            divisions = sorted({a["division"] for a in aliases})
            own = db.execute(
                "SELECT * FROM match_answers WHERE match_id=? AND round_index=? AND user_id=?",
                (match["id"], match["round_index"], session["id"]),
            ).fetchone()
            payload.update({
                "clueUrl": f'/api/matches/{match["code"]}/clue.svg?r={match["round_index"]}',
                "needsDivision": len(divisions) > 1,
                "divisions": divisions if len(divisions) > 1 else [],
                "answered": bool(own),
            })
            if match["phase"] == "reveal":
                question = db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
                answers = db.execute(
                    "SELECT ma.*,u.username FROM match_answers ma JOIN users u ON u.id=ma.user_id WHERE match_id=? AND round_index=? ORDER BY created_at",
                    (match["id"], match["round_index"]),
                ).fetchall()
                payload["reveal"] = {
                    "solution": solution_for(question),
                    "answers": [
                        {"username": a["username"], "correct": bool(a["correct"]), "points": a["points"]}
                        for a in answers
                    ],
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
        svg = make_clue_svg(question, difficulty).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(svg)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(svg)

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
        question_ids = load_json(match["question_ids_json"], [])
        question = db.execute("SELECT * FROM questions WHERE id=?", (question_ids[match["round_index"]],)).fetchone()
        correct, shown, error = check_answer(question["id"], payload)
        if error:
            return self.json(400, error=error)
        elapsed_ms = max(0, int((now() - match["phase_started_at"]) * 1000))
        first_correct = db.execute(
            "SELECT COUNT(*) n FROM match_answers WHERE match_id=? AND round_index=? AND correct=1",
            (match["id"], match["round_index"]),
        ).fetchone()["n"] == 0
        points = (1000 if first_correct else 600) + max(0, 300 - elapsed_ms // 100) if correct else 0
        try:
            db.execute(
                """
                INSERT INTO match_answers(match_id,round_index,user_id,answer,correct,elapsed_ms,points,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (match["id"], match["round_index"], session["id"], shown, int(correct), elapsed_ms, points, now()),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                return self.json(409, error="本题你已经提交过")
            raise
        score_field = "host_score" if session["id"] == match["host_id"] else "guest_score"
        db.execute(f"UPDATE matches SET {score_field}={score_field}+? WHERE id=?", (points, match["id"]))
        refreshed = db.execute("SELECT * FROM matches WHERE id=?", (match["id"],)).fetchone()
        self.advance_match(refreshed)
        return self.json(correct=correct, points=points)


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
