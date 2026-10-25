"""Tests for chat_sessions.py's session schema v2 (HUB0008 piece 1/11):
SCHEMA_VERSION, _migrate_session() (lossless, idempotent, read-time only) and the
four new _new_session() fields. No test here writes a session file -- the "read does
not modify the file" test asserts exactly that about _read_session().

Run: .venv/bin/python -m unittest test_chat_sessions_schema -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402

FIXTURE = Path(__file__).parent / "test_fixtures" / "chat_sessions" / "legacy_session.json"
V2_KEYS = ("schemaVersion", "account", "swaps", "summary", "chosen_project")
# The 14 keys _new_session() produced on master (before schema v2) -- the fixture's
# exact top-level key set.
MASTER_KEYS = (
    "id", "provider", "model", "effort", "roots", "repoRoot", "instancesDir",
    "claudeSessionId", "transcript", "status", "startedAt", "lastActivityAt",
    "endedAt", "reserved",
)


def _legacy():
    """A FRESH copy of the legacy fixture (the migration mutates the dict in place)."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FixtureShapeTest(unittest.TestCase):
    def test_fixture_is_exactly_the_master_shape(self):
        legacy = _legacy()
        self.assertEqual(set(legacy), set(MASTER_KEYS))
        for k in V2_KEYS:
            self.assertNotIn(k, legacy)
        self.assertEqual(len(legacy["transcript"]), 3)
        self.assertTrue(any("error" in row for row in legacy["transcript"]))
        self.assertTrue(any("templates" in row for row in legacy["transcript"]))


class MigrateSessionTest(unittest.TestCase):
    def test_legacy_migration_adds_defaults_and_keeps_everything(self):
        legacy = _legacy()
        original = {k: v for k, v in legacy.items()}
        out = chat_sessions._migrate_session(legacy)
        self.assertEqual(out["schemaVersion"], chat_sessions.SCHEMA_VERSION)
        self.assertIsNone(out["account"])
        self.assertEqual(out["swaps"], [])
        self.assertIsNone(out["summary"])
        self.assertIsNone(out["chosen_project"])
        # Every original key and value intact; exactly the five fields added, nothing
        # else changed or dropped (transcript untouched, unknown keys would be kept too).
        self.assertEqual(set(out), set(original) | set(V2_KEYS))
        for k, v in original.items():
            self.assertEqual(out[k], v, k)
        self.assertEqual(out["transcript"], original["transcript"])

    def test_migrating_twice_equals_migrating_once(self):
        once = chat_sessions._migrate_session(_legacy())
        twice = chat_sessions._migrate_session(once)
        self.assertEqual(once, twice)

    def test_new_session_has_the_v2_fields_alongside_master_keys(self):
        s = chat_sessions._new_session(["/home/demo/agent-manager"], None,
                                       chat_sessions.PROVIDER_CLAUDE, "opus", "high")
        self.assertEqual(s["schemaVersion"], chat_sessions.SCHEMA_VERSION)
        self.assertIsNone(s["account"])
        self.assertEqual(s["swaps"], [])
        self.assertIsNone(s["summary"])
        self.assertIsNone(s["chosen_project"])
        for k in MASTER_KEYS:
            self.assertIn(k, s)

    def test_v2_session_is_returned_untouched(self):
        session = _legacy()
        session["schemaVersion"] = chat_sessions.SCHEMA_VERSION
        session["account"] = "fake-account"
        session["swaps"] = [{"at": "2026-10-19T00:00:00Z"}]
        session["summary"] = {"upToIndex": 2, "text": "fake summary", "at": "2026-10-19T00:00:00Z"}
        session["extraSentinel"] = "keep-me"
        before = json.loads(json.dumps(session))
        out = chat_sessions._migrate_session(session)
        self.assertEqual(out, before)
        self.assertEqual(out["extraSentinel"], "keep-me")


class ReadSessionMigrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = Path(self._tmp.name)  # already created by TemporaryDirectory()

    def _write_file(self, session):
        p = chat_sessions._session_path(self.storage, session["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return p

    def test_reading_a_legacy_file_gives_the_defaults_with_originals_intact(self):
        legacy = _legacy()
        self._write_file(legacy)
        got = chat_sessions._read_session(self.storage, legacy["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["schemaVersion"], chat_sessions.SCHEMA_VERSION)
        self.assertIsNone(got["account"])
        self.assertEqual(got["swaps"], [])
        self.assertIsNone(got["summary"])
        for k, v in legacy.items():
            self.assertEqual(got[k], v, k)

    def test_reading_does_not_modify_the_file_on_disk(self):
        legacy = _legacy()
        p = self._write_file(legacy)
        before_bytes = p.read_bytes()
        before_mtime = p.stat().st_mtime_ns
        got = chat_sessions._read_session(self.storage, legacy["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["schemaVersion"], chat_sessions.SCHEMA_VERSION)
        # Migration is in memory only: the on-disk file is byte-identical and its
        # mtime is untouched -- the next normal write persists the new shape.
        self.assertEqual(p.read_bytes(), before_bytes)
        self.assertEqual(p.stat().st_mtime_ns, before_mtime)

    def test_reading_a_v2_file_returns_it_untouched(self):
        session = _legacy()
        session["schemaVersion"] = chat_sessions.SCHEMA_VERSION
        session["account"] = None
        session["swaps"] = []
        session["summary"] = None
        session["extraSentinel"] = "keep-me"
        self._write_file(session)
        got = chat_sessions._read_session(self.storage, session["id"])
        self.assertEqual(got, session)
        self.assertEqual(got["extraSentinel"], "keep-me")


if __name__ == "__main__":
    unittest.main()
