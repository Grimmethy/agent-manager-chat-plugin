"""Integration tests for the session-agent routes served by ``app``.

These tests require a live ``session_agent`` server on loopback and are
kept separate from the pure unit tests of ``agent_client`` so that the
unit tests can run without the server stack.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_client  # noqa: E402
import app  # noqa: E402
import session_agent  # noqa: E402

FIXTURE_HOME = Path(__file__).resolve().parent / "fixtures" / "home"
TOKEN = "test-token-12345"


class AgentRouteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="agent_route_"))
        self._home = self._tmp / "home"
        shutil.copytree(FIXTURE_HOME, self._home)

        self._agent = session_agent.Agent(home=self._home)
        self._server = session_agent.make_server("127.0.0.1", 0, self._agent)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self._client = app.app.test_client()

    def tearDown(self):
        self._stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _stop(self):
        self._server.shutdown()
        self._thread.join(timeout=5)

    def env(self):
        return {
            "AGENT_URL": "http://127.0.0.1:{}".format(self._port),
            "AGENT_TOKEN": TOKEN,
        }

    def test_disabled_without_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            resp = self._client.get("/agent/sessions")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["enabled"])

    def test_lists_sessions(self):
        with mock.patch.dict(os.environ, self.env()):
            resp = self._client.get("/agent/sessions")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("sessions", data)

    def test_wrong_token_is_unreachable_not_an_error(self):
        env = self.env()
        env["AGENT_TOKEN"] = "wrong-token"
        with mock.patch.dict(os.environ, env):
            resp = self._client.get("/agent/sessions")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["enabled"])

    def test_agent_down(self):
        self._stop()
        with mock.patch.dict(os.environ, self.env()):
            resp = self._client.get("/agent/sessions")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["enabled"])

    def test_events_passthrough_hides_token(self):
        with mock.patch.dict(os.environ, self.env()):
            resp = self._client.get("/agent/events")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn(TOKEN, body)

    def test_events_exclude_transcript_text(self):
        with mock.patch.dict(os.environ, self.env()):
            resp = self._client.get("/agent/events")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for event in data.get("events", []):
            self.assertNotIn("transcript", event.get("type", ""))

    def test_token_file_wins_and_is_read_per_call(self):
        token_file = self._tmp / "agent_token"
        token_file.write_text("file-token-67890")
        env = self.env()
        env["AGENT_TOKEN_FILE"] = str(token_file)
        with mock.patch.dict(os.environ, env):
            resp = self._client.get("/agent/sessions")
        self.assertEqual(resp.status_code, 200)

        token_file.write_text("rotated-token-abc")
        with mock.patch.dict(os.environ, env):
            resp2 = self._client.get("/agent/sessions")
        self.assertEqual(resp2.status_code, 200)


if __name__ == "__main__":
    unittest.main()
