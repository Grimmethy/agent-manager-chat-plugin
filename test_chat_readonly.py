"""Tests for read-only chat sessions (2026-11-13, Feature Forge grill sessions): a conversation started
with readOnly true gets Read, Grep and Glob only; the flag is stored, is part of the "reuse an empty
session" match, defaults to False for older saved sessions, and shows up in Claude's first-turn preamble.

No tokens or network: claude_client.generate is a capturing fake and the accounts registry is canned.

Run: python -m unittest test_chat_readonly -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import accounts  # noqa: E402
import app  # noqa: E402
import chat_sessions  # noqa: E402
import claude_client  # noqa: E402
import model_stats_client  # noqa: E402


class ReadOnlySessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage_dir = Path(self._tmp.name)
        self.root = str(self.storage_dir)

    def _start(self, **kw):
        return chat_sessions.start_new_conversation(
            self.storage_dir, [self.root], None, provider="claude", model="opus", **kw)

    def _send(self, session, message="hello"):
        calls = []

        def fake_generate(m, **kwargs):
            calls.append((m, kwargs))
            return {"sessionId": "sess-1", "model": "opus", "response": "ok"}

        with mock.patch.object(claude_client, "generate", side_effect=fake_generate), \
             mock.patch.object(accounts, "load", return_value=[]), \
             mock.patch.object(accounts, "live_label", return_value=None), \
             mock.patch.object(model_stats_client, "record_call"):
            chat_sessions._send_claude(session, message, self.storage_dir)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_default_is_not_read_only_and_flag_is_stored(self):
        self.assertFalse(self._start()["readOnly"])
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        ro = chat_sessions.start_new_conversation(Path(other.name), [self.root], None, provider="claude", model="opus", read_only=True)
        self.assertTrue(ro["readOnly"])
        self.assertTrue(chat_sessions.get_session(Path(other.name), ro["id"])["readOnly"])

    def test_an_empty_session_is_only_reused_in_the_same_mode(self):
        normal = self._start()
        self.assertEqual(self._start()["id"], normal["id"])
        ro = self._start(read_only=True)
        self.assertNotEqual(ro["id"], normal["id"])
        self.assertEqual(self._start(read_only=True)["id"], ro["id"])

    def test_a_session_saved_before_the_field_existed_counts_as_not_read_only(self):
        old = chat_sessions._new_session([self.root], None, "claude", "opus", None)
        del old["readOnly"]
        chat_sessions._write_session(self.storage_dir, old)
        self.assertEqual(self._start()["id"], old["id"])
        self.assertNotEqual(self._start(read_only=True)["id"], old["id"])

    def test_read_only_session_gets_only_read_grep_glob(self):
        _, kwargs = self._send(self._start(read_only=True))
        self.assertEqual(kwargs["allowed_tools"], "Read,Grep,Glob")

    def test_normal_session_keeps_the_full_tool_list(self):
        _, kwargs = self._send(self._start())
        self.assertEqual(kwargs["allowed_tools"], chat_sessions.CHAT_CLAUDE_ALLOWED_TOOLS)
        self.assertIn("Bash", kwargs["allowed_tools"])

    def test_preamble_says_read_only_instead_of_read_write(self):
        text, _ = self._send(self._start(read_only=True))
        self.assertIn("READ-ONLY", text)
        self.assertNotIn("full read/write access", text)

    def test_normal_multi_root_preamble_is_unchanged(self):
        session = chat_sessions.start_new_conversation(
            self.storage_dir, [self.root, str(self.storage_dir / "other")], None, provider="claude", model="opus")
        text, _ = self._send(session)
        self.assertIn("full read/write access", text)
        self.assertNotIn("READ-ONLY", text)


class ReadOnlyRouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def test_new_with_read_only_true(self):
        resp = self.client.post("/api/chat/new", json={"provider": "claude", "readOnly": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["readOnly"])

    def test_new_without_the_flag_is_not_read_only(self):
        self.assertFalse(self.client.post("/api/chat/new", json={"provider": "claude"}).get_json()["readOnly"])
        self.assertFalse(self.client.post("/api/chat/new").get_json()["readOnly"])

    def test_only_a_literal_true_counts(self):
        resp = self.client.post("/api/chat/new", json={"provider": "claude", "readOnly": "yes"})
        self.assertFalse(resp.get_json()["readOnly"])


if __name__ == "__main__":
    unittest.main()
