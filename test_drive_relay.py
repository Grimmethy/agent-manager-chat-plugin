import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import drive_links
import drive_log
import drive_relay

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_link(turn_budget=3, time_budget_min=60, now=BASE):
    d = tempfile.mkdtemp()
    link = drive_links.create(
        d, "driver-1", "target-1",
        turn_budget=turn_budget, time_budget_min=time_budget_min, now=now,
    )
    return d, link["id"]


class DriveRelayTest(unittest.TestCase):
    def test_stops_at_turn_budget(self):
        d, lid = make_link(turn_budget=3, time_budget_min=100000)
        calls = []
        def send(sid, text, driven_by):
            calls.append((sid, text, driven_by))
            return "r%d" % len(calls)
        out = drive_relay.run(d, lid, "hello", send, now=lambda: BASE)
        self.assertEqual(out["turns"], 3)
        self.assertEqual(out["stoppedBy"], "budget")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(drive_log.tail(d)), 3)

    def test_stops_on_time_budget(self):
        d, lid = make_link(turn_budget=100, time_budget_min=10, now=BASE)
        def send(sid, text, driven_by):
            self.fail("send must not be called after the time budget trips")
        out = drive_relay.run(d, lid, "hello", send, now=lambda: BASE + timedelta(minutes=11))
        self.assertEqual(out["stoppedBy"], "time")
        self.assertEqual(out["turns"], 0)

    def test_stop_pressed_mid_run(self):
        d, lid = make_link(turn_budget=100, time_budget_min=100000)
        n = {"c": 0}
        def send(sid, text, driven_by):
            n["c"] += 1
            if n["c"] == 2:
                drive_links.stop(d, lid)
            return "ok"
        out = drive_relay.run(d, lid, "hello", send, now=lambda: BASE)
        self.assertEqual(out["stoppedBy"], "stopped")
        self.assertEqual(out["turns"], 2)
        self.assertFalse(drive_links.get(d, lid)["active"])

    def test_roles_alternate(self):
        d, lid = make_link(turn_budget=3, time_budget_min=100000)
        def send(sid, text, driven_by):
            return "ok"
        drive_relay.run(d, lid, "hello", send, now=lambda: BASE)
        recs = drive_log.tail(d, 10)
        self.assertEqual(len(recs), 3)
        self.assertEqual((recs[0]["from"], recs[0]["to"]), ("driver-1", "target-1"))
        self.assertEqual((recs[1]["from"], recs[1]["to"]), ("target-1", "driver-1"))
        self.assertEqual((recs[2]["from"], recs[2]["to"]), ("driver-1", "target-1"))

    def test_empty_reply(self):
        d, lid = make_link(turn_budget=100, time_budget_min=100000)
        seq = ["first", ""]
        def send(sid, text, driven_by):
            return seq.pop(0)
        out = drive_relay.run(d, lid, "hello", send, now=lambda: BASE)
        self.assertEqual(out["stoppedBy"], "empty")
        self.assertEqual(out["turns"], 1)

    def test_send_raises(self):
        d, lid = make_link(turn_budget=100, time_budget_min=100000)
        def send(sid, text, driven_by):
            raise RuntimeError("boom")
        out = drive_relay.run(d, lid, "hello", send, now=lambda: BASE)  # not re-raised
        self.assertEqual(out["stoppedBy"], "error")
        self.assertEqual(out["turns"], 0)
        self.assertFalse(drive_links.get(d, lid)["active"])


if __name__ == "__main__":
    unittest.main()
