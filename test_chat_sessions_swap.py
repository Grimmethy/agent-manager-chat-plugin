"""Tests for chat_sessions.swap_session (HUB0008 4/11, multi-chat workspace):
changing a chat's provider/model/account in place -- every transcript row kept
verbatim, one marker row appended, one swaps entry appended, claudeSessionId
cleared, and _build_local_messages skipping marker rows in the replay while
keeping every other row.

No tokens or credentials anywhere -- account labels are plain fake strings, and
the fake claudeSessionId is an obviously-not-real id.

Run: python -m unittest test_chat_sessions_swap test_chat_sessions -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402


class SwapSessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage_dir = Path(self._tmp.name)
        # Fake repo root -- _build_local_messages' prompt helpers read AGENTS.md
        # from it best-effort; an empty temp dir just yields no section.
        self.root = str(self.storage_dir)
        session = chat_sessions.start_new_conversation(
            self.storage_dir, [self.root], None,
            provider="local", model="qwen2.5:14b", effort="low")
        self.sid = session["id"]
        # Two real transcript rows, identical in content and shape to real turns.
        s = chat_sessions.get_session(self.storage_dir, self.sid)
        self.originals = [
            {"role": "user", "text": "hello", "timestamp": "2026-10-19T00:00:00Z"},
            {"role": "assistant", "text": "hi there", "timestamp": "2026-10-19T00:01:00Z"},
        ]
        s["transcript"] = [dict(r) for r in self.originals]
        chat_sessions._write_session(self.storage_dir, s)

    def _session(self):
        return chat_sessions.get_session(self.storage_dir, self.sid)

    def test_local_to_claude_sets_fields_and_clears_resume_id(self):
        # A fake resume id that a prior Claude turn would have captured.
        s = self._session()
        s["claudeSessionId"] = "fake-resume-id-123"
        chat_sessions._write_session(self.storage_dir, s)
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "opus", account="work-01")
        self.assertIsNotNone(out)
        self.assertEqual(out["provider"], "claude")
        self.assertEqual(out["model"], "opus")
        self.assertEqual(out["account"], "work-01")
        self.assertIsNone(out["claudeSessionId"])
        # And back the other way: local has no account.
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "local", "qwen2.5:14b", effort="medium")
        self.assertEqual(out["provider"], "local")
        self.assertEqual(out["model"], "qwen2.5:14b")
        self.assertIsNone(out["account"])
        self.assertEqual(out["effort"], "medium")
        self.assertIsNone(out["claudeSessionId"])
        # Persists to disk, not just in memory.
        disk = self._session()
        self.assertEqual(disk["provider"], "local")
        self.assertIsNone(disk["account"])

    def test_swaps_list_grows_with_from_and_to(self):
        self.assertEqual(self._session()["swaps"], [])
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "opus", account="work-01")
        swaps = out["swaps"]
        self.assertEqual(len(swaps), 1)
        self.assertIn("at", swaps[0])
        self.assertEqual(swaps[0]["from"],
                         {"provider": "local", "model": "qwen2.5:14b", "account": None})
        self.assertEqual(swaps[0]["to"],
                         {"provider": "claude", "model": "opus", "account": "work-01"})
        out = chat_sessions.swap_session(self.storage_dir, self.sid, "local", "qwen2.5:14b")
        swaps = out["swaps"]
        self.assertEqual(len(swaps), 2)
        self.assertEqual(swaps[1]["from"],
                         {"provider": "claude", "model": "opus", "account": "work-01"})
        self.assertEqual(swaps[1]["to"],
                         {"provider": "local", "model": "qwen2.5:14b", "account": None})

    def test_marker_is_last_row_and_originals_untouched(self):
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "opus", account="work-01")
        transcript = out["transcript"]
        self.assertEqual(len(transcript), len(self.originals) + 1)
        self.assertEqual(transcript[:-1], self.originals)
        marker = transcript[-1]
        self.assertIs(marker["marker"], True)
        self.assertEqual(marker["role"], "assistant")
        self.assertIn("Switched from", marker["text"])
        self.assertIn("work-01", marker["text"])
        self.assertIn("timestamp", marker)

    def test_invalid_provider_raises_value_error(self):
        with self.assertRaises(ValueError):
            chat_sessions.swap_session(
                self.storage_dir, self.sid, "openai", "gpt-x")

    def test_missing_session_returns_none(self):
        self.assertIsNone(chat_sessions.swap_session(
            self.storage_dir, "chat-does-not-exist", "claude", "opus"))

    def test_ended_session_returns_none(self):
        chat_sessions.end_session(self.storage_dir, self.sid)
        self.assertIsNone(chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "opus"))

    def test_local_replay_skips_marker_but_keeps_every_other_row(self):
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "opus", account="work-01")
        s = chat_sessions.swap_session(self.storage_dir, self.sid, "local", "qwen2.5:14b")
        messages = chat_sessions._build_local_messages(s, "what next?")
        self.assertEqual(messages[0]["role"], "system")
        body = messages[1:-1]
        self.assertEqual(body, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        self.assertEqual(messages[-1], {"role": "user", "content": "what next?"})
        self.assertNotIn("Switched from", [m["content"] for m in body])

    def test_real_assistant_message_containing_word_marker_is_replayed(self):
        # The protected case: a REAL assistant row whose text contains the
        # substring "marker" but has no marker key must be replayed -- the skip
        # is keyed on the boolean flag, never on the text.
        s = self._session()
        s["transcript"].append({
            "role": "assistant",
            "text": 'you can add a "marker" to the file',
            "timestamp": "2026-10-19T00:02:00Z",
        })
        s["transcript"].append({
            "role": "assistant",
            "text": "Switched from local / qwen to something, said the marker.",
            "marker": True,
            "timestamp": "2026-10-19T00:03:00Z",
        })
        messages = chat_sessions._build_local_messages(s, "ok?")
        body = messages[1:-1]
        self.assertEqual(body, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "assistant", "content": 'you can add a "marker" to the file'},
        ])

    def test_two_swaps_in_a_row(self):
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "opus", account="work-01")
        out = chat_sessions.swap_session(
            self.storage_dir, self.sid, "claude", "sonnet", account="work-02")
        transcript = out["transcript"]
        # Two marker rows, both at the end, both flagged, all real rows kept.
        markers = [r for r in transcript if r.get("marker") is True]
        self.assertEqual(len(markers), 2)
        self.assertIs(transcript[-1]["marker"], True)
        self.assertIs(transcript[-2]["marker"], True)
        self.assertEqual(len([r for r in transcript if r.get("marker") is not True]),
                         len(self.originals))
        self.assertEqual(out["swaps"][0]["to"]["model"], "opus")
        self.assertEqual(out["swaps"][0]["to"]["account"], "work-01")
        self.assertEqual(out["swaps"][1]["to"]["model"], "sonnet")
        self.assertEqual(out["swaps"][1]["to"]["account"], "work-02")
        # Replay after both swaps: only the real rows survive.
        messages = chat_sessions._build_local_messages(out, "?")
        body = messages[1:-1]
        self.assertEqual(len(body), len(self.originals))
        self.assertNotIn("Switched from", [m["content"] for m in body])


if __name__ == "__main__":
    unittest.main()
