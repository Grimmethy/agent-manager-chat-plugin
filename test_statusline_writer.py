import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import statusline_writer as sw

PAYLOAD = {"session_id": "sess-1", "model": {"id": "model-a", "display_name": "Model A"},
           "context_window": {"context_window_size": 1000, "used_percentage": 14.2}}


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "statusline"
        self.addCleanup(shutil.rmtree, self.dir.parent, True)
        self.path = self.dir / "sess-1.json"

    def test_writes_private_file_with_expected_fields(self):
        self.assertTrue(sw.write_snapshot(self.dir, sw.extract(PAYLOAD)))
        data = json.loads(self.path.read_text())
        self.assertEqual({k: data[k] for k in ("sessionId", "contextWindowSize", "usedPercentage", "model")},
                         {"sessionId": "sess-1", "contextWindowSize": 1000, "usedPercentage": 14.2, "model": "model-a"})
        self.assertIsInstance(data["observedAt"], int)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["sess-1.json"])  # no temp files left

    def test_writes_only_when_window_or_model_changed(self):
        sw.write_snapshot(self.dir, sw.extract(PAYLOAD))
        before = self.path.stat()
        moved = json.loads(json.dumps(PAYLOAD))
        moved["context_window"]["used_percentage"] = 55.0  # percentage alone is not a change
        self.assertFalse(sw.write_snapshot(self.dir, sw.extract(moved)))
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        moved["model"]["id"] = "model-b"
        self.assertTrue(sw.write_snapshot(self.dir, sw.extract(moved)))
        self.assertEqual(json.loads(self.path.read_text())["model"], "model-b")
        moved["context_window"]["context_window_size"] = 2000
        self.assertTrue(sw.write_snapshot(self.dir, sw.extract(moved)))
        self.assertEqual(json.loads(self.path.read_text())["contextWindowSize"], 2000)

    def test_bad_input_is_ignored(self):
        for bad in (None, [], {}, {"session_id": "../evil"}, {"session_id": 5},
                    {"session_id": "ok", "context_window": {"context_window_size": "1000"}},
                    {"session_id": "ok", "context_window": {"context_window_size": True}},
                    {"session_id": "ok"}):
            self.assertFalse(sw.write_snapshot(self.dir, sw.extract(bad)), bad)
        self.assertFalse(self.dir.exists())

    def test_status_text(self):
        self.assertEqual(sw.status_text(PAYLOAD), "Model A 14%")
        self.assertEqual(sw.status_text({"model": {"id": "m"}}), "m")
        self.assertEqual(sw.status_text(None), "claude")

    def test_main_never_fails_the_status_line(self):
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(sw.main([str(self.dir)], stdin=io.StringIO("{not json")), 0)
            blocker = Path(self.dir.parent) / "file"
            blocker.write_text("x")  # a directory path that cannot be created
            self.assertEqual(sw.main([str(blocker / "sub")], stdin=io.StringIO(json.dumps(PAYLOAD))), 0)
        finally:
            sys.stdout = old
        self.assertEqual(out.getvalue().splitlines(), ["claude", "Model A 14%"])

    def test_runs_as_a_script(self):
        res = subprocess.run([sys.executable, str(Path(sw.__file__)), str(self.dir)], input=json.dumps(PAYLOAD),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "Model A 14%"))
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
