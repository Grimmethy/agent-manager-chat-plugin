"""Tests for chat_sessions.py's per-session storage and multi-session lifecycle
(2026-10-18, multi-chat workspace Phase 1): one file per session, written atomically;
one-time migration of the legacy single-file store; two sessions streaming at once
without losing each other's transcript; "New" adding a chat instead of ending one;
get_active_session = most recently used; end_session; list_sessions.

Run: .venv/bin/python -m unittest test_session_storage -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402

ROOTS = [str(app.CORE_REPO_ROOT)]


def _session(sid, *, status="active", started="2026-10-18T10:00:00Z", last=None, transcript=None, provider="local"):
    return {
        "id": sid, "provider": provider, "model": None, "effort": None,
        "roots": ROOTS, "repoRoot": ROOTS[0], "instancesDir": None, "claudeSessionId": None,
        "transcript": transcript or [], "status": status, "startedAt": started,
        "lastActivityAt": last, "endedAt": None, "reserved": False,
    }


class StorageTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Path(self.tmp.name)

    def _files(self):
        d = chat_sessions.sessions_dir(self.storage)
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


class PerSessionFilesTest(StorageTestBase):
    def test_each_session_is_its_own_file_and_no_temp_files_remain(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa"))
        chat_sessions._write_session(self.storage, _session("chat-2-bbbbbbbb"))
        self.assertEqual(self._files(), ["chat-1-aaaaaaaa.json", "chat-2-bbbbbbbb.json"])
        stored = json.loads((chat_sessions.sessions_dir(self.storage) / "chat-1-aaaaaaaa.json").read_text())
        self.assertEqual(stored["id"], "chat-1-aaaaaaaa")
        self.assertFalse(chat_sessions.chat_sessions_path(self.storage).exists())

    def test_write_sessions_only_touches_the_sessions_it_is_given(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa", transcript=[{"role": "user", "text": "keep me"}]))
        chat_sessions._write_sessions(self.storage, {"chat-2-bbbbbbbb": _session("chat-2-bbbbbbbb")})
        self.assertEqual(chat_sessions.get_session(self.storage, "chat-1-aaaaaaaa")["transcript"][0]["text"], "keep me")

    def test_ids_that_could_escape_the_directory_are_refused(self):
        with self.assertRaises(ValueError):
            chat_sessions._write_session(self.storage, _session("../evil"))
        self.assertIsNone(chat_sessions.get_session(self.storage, "../evil"))


class LegacyMigrationTest(StorageTestBase):
    def _write_legacy(self, sessions):
        chat_sessions.chat_sessions_path(self.storage).write_text(json.dumps(sessions), encoding="utf-8")

    def test_legacy_store_is_split_and_renamed_never_deleted(self):
        self._write_legacy({"chat-1-aaaaaaaa": _session("chat-1-aaaaaaaa"),
                            "chat-2-bbbbbbbb": _session("chat-2-bbbbbbbb", status="ended")})
        sessions = chat_sessions._read_sessions(self.storage)
        self.assertEqual(sorted(sessions), ["chat-1-aaaaaaaa", "chat-2-bbbbbbbb"])
        self.assertEqual(self._files(), ["chat-1-aaaaaaaa.json", "chat-2-bbbbbbbb.json"])
        self.assertFalse(chat_sessions.chat_sessions_path(self.storage).exists())
        migrated = self.storage / ".agent-manager-chat-sessions.json.migrated"
        self.assertTrue(migrated.exists())
        self.assertEqual(sorted(json.loads(migrated.read_text())), ["chat-1-aaaaaaaa", "chat-2-bbbbbbbb"])

    def test_second_access_is_a_no_op(self):
        self._write_legacy({"chat-1-aaaaaaaa": _session("chat-1-aaaaaaaa")})
        chat_sessions._read_sessions(self.storage)
        before = self._files()
        chat_sessions._read_sessions(self.storage)
        self.assertEqual(self._files(), before)

    def test_an_existing_per_session_file_is_not_overwritten(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa", transcript=[{"role": "user", "text": "newer"}]))
        self._write_legacy({"chat-1-aaaaaaaa": _session("chat-1-aaaaaaaa", transcript=[{"role": "user", "text": "older"}])})
        self.assertEqual(chat_sessions.get_session(self.storage, "chat-1-aaaaaaaa")["transcript"][0]["text"], "newer")

    def test_single_session_reads_also_migrate(self):
        self._write_legacy({"chat-1-aaaaaaaa": _session("chat-1-aaaaaaaa")})
        self.assertEqual(chat_sessions.get_session(self.storage, "chat-1-aaaaaaaa")["id"], "chat-1-aaaaaaaa")


class ConcurrentStreamsTest(StorageTestBase):
    """The bug per-session storage fixes: with one shared file, each stream_message held
    a stale copy of EVERY session and rewrote it on every chunk, so whichever turn wrote
    last erased the other session's turn."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_sessions_streaming_interleaved_both_keep_their_transcripts(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa"))
        chat_sessions._write_session(self.storage, _session("chat-2-bbbbbbbb"))

        def fake_stream(**kwargs):
            reply = f"reply for {kwargs['context_log_session_id']}"
            for ch in reply:
                yield {"type": "chunk", "text": ch}
            yield {"type": "final", "response": reply, "turnsUsed": 1, "forcedSummary": False}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            a = chat_sessions.stream_message(self.storage, "chat-1-aaaaaaaa", "question A")
            b = chat_sessions.stream_message(self.storage, "chat-2-bbbbbbbb", "question B")
            done = set()
            while len(done) < 2:  # strictly alternate one event from each until both finish
                for name, gen in (("a", a), ("b", b)):
                    if name in done:
                        continue
                    try:
                        next(gen)
                    except StopIteration:
                        done.add(name)

        for sid, question in (("chat-1-aaaaaaaa", "question A"), ("chat-2-bbbbbbbb", "question B")):
            transcript = chat_sessions.get_session(self.storage, sid)["transcript"]
            self.assertEqual([t["role"] for t in transcript], ["user", "assistant"])
            self.assertEqual(transcript[0]["text"], question)
            self.assertEqual(transcript[1]["text"], f"reply for {sid}")

    def test_busy_flag_is_set_while_streaming_and_cleared_after(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa"))

        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": "x"}
            yield {"type": "final", "response": "x", "turnsUsed": 1, "forcedSummary": False}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            gen = chat_sessions.stream_message(self.storage, "chat-1-aaaaaaaa", "hi")
            next(gen)
            self.assertTrue(chat_sessions.list_sessions(self.storage)[0]["busy"])
            list(gen)
        self.assertFalse(chat_sessions.list_sessions(self.storage)[0]["busy"])

    def test_busy_flag_is_cleared_when_the_turn_raises(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa"))

        def failing_stream(**kwargs):
            raise RuntimeError("boom")
            yield  # pragma: no cover

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", failing_stream):
            with self.assertRaises(RuntimeError):
                list(chat_sessions.stream_message(self.storage, "chat-1-aaaaaaaa", "hi"))
        self.assertFalse(chat_sessions.list_sessions(self.storage)[0]["busy"])


class LifecycleTest(StorageTestBase):
    def test_new_conversation_leaves_the_previous_one_active(self):
        first = chat_sessions.start_new_conversation(self.storage, ROOTS)
        second = chat_sessions.start_new_conversation(self.storage, ROOTS)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(chat_sessions.get_session(self.storage, first["id"])["status"], "active")
        self.assertEqual(chat_sessions.get_session(self.storage, second["id"])["status"], "active")

    def test_get_active_session_returns_the_most_recently_used(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa", started="2026-10-18T09:00:00Z", last="2026-10-18T12:00:00Z"))
        chat_sessions._write_session(self.storage, _session("chat-2-bbbbbbbb", started="2026-10-18T11:00:00Z"))
        chat_sessions._write_session(self.storage, _session("chat-3-cccccccc", started="2026-10-18T13:00:00Z", status="ended"))
        self.assertEqual(chat_sessions.get_active_session(self.storage, ROOTS)["id"], "chat-1-aaaaaaaa")
        chat_sessions.inject_user_message(self.storage, "chat-2-bbbbbbbb", "now this one")
        self.assertEqual(chat_sessions.get_active_session(self.storage, ROOTS)["id"], "chat-2-bbbbbbbb")

    def test_get_active_session_returns_a_just_created_chat(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa", last="2026-10-18T12:00:00Z"))
        new = chat_sessions.start_new_conversation(self.storage, ROOTS)
        self.assertEqual(chat_sessions.get_active_session(self.storage, ROOTS)["id"], new["id"])

    def test_get_active_session_creates_one_when_none_is_active(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa", status="ended"))
        created = chat_sessions.get_active_session(self.storage, ROOTS)
        self.assertNotEqual(created["id"], "chat-1-aaaaaaaa")
        self.assertEqual(created["status"], "active")
        self.assertIsNotNone(chat_sessions.get_session(self.storage, created["id"]))

    def test_end_session_ends_exactly_one(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa"))
        chat_sessions._write_session(self.storage, _session("chat-2-bbbbbbbb"))
        ended = chat_sessions.end_session(self.storage, "chat-1-aaaaaaaa")
        self.assertEqual(ended["status"], "ended")
        self.assertIsNotNone(ended["endedAt"])
        self.assertEqual(chat_sessions.get_session(self.storage, "chat-2-bbbbbbbb")["status"], "active")
        self.assertIsNone(chat_sessions.end_session(self.storage, "chat-1-aaaaaaaa"))
        self.assertIsNone(chat_sessions.end_session(self.storage, "chat-9-ffffffff"))

    def test_list_sessions_rows_and_order(self):
        chat_sessions._write_session(self.storage, _session(
            "chat-1-aaaaaaaa", started="2026-10-18T09:00:00Z", last="2026-10-18T10:00:00Z",
            transcript=[{"role": "user", "text": "  fix   the lease\ntests please"}, {"role": "assistant", "text": "ok"}]))
        chat_sessions._write_session(self.storage, _session("chat-2-bbbbbbbb", started="2026-10-18T11:00:00Z", provider="claude"))
        chat_sessions._write_session(self.storage, _session("chat-3-cccccccc", status="ended", started="2026-10-18T12:00:00Z"))
        rows = chat_sessions.list_sessions(self.storage)
        self.assertEqual([r["id"] for r in rows], ["chat-2-bbbbbbbb", "chat-1-aaaaaaaa"])
        second = rows[1]
        self.assertEqual(second["title"], "fix the lease tests please")
        self.assertEqual(second["turnCount"], 2)
        self.assertEqual(second["lastActivityAt"], "2026-10-18T10:00:00Z")
        self.assertEqual(rows[0]["title"], "New chat")
        self.assertEqual(rows[0]["provider"], "claude")
        self.assertEqual(set(rows[0]), {"id", "title", "provider", "model", "status", "startedAt",
                                        "lastActivityAt", "turnCount", "busy", "reserved"})

    def test_list_sessions_truncates_long_titles(self):
        chat_sessions._write_session(self.storage, _session("chat-1-aaaaaaaa", transcript=[{"role": "user", "text": "x" * 100}]))
        title = chat_sessions.list_sessions(self.storage)[0]["title"]
        self.assertEqual(len(title), 60)
        self.assertTrue(title.endswith("…"))


if __name__ == "__main__":
    unittest.main()
