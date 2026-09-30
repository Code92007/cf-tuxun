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
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_super_admin INTEGER NOT NULL DEFAULT 0,
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
            clue_kind TEXT NOT NULL DEFAULT 'statement',
            image_path TEXT,
            brain INTEGER NOT NULL DEFAULT 0,
            open_mode INTEGER NOT NULL DEFAULT 0,
            verification_text TEXT NOT NULL DEFAULT '',
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
            UNIQUE(question_id, contest_id, problem_index)
        );
        CREATE TABLE IF NOT EXISTS quiz_rounds (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            difficulty TEXT NOT NULL,
            rated INTEGER NOT NULL DEFAULT 0,
            scoring_mode TEXT NOT NULL DEFAULT 'classic',
            time_limit INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 1,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at REAL,
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
        CREATE TABLE IF NOT EXISTS question_exposures (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            source_key TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT 'seen',
            seen_at REAL NOT NULL,
            UNIQUE(user_id, source, source_key)
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
            round_deadline REAL,
            round_seconds INTEGER NOT NULL DEFAULT 30,
            abandon_seconds INTEGER NOT NULL DEFAULT 20,
            penalty_enabled INTEGER NOT NULL DEFAULT 1,
            penalty_first INTEGER NOT NULL DEFAULT 3,
            penalty_second INTEGER NOT NULL DEFAULT 5,
            penalty_repeat INTEGER NOT NULL DEFAULT 10,
            scoring_mode TEXT NOT NULL DEFAULT 'classic',
            unlimited INTEGER NOT NULL DEFAULT 0,
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
            settled INTEGER NOT NULL DEFAULT 1,
            abandoned INTEGER NOT NULL DEFAULT 0,
            pending_review INTEGER NOT NULL DEFAULT 0,
            distance REAL,
            attempt_count INTEGER NOT NULL DEFAULT 1,
            cooldown_until REAL,
            UNIQUE(match_id, round_index, user_id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            contest_id INTEGER NOT NULL,
            problem_index TEXT NOT NULL,
            clue_kind TEXT NOT NULL,
            image_path TEXT,
            clue_text TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            suggested_brain INTEGER NOT NULL DEFAULT 0,
            suggested_open INTEGER NOT NULL DEFAULT 0,
            accepted_answers TEXT NOT NULL DEFAULT '',
            verification_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            reviewer_id INTEGER REFERENCES users(id),
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at INTEGER,
            question_id INTEGER REFERENCES questions(id),
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS open_answer_candidates (
            id INTEGER PRIMARY KEY,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            requester_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            contest_id INTEGER NOT NULL,
            problem_index TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            similarity REAL NOT NULL DEFAULT 0,
            hit_count INTEGER NOT NULL DEFAULT 1,
            reviewer_id INTEGER REFERENCES users(id),
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_at INTEGER,
            created_at INTEGER NOT NULL,
            UNIQUE(question_id, contest_id, problem_index)
        );
        CREATE TABLE IF NOT EXISTS daily_questions (
            day TEXT NOT NULL,
            position INTEGER NOT NULL,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            PRIMARY KEY(day, position),
            UNIQUE(day, question_id)
        );
        CREATE TABLE IF NOT EXISTS daily_runs (
            day TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            current_index INTEGER NOT NULL DEFAULT 0,
            question_started_at REAL,
            total_score INTEGER NOT NULL DEFAULT 0,
            finished_at REAL,
            PRIMARY KEY(day, user_id)
        );
        CREATE TABLE IF NOT EXISTS daily_answers (
            day TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            question_id INTEGER NOT NULL REFERENCES questions(id),
            answer TEXT NOT NULL,
            contest_id INTEGER,
            problem_index TEXT,
            distance REAL,
            score INTEGER NOT NULL DEFAULT 0,
            elapsed_ms INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(day, user_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_alias_question ON aliases(question_id);
        CREATE INDEX IF NOT EXISTS idx_attempt_user ON attempts(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_exposure_user ON question_exposures(user_id, seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_exposure_question ON question_exposures(question_id, seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_match_code ON matches(code);
        CREATE INDEX IF NOT EXISTS idx_submission_status ON submissions(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_open_candidate_status ON open_answer_candidates(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_runs_board ON daily_runs(day, finished_at, total_score DESC);
        """
    )
    _ensure_column(db, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "users", "is_super_admin", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "questions", "clue_kind", "TEXT NOT NULL DEFAULT 'statement'")
    _ensure_column(db, "questions", "image_path", "TEXT")
    _ensure_column(db, "questions", "brain", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "questions", "open_mode", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "questions", "verification_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "submissions", "clue_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "submissions", "suggested_open", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "submissions", "accepted_answers", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "submissions", "verification_text", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(db, "quiz_rounds", "rated", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "quiz_rounds", "scoring_mode", "TEXT NOT NULL DEFAULT 'classic'")
    _ensure_column(db, "quiz_rounds", "time_limit", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "quiz_rounds", "max_attempts", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(db, "quiz_rounds", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "quiz_rounds", "last_attempt_at", "REAL")
    _ensure_column(db, "matches", "round_deadline", "REAL")
    _ensure_column(db, "matches", "round_seconds", "INTEGER NOT NULL DEFAULT 30")
    _ensure_column(db, "matches", "abandon_seconds", "INTEGER NOT NULL DEFAULT 20")
    _ensure_column(db, "matches", "penalty_enabled", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(db, "matches", "penalty_first", "INTEGER NOT NULL DEFAULT 3")
    _ensure_column(db, "matches", "penalty_second", "INTEGER NOT NULL DEFAULT 5")
    _ensure_column(db, "matches", "penalty_repeat", "INTEGER NOT NULL DEFAULT 10")
    _ensure_column(db, "matches", "scoring_mode", "TEXT NOT NULL DEFAULT 'classic'")
    _ensure_column(db, "matches", "unlimited", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "match_answers", "settled", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(db, "match_answers", "abandoned", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "match_answers", "pending_review", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(db, "match_answers", "distance", "REAL")
    _ensure_column(db, "match_answers", "attempt_count", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(db, "match_answers", "cooldown_until", "REAL")
    _migrate_aliases(db)
    seed_questions(db)
    _backfill_question_exposures(db)
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))


def _backfill_question_exposures(db):
    db.execute(
        """
        INSERT OR IGNORE INTO question_exposures(user_id,question_id,source,source_key,outcome,seen_at)
        SELECT user_id,question_id,'solo-attempt',CAST(id AS TEXT),
            CASE WHEN correct=1 THEN 'correct' ELSE 'incorrect' END,created_at
        FROM attempts
        """
    )
    rows = db.execute(
        """
        SELECT ma.user_id,ma.match_id,ma.round_index,ma.correct,ma.abandoned,ma.pending_review,ma.created_at,
            m.question_ids_json
        FROM match_answers ma JOIN matches m ON m.id=ma.match_id
        """
    ).fetchall()
    for row in rows:
        try:
            question_ids = json.loads(row["question_ids_json"])
            question_id = question_ids[row["round_index"]]
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            continue
        outcome = "correct" if row["correct"] else "abandoned" if row["abandoned"] else "pending" if row["pending_review"] else "incorrect"
        db.execute(
            """
            INSERT OR IGNORE INTO question_exposures(
                user_id,question_id,source,source_key,outcome,seen_at
            ) VALUES(?,?, 'battle', ?, ?, ?)
            """,
            (row["user_id"], question_id, f'{row["match_id"]}:{row["round_index"]}', outcome, row["created_at"]),
        )


def seed_questions(db=None):
    db = db or get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        for item in SEED_QUESTIONS:
            db.execute(
                """
                INSERT INTO questions(
                    canonical_key,title,clue,rating,contest_time,round_type,source_url,
                    clue_kind,image_path,brain,open_mode,verification_text
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    title=excluded.title, clue=excluded.clue, rating=excluded.rating,
                    contest_time=excluded.contest_time, round_type=excluded.round_type,
                    source_url=excluded.source_url, clue_kind=excluded.clue_kind,
                    image_path=excluded.image_path, brain=excluded.brain,
                    open_mode=excluded.open_mode,verification_text=excluded.verification_text
                """,
                (
                    item["key"], item["title"], item["clue"], item["rating"],
                    item["contest_time"], item["round_type"], item["source_url"],
                    item.get("clue_kind", "statement"), item.get("image_path"), item.get("brain", 0),
                    item.get("open_mode", 0), item.get("verification_text", ""),
                ),
            )
            qid = db.execute("SELECT id FROM questions WHERE canonical_key=?", (item["key"],)).fetchone()["id"]
            for contest_id, index, round_number, division in item["aliases"]:
                db.execute(
                    """
                    INSERT INTO aliases(question_id, contest_id, problem_index, round_number, division)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(question_id, contest_id, problem_index) DO UPDATE SET
                        round_number=excluded.round_number, division=excluded.division
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
        "isAdmin": bool(row["is_admin"] or row["is_super_admin"]),
        "isSuperAdmin": bool(row["is_super_admin"]),
    }


def parse_filters(raw):
    raw = raw or {}
    difficulty = raw.get("difficulty", "medium")
    if difficulty not in {"easy", "medium", "hard", "all", "brain"}:
        difficulty = "medium"
    question_mode = raw.get("questionMode", "standard")
    if question_mode not in {"standard", "open"}:
        question_mode = "standard"
    allowed_round_types = {"Div. 1", "Div. 2", "Div. 3", "Div. 4", "Div. 1 + Div. 2", "Edu"}
    return {
        "difficulty": difficulty,
        "questionMode": question_mode,
        "contestMin": _int_or_none(raw.get("contestMin")),
        "contestMax": _int_or_none(raw.get("contestMax")),
        "yearMin": _int_or_none(raw.get("yearMin")),
        "yearMax": _int_or_none(raw.get("yearMax")),
        "roundTypes": [x for x in raw.get("roundTypes", []) if x in allowed_round_types],
    }


def _int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def matching_question_ids(filters, limit=100):
    filters = parse_filters(filters)
    where = ["q.active=1", "q.unique_checked=1", "q.open_mode=?"]
    params = [int(filters["questionMode"] == "open")]
    if filters["difficulty"] == "brain":
        where.append("q.brain=1")
    elif filters["difficulty"] == "easy":
        where.extend(["q.rating <= 1200", "q.brain=0"])
    elif filters["difficulty"] == "medium":
        where.extend(["q.rating BETWEEN 1300 AND 1900", "q.brain=0"])
    elif filters["difficulty"] == "hard":
        where.extend(["q.rating >= 2000", "q.brain=0"])
    else:
        where.append("q.brain=0")
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
        round_clauses = []
        for round_type in filters["roundTypes"]:
            if round_type == "Div. 1 + Div. 2":
                round_clauses.append("q.round_type IN ('Div. 1 + Div. 2','Div. 1 / Div. 2')")
            else:
                round_clauses.append("q.round_type=?")
                params.append(round_type)
        where.append(f"({' OR '.join(round_clauses)})")
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


def _ensure_column(db, table, column, definition):
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate_aliases(db):
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='aliases'").fetchone()["sql"]
    if "UNIQUE(contest_id, problem_index)" not in schema.replace("\n", " "):
        return
    db.execute("PRAGMA foreign_keys = OFF")
    db.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE aliases_new (
            id INTEGER PRIMARY KEY,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            contest_id INTEGER NOT NULL,
            problem_index TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            division TEXT NOT NULL,
            UNIQUE(question_id, contest_id, problem_index)
        );
        INSERT INTO aliases_new SELECT * FROM aliases;
        DROP TABLE aliases;
        ALTER TABLE aliases_new RENAME TO aliases;
        CREATE INDEX idx_alias_question ON aliases(question_id);
        COMMIT;
        """
    )
    db.execute("PRAGMA foreign_keys = ON")
