import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import statusline_writer as sw

PAYLOAD = {"session_id": "sess-1", "model": {"id": "model-a", "display_name": "Model A"},
           "context_window": {"context_window_size": 1000, "used_percentage": 14.2}}

# Fake fixture only (no real tokens/credentials): the ASSUMED rate_limits shape.
LIMITS_PAYLOAD = {"session_id": "sess-1", "model": {"id": "model-a", "display_name": "Model A"},
                  "rate_limits": {"five_hour": {"used_percentage": 42.5, "resets_at": 1700000000},
                                  "seven_day": {"used_percentage": 17, "resets_at": 1700000000}}}


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

    # ---- usage limits (piece 3/7) ----

    def test_extract_limits_assumed_shape(self):
        self.assertEqual(sw.extract_limits(LIMITS_PAYLOAD),
                         {"fiveHour": {"usedPercentage": 42.5, "resetsAt": 1700000000},
                          "weekly": {"usedPercentage": 17, "resetsAt": 1700000000}})

    def test_extract_limits_iso_resets_at(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": "2025-01-15T08:00:00Z"},
                                   "seven_day": {"used_percentage": 2, "resets_at": "2025-01-16T00:00:00+02:00"}}}
        limits = sw.extract_limits(payload)
        self.assertEqual(limits["fiveHour"]["resetsAt"],
                         int(datetime.fromisoformat("2025-01-15T08:00:00+00:00").timestamp()))
        self.assertEqual(limits["weekly"]["resetsAt"],
                         int(datetime.fromisoformat("2025-01-16T00:00:00+02:00").timestamp()))
        self.assertIsInstance(limits["fiveHour"]["resetsAt"], int)

    def test_extract_limits_missing_rate_limits(self):
        for payload in ({}, {"session_id": "abc"}, {"rate_limits": "nope"}, {"rate_limits": None},
                        {"rate_limits": []}, None, []):
            self.assertIsNone(sw.extract_limits(payload), payload)

    def test_extract_limits_null_per_model_caps(self):
        limits = sw.extract_limits({"rate_limits": {"five_hour": None,
                                                    "seven_day": {"used_percentage": 5, "resets_at": 1}}})
        self.assertIsNone(limits["fiveHour"])
        self.assertEqual(limits["weekly"], {"usedPercentage": 5, "resetsAt": 1})

    def test_extract_limits_non_numeric_percentage(self):
        limits = sw.extract_limits({"rate_limits": {"five_hour": {"used_percentage": "high",
                                                                  "resets_at": "not a date"}}})
        self.assertEqual(limits["fiveHour"], {"usedPercentage": None, "resetsAt": None})

    def test_write_limits_label_validation(self):
        limits = sw.extract_limits(LIMITS_PAYLOAD)
        for bad in ("", "bad label!", "a" * 41, "x/../y", 123, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                sw.write_limits(self.dir, bad, limits)
        label = "a" * 40  # maximum allowed length is accepted
        path = Path(sw.write_limits(self.dir, label, limits))
        data = json.loads(path.read_text())
        self.assertEqual(set(data), {"fiveHour", "weekly", "observedAt"})
        self.assertEqual(data["fiveHour"], {"usedPercentage": 42.5, "resetsAt": 1700000000})
        self.assertEqual(data["weekly"], {"usedPercentage": 17, "resetsAt": 1700000000})
        self.assertIsInstance(data["observedAt"], int)

    def test_raw_sample_written_once_and_not_overwritten(self):
        first = {"five_hour": {"used_percentage": 1, "resets_at": 10}}
        second = {"five_hour": {"used_percentage": 99}, "seven_day": None}
        path = Path(sw._write_raw_sample_once(self.dir, first))
        before = path.read_text()
        sw._write_raw_sample_once(self.dir, second)  # never overwrites
        self.assertEqual(path.read_text(), before)
        self.assertEqual(json.loads(before), first)

    def test_limits_and_sample_file_modes(self):
        limits = sw.extract_limits(LIMITS_PAYLOAD)
        paths = (Path(sw.write_limits(self.dir, "acc-1", limits)),
                 Path(sw._write_raw_sample_once(self.dir, {"five_hour": None})))
        for p in paths:
            self.assertEqual(p.stat().st_mode & 0o777, 0o600, p)

    def test_main_without_label_writes_sample_but_no_limits_file(self):
        env = os.environ.pop("AM_ACCOUNT_LABEL", None)
        try:
            out = io.StringIO()
            old, sys.stdout = sys.stdout, out
            try:
                self.assertEqual(sw.main([str(self.dir)], stdin=io.StringIO(json.dumps(LIMITS_PAYLOAD))), 0)
            finally:
                sys.stdout = old
        finally:
            if env is not None:
                os.environ["AM_ACCOUNT_LABEL"] = env
        self.assertEqual(out.getvalue().splitlines(), ["Model A"])
        self.assertEqual([p.name for p in self.dir.iterdir()], ["rate_limits.sample.json"])  # no limits.*.json
        self.assertEqual(json.loads((self.dir / "rate_limits.sample.json").read_text()), LIMITS_PAYLOAD["rate_limits"])

    def test_main_with_label_writes_limits_file_and_never_fails(self):
        env = os.environ.get("AM_ACCOUNT_LABEL")
        try:
            out = io.StringIO()
            old, sys.stdout = sys.stdout, out
            try:
                os.environ["AM_ACCOUNT_LABEL"] = "acc-1"
                self.assertEqual(sw.main([str(self.dir)], stdin=io.StringIO(json.dumps(LIMITS_PAYLOAD))), 0)
                os.environ["AM_ACCOUNT_LABEL"] = "bad label!"  # invalid label: still never fails
                self.assertEqual(sw.main([str(self.dir)], stdin=io.StringIO(json.dumps(LIMITS_PAYLOAD))), 0)
            finally:
                sys.stdout = old
        finally:
            if env is None:
                os.environ.pop("AM_ACCOUNT_LABEL", None)
            else:
                os.environ["AM_ACCOUNT_LABEL"] = env
        self.assertEqual(out.getvalue().splitlines(), ["Model A", "Model A"])
        data = json.loads((self.dir / "limits.acc-1.json").read_text())
        self.assertEqual(data["fiveHour"], {"usedPercentage": 42.5, "resetsAt": 1700000000})
        self.assertFalse((self.dir / "limits.bad label!.json").exists())
        self.assertTrue((self.dir / "rate_limits.sample.json").exists())

    def test_main_without_rate_limits_writes_no_sample(self):
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(sw.main([str(self.dir)], stdin=io.StringIO(json.dumps(PAYLOAD))), 0)
        finally:
            sys.stdout = old
        self.assertEqual(out.getvalue().splitlines(), ["Model A 14%"])
        self.assertFalse((self.dir / "rate_limits.sample.json").exists())
        self.assertFalse(any(p.name.startswith("limits.") for p in self.dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
