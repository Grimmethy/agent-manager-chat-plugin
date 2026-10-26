"""POST /api/chat/<id>/message, local provider: the SSE response starts BEFORE the (slow) internal preempt call and a failed preempt is not an error frame.

Time-to-first-byte is checked by ordering, not wall clock: the first streamed chunk is read while the faked preempt has not been called yet. Everything internal is
faked (internal_api_client.preempt, chat_sessions.stream_message): no network, no model.

Run: .venv/bin/python -m unittest test_chat_routes_preempt_stream -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402
import internal_api_client  # noqa: E402

SID = "chat-preempt-stream-test"


def _session(provider="local"):
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": SID, "provider": provider, "model": "fake-model", "effort": None,
        "roots": [root], "repoRoot": root, "instancesDir": None, "claudeSessionId": None,
        "transcript": [{"role": "user", "text": "hello"}],
        "status": "active", "startedAt": "2026-10-26T00:00:00Z", "endedAt": None, "reserved": False,
    }


def _fake_stream(storage_dir, session_id, message):
    yield {"type": "chunk", "text": "hi"}
    yield {"type": "final", "session": chat_sessions.get_session(storage_dir, session_id)}


def _frames(chunks):
    return [json.loads(c[len("data: "):]) for c in "".join(chunks).split("\n\n") if c.startswith("data: ")]


class PreemptStreamTest(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def _post(self, provider="local", preempt=None):
        chat_sessions._write_sessions(self.storage, {SID: _session(provider)})
        self.calls = []

        def default_preempt():
            self.calls.append("preempt")
            return ["worker-1"]
        patches = [mock.patch.object(chat_sessions, "stream_message", _fake_stream),
                   mock.patch.object(internal_api_client, "preempt", preempt or default_preempt)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.client.post(f"/api/chat/{SID}/message", json={"message": "hi"}, buffered=False)

    def test_the_first_bytes_go_out_before_preempt_is_called(self):
        resp = self._post()
        stream = iter(resp.response)
        first = next(stream)
        self.assertTrue(bytes(first).startswith(b": working"), first)
        self.assertEqual(self.calls, [], "preempt must not have run before the first chunk reached the client")
        rest = [bytes(c).decode() for c in stream]
        self.assertEqual(self.calls, ["preempt"])
        self.assertEqual([f["type"] for f in _frames(rest)], ["preempt", "chunk", "final"])

    def test_a_failed_preempt_is_logged_not_sent_as_an_error_frame(self):
        failing = mock.Mock(side_effect=internal_api_client.InternalApiError("core is slow"))
        resp = self._post(preempt=failing)
        chunks = [bytes(c).decode() for c in resp.response]
        self.assertEqual([f["type"] for f in _frames(chunks)], ["chunk", "final"])
        self.assertNotIn("preempt failed", "".join(chunks))
        failing.assert_called_once()

    def test_a_preempt_that_freed_nothing_sends_no_preempt_frame(self):
        resp = self._post(preempt=lambda: [])
        chunks = [bytes(c).decode() for c in resp.response]
        self.assertEqual([f["type"] for f in _frames(chunks)], ["chunk", "final"])

    def test_a_claude_turn_never_calls_preempt_and_sends_no_comment(self):
        resp = self._post(provider="claude")
        chunks = [bytes(c).decode() for c in resp.response]
        self.assertEqual(self.calls, [])
        self.assertFalse(any(c.startswith(":") for c in chunks))
        self.assertEqual([f["type"] for f in _frames(chunks)], ["chunk", "final"])


if __name__ == "__main__":
    unittest.main()
