"""POST /api/chat/<id>/reserve: when the server-side reservation succeeds but persisting the local flag fails (AC-4 divergence), the failure is
logged with the stable tag AC4_DIVERGENCE (session, model, error, the manual-reconciliation note) and still surfaces as a 500. The happy path and
a failing server call are untouched. internal_api_client.reserve is faked: no network.

Run: .venv/bin/python -m unittest test_chat_routes_reserve_divergence -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402
import internal_api_client  # noqa: E402
import ollama_client  # noqa: E402

SID = "chat-divergence-test"


def _session(provider="local"):
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": SID, "provider": provider, "model": "fake-model", "effort": None,
        "roots": [root], "repoRoot": root, "instancesDir": None, "claudeSessionId": None,
        "transcript": [{"role": "user", "text": "hello"}],
        "status": "active", "startedAt": "2026-10-26T00:00:00Z", "endedAt": None, "reserved": False,
    }


class ReserveDivergenceTest(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()
        chat_sessions._write_sessions(self.storage, {SID: _session()})

    def _post(self, on=True):
        return self.client.post(f"/api/chat/{SID}/reserve", json={"on": on})

    def test_a_failing_local_write_after_a_successful_server_reserve_logs_the_tag_and_still_returns_500(self):
        with mock.patch.object(internal_api_client, "reserve", return_value={"ok": True}) as reserve, \
                mock.patch.object(chat_sessions, "set_reserved", side_effect=OSError("disk full")), \
                self.assertLogs("routes.chat", level="ERROR") as logs:
            resp = self._post(True)
        self.assertEqual(resp.status_code, 500)
        reserve.assert_called_once()
        line = "\n".join(logs.output)
        for needle in ("AC4_DIVERGENCE", SID, "model=" + str(ollama_client.MODEL), "OSError", "disk full", "Manual reconciliation required"):
            self.assertIn(needle, line)

    def test_the_happy_path_logs_nothing_and_persists_the_flag(self):
        with mock.patch.object(internal_api_client, "reserve", return_value={"ok": True}):
            with self.assertNoLogs("routes.chat", level="ERROR"):
                resp = self._post(True)
        self.assertEqual(resp.status_code, 200)
        self.assertIs(resp.get_json()["reserved"], True)
        self.assertIs(chat_sessions.get_session(self.storage, SID)["reserved"], True)

    def test_a_failing_server_call_is_a_502_and_is_not_tagged_as_a_divergence(self):
        failing = mock.Mock(side_effect=internal_api_client.InternalApiError("down"))
        with mock.patch.object(internal_api_client, "reserve", failing):
            with self.assertNoLogs("routes.chat", level="ERROR"):
                resp = self._post(True)
        self.assertEqual(resp.status_code, 502)
        self.assertIs(chat_sessions.get_session(self.storage, SID)["reserved"], False)


if __name__ == "__main__":
    unittest.main()
