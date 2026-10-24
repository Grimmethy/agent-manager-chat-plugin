"""Tests for handoff.py (pure, no I/O) -- HUB0008 5/11.

Run from the repo root:  python -m unittest test_handoff -v
"""
import os
import unittest


def row(role="user", text="hi", **extra):
    out = {"role": role, "text": text, "timestamp": "2026-01-01T00:00:00Z"}
    out.update(extra)
    return out


class WindowTokensTest(unittest.TestCase):
    def test_defaults_when_env_unset(self):
        for name in ("AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS",
                     "AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS"):
            self.assertNotIn(name, os.environ)
        import handoff
        self.assertEqual(handoff.window_tokens("claude"), 200000)
        self.assertEqual(handoff.window_tokens("local"), 16384)

    def test_valid_env_override(self):
        import handoff
        old = dict(os.environ)
        try:
            os.environ["AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS"] = "128000"
            os.environ["AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS"] = "8192"
            self.assertEqual(handoff.window_tokens("claude"), 128000)
            self.assertEqual(handoff.window_tokens("local"), 8192)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_bad_env_values_fall_back_to_default(self):
        import handoff
        old = dict(os.environ)
        try:
            for bad in ("not-a-number", "0", "-5"):
                os.environ["AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS"] = bad
                os.environ["AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS"] = bad
                self.assertEqual(handoff.window_tokens("claude"), 200000)
                self.assertEqual(handoff.window_tokens("local"), 16384)
        finally:
            os.environ.clear()
            os.environ.update(old)


class EstimateTokensTest(unittest.TestCase):
    def test_known_lengths(self):
        import handoff
        self.assertEqual(handoff.estimate_tokens(""), 0)
        self.assertEqual(handoff.estimate_tokens("abcde"), 2)  # ceil(5/4)
        self.assertEqual(handoff.estimate_tokens("abcd" * 5), 5)  # ceil(20/4)


class TranscriptTokensTest(unittest.TestCase):
    def test_error_and_marker_rows_excluded(self):
        import handoff
        transcript = [
            row("user", "a" * 40),                     # 10 tokens
            row("assistant", "b" * 80, error=True),    # excluded
            row("user", "c" * 12, marker=True),        # excluded
            row("assistant", "d" * 16),                # 4 tokens
        ]
        self.assertEqual(handoff.transcript_tokens(transcript), 14)

    def test_empty_and_none(self):
        import handoff
        self.assertEqual(handoff.transcript_tokens([]), 0)
        self.assertEqual(handoff.transcript_tokens(None), 0)


class NeedsSummaryTest(unittest.TestCase):
    def _make(self, chars):
        return [row("user", "x" * chars)]

    def test_claude_threshold(self):
        import handoff
        half = 0.5 * 200000
        self.assertFalse(handoff.needs_summary(self._make(int(half) * 4 - 4), "claude"))
        self.assertFalse(handoff.needs_summary(self._make(int(half) * 4), "claude"))  # exactly at
        self.assertTrue(handoff.needs_summary(self._make(int(half) * 4 + 4), "claude"))

    def test_local_threshold(self):
        import handoff
        half = 0.5 * 16384
        self.assertFalse(handoff.needs_summary(self._make(int(half) * 4 - 4), "local"))
        self.assertFalse(handoff.needs_summary(self._make(int(half) * 4), "local"))
        self.assertTrue(handoff.needs_summary(self._make(int(half) * 4 + 4), "local"))


class BuildHandoffTest(unittest.TestCase):
    def test_empty_transcript_returns_header_only(self):
        import handoff
        result = handoff.build_handoff([], None, 5, 1000)
        self.assertEqual(result, handoff.HANDOFF_HEADER)

    def test_empty_transcript_with_summary_never_raises(self):
        import handoff
        result = handoff.build_handoff([], "the summary", 5, 1000)
        lines = result.split("\n")
        self.assertEqual(lines[0], handoff.HANDOFF_HEADER)
        self.assertEqual(lines[1], "the summary")

    def test_keeps_last_n_oldest_first_with_summary_and_omitted_count(self):
        import handoff
        transcript = [row("user", "one"), row("assistant", "two"),
                      row("user", "three"), row("assistant", "four")]
        result = handoff.build_handoff(transcript, "earlier stuff", 2, 1000)
        lines = result.split("\n")
        self.assertEqual(lines[0], handoff.HANDOFF_HEADER)
        self.assertEqual(lines[1], "earlier stuff")
        self.assertEqual(lines[2], "User: three")
        self.assertEqual(lines[3], "Assistant: four")
        self.assertIn("2 earlier row(s) are not included", result)

    def test_drops_oldest_first_to_fit_max_tokens(self):
        import handoff
        transcript = [
            row("user", "old " + "a" * 100),
            row("assistant", "middle " + "b" * 100),
            row("user", "recent question"),
        ]
        result = handoff.build_handoff(transcript, None, 3, 60)
        self.assertLessEqual(handoff.estimate_tokens(result), 60)
        self.assertIn("User: recent question", result)
        self.assertNotIn("old aaaa", result)  # oldest dropped first
        self.assertIn("earlier row(s) are not included", result)

    def test_error_and_marker_rows_never_rendered(self):
        import handoff
        transcript = [row("user", "ok"), row("user", "boom", error=True),
                      row("user", "swap", marker=True)]
        result = handoff.build_handoff(transcript, None, 10, 1000)
        self.assertIn("User: ok", result)
        self.assertNotIn("boom", result)
        self.assertNotIn("swap", result)
        # error/marker rows are not conversation rows, so nothing is "omitted"
        self.assertNotIn("earlier row(s)", result)


if __name__ == "__main__":
    unittest.main()
