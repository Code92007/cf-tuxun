import http.client
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = TEST_DATA.name

from app import Handler, RATE_BUCKETS, ThreadingHTTPServer, check_answer  # noqa: E402
from cfshot.db import get_db, init_db, load_json  # noqa: E402
from scripts.import_pack import validate_pack  # noqa: E402


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Accept": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        conn.request(method, path, body=encoded, headers=headers)
        response = conn.getresponse()
        data = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        content_type = response.getheader("Content-Type", "")
        payload = json.loads(data) if "json" in content_type else data
        conn.close()
        if isinstance(payload, dict) and payload.get("csrf"):
            self.csrf = payload["csrf"]
        return response.status, payload


class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        TEST_DATA.cleanup()

    def setUp(self):
        RATE_BUCKETS.clear()

    def register(self, username):
        client = Client(self.port)
        status, payload = client.request("POST", "/api/auth/register", {"username": username, "password": "correct-horse"})
        self.assertEqual(status, 200, payload)
        return client, payload

    def test_registration_quiz_and_shared_aliases(self):
        client, _ = self.register("quiz_user")
        status, config = client.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(config["questionCount"], 12)

        status, payload = client.request("POST", "/api/quiz/next", {"filters": {"difficulty": "easy"}})
        self.assertEqual(status, 200, payload)
        token = payload["question"]["token"]
        status, svg = client.request("GET", payload["question"]["clueUrl"])
        self.assertEqual(status, 200)
        self.assertTrue(b"PROBLEM STATEMENT" in svg or svg.startswith(b"\x89PNG"))

        row = get_db().execute("SELECT question_id FROM quiz_rounds WHERE token=?", (token,)).fetchone()
        alias = get_db().execute("SELECT * FROM aliases WHERE question_id=? LIMIT 1", (row["question_id"],)).fetchone()
        answer = f'{alias["contest_id"]}{alias["problem_index"]}'
        status, result = client.request("POST", "/api/quiz/answer", {"token": token, "answerMode": "contest", "contestAnswer": answer})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["correct"])
        self.assertGreater(result["points"], 0)

        shared = get_db().execute("SELECT id FROM questions WHERE canonical_key='2268C'").fetchone()
        self.assertTrue(check_answer(shared["id"], {"answerMode": "contest", "contestAnswer": "2269E"})[0])
        self.assertTrue(check_answer(shared["id"], {"answerMode": "contest", "contestAnswer": "2268C"})[0])
        self.assertTrue(check_answer(shared["id"], {"answerMode": "round", "roundNumber": 1124, "division": "Div. 2", "roundIndex": "E"})[0])
        self.assertTrue(check_answer(shared["id"], {"answerMode": "round", "roundNumber": 1124, "division": "Div. 1", "roundIndex": "C"})[0])

    def test_two_player_room_flow(self):
        host, _ = self.register("room_host")
        guest, _ = self.register("room_guest")
        status, created = host.request("POST", "/api/matches", {"rated": True, "rounds": 3, "filters": {"difficulty": "medium"}})
        self.assertEqual(status, 200, created)
        code = created["code"]
        status, _ = guest.request("POST", "/api/matches/join", {"code": code})
        self.assertEqual(status, 200)
        status, _ = host.request("POST", f"/api/matches/{code}/start", {})
        self.assertEqual(status, 200)

        match = get_db().execute("SELECT * FROM matches WHERE code=?", (code,)).fetchone()
        question_id = load_json(match["question_ids_json"])[0]
        alias = get_db().execute("SELECT * FROM aliases WHERE question_id=? LIMIT 1", (question_id,)).fetchone()
        answer = f'{alias["contest_id"]}{alias["problem_index"]}'
        status, host_result = host.request("POST", f"/api/matches/{code}/answer", {"answerMode": "contest", "contestAnswer": answer})
        self.assertEqual(status, 200, host_result)
        self.assertTrue(host_result["correct"])
        status, guest_result = guest.request("POST", f"/api/matches/{code}/answer", {"answerMode": "contest", "contestAnswer": answer})
        self.assertEqual(status, 200, guest_result)
        self.assertTrue(guest_result["correct"])
        self.assertGreater(host_result["points"], guest_result["points"])
        status, room = host.request("GET", f"/api/matches/{code}")
        self.assertEqual(status, 200)
        self.assertEqual(room["match"]["phase"], "reveal")

    def test_players_can_leave_waiting_rooms(self):
        host, _ = self.register("leave_host")
        guest, _ = self.register("leave_guest")
        status, created = host.request("POST", "/api/matches", {"rounds": 3, "filters": {"difficulty": "medium"}})
        self.assertEqual(status, 200, created)
        code = created["code"]
        self.assertEqual(guest.request("POST", "/api/matches/join", {"code": code})[0], 200)

        status, left = guest.request("POST", f"/api/matches/{code}/leave", {})
        self.assertEqual(status, 200, left)
        self.assertFalse(left["roomClosed"])
        room = get_db().execute("SELECT * FROM matches WHERE code=?", (code,)).fetchone()
        self.assertIsNone(room["guest_id"])

        self.assertEqual(guest.request("POST", "/api/matches/join", {"code": code})[0], 200)
        status, left = host.request("POST", f"/api/matches/{code}/leave", {})
        self.assertEqual(status, 200, left)
        self.assertTrue(left["roomClosed"])
        self.assertIsNone(get_db().execute("SELECT id FROM matches WHERE code=?", (code,)).fetchone())
        self.assertEqual(guest.request("GET", f"/api/matches/{code}")[0], 404)

    def test_leaving_active_room_counts_as_forfeit(self):
        host, _ = self.register("forfeit_host")
        guest, _ = self.register("forfeit_guest")
        status, created = host.request("POST", "/api/matches", {"rated": True, "rounds": 3, "filters": {"difficulty": "medium"}})
        self.assertEqual(status, 200, created)
        code = created["code"]
        self.assertEqual(guest.request("POST", "/api/matches/join", {"code": code})[0], 200)
        self.assertEqual(host.request("POST", f"/api/matches/{code}/start", {})[0], 200)

        status, left = guest.request("POST", f"/api/matches/{code}/leave", {})
        self.assertEqual(status, 200, left)
        self.assertTrue(left["forfeited"])
        status, room = host.request("GET", f"/api/matches/{code}")
        self.assertEqual(status, 200, room)
        self.assertEqual(room["match"]["status"], "finished")
        self.assertEqual(room["match"]["winner"], "forfeit_host")

    def test_question_pack_rejects_ambiguous_and_gym_clues(self):
        bad = [{
            "key": "G1A", "title": "Visible Name", "clue": "Visible Name is short",
            "rating": 800, "contest_time": 1, "round_type": "Gym",
            "source_url": "https://codeforces.com/gym/1/problem/A",
            "aliases": [[1, "A", 1, "Div. 2"]],
        }]
        errors = validate_pack(bad)
        self.assertTrue(any("too short" in error for error in errors))
        self.assertTrue(any("leaks" in error for error in errors))
        self.assertTrue(any("Gym" in error for error in errors))

    def test_brain_pool_and_reviewed_variant_preserve_same_problem_alias(self):
        client, registered = self.register("contributor")
        status, brain = client.request("POST", "/api/quiz/next", {"filters": {"difficulty": "brain"}})
        self.assertEqual(status, 200, brain)
        status, clue = client.request("GET", brain["question"]["clueUrl"])
        self.assertEqual(status, 200)
        self.assertTrue(clue.startswith(b"\x89PNG"))
        status, abandoned = client.request("POST", "/api/quiz/abandon", {"token": brain["question"]["token"]})
        self.assertEqual(status, 200, abandoned)
        self.assertTrue(abandoned["solutionWithheld"])
        self.assertIsNone(abandoned["solution"])

        tiny_png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"review-test-image"
        ).decode()
        status, submitted = client.request("POST", "/api/submissions", {
            "answer": "2257D",
            "clueKind": "image",
            "imageData": f"data:image/png;base64,{tiny_png}",
            "note": "A second visual clue for the same problem.",
            "suggestedBrain": True,
        })
        self.assertEqual(status, 200, submitted)
        get_db().execute("UPDATE users SET is_admin=1 WHERE id=?", (registered["user"]["id"],))
        status, queue = client.request("GET", "/api/admin/submissions")
        self.assertEqual(status, 200, queue)
        self.assertEqual(queue["submissions"][0]["status"], "pending")
        submission_id = submitted["submission"]["id"]
        status, reviewed = client.request("POST", f"/api/admin/submissions/{submission_id}/review", {
            "action": "approve",
            "title": "Bermuda Rectangle",
            "rating": 1600,
            "roundNumber": 1117,
            "division": "Div. 2",
            "contestTime": 1786977300,
            "brain": True,
            "reviewNote": "Unique crop verified.",
        })
        self.assertEqual(status, 200, reviewed)
        variants = get_db().execute(
            "SELECT COUNT(*) n FROM aliases WHERE contest_id=2257 AND problem_index='D'"
        ).fetchone()["n"]
        self.assertGreaterEqual(variants, 2)
        submission = get_db().execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
        self.assertEqual(submission["status"], "approved")
        self.assertTrue(submission["image_path"])

    def test_rated_solo_requires_a_broad_pool_and_penalizes_abandon(self):
        client, registered = self.register("rated_solo")
        status, rejected = client.request("POST", "/api/quiz/next", {
            "rated": True,
            "filters": {"difficulty": "easy", "contestMin": 2260},
        })
        self.assertEqual(status, 400, rejected)
        self.assertIn("跨度", rejected["error"])

        status, started = client.request("POST", "/api/quiz/next", {
            "rated": True,
            "filters": {"difficulty": "brain"},
        })
        self.assertEqual(status, 200, started)
        token = started["question"]["token"]
        round_row = get_db().execute("SELECT * FROM quiz_rounds WHERE token=?", (token,)).fetchone()
        question = get_db().execute("SELECT * FROM questions WHERE id=?", (round_row["question_id"],)).fetchone()
        self.assertEqual(round_row["rated"], 1)
        self.assertEqual(round_row["difficulty"], "all")
        self.assertEqual(round_row["time_limit"], 120)
        self.assertEqual(round_row["max_attempts"], 10)
        self.assertEqual(question["brain"], 0)

        old_rating = registered["user"]["rating"]
        status, abandoned = client.request("POST", "/api/quiz/abandon", {"token": token})
        self.assertEqual(status, 200, abandoned)
        self.assertTrue(abandoned["rated"])
        self.assertLess(abandoned["ratingDelta"], 0)
        self.assertEqual(abandoned["user"]["rating"], old_rating + abandoned["ratingDelta"])
        attempt = get_db().execute("SELECT * FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 1", (registered["user"]["id"],)).fetchone()
        self.assertEqual(attempt["mode"], "solo-rated")
        self.assertEqual(attempt["answer"], "放弃")

    def test_timed_solo_allows_retries_with_server_side_limits(self):
        client, _ = self.register("timed_solo")
        status, started = client.request("POST", "/api/quiz/next", {
            "timed": True,
            "timeLimit": 30,
            "filters": {"difficulty": "all"},
        })
        self.assertEqual(status, 200, started)
        token = started["question"]["token"]
        wrong = {"token": token, "answerMode": "contest", "contestAnswer": "1A"}

        status, first = client.request("POST", "/api/quiz/answer", wrong)
        self.assertEqual(status, 200, first)
        self.assertFalse(first["settled"])
        self.assertEqual(first["attemptsUsed"], 1)
        self.assertEqual(first["attemptsLeft"], 9)

        status, limited = client.request("POST", "/api/quiz/answer", wrong)
        self.assertEqual(status, 429, limited)
        self.assertGreaterEqual(limited["retryAfter"], 1)

        get_db().execute(
            "UPDATE quiz_rounds SET attempt_count=9,last_attempt_at=? WHERE token=?",
            (time.time() - 6, token),
        )
        status, exhausted = client.request("POST", "/api/quiz/answer", wrong)
        self.assertEqual(status, 200, exhausted)
        self.assertTrue(exhausted["settled"])
        self.assertEqual(exhausted["terminalReason"], "attempts")
        self.assertEqual(exhausted["attemptsUsed"], 10)
        self.assertIsNotNone(exhausted["solution"])

        status, second = client.request("POST", "/api/quiz/next", {
            "timed": True,
            "timeLimit": 30,
            "filters": {"difficulty": "all"},
        })
        self.assertEqual(status, 200, second)
        timeout_token = second["question"]["token"]
        get_db().execute(
            "UPDATE quiz_rounds SET started_at=? WHERE token=?",
            (time.time() - 31, timeout_token),
        )
        status, timed_out = client.request("POST", "/api/quiz/timeout", {"token": timeout_token})
        self.assertEqual(status, 200, timed_out)
        self.assertEqual(timed_out["terminalReason"], "timeout")

    def test_super_admin_controls_review_admin_roles(self):
        super_client, super_user = self.register("super_admin")
        admin_client, admin_user = self.register("review_admin")
        member_client, member_user = self.register("role_member")
        get_db().execute(
            "UPDATE users SET is_admin=1,is_super_admin=1 WHERE id=?",
            (super_user["user"]["id"],),
        )
        get_db().execute("UPDATE users SET is_admin=1 WHERE id=?", (admin_user["user"]["id"],))

        self.assertEqual(admin_client.request("GET", "/api/admin/users")[0], 403)
        self.assertEqual(admin_client.request(
            "POST", f'/api/admin/users/{member_user["user"]["id"]}/role', {"isAdmin": True}
        )[0], 403)

        status, users = super_client.request("GET", "/api/admin/users")
        self.assertEqual(status, 200, users)
        self.assertTrue(any(user["isSuperAdmin"] for user in users["users"] if user["username"] == "super_admin"))
        status, promoted = super_client.request(
            "POST", f'/api/admin/users/{member_user["user"]["id"]}/role', {"isAdmin": True}
        )
        self.assertEqual(status, 200, promoted)
        self.assertTrue(promoted["user"]["isAdmin"])
        self.assertEqual(member_client.request("GET", "/api/admin/submissions")[0], 200)

        status, demoted = super_client.request(
            "POST", f'/api/admin/users/{member_user["user"]["id"]}/role', {"isAdmin": False}
        )
        self.assertEqual(status, 200, demoted)
        self.assertFalse(demoted["user"]["isAdmin"])
        self.assertEqual(member_client.request("GET", "/api/admin/submissions")[0], 403)
        self.assertEqual(super_client.request(
            "POST", f'/api/admin/users/{super_user["user"]["id"]}/role', {"isAdmin": False}
        )[0], 400)

    def test_existing_alias_schema_migrates_without_losing_data(self):
        from cfshot import db as db_module

        legacy_dir = tempfile.TemporaryDirectory()
        previous_dir = os.environ["DATA_DIR"]
        previous_conn = db_module._local.conn
        try:
            os.environ["DATA_DIR"] = legacy_dir.name
            db_module._local.conn = None
            path = os.path.join(legacy_dir.name, "cfsnap.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE questions (id INTEGER PRIMARY KEY);
                CREATE TABLE aliases (
                    id INTEGER PRIMARY KEY,
                    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                    contest_id INTEGER NOT NULL,
                    problem_index TEXT NOT NULL,
                    round_number INTEGER NOT NULL,
                    division TEXT NOT NULL,
                    UNIQUE(contest_id, problem_index)
                );
                INSERT INTO questions(id) VALUES(1);
                INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division)
                VALUES(1,2257,'D',1117,'Div. 2');
                """
            )
            conn.close()
            migrated = db_module.get_db()
            db_module._migrate_aliases(migrated)
            migrated.execute("INSERT INTO questions(id) VALUES(2)")
            migrated.execute(
                "INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(2,2257,'D',1117,'Div. 2')"
            )
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM aliases").fetchone()[0], 2)
            migrated.close()
        finally:
            os.environ["DATA_DIR"] = previous_dir
            db_module._local.conn = previous_conn
            legacy_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
