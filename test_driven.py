"""Tests for driven.admit() -- the gate that admits one driven turn against its drive
link -- and for chat_sessions.py's drivenBy transcript tagging (a driven turn's user
row carries 'drivenBy': <driver id>; a normal turn's row keeps the prior shape with no
drivenBy key).

All token/link strings are fake literals only; every test runs against a temp
storage_dir (tempfile.TemporaryDirectory), the same convention test_chat_sessions.py
and test_drive_links.py already use.

Run: .venv/bin/python -m unittest test_driven -v
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import driven  # noqa: E402
import drive_links  # noqa: E402
import chat_sessions  # noqa: E402
import app  # noqa: E402

DRIVER = "test-driver-1"
TARGET = "test-target-1"
WRONG_DRIVER = "test-wrong-driver-1"
WRONG_TARGET = "test-wrong-target-1"
UNKNOWN_LINK_ID = "drive-0000000000-deadbeef"


def _session():
    # Mirrors test_chat_sessions._session() exactly.
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


class AdmitTest(unittest.TestCase):
    """driven.admit against a temp storage_dir: ok / mismatch / stopped / budget /
    time / unknown, with fake driver/target ids only."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.addCleanup(self._td.cleanup)

    def _create_link(self, **kwargs):
        return drive_links.create(self.dir, DRIVER, TARGET, **kwargs)

    def test_admit_ok_returns_ok_and_increments_turns_used(self):
        link = self._create_link()
        result = driven.admit(self.dir, link["id"], DRIVER, TARGET)
        self.assertEqual(result, {"ok": True, "reason": None})
        stored = drive_links.get(self.dir, link["id"])
        self.assertEqual(stored["turnsUsed"], 1)
        self.assertTrue(stored["active"])

    def test_admit_wrong_driver_is_mismatch(self):
        link = self._create_link()
        result = driven.admit(self.dir, link["id"], WRONG_DRIVER, TARGET)
        self.assertEqual(result, {"ok": False, "reason": "mismatch"})
        self.assertEqual(drive_links.get(self.dir, link["id"])["turnsUsed"], 0)

    def test_admit_wrong_target_is_mismatch(self):
        link = self._create_link()
        result = driven.admit(self.dir, link["id"], DRIVER, WRONG_TARGET)
        self.assertEqual(result, {"ok": False, "reason": "mismatch"})
        self.assertEqual(drive_links.get(self.dir, link["id"])["turnsUsed"], 0)

    def test_admit_not_active_is_mismatch(self):
        link = self._create_link()
        drive_links.stop(self.dir, link["id"], reason="user")
        result = driven.admit(self.dir, link["id"], DRIVER, TARGET)
        self.assertEqual(result, {"ok": False, "reason": "mismatch"})

    def test_admit_stopped_link_is_refused(self):
        # A stopped link is not active, so driven.py's identity/active guard reports
        # it as "mismatch" (not-active is one of the mismatch causes); the point this
        # test guards is that no stopped link is ever admitted.
        link = self._create_link()
        stopped = drive_links.stop(self.dir, link["id"], reason="user")
        self.assertFalse(stopped["active"])
        result = driven.admit(self.dir, link["id"], DRIVER, TARGET)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "mismatch")

    def test_admit_budget_exhaustion_is_refused_with_budget(self):
        link = self._create_link(turn_budget=1)
        first = driven.admit(self.dir, link["id"], DRIVER, TARGET)
        self.assertEqual(first, {"ok": True, "reason": None})
        second = driven.admit(self.dir, link["id"], DRIVER, TARGET)
        self.assertEqual(second, {"ok": False, "reason": "budget"})
        stored = drive_links.get(self.dir, link["id"])
        self.assertFalse(stored["active"])
        self.assertEqual(stored["stopReason"], "budget")

    def test_admit_time_exhaustion_is_refused_with_time(self):
        created = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        link = self._create_link(time_budget_min=5, now=created)
        early = created + timedelta(minutes=1)
        self.assertEqual(driven.admit(self.dir, link["id"], DRIVER, TARGET, now=early),
                         {"ok": True, "reason": None})
        late = created + timedelta(minutes=6)
        self.assertEqual(driven.admit(self.dir, link["id"], DRIVER, TARGET, now=late),
                         {"ok": False, "reason": "time"})
        stored = drive_links.get(self.dir, link["id"])
        self.assertFalse(stored["active"])
        self.assertEqual(stored["stopReason"], "time")

    def test_admit_unknown_link_id_is_refused_with_unknown(self):
        result = driven.admit(self.dir, UNKNOWN_LINK_ID, DRIVER, TARGET)
        self.assertEqual(result, {"ok": False, "reason": "unknown"})


class DrivenByTranscriptRowTest(unittest.TestCase):
    """A driven turn's stored user row carries 'drivenBy': <driver id>; a normal
    turn's user row has NO drivenBy key (equals the prior row shape). Reuses the
    _session / _seed_active_session / fake-stream conventions from
    test_chat_sessions.py."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _seed_active_session(self):
        session = _session()
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        return session["id"]

    def _last_user_row(self, session_id):
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        return stored["transcript"][-2]  # [user, assistant placeholder] pair

    def test_driven_turn_user_row_carries_driven_by(self):
        session_id = self._seed_active_session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools",
                               _fake_stream([{"type": "final", "response": "ok",
                                              "turnsUsed": 1, "forcedSummary": False}])):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi",
                                              driven_by=DRIVER))
        row = self._last_user_row(session_id)
        self.assertEqual(row["role"], "user")
        self.assertEqual(row["drivenBy"], DRIVER)

    def test_normal_turn_user_row_has_no_driven_by_key(self):
        session_id = self._seed_active_session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools",
                               _fake_stream([{"type": "final", "response": "pong",
                                              "turnsUsed": 1, "forcedSummary": False}])):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))
        row = self._last_user_row(session_id)
        self.assertNotIn("drivenBy", row)
        # prior shape: role + text + timestamp, nothing more
        self.assertEqual(set(row.keys()), {"role", "text", "timestamp"})
        self.assertEqual(row["text"], "hi")


if __name__ == "__main__":
    unittest.main()
