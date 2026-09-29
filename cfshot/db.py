import json
import os
import sqlite3
import threading
import time

from .seed import SEED_QUESTIONS


_local = threading.local()


def db_path():
    data_dir = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "cfsnap.db")


def get_db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(db_path(), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            rating INTEGER NOT NULL DEFAULT 1200,
            total_score INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            correct INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            best_streak INTEGER NOT NULL DEFAULT 0,
            games INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            csrf TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY,
            canonical_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            clue TEXT NOT NULL,
            rating INTEGER NOT NULL,
            contest_time INTEGER NOT NULL,
            round_type TEXT NOT NULL,
            source_url TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            unique_checked INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS aliases (
            id INTEGER PRIMARY KEY,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            contest_id INTEGER NOT NULL,
            problem_index TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            division TEXT NOT NULL,
            UNIQUE(contest_id, problem_index)
        );
        CREATE TABLE IF NOT EXISTS quiz_rounds (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            difficulty TEXT NOT NULL,
            started_at REAL NOT NULL,
            answered_at REAL,
            answer TEXT,
            correct INTEGER,
            points INTEGER
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            question_id INTEGER NOT NULL REFERENCES questions(id),
            mode TEXT NOT NULL,
            answer TEXT NOT NULL,
            correct INTEGER NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            points INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            host_id INTEGER NOT NULL REFERENCES users(id),
            guest_id INTEGER REFERENCES users(id),
            rated INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            phase TEXT NOT NULL DEFAULT 'waiting',
            filters_json TEXT NOT NULL,
            question_ids_json TEXT NOT NULL DEFAULT '[]',
            round_index INTEGER NOT NULL DEFAULT 0,
            rounds INTEGER NOT NULL DEFAULT 5,
            host_score INTEGER NOT NULL DEFAULT 0,
            guest_score INTEGER NOT NULL DEFAULT 0,
            phase_started_at REAL,
            rated_applied INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS match_answers (
            id INTEGER PRIMARY KEY,
            match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            round_index INTEGER NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id),
            answer TEXT NOT NULL,
            correct INTEGER NOT NULL,
            elapsed_ms INTEGER NOT NULL,
            points INTEGER NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(match_id, round_index, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_alias_question ON aliases(question_id);
        CREATE INDEX IF NOT EXISTS idx_attempt_user ON attempts(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_match_code ON matches(code);
        """
    )
    seed_questions(db)
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


def seed_questions(db=None):
    db = db or get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        for item in SEED_QUESTIONS:
            db.execute(
                """
                INSERT INTO questions(canonical_key, title, clue, rating, contest_time, round_type, source_url)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    title=excluded.title, clue=excluded.clue, rating=excluded.rating,
                    contest_time=excluded.contest_time, round_type=excluded.round_type,
                    source_url=excluded.source_url
                """,
                (
                    item["key"], item["title"], item["clue"], item["rating"],
                    item["contest_time"], item["round_type"], item["source_url"],
                ),
            )
            qid = db.execute("SELECT id FROM questions WHERE canonical_key=?", (item["key"],)).fetchone()["id"]
            for contest_id, index, round_number, division in item["aliases"]:
                db.execute(
                    """
                    INSERT INTO aliases(question_id, contest_id, problem_index, round_number, division)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(contest_id, problem_index) DO UPDATE SET
                        question_id=excluded.question_id, round_number=excluded.round_number,
                        division=excluded.division
                    """,
                    (qid, contest_id, index, round_number, division),
                )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def rowdict(row):
    return dict(row) if row is not None else None


def aliases_for(question_id):
    return [dict(row) for row in get_db().execute(
        "SELECT contest_id, problem_index, round_number, division FROM aliases WHERE question_id=? ORDER BY contest_id",
        (question_id,),
    )]


def public_user(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "rating": row["rating"],
        "totalScore": row["total_score"],
        "attempts": row["attempts"],
        "correct": row["correct"],
        "accuracy": round(100 * row["correct"] / row["attempts"]) if row["attempts"] else 0,
        "streak": row["streak"],
        "bestStreak": row["best_streak"],
        "games": row["games"],
        "wins": row["wins"],
    }


def parse_filters(raw):
    raw = raw or {}
    difficulty = raw.get("difficulty", "medium")
    if difficulty not in {"easy", "medium", "hard", "all"}:
        difficulty = "medium"
    return {
        "difficulty": difficulty,
        "contestMin": _int_or_none(raw.get("contestMin")),
        "contestMax": _int_or_none(raw.get("contestMax")),
        "yearMin": _int_or_none(raw.get("yearMin")),
        "yearMax": _int_or_none(raw.get("yearMax")),
        "roundTypes": [x for x in raw.get("roundTypes", []) if x in {"Div. 1", "Div. 2", "Div. 3", "Edu"}],
    }


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def matching_question_ids(filters, limit=100):
    filters = parse_filters(filters)
    where = ["q.active=1", "q.unique_checked=1"]
    params = []
    if filters["difficulty"] == "easy":
        where.append("q.rating <= 1200")
    elif filters["difficulty"] == "medium":
        where.append("q.rating BETWEEN 1300 AND 1900")
    elif filters["difficulty"] == "hard":
        where.append("q.rating >= 2000")
    if filters["contestMin"] is not None:
        where.append("EXISTS (SELECT 1 FROM aliases a WHERE a.question_id=q.id AND a.contest_id>=?)")
        params.append(filters["contestMin"])
    if filters["contestMax"] is not None:
        where.append("EXISTS (SELECT 1 FROM aliases a WHERE a.question_id=q.id AND a.contest_id<=?)")
        params.append(filters["contestMax"])
    if filters["yearMin"] is not None:
        where.append("CAST(strftime('%Y', q.contest_time, 'unixepoch') AS INTEGER) >= ?")
        params.append(filters["yearMin"])
    if filters["yearMax"] is not None:
        where.append("CAST(strftime('%Y', q.contest_time, 'unixepoch') AS INTEGER) <= ?")
        params.append(filters["yearMax"])
    if filters["roundTypes"]:
        markers = " OR ".join("q.round_type LIKE ?" for _ in filters["roundTypes"])
        where.append(f"({markers})")
        params.extend(f"%{x}%" for x in filters["roundTypes"])
    sql = f"SELECT q.id FROM questions q WHERE {' AND '.join(where)} ORDER BY RANDOM() LIMIT ?"
    params.append(limit)
    return [row["id"] for row in get_db().execute(sql, params)]


def dump_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def load_json(value, default=None):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
