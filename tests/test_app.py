import http.client
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = TEST_DATA.name

from app import (  # noqa: E402
    Handler,
    RATE_BUCKETS,
    ThreadingHTTPServer,
    check_answer,
    daily_day,
    distance_score,
    record_question_exposure,
    select_fresh_questions,
    settle_question_exposure,
)
from cfshot.db import get_db, init_db, load_json, matching_question_ids, parse_filters  # noqa: E402
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
        client, registered = self.register("quiz_user")
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
        single = get_db().execute("SELECT id FROM questions WHERE canonical_key='2194A'").fetchone()
        missing_division = check_answer(single["id"], {
            "answerMode": "round", "roundNumber": 1078, "roundIndex": "A",
        })
        self.assertTrue(missing_division[0])
        ambiguous_division = check_answer(shared["id"], {
            "answerMode": "round", "roundNumber": 1124, "roundIndex": "E",
        })
        self.assertFalse(ambiguous_division[0])
        self.assertIn("多个组别", ambiguous_division[2])
        ambiguous_token = "ambiguous-round-token"
        get_db().execute(
            """
            INSERT INTO quiz_rounds(
                token,user_id,question_id,difficulty,time_limit,max_attempts,started_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (ambiguous_token, registered["user"]["id"], shared["id"], "medium", 120, 10, time.time()),
        )
        status, ambiguity = client.request("POST", "/api/quiz/answer", {
            "token": ambiguous_token, "answerMode": "round", "roundNumber": 1124, "roundIndex": "E",
        })
        self.assertEqual(status, 400, ambiguity)
        self.assertIn("多个组别", ambiguity["error"])
        self.assertEqual(get_db().execute(
            "SELECT attempt_count FROM quiz_rounds WHERE token=?", (ambiguous_token,)
        ).fetchone()["attempt_count"], 0)
        self.assertTrue(check_answer(single["id"], {
            "answerMode": "round", "roundNumber": 1078, "division": "2", "roundIndex": "A",
        })[0])
        combined = get_db().execute(
            "SELECT * FROM aliases WHERE division='Div. 1 + Div. 2' LIMIT 1"
        ).fetchone()
        self.assertTrue(check_answer(combined["question_id"], {
            "answerMode": "round", "roundNumber": combined["round_number"],
            "division": "12", "roundIndex": combined["problem_index"],
        })[0])
        combined_without_division = check_answer(combined["question_id"], {
            "answerMode": "round", "roundNumber": combined["round_number"],
            "roundIndex": combined["problem_index"],
        })
        round_divisions = {
            row["division"] for row in get_db().execute(
                "SELECT DISTINCT division FROM aliases WHERE round_number=?", (combined["round_number"],)
            )
        }
        self.assertEqual(combined_without_division[0], len(round_divisions) == 1)
        educational = get_db().execute("SELECT * FROM aliases WHERE division='Edu' LIMIT 1").fetchone()
        self.assertTrue(check_answer(educational["question_id"], {
            "answerMode": "round", "roundNumber": educational["round_number"],
            "division": "e", "roundIndex": educational["problem_index"],
        })[0])
        invalid_division = check_answer(single["id"], {
            "answerMode": "round", "roundNumber": 1078, "division": "5", "roundIndex": "A",
        })
        self.assertFalse(invalid_division[0])
        self.assertIn("1、2、3、4、12 或 E", invalid_division[2])

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

    def test_battle_penalties_and_abandon_grace_period(self):
        host, _ = self.register("penalty_host")
        guest, _ = self.register("penalty_guest")
        status, created = host.request("POST", "/api/matches", {
            "rounds": 3,
            "roundSeconds": 90,
            "abandonSeconds": 20,
            "penaltyEnabled": True,
            "penaltyFirst": 3,
            "penaltySecond": 5,
            "penaltyRepeat": 10,
            "filters": {"difficulty": "medium"},
        })
        self.assertEqual(status, 200, created)
        code = created["code"]
        self.assertEqual(guest.request("POST", "/api/matches/join", {"code": code})[0], 200)
        self.assertEqual(host.request("POST", f"/api/matches/{code}/start", {})[0], 200)

        status, room = host.request("GET", f"/api/matches/{code}")
        self.assertEqual(status, 200, room)
        self.assertEqual(room["match"]["rules"]["roundSeconds"], 90)
        self.assertEqual(room["match"]["rules"]["penalties"], [3, 5, 10])
        wrong = {"answerMode": "contest", "contestAnswer": "1A"}

        status, first = host.request("POST", f"/api/matches/{code}/answer", wrong)
        self.assertEqual(status, 200, first)
        self.assertFalse(first["settled"])
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["cooldown"], 3)
        self.assertEqual(host.request("POST", f"/api/matches/{code}/answer", wrong)[0], 429)

        match = get_db().execute("SELECT id FROM matches WHERE code=?", (code,)).fetchone()
        get_db().execute(
            "UPDATE match_answers SET cooldown_until=? WHERE match_id=? AND user_id=(SELECT id FROM users WHERE username='penalty_host')",
            (time.time() - 1, match["id"]),
        )
        status, second = host.request("POST", f"/api/matches/{code}/answer", wrong)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["cooldown"], 5)
        get_db().execute(
            "UPDATE match_answers SET cooldown_until=? WHERE match_id=? AND user_id=(SELECT id FROM users WHERE username='penalty_host')",
            (time.time() - 1, match["id"]),
        )
        status, third = host.request("POST", f"/api/matches/{code}/answer", wrong)
        self.assertEqual(status, 200, third)
        self.assertEqual(third["attempts"], 3)
        self.assertEqual(third["cooldown"], 10)

        status, abandoned = host.request("POST", f"/api/matches/{code}/abandon", {})
        self.assertEqual(status, 200, abandoned)
        self.assertLessEqual(abandoned["secondsLeft"], 20)
        status, guest_view = guest.request("GET", f"/api/matches/{code}")
        self.assertEqual(status, 200, guest_view)
        self.assertTrue(guest_view["match"]["opponentAbandoned"])
        self.assertLessEqual(guest_view["match"]["secondsLeft"], 20)

        self.assertEqual(guest.request("POST", f"/api/matches/{code}/abandon", {})[0], 200)
        status, reveal = host.request("GET", f"/api/matches/{code}")
        self.assertEqual(status, 200, reveal)
        self.assertEqual(reveal["match"]["phase"], "reveal")
        statuses = {row["username"]: row["status"] for row in reveal["match"]["reveal"]["answers"]}
        self.assertEqual(statuses, {"penalty_host": "abandoned", "penalty_guest": "abandoned"})

    def test_battle_can_disable_wrong_answer_penalty(self):
        host, _ = self.register("no_penalty_host")
        guest, _ = self.register("no_penalty_guest")
        status, created = host.request("POST", "/api/matches", {
            "rounds": 3,
            "penaltyEnabled": False,
            "filters": {"difficulty": "medium"},
        })
        self.assertEqual(status, 200, created)
        code = created["code"]
        self.assertEqual(guest.request("POST", "/api/matches/join", {"code": code})[0], 200)
        self.assertEqual(host.request("POST", f"/api/matches/{code}/start", {})[0], 200)
        wrong = {"answerMode": "contest", "contestAnswer": "1A"}
        self.assertEqual(host.request("POST", f"/api/matches/{code}/answer", wrong)[1]["cooldown"], 0)
        status, second = host.request("POST", f"/api/matches/{code}/answer", wrong)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["cooldown"], 0)

    def test_question_selection_avoids_recent_history_for_both_players(self):
        _, first = self.register("history_one")
        _, second = self.register("history_two")
        user_ids = [first["user"]["id"], second["user"]["id"]]
        candidates = [row["id"] for row in get_db().execute("SELECT id FROM questions ORDER BY id LIMIT 8")]
        for index, question_id in enumerate(candidates[:4]):
            user_id = user_ids[index % 2]
            record_question_exposure(user_id, question_id, "test", str(index))
            settle_question_exposure(user_id, "test", str(index), "correct")
        selected = select_fresh_questions(candidates, 3, user_ids)
        self.assertEqual(len(selected), 3)
        self.assertTrue(set(selected).isdisjoint(candidates[:4]))

    def test_round_type_filters_include_new_categories_without_overlap(self):
        parsed = parse_filters({"roundTypes": ["Div. 1", "Div. 1 + Div. 2", "Div. 4", "Edu"]})
        self.assertEqual(parsed["roundTypes"], ["Div. 1", "Div. 1 + Div. 2", "Div. 4", "Edu"])
        shared = get_db().execute("SELECT id FROM questions WHERE canonical_key='2268C'").fetchone()["id"]
        self.assertIn(shared, matching_question_ids({"difficulty": "all", "roundTypes": ["Div. 1 + Div. 2"]}, 5000))
        self.assertNotIn(shared, matching_question_ids({"difficulty": "all", "roundTypes": ["Div. 1"]}, 5000))

    def test_open_questions_are_isolated_from_standard_pools(self):
        client, _ = self.register("pool_isolation_user")
        db = get_db()
        open_question_id = db.execute(
            """
            INSERT INTO questions(
                canonical_key,title,clue,rating,contest_time,round_type,source_url,open_mode
            ) VALUES(?,?,?,?,?,?,?,1)
            """,
            ("isolated-open-pool", "Open Pool", "A deliberate multi-answer clue", 1500, 1,
             "Div. 2", "https://codeforces.com/contest/2399/problem/A"),
        ).lastrowid
        db.execute(
            "INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
            (open_question_id, 2399, "A", 1, "Div. 2"),
        )

        parsed = parse_filters({"difficulty": "all", "questionMode": "invalid"})
        self.assertEqual(parsed["questionMode"], "standard")
        self.assertNotIn(open_question_id, matching_question_ids({"difficulty": "all"}, 5000))
        self.assertIn(open_question_id, matching_question_ids({
            "difficulty": "all", "questionMode": "open",
        }, 5000))

        status, standard = client.request("POST", "/api/quiz/next", {
            "filters": {"difficulty": "medium"},
        })
        self.assertEqual(status, 200, standard)
        standard_id = db.execute(
            "SELECT question_id FROM quiz_rounds WHERE token=?", (standard["question"]["token"],)
        ).fetchone()["question_id"]
        self.assertEqual(db.execute(
            "SELECT open_mode FROM questions WHERE id=?", (standard_id,)
        ).fetchone()["open_mode"], 0)

        status, opened = client.request("POST", "/api/quiz/next", {
            "filters": {"difficulty": "medium", "questionMode": "open"},
        })
        self.assertEqual(status, 200, opened)
        opened_id = db.execute(
            "SELECT question_id FROM quiz_rounds WHERE token=?", (opened["question"]["token"],)
        ).fetchone()["question_id"]
        self.assertEqual(db.execute(
            "SELECT open_mode FROM questions WHERE id=?", (opened_id,)
        ).fetchone()["open_mode"], 1)

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

        status, rejected = client.request("POST", "/api/quiz/next", {
            "scoringMode": "distance", "filters": {"difficulty": "brain"},
        })
        self.assertEqual(status, 400, rejected)
        self.assertIn("不支持距离积分", rejected["error"])
        status, rejected = client.request("POST", "/api/matches", {
            "scoringMode": "distance", "rounds": 3, "filters": {"difficulty": "brain"},
        })
        self.assertEqual(status, 400, rejected)

        brain_question_id = get_db().execute(
            "SELECT question_id FROM quiz_rounds WHERE token=?", (brain["question"]["token"],)
        ).fetchone()["question_id"]
        protected_token = "legacy-brain-distance"
        get_db().execute(
            """
            INSERT INTO quiz_rounds(
                token,user_id,question_id,difficulty,scoring_mode,time_limit,max_attempts,started_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (protected_token, registered["user"]["id"], brain_question_id, "brain", "distance", 120, 1, time.time()),
        )
        status, protected = client.request("POST", "/api/quiz/answer", {
            "token": protected_token, "answerMode": "contest", "contestAnswer": "1A",
        })
        self.assertEqual(status, 200, protected)
        self.assertFalse(protected["correct"])
        self.assertEqual(protected["points"], 0)
        self.assertIsNone(protected["distance"])
        self.assertIsNone(protected["contestGap"])
        self.assertIsNone(protected["indexGap"])
        self.assertTrue(protected["solutionWithheld"])
        self.assertIsNone(protected["solution"])

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

    def test_distance_score_rewards_accuracy_before_speed(self):
        question_id = get_db().execute("SELECT id FROM questions WHERE canonical_key='2268C'").fetchone()["id"]
        exact_slow = distance_score(question_id, 2269, "E", 115, 120)
        exact_fast = distance_score(question_id, 2269, "E", 1, 120)
        exact_untimed = distance_score(question_id, 2269, "E", 3600, 0)
        nearby = distance_score(question_id, 2268, "D", 1, 120)
        far = distance_score(question_id, 100, "A", 1, 120)
        self.assertGreater(exact_fast["score"], exact_slow["score"])
        self.assertGreater(exact_slow["score"], nearby["score"])
        self.assertGreater(nearby["score"], far["score"])
        self.assertLessEqual(exact_fast["score"], 5000)
        self.assertEqual(exact_untimed["score"], 5000)
        self.assertEqual(far["score"], 0)

    def test_solo_distance_mode_scores_and_locks_one_answer(self):
        client, _ = self.register("solo_distance_user")
        status, started = client.request("POST", "/api/quiz/next", {
            "scoringMode": "distance",
            "timed": True,
            "timeLimit": 120,
            "filters": {"difficulty": "medium"},
        })
        self.assertEqual(status, 200, started)
        self.assertEqual(started["question"]["scoringMode"], "distance")
        self.assertEqual(started["question"]["maxAttempts"], 1)
        token = started["question"]["token"]
        question_id = get_db().execute(
            "SELECT question_id FROM quiz_rounds WHERE token=?", (token,)
        ).fetchone()["question_id"]
        alias = get_db().execute(
            "SELECT * FROM aliases WHERE question_id=? ORDER BY id LIMIT 1", (question_id,)
        ).fetchone()
        answer = f'{alias["contest_id"]}{alias["problem_index"]}'
        status, scored = client.request("POST", "/api/quiz/answer", {
            "token": token, "answerMode": "contest", "contestAnswer": answer,
        })
        self.assertEqual(status, 200, scored)
        self.assertEqual(scored["scoringMode"], "distance")
        self.assertTrue(scored["settled"])
        self.assertTrue(scored["correct"])
        self.assertGreater(scored["points"], 4500)
        self.assertEqual(scored["distance"], 0)
        self.assertEqual(client.request("POST", "/api/quiz/answer", {
            "token": token, "answerMode": "contest", "contestAnswer": answer,
        })[0], 409)

    def test_distance_battle_locks_one_guess_and_compares_total_scores(self):
        host, _ = self.register("distance_host")
        guest, _ = self.register("distance_guest")
        status, created = host.request("POST", "/api/matches", {
            "rounds": 3,
            "roundSeconds": 120,
            "scoringMode": "distance",
            "filters": {"difficulty": "medium"},
        })
        self.assertEqual(status, 200, created)
        code = created["code"]
        self.assertEqual(guest.request("POST", "/api/matches/join", {"code": code})[0], 200)
        self.assertEqual(host.request("POST", f"/api/matches/{code}/start", {})[0], 200)
        match = get_db().execute("SELECT * FROM matches WHERE code=?", (code,)).fetchone()
        question_id = load_json(match["question_ids_json"])[0]
        alias = get_db().execute("SELECT * FROM aliases WHERE question_id=? ORDER BY id LIMIT 1", (question_id,)).fetchone()
        exact = f'{alias["contest_id"]}{alias["problem_index"]}'

        status, host_score = host.request("POST", f"/api/matches/{code}/answer", {
            "answerMode": "contest", "contestAnswer": exact,
        })
        self.assertEqual(status, 200, host_score)
        self.assertTrue(host_score["settled"])
        self.assertGreater(host_score["points"], 4500)
        self.assertEqual(host.request("POST", f"/api/matches/{code}/answer", {
            "answerMode": "contest", "contestAnswer": exact,
        })[0], 409)

        status, guest_score = guest.request("POST", f"/api/matches/{code}/answer", {
            "answerMode": "contest", "contestAnswer": "1A",
        })
        self.assertEqual(status, 200, guest_score)
        self.assertEqual(guest_score["points"], 0)
        status, room = host.request("GET", f"/api/matches/{code}")
        self.assertEqual(status, 200, room)
        self.assertEqual(room["match"]["phase"], "reveal")
        self.assertEqual(room["match"]["scoringMode"], "distance")

    def test_daily_challenge_is_shared_and_only_completed_runs_rank(self):
        first, _ = self.register("daily_first")
        second, _ = self.register("daily_second")
        status, first_ready = first.request("GET", "/api/daily")
        self.assertEqual(status, 200, first_ready)
        self.assertEqual(first_ready["challenge"]["status"], "ready")
        status, first_started = first.request("POST", "/api/daily/start", {})
        self.assertEqual(status, 200, first_started)
        status, second_started = second.request("POST", "/api/daily/start", {})
        self.assertEqual(status, 200, second_started)
        self.assertEqual(first_started["challenge"]["question"]["position"], 1)
        self.assertEqual(second_started["challenge"]["question"]["position"], 1)
        day = daily_day()
        self.assertEqual(get_db().execute(
            "SELECT COUNT(*) n FROM daily_questions WHERE day=?", (day,)
        ).fetchone()["n"], 5)
        self.assertFalse(any(row["username"] == "daily_first" for row in first.request(
            "GET", "/api/daily/leaderboard"
        )[1]["players"]))

        for position in range(5):
            question_id = get_db().execute(
                "SELECT question_id FROM daily_questions WHERE day=? AND position=?", (day, position)
            ).fetchone()["question_id"]
            alias = get_db().execute(
                "SELECT * FROM aliases WHERE question_id=? ORDER BY id LIMIT 1", (question_id,)
            ).fetchone()
            status, answered = first.request("POST", "/api/daily/answer", {
                "contestAnswer": f'{alias["contest_id"]}{alias["problem_index"]}',
            })
            self.assertEqual(status, 200, answered)
            self.assertGreater(answered["result"]["score"], 4500)
            if position < 4:
                self.assertEqual(first.request("POST", "/api/daily/start", {})[0], 200)
        self.assertTrue(answered["result"]["completed"])
        status, board = first.request("GET", "/api/daily/leaderboard")
        self.assertEqual(status, 200, board)
        self.assertEqual(board["players"][0]["username"], "daily_first")
        self.assertGreater(board["players"][0]["score"], 22500)

    def test_open_mode_queues_unknown_answers_without_spending_attempts(self):
        client, registered = self.register("open_answer_user")
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO questions(
                canonical_key,title,clue,rating,contest_time,round_type,source_url,open_mode,verification_text
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            ("open-test", "Open Test", "A sufficiently unique open clue for tests", 1500, 1, "Div. 2", "https://codeforces.com", 1, ""),
        )
        question_id = cursor.lastrowid
        db.execute(
            "INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
            (question_id, 2291, "A", 1, "Div. 2"),
        )
        token = "open-pending-token"
        db.execute(
            """
            INSERT INTO quiz_rounds(token,user_id,question_id,difficulty,time_limit,max_attempts,started_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (token, registered["user"]["id"], question_id, "medium", 120, 10, time.time()),
        )
        status, pending = client.request("POST", "/api/quiz/answer", {
            "token": token, "answerMode": "contest", "contestAnswer": "2292B",
        })
        self.assertEqual(status, 200, pending)
        self.assertTrue(pending["pendingReview"])
        self.assertEqual(pending["attemptsUsed"], 0)
        self.assertEqual(db.execute(
            "SELECT attempt_count FROM quiz_rounds WHERE token=?", (token,)
        ).fetchone()["attempt_count"], 0)

        db.execute("UPDATE users SET is_admin=1 WHERE id=?", (registered["user"]["id"],))
        status, reviewed = client.request(
            "POST", f'/api/admin/open-candidates/{pending["candidateId"]}/review', {"action": "approve"}
        )
        self.assertEqual(status, 200, reviewed)
        self.assertTrue(check_answer(question_id, {
            "answerMode": "contest", "contestAnswer": "2292B",
        })[0])

        second = db.execute(
            """
            INSERT INTO questions(
                canonical_key,title,clue,rating,contest_time,round_type,source_url,open_mode,verification_text
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            ("open-auto-test", "Open Auto", "Another open clue", 1500, 1, "Div. 2", "https://codeforces.com", 1,
             "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"),
        ).lastrowid
        db.execute(
            "INSERT INTO aliases(question_id,contest_id,problem_index,round_number,division) VALUES(?,?,?,?,?)",
            (second, 2293, "C", 2, "Div. 2"),
        )
        auto_token = "open-auto-token"
        db.execute(
            "INSERT INTO quiz_rounds(token,user_id,question_id,difficulty,started_at) VALUES(?,?,?,?,?)",
            (auto_token, registered["user"]["id"], second, "medium", time.time()),
        )
        with patch("app.fetch_codeforces_problem_text", return_value="alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"):
            status, approved = client.request("POST", "/api/quiz/answer", {
                "token": auto_token, "answerMode": "contest", "contestAnswer": "2294D",
            })
        self.assertEqual(status, 200, approved)
        self.assertTrue(approved["correct"])

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
