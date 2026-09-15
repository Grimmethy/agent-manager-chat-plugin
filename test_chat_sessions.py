"""Tests for chat_sessions.py's _stream_local() footer logic -- the three distinct
forced-summary causes (2026-09-15, Grimmethy: "Different stop reasons should absolutely
be distinguishable. It keeps the system understandable.") and the false-message bug this
replaces: 'voluntary-stop' used to share 'turns'' "ran out of its 100-turn tool budget"
text even when the real turnsUsed was nowhere near the cap (confirmed live via
model-stats.db: 2 of 3 real Chat calls in one session showed that exact false message).

Run: .venv/bin/python -m unittest test_chat_sessions -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402
import app  # noqa: E402


def _session():
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": "chat-test-session",
        "provider": "local",
        "repoRoot": root,
        "roots": [root],
        "transcript": [],
    }


def _fake_stream(events):
    def _gen(**kwargs):
        yield from events
    return _gen


class StreamLocalFooterTest(unittest.TestCase):
    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)

    def _run(self, events):
        session = _session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
            return "".join(chat_sessions._stream_local(session, "hi"))

    def test_context_reason_says_context_room(self):
        out = self._run([
            {"type": "chunk", "text": "answer"},
            {"type": "final", "response": "answer", "turnsUsed": 3,
             "forcedSummary": True, "forcedSummaryReason": "context"},
        ])
        self.assertIn("context room", out)
        self.assertNotIn("100-turn", out)

    def test_turns_reason_says_100_turn_budget(self):
        out = self._run([
            {"type": "chunk", "text": "answer"},
            {"type": "final", "response": "answer", "turnsUsed": 100,
             "forcedSummary": True, "forcedSummaryReason": "turns"},
        ])
        self.assertIn("100-turn tool budget", out)
        self.assertNotIn("context room", out)

    def test_voluntary_stop_gets_no_misleading_footer(self):
        # The bug this test guards: turnsUsed=2 (nowhere near the 100-turn cap), but the
        # model just didn't emit a RESOLUTION: line on its first try. No exhaustion
        # occurred at all -- the old code claimed "ran out of its 100-turn tool budget"
        # here, which was simply false.
        out = self._run([
            {"type": "chunk", "text": "pong"},
            {"type": "final", "response": "pong", "turnsUsed": 2,
             "forcedSummary": True, "forcedSummaryReason": "voluntary-stop"},
        ])
        self.assertEqual(out, "pong")
        self.assertNotIn("100-turn", out)
        self.assertNotIn("context room", out)
        self.assertNotIn("Ran out of", out)

    def test_clean_finish_with_no_forced_summary_gets_no_footer(self):
        out = self._run([
            {"type": "chunk", "text": "answer"},
            {"type": "final", "response": "answer", "turnsUsed": 2, "forcedSummary": False},
        ])
        self.assertEqual(out, "answer")


if __name__ == "__main__":
    unittest.main()
