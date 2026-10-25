"""Tests for limit_errors.py (HUB0008 8/11): the ASSUMED-phrase classifier and the
0600 JSONL sample recorder. No tokens or credentials anywhere -- only error text."""
import json
import os
import tempfile
import unittest

import limit_errors


def _lines(storage_dir):
    path = os.path.join(storage_dir, limit_errors.SAMPLES_FILE)
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class ClassifyTest(unittest.TestCase):
    def test_assumed_usage_limit_phrases(self):
        for text in ("You have hit your usage limit",
                     "usage limit reached, resets tomorrow",
                     "Your plan's LIMIT REACHED -- try again in 1h",
                     "Usage Limit: 2026-10-20T00:00:00Z"):
            self.assertEqual(limit_errors.classify(text), {"kind": "usage_limit"})

    def test_assumed_rate_limit_phrases(self):
        for text in ("HTTP 429 Too Many Requests",
                     "rate limit exceeded for this key",
                     "error code: rate_limit"):
            self.assertEqual(limit_errors.classify(text), {"kind": "rate_limit"})

    def test_case_insensitive(self):
        self.assertEqual(limit_errors.classify("USAGE LIMIT REACHED"), {"kind": "usage_limit"})
        self.assertEqual(limit_errors.classify("429 / RATE LIMIT"), {"kind": "rate_limit"})

    def test_unrelated_errors_are_none(self):
        for text in ("connection refused by the local model",
                     "no such file: out.json",
                     "disk full",
                     "",
                     None):
            self.assertEqual(limit_errors.classify(text)["kind"], None)


class RecordSampleTest(unittest.TestCase):
    def test_one_line_per_call_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            limit_errors.record_sample(tmp, "usage limit reached")
            limit_errors.record_sample(tmp, "429 rate limit")
            path = os.path.join(tmp, limit_errors.SAMPLES_FILE)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            lines = _lines(tmp)
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertIn("at", line)
                self.assertIn("text", line)
            self.assertEqual(lines[0]["text"], "usage limit reached")
            self.assertEqual(lines[1]["text"], "429 rate limit")

    def test_text_truncated_to_500_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            long_text = "x" * 900
            limit_errors.record_sample(tmp, long_text)
            lines = _lines(tmp)
            self.assertEqual(len(lines), 1)
            self.assertEqual(len(lines[0]["text"]), 500)


if __name__ == "__main__":
    unittest.main()
