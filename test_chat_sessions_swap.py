"""Tests for chat_sessions.swap_session (HUB0008 4/11, multi-chat workspace):
changing a chat's provider/model/account in place -- every transcript row kept
verbatim, one marker row appended, one swaps entry appended, claudeSessionId
cleared, and _build_local_messages skipping marker rows in the replay while
keeping every other row.

No tokens or credentials anywhere -- account labels are plain fake strings, and
the fake claudeSessionId is an obviously-not-real id.

Run: python -m unittest test_chat_sessions_swap test_chat_sessions -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import accounts  # noqa: E402
import chat_sessions  # noqa: E402
import claude_client  # noqa: E402
import handoff  # noqa: E402
import model_stats_client  # noqa: E402
import ollama_client  # noqa: E402


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


class HandOffFirstTurnTests(unittest.TestCase):
    """HUB0008 6/11 -- _send_claude's first turn after a swap carries a hand-off;
    a fresh session's first turn and later (resumed) turns are unchanged.
    claude_client.generate is replaced by a prompt-capturing fake; no tokens."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage_dir = Path(self._tmp.name)
        self.root = str(self.storage_dir)
        session = chat_sessions.start_new_conversation(
            self.storage_dir, [self.root], None,
            provider="claude", model="opus")
        self.sid = session["id"]

    def _session(self):
        return chat_sessions.get_session(self.storage_dir, self.sid)

    def _two_rows(self):
        return [
            {"role": "user", "text": "please rename foo.py to bar.py",
             "timestamp": "2026-10-19T00:00:00Z"},
            {"role": "assistant", "text": "done, renamed it",
             "timestamp": "2026-10-19T00:01:00Z"},
        ]

    def _send_captured(self, session, message):
        """One _send_claude turn with canned registry and a prompt-capturing fake."""
        calls = []

        def fake_generate(m, **kwargs):
            calls.append(m)
            return {"sessionId": "sess-new", "model": "opus", "response": "ok"}

        with mock.patch.object(claude_client, "generate", side_effect=fake_generate), \
             mock.patch.object(accounts, "load", return_value=[]), \
             mock.patch.object(accounts, "live_label", return_value=None), \
             mock.patch.object(model_stats_client, "record_call"):
            chat_sessions._send_claude(session, message, self.storage_dir)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_swapped_first_claude_turn_has_handoff_then_new_message(self):
        s = self._session()
        s["transcript"] = [dict(r) for r in self._two_rows()]
        s["provider"] = "claude"
        s["claudeSessionId"] = None
        chat_sessions._write_session(self.storage_dir, s)
        out = self._send_captured(self._session(), "and now delete bar.py")
        # The hand-off (handoff.py's header + both earlier rows) is present...
        self.assertIn("HANDOFF: this conversation is continuing", out)
        self.assertIn("User: please rename foo.py to bar.py", out)
        self.assertIn("Assistant: done, renamed it", out)
        # ...comes BEFORE the new message, which stays the message's tail.
        self.assertLess(out.index("HANDOFF:"), out.index("and now delete bar.py"))
        self.assertTrue(out.endswith("and now delete bar.py"))
        # A separator line sits between them.
        self.assertIn("\n\n---\n\n", out)
        # The current turn's own rows are not replayed inside the hand-off.
        self.assertNotIn("User: and now delete bar.py", out)

    def test_fresh_session_first_turn_is_unchanged(self):
        # No earlier rows at all: the prompt is exactly the new message.
        out = self._send_captured(self._session(), "hello world")
        self.assertEqual(out, "hello world")

    def test_later_turn_with_resume_id_has_no_handoff(self):
        s = self._session()
        s["transcript"] = [dict(r) for r in self._two_rows()]
        s["claudeSessionId"] = "sess-existing"  # the CLI already knows the conversation
        chat_sessions._write_session(self.storage_dir, s)
        out = self._send_captured(self._session(), "second question")
        self.assertEqual(out, "second question")

    def test_stored_summary_text_reaches_the_next_handoff(self):
        s = self._session()
        s["transcript"] = [dict(r) for r in self._two_rows()]
        s["claudeSessionId"] = None
        s["summary"] = {"upToIndex": 2, "text": "STORED-SUMMARY-TEXT",
                        "at": "2026-10-19T00:02:00Z"}
        chat_sessions._write_session(self.storage_dir, s)
        out = self._send_captured(self._session(), "carry on")
        self.assertIn("STORED-SUMMARY-TEXT", out)
        self.assertLess(out.index("STORED-SUMMARY-TEXT"), out.index("carry on"))


class SummarizeOlderTests(unittest.TestCase):
    """HUB0008 6/11 -- summarize_older(): writes session["summary"], never touches
    transcript rows, skips when unneeded, and re-raises a summarizer error with
    state untouched. All summarizers/generates here are fakes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage_dir = Path(self._tmp.name)
        session = chat_sessions.start_new_conversation(
            self.storage_dir, [str(self.storage_dir)], None,
            provider="claude", model="opus")
        self.sid = session["id"]
        # 25 rows -> 5 are "older" than the last CHAT_RECENT_TURNS_KEPT (20).
        self.rows = [
            {"role": "user" if i % 2 else "assistant",
             "text": "row %d about the work" % i,
             "timestamp": "2026-10-19T00:%02d:00Z" % i}
            for i in range(1, 26)
        ]
        s = chat_sessions.get_session(self.storage_dir, self.sid)
        s["transcript"] = [dict(r) for r in self.rows]
        chat_sessions._write_session(self.storage_dir, s)

    def _session(self):
        return chat_sessions.get_session(self.storage_dir, self.sid)

    def _small_claude_window(self):
        # 1000-token window -> needs_summary threshold 500 tokens (~2000 chars);
        # 25 rows x ~20 chars (~130 tokens) -- make rows long enough to cross it.
        return mock.patch.dict(os.environ,
                               {"AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS": "64"})

    def test_writes_summary_and_keeps_every_row(self):
        captured = []

        def fake_sum(text):
            captured.append(text)
            return "SUMMARY-TEXT"

        with self._small_claude_window():
            out = chat_sessions.summarize_older(
                self.storage_dir, self.sid, summarizer=fake_sum)
        self.assertIsNotNone(out)
        self.assertEqual(out["summary"]["text"], "SUMMARY-TEXT")
        self.assertEqual(out["summary"]["upToIndex"],
                         len(self.rows) - chat_sessions.CHAT_RECENT_TURNS_KEPT)
        self.assertIn("at", out["summary"])
        # Only the OLDER rows reached the summarizer; recent ones did not.
        self.assertEqual(len(captured), 1)
        self.assertIn("row 1", captured[0])
        self.assertNotIn("row 6", captured[0])
        # Persisted: every transcript row kept verbatim, summary on disk too.
        disk = self._session()
        self.assertEqual(disk["transcript"], [dict(r) for r in self.rows])
        self.assertEqual(disk["summary"]["text"], "SUMMARY-TEXT")

    def test_skips_when_needs_summary_is_false(self):
        calls = []
        # Large window -> the transcript fits comfortably: no summary, no call.
        with mock.patch.dict(os.environ,
                             {"AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS": "200000"}):
            out = chat_sessions.summarize_older(
                self.storage_dir, self.sid,
                summarizer=lambda t: calls.append(t) or "X")
        self.assertIsNone(out["summary"])
        self.assertEqual(calls, [])

    def test_no_older_rows_is_a_noop(self):
        s = self._session()
        s["transcript"] = [dict(r) for r in self.rows[:5]]  # fewer than 20 rows
        chat_sessions._write_session(self.storage_dir, s)
        calls = []
        with self._small_claude_window():
            out = chat_sessions.summarize_older(
                self.storage_dir, self.sid,
                summarizer=lambda t: calls.append(t) or "X")
        self.assertIsNone(out["summary"])
        self.assertEqual(calls, [])

    def test_summarizer_error_leaves_state_unchanged_and_reraises(self):
        def boom(text):
            raise RuntimeError("summarizer down")

        with self._small_claude_window():
            with self.assertRaises(RuntimeError):
                chat_sessions.summarize_older(
                    self.storage_dir, self.sid, summarizer=boom)
        disk = self._session()
        self.assertIsNone(disk["summary"])
        self.assertEqual(disk["transcript"], [dict(r) for r in self.rows])

    def test_default_summarizer_calls_ollama_generate_with_600(self):
        calls = []

        def fake_generate(prompt, **kwargs):
            calls.append((prompt, kwargs))
            return {"response": "FROM-OLLAMA", "thinking": ""}

        with self._small_claude_window(), \
             mock.patch.object(ollama_client, "generate", side_effect=fake_generate):
            out = chat_sessions.summarize_older(self.storage_dir, self.sid)
        self.assertEqual(out["summary"]["text"], "FROM-OLLAMA")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["num_predict"], 600)
        self.assertIn("row 1", calls[0][0])

    def test_missing_session_returns_none(self):
        self.assertIsNone(chat_sessions.summarize_older(
            self.storage_dir, "chat-does-not-exist", summarizer=lambda t: "X"))


if __name__ == "__main__":
    unittest.main()
