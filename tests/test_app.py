import http.client
import json
import os
import tempfile
import threading
import unittest


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = TEST_DATA.name

from app import Handler, ThreadingHTTPServer, check_answer  # noqa: E402
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
        self.assertIn(b"PROBLEM STATEMENT", svg)

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


if __name__ == "__main__":
    unittest.main()
