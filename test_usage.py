"""Tests for usage.read_usage (HUB0007 2/4).

The snapshot on disk is what statusline_writer.write_limits writes: the key is `usedPercentage`,
while read_usage reports it as `percent`. The round-trip test builds its file with the real writer
so the two cannot drift apart again (an earlier draft read the wrong key)."""
import json
import os
import tempfile
import unittest

import statusline_writer as sw
from usage import read_usage

NOW = 1_800_000_000
SIX_HOURS = 6 * 3600


class ReadUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, label, body, raw=None):
        path = os.path.join(self.dir, "limits.%s.json" % label)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(body))
        return path

    def snapshot(self, five=None, weekly=None, observed=NOW):
        body = {"fiveHour": five, "weekly": weekly}
        if observed is not None:
            body["observedAt"] = observed
        return body

    # --- percent and the rollover boundary -------------------------------------------------
    def test_percent_kept_before_resets_at(self):
        self.put("work", self.snapshot(five={"usedPercentage": 42.5, "resetsAt": NOW + 3600}))
        u = read_usage(self.dir, "work", NOW)
        self.assertEqual(u["fiveHour"], {"percent": 42.5, "resetsAt": NOW + 3600, "reset": False})

    def test_one_second_before_resets_at_is_still_kept(self):
        self.put("work", self.snapshot(five={"usedPercentage": 80, "resetsAt": NOW + 1}))
        self.assertEqual(read_usage(self.dir, "work", NOW)["fiveHour"]["percent"], 80)

    def test_reset_after_resets_at(self):
        self.put("work", self.snapshot(five={"usedPercentage": 99, "resetsAt": NOW - 1}))
        self.assertEqual(read_usage(self.dir, "work", NOW)["fiveHour"], {"percent": None, "resetsAt": NOW - 1, "reset": True})

    def test_exactly_at_resets_at_counts_as_reset(self):
        self.put("work", self.snapshot(five={"usedPercentage": 99, "resetsAt": NOW}))
        u = read_usage(self.dir, "work", NOW)["fiveHour"]
        self.assertTrue(u["reset"])
        self.assertIsNone(u["percent"])

    def test_weekly_window_is_read_independently(self):
        self.put("work", self.snapshot(five={"usedPercentage": 10, "resetsAt": NOW - 5},
                                       weekly={"usedPercentage": 70, "resetsAt": NOW + 5}))
        u = read_usage(self.dir, "work", NOW)
        self.assertTrue(u["fiveHour"]["reset"])
        self.assertEqual(u["weekly"], {"percent": 70, "resetsAt": NOW + 5, "reset": False})

    # --- missing, corrupt and unusable input -----------------------------------------------
    def test_missing_file_is_none(self):
        self.assertIsNone(read_usage(self.dir, "nobody", NOW))

    def test_corrupt_file_is_none(self):
        self.put("bad", None, raw="{not json")
        self.assertIsNone(read_usage(self.dir, "bad", NOW))

    def test_json_that_is_not_an_object_is_none(self):
        self.put("arr", None, raw="[1, 2]")
        self.assertIsNone(read_usage(self.dir, "arr", NOW))

    def test_invalid_labels_are_none_and_cannot_reach_other_files(self):
        os.makedirs(os.path.join(self.dir, "limits.x"))
        with open(os.path.join(self.dir, "limits.x", "bar.json"), "w", encoding="utf-8") as fh:
            json.dump(self.snapshot(), fh)
        for label in ("x/bar", "../etc/x", "", None, 7):
            self.assertIsNone(read_usage(self.dir, label, NOW), repr(label))

    def test_missing_windows_and_non_numeric_values_give_none_values(self):
        self.put("empty", self.snapshot())
        e = read_usage(self.dir, "empty", NOW)
        none_window = {"percent": None, "resetsAt": None, "reset": False}
        self.assertEqual((e["fiveHour"], e["weekly"]), (none_window, none_window))
        self.put("odd", {"fiveHour": {"usedPercentage": True, "resetsAt": "soon"}, "observedAt": NOW})
        o = read_usage(self.dir, "odd", NOW)
        self.assertEqual(o["fiveHour"], none_window)

    # --- stale flag ------------------------------------------------------------------------
    def test_stale_flag_true_when_older_than_six_hours(self):
        self.put("old", self.snapshot(observed=NOW - SIX_HOURS - 1))
        self.assertTrue(read_usage(self.dir, "old", NOW)["stale"])

    def test_stale_flag_false_when_fresh_and_at_exactly_six_hours(self):
        self.put("fresh", self.snapshot(observed=NOW - 60))
        self.assertFalse(read_usage(self.dir, "fresh", NOW)["stale"])
        self.put("edge", self.snapshot(observed=NOW - SIX_HOURS))
        self.assertFalse(read_usage(self.dir, "edge", NOW)["stale"])

    def test_missing_observed_at_counts_as_stale(self):
        self.put("noobs", self.snapshot(observed=None))
        self.assertTrue(read_usage(self.dir, "noobs", NOW)["stale"])

    def test_result_has_only_the_documented_keys(self):
        self.put("work", self.snapshot(five={"usedPercentage": 1, "resetsAt": NOW + 9}))
        u = read_usage(self.dir, "work", NOW)
        self.assertEqual(set(u), {"fiveHour", "weekly", "stale"})
        self.assertEqual(set(u["fiveHour"]), {"percent", "resetsAt", "reset"})

    # --- round trip through the real writer ------------------------------------------------
    def test_round_trip_through_the_real_snapshot_writer(self):
        payload = {"rate_limits": {"five_hour": {"used_percentage": 42.5, "resets_at": NOW + 3600},
                                   "seven_day": {"used_percentage": 10, "resets_at": NOW + 86400}}}
        limits = sw.extract_limits(payload)
        sw.write_limits(self.dir, "work", limits)
        with open(os.path.join(self.dir, "limits.work.json"), encoding="utf-8") as fh:
            on_disk = json.load(fh)
        written = on_disk["fiveHour"]["usedPercentage"]
        self.assertEqual(written, 42.5)
        self.assertNotIn("percent", on_disk["fiveHour"])
        u = read_usage(self.dir, "work", on_disk["observedAt"])
        self.assertEqual(u["fiveHour"]["percent"], written)
        self.assertEqual(u["weekly"]["percent"], 10)
        self.assertFalse(u["stale"])


if __name__ == "__main__":
    unittest.main()
