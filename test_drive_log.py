import shutil
import tempfile
import unittest
from pathlib import Path

import drive_log as dl

FRM = "alice"
TO = "bob"
TEXT = "hello there"


class DriveLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "drive"
        self.addCleanup(shutil.rmtree, self.dir.parent, True)
        self.path = self.dir / dl.LOG_FILENAME

    def test_append_tail_roundtrip(self):
        record = dl.append(self.dir, FRM, TO, TEXT)
        self.assertEqual(record["text"], TEXT)
        entries = dl.tail(self.dir)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["from"], FRM)
        self.assertEqual(entries[0]["to"], TO)
        self.assertEqual(entries[0]["text"], TEXT)
        self.assertIn("time", entries[0])

    def test_truncates_text_to_200_without_ellipsis(self):
        long_text = "x" * 250
        record = dl.append(self.dir, FRM, TO, long_text)
        self.assertEqual(len(record["text"]), 200)
        self.assertEqual(record["text"], "x" * 200)
        self.assertFalse(record["text"].endswith("\u2026"))
        self.assertFalse(record["text"].endswith("..."))
        stored = dl.tail(self.dir)[0]["text"]
        self.assertEqual(len(stored), 200)
        self.assertEqual(stored, "x" * 200)

    def test_pair_filter_matches_from_direction(self):
        dl.append(self.dir, "A", "B", "a-to-b")
        dl.append(self.dir, "C", "D", "unrelated")
        entries = dl.tail(self.dir, pair="A")
        self.assertEqual([e["text"] for e in entries], ["a-to-b"])
        self.assertEqual([e["text"] for e in dl.tail(self.dir, pair="D")], ["unrelated"])

    def test_pair_filter_matches_to_direction(self):
        dl.append(self.dir, "B", "A", "b-to-a")
        dl.append(self.dir, "C", "D", "unrelated")
        entries = dl.tail(self.dir, pair="A")
        self.assertEqual([e["text"] for e in entries], ["b-to-a"])
        self.assertEqual([e["text"] for e in dl.tail(self.dir, pair="C")], ["unrelated"])

    def test_malformed_line_is_skipped(self):
        dl.append(self.dir, FRM, TO, "good-first")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
            f.write("\n")
        dl.append(self.dir, FRM, TO, "good-second")
        entries = dl.tail(self.dir)
        self.assertEqual([e["text"] for e in entries], ["good-first", "good-second"])

    def test_missing_file_returns_empty_list(self):
        fresh = self.dir / "empty"
        self.assertEqual(dl.tail(fresh), [])
        self.assertEqual(dl.tail(fresh, n=5, pair="A"), [])

    def test_creates_private_file_and_dir(self):
        dl.append(self.dir, FRM, TO, TEXT)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)

    def test_tail_returns_last_n_in_append_order(self):
        for i in range(5):
            dl.append(self.dir, FRM, TO, "msg-%d" % i)
        entries = dl.tail(self.dir, n=3)
        self.assertEqual([e["text"] for e in entries], ["msg-2", "msg-3", "msg-4"])
        entries = dl.tail(self.dir)  # default n=50 returns all
        self.assertEqual([e["text"] for e in entries], ["msg-%d" % i for i in range(5)])


if __name__ == "__main__":
    unittest.main()
