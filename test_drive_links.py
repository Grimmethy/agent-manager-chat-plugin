import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import drive_links


class DriveLinksTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_create_and_persist_with_0600(self):
        link = drive_links.create(self.dir, "chat-alpha", "chat-beta")
        p = os.path.join(self.dir, "drive-links.json")
        self.assertTrue(os.path.exists(p))
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(link["driver"], "chat-alpha")
        self.assertEqual(link["target"], "chat-beta")
        self.assertEqual(link["turnBudget"], 10)
        self.assertEqual(link["timeBudgetMin"], 60)
        self.assertEqual(link["turnsUsed"], 0)
        self.assertTrue(link["active"])
        self.assertIsNone(link["stoppedAt"])
        self.assertIsNone(link["stopReason"])
        self.assertRegex(link["id"], r"^drive-\d+-[0-9a-f]{8}$")
        datetime.fromisoformat(link["createdAt"])
        self.assertEqual(drive_links.get(self.dir, link["id"]), link)

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            drive_links.create(self.dir, "chat-a", "chat-a")
        with self.assertRaises(ValueError):
            drive_links.create(self.dir, "a b", "chat-b")
        with self.assertRaises(ValueError):
            drive_links.create(self.dir, "chat-a", "chat-b", turn_budget=0)
        with self.assertRaises(ValueError):
            drive_links.create(self.dir, "chat-a", "chat-b", time_budget_min=0)
        self.assertEqual(drive_links.list_links(self.dir), [])

    def test_consume_increments_turns_used(self):
        link = drive_links.create(self.dir, "chat-a", "chat-b")
        r1 = drive_links.consume(self.dir, link["id"])
        self.assertTrue(r1["ok"])
        self.assertIsNone(r1["reason"])
        self.assertEqual(r1["link"]["turnsUsed"], 1)
        r2 = drive_links.consume(self.dir, link["id"])
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["link"]["turnsUsed"], 2)

    def test_eleventh_consume_refused_with_budget(self):
        link = drive_links.create(self.dir, "chat-a", "chat-b")
        for _ in range(10):
            r = drive_links.consume(self.dir, link["id"])
            self.assertTrue(r["ok"])
        self.assertEqual(drive_links.get(self.dir, link["id"])["turnsUsed"], 10)
        r11 = drive_links.consume(self.dir, link["id"])
        self.assertFalse(r11["ok"])
        self.assertEqual(r11["reason"], "budget")
        self.assertFalse(r11["link"]["active"])
        self.assertEqual(r11["link"]["stopReason"], "budget")
        stored = drive_links.get(self.dir, link["id"])
        self.assertFalse(stored["active"])

    def test_time_budget_stops_with_time(self):
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        link = drive_links.create(self.dir, "chat-a", "chat-b", time_budget_min=5, now=created)
        late = created + timedelta(minutes=6)
        r = drive_links.consume(self.dir, link["id"], now=late)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "time")
        self.assertFalse(r["link"]["active"])
        self.assertEqual(r["link"]["stopReason"], "time")
        early = created + timedelta(minutes=1)
        self.assertIsNone(drive_links.get(self.dir, "drive-x"))

    def test_stop_is_idempotent_and_later_consume_stopped(self):
        link = drive_links.create(self.dir, "chat-a", "chat-b")
        s1 = drive_links.stop(self.dir, link["id"])
        self.assertFalse(s1["active"])
        self.assertEqual(s1["stopReason"], "user")
        s2 = drive_links.stop(self.dir, link["id"], reason="user")
        self.assertFalse(s2["active"])
        r = drive_links.consume(self.dir, link["id"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "stopped")

    def test_stop_unknown_id_returns_none(self):
        self.assertIsNone(drive_links.stop(self.dir, "drive-0000000000-00000000"))

    def test_unknown_id_on_consume(self):
        r = drive_links.consume(self.dir, "drive-0000000000-00000000")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "unknown")
        self.assertIsNone(r["link"])

    def test_list_links_active_only(self):
        a = drive_links.create(self.dir, "chat-a", "chat-b")
        b = drive_links.create(self.dir, "chat-b", "chat-c")
        drive_links.stop(self.dir, a["id"])
        active = drive_links.list_links(self.dir, active_only=True)
        self.assertEqual([l["id"] for l in active], [b["id"]])
        self.assertEqual(len(drive_links.list_links(self.dir)), 2)

    def test_corrupt_file_reads_as_no_links(self):
        with open(os.path.join(self.dir, "drive-links.json"), "w") as f:
            f.write("{{{not json")
        self.assertEqual(drive_links.list_links(self.dir), [])
        self.assertIsNone(drive_links.get(self.dir, "drive-0000000000-00000000"))

    def test_missing_file_reads_as_no_links(self):
        self.assertEqual(drive_links.list_links(self.dir), [])
        self.assertIsNone(drive_links.get(self.dir, "nope"))


if __name__ == "__main__":
    unittest.main()
