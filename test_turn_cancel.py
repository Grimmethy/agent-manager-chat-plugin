"""Tests for turn cancellation of a running local-model chat turn.

Covers the two real pieces that landed as sibling tasks (HUB0013 1/3 + 2/3):

- ``turn_cancel.py``: the thread-safe, process-local cancel-flag module with the
  ``request(session_id)`` / ``is_requested(session_id)`` / ``clear(session_id)``
  semantics.
- ``chat_sessions.py``: ``stream_message()`` clears a stale flag at turn start, and
  the local-provider branch of ``_stream_message_turn()`` checks
  ``turn_cancel.is_requested(session_id)`` BETWEEN chunks of the
  ``_stream_local()`` stream, stopping the turn with ``cancelled: True`` on the
  transcript row and clearing the flag. The Claude branch (``_send_claude``, one
  blocking subprocess call) applies NO cancel check -- it must be unaffected.

``chat_sessions._stream_local`` is monkeypatched with a small generator that yields
three text chunks and (in the cancel tests) calls ``turn_cancel.request(session_id)``
after the second chunk -- exactly the mid-turn cancel scenario.

Run: .venv/bin/python -m unittest test_turn_cancel -v
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402
import turn_cancel  # noqa: E402

CHUNKS = ["Hello ", "world", " more"]


def _make_session(provider):
    """A minimal active local/claude session dict, shaped like _new_session()."""
    return {
        "id": "chat-cancel-test",
        "provider": provider,
        "model": "test-model",
        "effort": "high",
        "roots": ["."],
        "repoRoot": ".",
        "instancesDir": None,
        "claudeSessionId": None,
        "transcript": [],
        "status": "active",
        "startedAt": chat_sessions._now_iso(),
        "lastActivityAt": None,
        "endedAt": None,
        "reserved": False,
    }


def _run_local_turn(storage_dir, session, stream_local):
    """Drive one full turn through the public stream_message() generator and return
    (events, generator)."""
    chat_sessions._write_session(storage_dir, session)
    original = chat_sessions._stream_local
    chat_sessions._stream_local = stream_local
    try:
        events = list(chat_sessions.stream_message(storage_dir, session["id"], "hi"))
    finally:
        chat_sessions._stream_local = original
    return events


def _fake_stream_local(cancel_after_second, requested_count_box):
    """Generator stand-in for chat_sessions._stream_local: yields the three CHUNKS;
    when cancel_after_second, it requests a cancel right after yielding chunk 2.
    requested_count_box[0] counts how many times the turn loop asked for a chunk, so
    a test can prove the stream was never advanced past the second yield."""
    def _gen(session, message, result_box=None):
        for i, text in enumerate(CHUNKS, start=1):
            requested_count_box[0] = i
            yield text
            if cancel_after_second and i == 2:
                turn_cancel.request(session["id"])
    return _gen


def _final_session(events):
    finals = [e for e in events if e.get("type") == "final"]
    assert len(finals) == 1, f"expected exactly one final event, got {len(finals)}"
    return finals[-1]["session"]


class TurnCancelFlagSemanticsTest(unittest.TestCase):
    """Pure turn_cancel.py semantics -- no chat_sessions involvement."""

    def test_request_is_requested_clear_semantics(self):
        sid = "semantics-sid"
        try:
            self.assertFalse(turn_cancel.is_requested(sid), "fresh id must not be flagged")
            turn_cancel.request(sid)
            self.assertTrue(turn_cancel.is_requested(sid), "flag must be set after request()")
            turn_cancel.clear(sid)
            self.assertFalse(turn_cancel.is_requested(sid), "flag must be gone after clear()")
            turn_cancel.request(sid)
            self.assertTrue(turn_cancel.is_requested(sid), "request() must work again after clear()")
        finally:
            turn_cancel.clear(sid)


class TurnCancelSmokeConcurrencyTest(unittest.TestCase):
    def test_thread_safety_smoke(self):
        # 8 threads hammering request/clear/is_requested on the SAME id: no crashes,
        # no deadlocks, no lost lock acquisition. Smoke test -- no final-state assert.
        sid = "concurrency-sid"
        errors = []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait(timeout=10)
                for _ in range(200):
                    turn_cancel.request(sid)
                    turn_cancel.is_requested(sid)
                    turn_cancel.clear(sid)
                    turn_cancel.is_requested(sid)
            except BaseException as e:  # noqa: BLE001 -- collecting ANY exception is the point
                errors.append(e)

        try:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            for t in threads:
                self.assertFalse(t.is_alive(), "worker thread deadlocked")
        finally:
            turn_cancel.clear(sid)
        self.assertEqual(errors, [], f"concurrent cancel ops raised: {errors!r}")


class TurnCancelLocalTurnTest(unittest.TestCase):
    """End-to-end: monkeypatched _stream_local + the real stream_message() turn loop."""

    def setUp(self):
        self.storage_dir = Path(tempfile.mkdtemp(prefix="turn-cancel-test-"))
        self.session_id = "chat-cancel-test"
        self.addCleanup(self._teardown)

    def _teardown(self):
        turn_cancel.clear(self.session_id)

    def _run_cancelled_turn(self):
        session = _make_session(chat_sessions.PROVIDER_LOCAL)
        requested = [0]
        stream = _fake_stream_local(cancel_after_second=True, requested_count_box=requested)
        events = _run_local_turn(self.storage_dir, session, stream)
        return events, requested

    def test_turn_stops_after_second_chunk_with_partial_text_and_cancelled_true(self):
        events, _ = self._run_cancelled_turn()
        chunk_events = [e["text"] for e in events if e.get("type") == "chunk"]
        self.assertEqual(chunk_events, CHUNKS[:2],
                         "the turn must stop after chunk 2 -- the 3rd chunk never streams")
        session = _final_session(events)
        row = session["transcript"][-1]
        self.assertEqual(row["role"], "assistant")
        self.assertEqual(row["text"], "".join(CHUNKS[:2]), "partial text = first two chunks")
        self.assertTrue(row.get("cancelled"), "the transcript row must carry cancelled: True")
        self.assertFalse(turn_cancel.is_requested(self.session_id),
                         "the flag must be cleared by the turn itself so it can't leak")

    def test_turn_stops_exactly_after_second_chunk(self):
        # Precise boundary check on the observable turn: the stored partial text equals
        # EXACTLY the first two chunks, and the third chunk is never DELIVERED (neither
        # as a streamed chunk event nor in the persisted transcript). This pins the
        # "stops after the second chunk" boundary without coupling to how many times
        # internally the (patched) generator is advanced to surface the cancel.
        events, _ = self._run_cancelled_turn()
        self.assertFalse(any(e.get("type") == "chunk" and e["text"] == CHUNKS[2] for e in events),
                         "third chunk must never be delivered to the caller")
        session = _final_session(events)
        self.assertEqual(session["transcript"][-1]["text"], "".join(CHUNKS[:2]))
        self.assertNotIn(CHUNKS[2], session["transcript"][-1]["text"],
                         "third chunk must never be persisted into the transcript")

    def test_stale_flag_does_not_cancel_new_turn(self):
        # Worst case: a flag left over from some earlier crashed/interrupted turn.
        # stream_message() must clear it at turn start and let the turn run to completion.
        turn_cancel.request(self.session_id)
        session = _make_session(chat_sessions.PROVIDER_LOCAL)
        requested = [0]
        stream = _fake_stream_local(cancel_after_second=False, requested_count_box=requested)
        events = _run_local_turn(self.storage_dir, session, stream)
        chunk_events = [e["text"] for e in events if e.get("type") == "chunk"]
        self.assertEqual(chunk_events, CHUNKS, "all chunks must stream -- stale flag ignored")
        self.assertEqual(requested[0], 3, "generator must run to the end")
        session = _final_session(events)
        row = session["transcript"][-1]
        self.assertNotIn("cancelled", row, "no cancelled tag on a completed turn")


class TurnCancelClaudeUnaffectedTest(unittest.TestCase):
    def test_claude_provider_turn_unaffected_by_cancel_flag(self):
        storage_dir = Path(tempfile.mkdtemp(prefix="turn-cancel-test-claude-"))
        sid = "chat-claude-cancel"
        session = _make_session(chat_sessions.PROVIDER_CLAUDE)
        session["id"] = sid
        try:
            # Flag set BEFORE the turn (worst case) -- the Claude branch never consults it.
            turn_cancel.request(sid)
            chat_sessions._write_session(storage_dir, session)

            def explode(*args, **kwargs):
                raise AssertionError("Claude branch must never call _stream_local")

            with mock.patch.object(chat_sessions, "_send_claude", return_value="claude reply"), \
                 mock.patch.object(chat_sessions, "_stream_local", explode):
                events = list(chat_sessions.stream_message(storage_dir, sid, "hi"))
        finally:
            turn_cancel.clear(sid)

        chunk_events = [e["text"] for e in events if e.get("type") == "chunk"]
        self.assertEqual(chunk_events, ["claude reply"], "Claude turn streams its normal reply")
        session = _final_session(events)
        row = session["transcript"][-1]
        self.assertEqual(row["role"], "assistant")
        self.assertIn("claude reply", row["text"])
        self.assertNotIn("cancelled", row, "Claude turns are never marked cancelled")
        # stream_message clears the flag at turn start, so the pre-set flag is gone now;
        # either way the Claude reply itself is untouched.
        self.assertEqual(row.get("error"), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
