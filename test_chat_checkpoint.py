"""Tests for the chat checkpoint / interrupt-reason contract (HUB0048 piece 1/6).

Covers: INTERRUPT_REASONS, _mark_partial_checkpoint, _set_interrupt_reason,
_pop_interrupt_reason, _recover_interrupted, the exception handler in
_stream_message_turn, get_session recovery, _build_local_messages appending the
interruption note, record_turn_error keeping partial text, and the stored-reason
wins-over-exception-type rule.

Run: .venv/bin/python -m unittest test_chat_checkpoint -v
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402


def _make_session(transcript=None, session_id="chat-test-123"):
    return {
        "id": session_id,
        "provider": "local",
        "repoRoot": ".",
        "roots": ["."],
        "status": "active",
        "transcript": transcript or [],
    }


def _fake_stream(events):
    """Yields a fixed list of events, used as a stand-in for _stream_local."""
    def _gen(*args, **kwargs):
        yield from events
    return _gen


class InterruptReasonTest(unittest.TestCase):
    """_set_interrupt_reason / _pop_interrupt_reason contract (one-shot, locked)."""

    def setUp(self):
        # Start each test with a clean registry so tests don't leak reasons.
        with chat_sessions._interrupt_reason_lock:
            chat_sessions._interrupt_reasons.clear()

    def test_set_and_pop_returns_stored(self):
        chat_sessions._set_interrupt_reason("s1", "stopped")
        self.assertEqual(chat_sessions._pop_interrupt_reason("s1", "x"), "stopped")

    def test_pop_removes_key(self):
        chat_sessions._set_interrupt_reason("s1", "stopped")
        chat_sessions._pop_interrupt_reason("s1", "x")
        # A second pop must return the default (key gone).
        self.assertEqual(chat_sessions._pop_interrupt_reason("s1", "fallback"), "fallback")

    def test_pop_missing_returns_default(self):
        self.assertEqual(chat_sessions._pop_interrupt_reason("nope", "default"), "default")

    def test_invalid_reason_raises_valueerror(self):
        with self.assertRaises(ValueError):
            chat_sessions._set_interrupt_reason("s1", "bogus")


class MarkPartialCheckpointTest(unittest.TestCase):
    """_mark_partial_checkpoint: marks last assistant when text is non-empty."""

    def test_marks_last_assistant_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "partial answer"},
            ])
            changed = chat_sessions._mark_partial_checkpoint(storage, session, "stopped")
            self.assertTrue(changed)
            last = session["transcript"][-1]
            self.assertTrue(last["partial"])
            self.assertEqual(last["reason"], "stopped")
            # Persisted to disk.
            loaded = chat_sessions._read_session(storage, "chat-test-123")
            self.assertTrue(loaded["transcript"][-1]["partial"])

    def test_empty_text_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": ""},
            ])
            self.assertFalse(chat_sessions._mark_partial_checkpoint(storage, session, "stopped"))

    def test_no_assistant_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([{"role": "user", "text": "hi"}])
            self.assertFalse(chat_sessions._mark_partial_checkpoint(storage, session, "stopped"))

    def test_invalid_reason_raises(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "x"},
            ])
            with self.assertRaises(ValueError):
                chat_sessions._mark_partial_checkpoint(storage, session, "bogus")


class StreamMessageTurnInterruptTest(unittest.TestCase):
    """_stream_message_turn exception path: text kept, entry marked partial."""

    def setUp(self):
        with chat_sessions._interrupt_reason_lock:
            chat_sessions._interrupt_reasons.clear()

    def _run_with_exception(self, exception, events_before=None):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            streamed = events_before or []
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "", "streaming": True},
            ])
            # Patch _stream_local with a function that raises the exception after yielding events.
            def _raising_stream(*args, **kwargs):
                yield from streamed
                raise exception
            with mock.patch("chat_sessions._stream_local", _raising_stream):
                with self.assertRaises(type(exception)):
                    list(chat_sessions._stream_message_turn(storage, session, "hi"))
            return session

    def test_timeout_error_marks_partial_with_timeout(self):
        session = self._run_with_exception(TimeoutError("timed out"),
                                           events_before=["hello "])
        last = session["transcript"][-1]
        self.assertTrue(last["partial"])
        self.assertEqual(last["reason"], "timeout")
        self.assertTrue(last["text"].strip())

    def test_generator_exit_no_stored_reason_gives_disconnect(self):
        session = self._run_with_exception(GeneratorExit(), events_before=["partial "])
        last = session["transcript"][-1]
        self.assertTrue(last["partial"])
        self.assertEqual(last["reason"], "disconnect")

    def test_stored_reason_wins_over_exception_type(self):
        with chat_sessions._interrupt_reason_lock:
            chat_sessions._interrupt_reasons["chat-test-123"] = "stopped"
        session = self._run_with_exception(GeneratorExit(), events_before=["partial "])
        last = session["transcript"][-1]
        self.assertEqual(last["reason"], "stopped")

    def test_exception_before_any_text_marks_nothing(self):
        # No streamed chunks at all: _mark_partial_checkpoint sees empty text
        # and returns False; the entry is NOT marked partial.
        session = self._run_with_exception(TimeoutError("no text"))
        last = session["transcript"][-1]
        self.assertFalse(last.get("partial"))
        self.assertEqual(last["text"], "")

    def test_normal_turn_clears_streaming(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "", "streaming": True},
            ])
            events = ["chunk1", "chunk2", {"type": "final", "final_text": "chunk1chunk2"}]
            # Patch _stream_local so the turn completes without hitting the real API.
            def _fake_local(*args, **kwargs):
                yield from events
            with mock.patch("chat_sessions._stream_local", _fake_local):
                list(chat_sessions._stream_message_turn(storage, session, "hi"))
            last = session["transcript"][-1]
            self.assertNotIn("streaming", last)
            self.assertNotIn("partial", last)


class RecoverInterruptedTest(unittest.TestCase):
    """_recover_interrupted: marks streaming entry 'restart' when not busy."""

    def test_marks_streaming_entry_restart_when_not_busy(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "partial reply", "streaming": True},
            ])
            # Ensure session is NOT in _busy_sessions.
            self.assertNotIn(session["id"], chat_sessions._busy_sessions)
            changed = chat_sessions._recover_interrupted(storage, session)
            self.assertTrue(changed)
            last = session["transcript"][-1]
            self.assertTrue(last["partial"])
            self.assertEqual(last["reason"], "restart")
            self.assertNotIn("streaming", last)

    def test_does_nothing_when_busy(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "partial reply", "streaming": True},
            ])
            chat_sessions._busy_sessions.add(session["id"])
            try:
                changed = chat_sessions._recover_interrupted(storage, session)
                self.assertFalse(changed)
                last = session["transcript"][-1]
                self.assertNotIn("partial", last)
                self.assertIn("streaming", last)
            finally:
                chat_sessions._busy_sessions.discard(session["id"])


class GetSessionRecoveryTest(unittest.TestCase):
    """get_session() calls _recover_interrupted before returning."""

    def test_get_session_recovers_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "partial reply", "streaming": True},
            ])
            chat_sessions._write_session(storage, session)
            # Pretend the session is no longer live.
            chat_sessions._busy_sessions.discard("chat-test-123")
            recovered = chat_sessions.get_session(storage, "chat-test-123")
            self.assertTrue(recovered["transcript"][-1]["partial"])
            self.assertEqual(recovered["transcript"][-1]["reason"], "restart")


class BuildLocalMessagesTest(unittest.TestCase):
    """_build_local_messages: appends interruption note for partial entries only."""

    def test_partial_entry_gets_note(self):
        session = _make_session([
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "partial reply", "partial": True, "reason": "stopped"},
        ])
        msgs = chat_sessions._build_local_messages(session, "next")
        last_user = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertIn("[this reply was interrupted: stopped]", last_user["content"])

    def test_complete_entry_no_note(self):
        session = _make_session([
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "complete reply"},
        ])
        msgs = chat_sessions._build_local_messages(session, "next")
        last_user = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertNotIn("interrupted", last_user["content"])

    def test_partial_without_reason_no_note(self):
        session = _make_session([
            {"role": "user", "text": "hi"},
            {"role": "assistant", "text": "partial", "partial": True},
        ])
        msgs = chat_sessions._build_local_messages(session, "next")
        last_user = [m for m in msgs if m["role"] == "assistant"][-1]
        self.assertNotIn("interrupted", last_user["content"])


class RecordTurnErrorTest(unittest.TestCase):
    """record_turn_error keeps partial text and marks it partial with reason 'error'."""

    def test_keeps_partial_text_and_marks_partial(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "partial answer"},
            ])
            chat_sessions._write_session(storage, session)
            chat_sessions.record_turn_error(storage, "chat-test-123", "boom")
            loaded = chat_sessions._read_session(storage, "chat-test-123")
            last = loaded["transcript"][-1]
            self.assertIn("partial answer", last["text"])
            self.assertIn("Error: boom", last["text"])
            self.assertTrue(last.get("partial"))
            self.assertEqual(last["reason"], "error")

    def test_empty_text_appends_error_only(self):
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": ""},
            ])
            chat_sessions._write_session(storage, session)
            chat_sessions.record_turn_error(storage, "chat-test-123", "boom")
            loaded = chat_sessions._read_session(storage, "chat-test-123")
            last = loaded["transcript"][-1]
            self.assertEqual(last["text"], "Error: boom")
            # No partial mark when there was nothing to keep.
            self.assertFalse(last.get("partial"))


class MutationCheckTest(unittest.TestCase):
    """Mutation checks: flip each change and confirm a test fails."""

    def test_mutation_stored_reason_loses_to_exception_type(self):
        """Flip: make the exception type default win even when a reason is stored.
        A test that asserts 'stopped' wins MUST fail under this mutation."""
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            with chat_sessions._interrupt_reason_lock:
                chat_sessions._interrupt_reasons["chat-test-123"] = "stopped"
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "", "streaming": True},
            ])
            # Simulate the mutation: default always wins, ignoring stored reason.
            def _broken_pop(sid, default):
                return default
            def _raising_stream(*args, **kwargs):
                yield from ["partial "]
                raise GeneratorExit()
            with mock.patch("chat_sessions._stream_local", _raising_stream):
                with mock.patch("chat_sessions._pop_interrupt_reason", _broken_pop):
                    with self.assertRaises(GeneratorExit):
                        list(chat_sessions._stream_message_turn(storage, session, "hi"))
            last = session["transcript"][-1]
            # Under the mutation the reason is the default ('disconnect'), NOT 'stopped'.
            self.assertEqual(last["reason"], "disconnect")
            with self.assertRaises(AssertionError):
                self.assertEqual(last["reason"], "stopped")

    def test_mutation_remove_get_session_recovery(self):
        """Flip: remove the _recover_interrupted call from get_session.
        A test that asserts get_session recovers MUST fail under this mutation."""
        with tempfile.TemporaryDirectory() as td:
            storage = Path(td)
            chat_sessions._migrate_legacy_store(storage)
            session = _make_session([
                {"role": "user", "text": "hi"},
                {"role": "assistant", "text": "partial reply", "streaming": True},
            ])
            chat_sessions._write_session(storage, session)
            chat_sessions._busy_sessions.discard("chat-test-123")
            # Mutation: get_session no longer calls _recover_interrupted.
            with mock.patch("chat_sessions._recover_interrupted", return_value=False):
                recovered = chat_sessions.get_session(storage, "chat-test-123")
            # Under the mutation the entry is NOT recovered -- no 'partial' key.
            self.assertFalse(recovered["transcript"][-1].get("partial"))
            self.assertNotIn("reason", recovered["transcript"][-1])


if __name__ == "__main__":
    unittest.main()
