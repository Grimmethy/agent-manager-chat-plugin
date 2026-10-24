"""Tests for agent_client.py and the terminal-sessions routes, against a REAL session_agent
server on loopback fed by the synthetic fixtures (no mocks of the HTTP layer).

Run: python -m unittest test_agent_client -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import agent_client  # noqa: E402
import app  # noqa: E402
import session_agent  # noqa: E402

FIXTURE_HOME = Path(__file__).parent / "test_fixtures" / "session_agent" / "claude_home"
TOKEN = "synthetic-test-token"


class AgentRouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        home = Path(tmp) / "claude_home"
        shutil.copytree(FIXTURE_HOME, home)
        self.agent = session_agent.Agent(home, token=TOKEN, lister=None, alive=lambda pid: pid != 1003)
        self.agent.refresh()
        self.srv = session_agent.make_server(self.agent, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.client = app.app.test_client()

    def _stop(self):
        if self.agent.stop.is_set():
            return
        self.agent.stop.set()
        self.srv.shutdown()
        self.srv.server_close()

    def env(self, url=True, token=TOKEN):
        values = {"AGENT_MANAGER_SESSION_AGENT_URL": self.url if url else "",
                  "AGENT_MANAGER_SESSION_AGENT_TOKEN": token or ""}
        return mock.patch.dict(os.environ, values)

    def test_disabled_without_url(self):
        with self.env(url=False):
            body = self.client.get("/api/chat/terminal-sessions").get_json()
            self.assertEqual(body, {"enabled": False, "reachable": False, "sessions": [],
                                   "hosts": [], "skipped": []})
            self.assertEqual(self.client.get("/api/chat/terminal-sessions/events").status_code, 404)

    def test_lists_sessions(self):
        with self.env():
            body = self.client.get("/api/chat/terminal-sessions").get_json()
        self.assertTrue(body["enabled"] and body["reachable"])
        self.assertEqual({r["id"] for r in body["sessions"]}, {"sess-busy", "sess-wait", "sess-idle"})

    def test_wrong_token_is_unreachable_not_an_error(self):
        with self.env(token="wrong"):
            resp = self.client.get("/api/chat/terminal-sessions")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["enabled"], True)
        self.assertFalse(body["reachable"])
        self.assertEqual(body["sessions"], [])
        self.assertEqual(body["hosts"], ["default"])
        self.assertEqual([s["host"] for s in body["skipped"]], ["default"])
        self.assertTrue(body["skipped"][0]["reason"])

    def test_agent_down(self):
        self._stop()
        with self.env():
            self.assertFalse(self.client.get("/api/chat/terminal-sessions").get_json()["reachable"])
            self.assertEqual(self.client.get("/api/chat/terminal-sessions/events").status_code, 502)

    def test_events_passthrough_hides_token(self):
        with self.env():
            resp = self.client.get("/api/chat/terminal-sessions/events")
            self.assertEqual(resp.mimetype, "text/event-stream")
            stream = iter(resp.response)
            text = b""
            while text.count(b"event: session.updated") < 3:
                text += next(stream)
            resp.close()
        self.assertNotIn(TOKEN.encode(), text)
        first = [l for l in text.decode().splitlines() if l.startswith("data: ")][0]
        self.assertIn("status", json.loads(first[6:]))

    def test_events_exclude_transcript_text(self):
        with self.env():
            resp = self.client.get("/api/chat/terminal-sessions/events")
            stream = iter(resp.response)
            text = b""
            while text.count(b"event: session.updated") < 3:
                text += next(stream)
            self.agent.hub.publish("transcript.append", {"id": "x", "entries": [{"text": "private-text"}]})
            self.agent.hub.publish("session.removed", {"id": "sess-wait"})
            while b"session.removed" not in text:
                text += next(stream)
            resp.close()
        self.assertNotIn(b"private-text", text)
        self.assertNotIn(b"transcript.append", text)

    def test_token_file_wins_and_is_read_per_call(self):
        tf = Path(tempfile.mkdtemp()) / "tok"
        self.addCleanup(shutil.rmtree, tf.parent, True)
        values = {"AGENT_MANAGER_SESSION_AGENT_URL": self.url, "AGENT_MANAGER_SESSION_AGENT_TOKEN": "wrong",
                  "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": str(tf)}
        with mock.patch.dict(os.environ, values):
            tf.write_text("not-the-token\n")
            self.assertFalse(self.client.get("/api/chat/terminal-sessions").get_json()["reachable"])
            tf.write_text(TOKEN + "\n")  # e.g. the agent restarted with --token auto
            self.assertTrue(self.client.get("/api/chat/terminal-sessions").get_json()["reachable"])
            tf.unlink()  # unreadable file -> no header -> unreachable, not an error
            self.assertFalse(self.client.get("/api/chat/terminal-sessions").get_json()["reachable"])

    def test_filter_events_reassembles_split_blocks(self):
        blocks = (b"event: transcript.append\ndata: {\"a\": 1}\n\n: keepalive\n\n"
                  b"event: session.updated\ndata: {\"id\": \"s\"}\n\nevent: other\ndata: 1\n\n")
        chunks = [blocks[i:i + 7] for i in range(0, len(blocks), 7)]
        out = b"".join(agent_client.filter_events(iter(chunks)))
        self.assertEqual(out, b": keepalive\n\nevent: session.updated\ndata: {\"id\": \"s\"}\n\n")

    def test_list_sessions_rejects_wrong_shape(self):
        with mock.patch.dict(os.environ, {"AGENT_MANAGER_SESSION_AGENT_URL": self.url}):
            with mock.patch("agent_client.requests.get") as get:
                get.return_value.json.return_value = {"not": "a list"}
                with self.assertRaises(agent_client.AgentError):
                    agent_client.list_sessions()


if __name__ == "__main__":
    unittest.main()
