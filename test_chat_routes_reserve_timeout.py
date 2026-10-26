"""internal_api_client.reserve's timeout: long by default (the user's reserve toggle really waits behind a busy worker lane), short only for the best-effort
per-message refresh of an already-reserved session. requests.post and the stream are faked: no network, no model.

Run: .venv/bin/python -m unittest test_chat_routes_reserve_timeout -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402
import internal_api_client as iac  # noqa: E402

SID = "chat-reserve-timeout-test"


def _fake_response(payload):
    r = mock.Mock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class ClientTimeoutTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_default_timeout_is_the_long_one(self):
        with mock.patch("requests.post", return_value=_fake_response({"reserved": True})) as m:
            iac.reserve("s1", "m", True)
        self.assertEqual(m.call_args.kwargs["timeout"], 300)
        self.assertEqual(iac.RESERVE_TIMEOUT_S, 300)

    def test_an_explicit_timeout_is_passed_through(self):
        with mock.patch("requests.post", return_value=_fake_response({"reserved": True})) as m:
            iac.reserve("s1", "m", True, timeout=20)
        self.assertEqual(m.call_args.kwargs["timeout"], 20)


class RouteTimeoutTest(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()
        root = str(app.CORE_REPO_ROOT)
        self.session = {"id": SID, "provider": "local", "model": "fake-model", "effort": None, "roots": [root], "repoRoot": root, "instancesDir": None,
                        "claudeSessionId": None, "transcript": [{"role": "user", "text": "hello"}], "status": "active",
                        "startedAt": "2026-10-26T00:00:00Z", "endedAt": None, "reserved": True}
        chat_sessions._write_sessions(self.storage, {SID: self.session})

    def _fake_stream(self, storage_dir, session_id, message):
        yield {"type": "final", "session": chat_sessions.get_session(storage_dir, session_id)}

    def test_the_per_message_refresh_uses_the_short_timeout(self):
        with mock.patch.object(chat_sessions, "stream_message", self._fake_stream), \
                mock.patch.object(iac, "preempt", return_value=[]), \
                mock.patch.object(iac, "reserve", return_value={"reserved": True}) as reserve:
            resp = self.client.post(f"/api/chat/{SID}/message", json={"message": "hi"})
            resp.get_data()
        reserve.assert_called_once()
        self.assertEqual(reserve.call_args.kwargs.get("timeout"), 20)
        self.assertIs(reserve.call_args.kwargs.get("on"), True)

    def test_the_user_toggle_keeps_the_long_default(self):
        with mock.patch.object(iac, "reserve", return_value={"reserved": True}) as reserve:
            resp = self.client.post(f"/api/chat/{SID}/reserve", json={"on": True})
        self.assertEqual(resp.status_code, 200)
        reserve.assert_called_once()
        self.assertNotIn("timeout", reserve.call_args.kwargs, "the toggle must not pass a short timeout")


if __name__ == "__main__":
    unittest.main()
