"""Tests for chat_sessions.py's empty-reply retry/flag logic (2026-10-30, HUB0033):
a turn that ends with no visible reply text must be retried or flagged, never stored
as a normal reply.

Run: python -m unittest test_chat_empty_reply -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402
import app  # noqa: E402


def _session(provider="local"):
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": "chat-test-session",
        "provider": provider,
        "repoRoot": root,
        "roots": [root],
        "transcript": [],
    }


def _fake_stream_local(events_factory):
    """Returns a fake _stream_local that yields events from events_factory(session, message, result_box)."""
    def _fake(session, message, result_box=None):
        events = events_factory(session, message, result_box)
        for item in events:
            if isinstance(item, dict):
                yield item
            else:
                yield item
    return _fake


class EmptyReplyRetryTest(unittest.TestCase):
    """Tests for the empty-reply retry/flag logic in _stream_message_turn."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _seed_active_session(self, provider="local"):
        session = _session(provider)
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        return session["id"]

    def test_empty_text_after_tools_triggers_one_retry_and_keeps_retry_text(self):
        """Empty text after tools triggers exactly one retry and keeps the retry text."""
        session_id = self._seed_active_session()
        call_count = [0]

        def fake_stream(session, message, result_box):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: empty text but with tool events
                if result_box is not None:
                    result_box["final_text"] = ""
                    result_box["tools"] = [{"tool": "run_bash", "args": {"command": "ls"}, "result": "file1 file2"}]
                    result_box["proposals"] = []
                yield {"type": "tool_start", "name": "run_bash", "summary": "ls"}
                yield {"type": "tool_end", "name": "run_bash", "ok": True, "summary": "file1 file2"}
                yield {"type": "final", "response": "", "turnsUsed": 2, "forcedSummary": False,
                       "toolCallLog": [{"tool": "run_bash", "args": {"command": "ls"}, "result": "file1 file2"}]}
            else:
                # Second call (retry): non-empty text
                if result_box is not None:
                    result_box["final_text"] = "Here are the files: file1, file2"
                    result_box["tools"] = []
                    result_box["proposals"] = []
                yield {"type": "final", "response": "Here are the files: file1, file2", "turnsUsed": 1,
                       "forcedSummary": False, "toolCallLog": []}

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        self.assertEqual(call_count[0], 2, "Should retry exactly once")
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        self.assertIn("Here are the files: file1, file2", stored["transcript"][-1]["text"])
        self.assertNotIn("error", stored["transcript"][-1].get("text", "").lower())

    def test_empty_then_empty_stores_error_entry_with_tool_count(self):
        """Empty then empty stores the error entry with the tool count."""
        session_id = self._seed_active_session()
        call_count = [0]

        def fake_stream(session, message, result_box):
            call_count[0] += 1
            # Always empty
            if result_box is not None:
                result_box["final_text"] = ""
                result_box["tools"] = [{"tool": "run_bash", "args": {"command": "ls"}, "result": ""}]
                result_box["proposals"] = []
            yield {"type": "tool_start", "name": "run_bash", "summary": "ls"}
            yield {"type": "tool_end", "name": "run_bash", "ok": True, "summary": ""}
            yield {"type": "final", "response": "", "turnsUsed": 2, "forcedSummary": False,
                   "toolCallLog": [{"tool": "run_bash", "args": {"command": "ls"}, "result": ""}]}

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        self.assertEqual(call_count[0], 2, "Should retry exactly once even if second call is also empty")
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        last_entry = stored["transcript"][-1]
        self.assertTrue(last_entry.get("error"), "Should be flagged as error")
        self.assertIn("it ran 1 tool(s)", last_entry["text"], "Should include tool count")

    def test_normal_reply_never_retries(self):
        """A normal reply never retries."""
        session_id = self._seed_active_session()
        call_count = [0]

        def fake_stream(session, message, result_box):
            call_count[0] += 1
            if result_box is not None:
                result_box["final_text"] = "Normal reply"
                result_box["tools"] = []
                result_box["proposals"] = []
            yield {"type": "final", "response": "Normal reply", "turnsUsed": 1, "forcedSummary": False,
                   "toolCallLog": []}

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        self.assertEqual(call_count[0], 1, "Should not retry for normal reply")
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        self.assertEqual(stored["transcript"][-1]["text"], "Normal reply")

    def test_reply_only_footer_counts_as_empty(self):
        """A reply that is only a footer counts as empty."""
        session_id = self._seed_active_session()
        call_count = [0]

        def fake_stream(session, message, result_box):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: only footer, no visible text
                if result_box is not None:
                    result_box["final_text"] = "\n\nTASK-REF: N/A"
                    result_box["tools"] = [{"tool": "run_bash", "args": {"command": "ls"}, "result": "file1"}]
                    result_box["proposals"] = []
                yield {"type": "tool_start", "name": "run_bash", "summary": "ls"}
                yield {"type": "tool_end", "name": "run_bash", "ok": True, "summary": "file1"}
                yield {"type": "final", "response": "\n\nTASK-REF: N/A", "turnsUsed": 2,
                       "forcedSummary": False, "toolCallLog": [{"tool": "run_bash", "args": {"command": "ls"}, "result": "file1"}]}
            else:
                # Second call: non-empty
                if result_box is not None:
                    result_box["final_text"] = "Got it"
                    result_box["tools"] = []
                    result_box["proposals"] = []
                yield {"type": "final", "response": "Got it", "turnsUsed": 1, "forcedSummary": False,
                       "toolCallLog": []}

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        self.assertEqual(call_count[0], 2, "Should retry when only footer is present")
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        self.assertIn("Got it", stored["transcript"][-1]["text"])

    def test_footer_less_normal_reply_is_untouched(self):
        """A footer-less normal reply is untouched."""
        session_id = self._seed_active_session()
        call_count = [0]

        def fake_stream(session, message, result_box):
            call_count[0] += 1
            if result_box is not None:
                result_box["final_text"] = "Just a reply"
                result_box["tools"] = []
                result_box["proposals"] = []
            yield {"type": "final", "response": "Just a reply", "turnsUsed": 1, "forcedSummary": False,
                   "toolCallLog": []}

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        self.assertEqual(call_count[0], 1, "Should not retry")
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        self.assertEqual(stored["transcript"][-1]["text"], "Just a reply")

    def test_retry_call_receives_nudge_and_normal_call_does_not(self):
        """The retry call receives the nudge and the normal call does not."""
        session_id = self._seed_active_session()
        messages_received = []

        def fake_stream(session, message, result_box):
            messages_received.append(message)
            # First call: empty
            if len(messages_received) == 1:
                if result_box is not None:
                    result_box["final_text"] = ""
                    result_box["tools"] = [{"tool": "run_bash", "args": {"command": "ls"}, "result": ""}]
                    result_box["proposals"] = []
                yield {"type": "final", "response": "", "turnsUsed": 1, "forcedSummary": False,
                       "toolCallLog": [{"tool": "run_bash", "args": {"command": "ls"}, "result": ""}]}
            else:
                # Second call: non-empty
                if result_box is not None:
                    result_box["final_text"] = "Retry answer"
                    result_box["tools"] = []
                    result_box["proposals"] = []
                yield {"type": "final", "response": "Retry answer", "turnsUsed": 1, "forcedSummary": False,
                       "toolCallLog": []}

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "original message"))

        self.assertEqual(len(messages_received), 2)
        self.assertEqual(messages_received[0], "original message", "First call should have original message")
        self.assertIn("You ran tools but gave no answer", messages_received[1],
                      "Second call should have nudge")
        self.assertIn("Answer now in plain words", messages_received[1],
                      "Second call should have full nudge text")

    def test_claude_provider_untouched(self):
        """Claude provider untouched by empty-reply logic."""
        session_id = self._seed_active_session(provider="claude")
        call_count = [0]

        def fake_stream(session, message, result_box):
            call_count[0] += 1
            # Claude path doesn't use _stream_local, but we patch it anyway to count
            yield {"type": "final", "response": "", "turnsUsed": 1, "forcedSummary": False,
                   "toolCallLog": []}

        def fake_claude(session, message, storage_dir):
            # Claude path returns reply directly
            return ""

        with mock.patch.object(chat_sessions, "_stream_local", side_effect=fake_stream):
            with mock.patch.object(chat_sessions, "_send_claude", side_effect=fake_claude):
                list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        # Claude path should not have called _stream_local at all
        self.assertEqual(call_count[0], 0, "Claude path should not use _stream_local")
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        # Claude empty reply is not retried (Claude path is unchanged)
        self.assertEqual(stored["transcript"][-1]["text"], "")


class VisibleTextHelperTest(unittest.TestCase):
    """Tests for the _visible_text helper function."""

    def test_empty_string_returns_empty(self):
        self.assertEqual(chat_sessions._visible_text(""), "")

    def test_whitespace_only_returns_empty(self):
        self.assertEqual(chat_sessions._visible_text("   \n  "), "")

    def test_text_with_footer_strips_footer(self):
        text = "Hello world\n\nTASK-REF: t-123"
        self.assertEqual(chat_sessions._visible_text(text), "Hello world")

    def test_text_with_context_log_footer_strips_it(self):
        text = "Answer here\n\nCONTEXT-LOG: summary"
        self.assertEqual(chat_sessions._visible_text(text), "Answer here")

    def test_only_footer_returns_empty(self):
        text = "\n\nTASK-REF: N/A"
        self.assertEqual(chat_sessions._visible_text(text), "")

    def test_no_footer_returns_text(self):
        text = "Just a reply"
        self.assertEqual(chat_sessions._visible_text(text), "Just a reply")


if __name__ == "__main__":
    unittest.main()
