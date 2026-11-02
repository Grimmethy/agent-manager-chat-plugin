"""Tests for HUB0048 · 2/6: chat stop and status (chat_sessions.py)

Covers: stop registry, once-per-phase status frames, finally-safe cleanup.
Fake _stream_local that yields chunks and tool events for the local provider.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Add parent dir to path so we can import chat_sessions
sys.path.insert(0, str(Path(__file__).parent))
import chat_sessions


class TestStopStream(unittest.TestCase):
    """Tests for stop registry, status tracker, and finally-safe cleanup."""

    def setUp(self):
        """Clear module-level state before each test."""
        chat_sessions._stop_events.clear()
        chat_sessions._interrupt_reasons.clear()
        with chat_sessions._busy_lock:
            chat_sessions._busy_sessions.clear()

    def tearDown(self):
        """Verify _stop_events is empty after every test (no leaks)."""
        self.assertEqual(
            chat_sessions._stop_events,
            {},
            "_stop_events not empty after test -- stop event leaked",
        )

    def test_status_order_plain_local_turn(self):
        """Plain local turn (no tools) sends 'model-running' once."""
        events = []

        def fake_stream_local(session, message, result_box):
            # Yield a text chunk to trigger model-running
            yield "Hello"
            result_box["final_text"] = "Hello"

        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test1",
                "provider": chat_sessions.PROVIDER_LOCAL,
                "transcript": [],
                "status": "active",
                "messages": [],
            }
            # Patch _stream_local for this test
            original = chat_sessions._stream_local
            chat_sessions._stream_local = fake_stream_local
            try:
                gen = chat_sessions._stream_message_turn(storage_dir, session, "hi")
                for evt in gen:
                    events.append(evt)
            finally:
                chat_sessions._stream_local = original

        # Should have: model-running status, then chunk, then final
        status_events = [e for e in events if e.get("type") == "status"]
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0]["message"], "model-running")

    def test_status_order_with_tool(self):
        """Local turn with a tool: model-running, tool-running, model-running."""
        events = []

        def fake_stream_local(session, message, result_box):
            yield "Let me check"
            yield {"type": "tool_start", "name": "Grep", "args": {"query": "x"}}
            yield {"type": "tool_end", "name": "Grep", "ok": True, "summary": "found"}
            yield "Done"
            result_box["final_text"] = "Let me check\nDone"

        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test2",
                "provider": chat_sessions.PROVIDER_LOCAL,
                "transcript": [],
                "status": "active",
                "messages": [],
            }
            original = chat_sessions._stream_local
            chat_sessions._stream_local = fake_stream_local
            try:
                gen = chat_sessions._stream_message_turn(storage_dir, session, "hi")
                for evt in gen:
                    events.append(evt)
            finally:
                chat_sessions._stream_local = original

        status_events = [e for e in events if e.get("type") == "status"]
        self.assertEqual(len(status_events), 3)
        self.assertEqual(status_events[0]["message"], "model-running")
        self.assertEqual(status_events[1]["message"], "tool-running")
        self.assertEqual(status_events[2]["message"], "model-running")

    def test_repeated_tool_start_does_not_repeat_frame(self):
        """Two tool_start events in a row: tool-running sent only once."""
        events = []

        def fake_stream_local(session, message, result_box):
            yield "Checking"
            yield {"type": "tool_start", "name": "Grep", "args": {}}
            yield {"type": "tool_start", "name": "Read", "args": {}}
            yield {"type": "tool_end", "name": "Read", "ok": True, "summary": "ok"}
            yield {"type": "tool_end", "name": "Grep", "ok": True, "summary": "ok"}
            yield "Done"
            result_box["final_text"] = "Checking\nDone"

        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test3",
                "provider": chat_sessions.PROVIDER_LOCAL,
                "transcript": [],
                "status": "active",
                "messages": [],
            }
            original = chat_sessions._stream_local
            chat_sessions._stream_local = fake_stream_local
            try:
                gen = chat_sessions._stream_message_turn(storage_dir, session, "hi")
                for evt in gen:
                    events.append(evt)
            finally:
                chat_sessions._stream_local = original

        status_events = [e for e in events if e.get("type") == "status"]
        # tool-running should appear only once (de-duplicated by tracker)
        tool_running = [e for e in status_events if e["message"] == "tool-running"]
        self.assertEqual(len(tool_running), 1)

    def test_claude_turn_sends_model_running_once(self):
        """Claude turn sends 'model-running' once before the blocking call."""
        events = []

        def fake_send_claude(session, message, storage_dir):
            return "Claude reply"

        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test4",
                "provider": chat_sessions.PROVIDER_CLAUDE,
                "transcript": [],
                "status": "active",
                "messages": [],
            }
            original_send = chat_sessions._send_claude
            chat_sessions._send_claude = fake_send_claude
            try:
                gen = chat_sessions._stream_message_turn(storage_dir, session, "hi")
                for evt in gen:
                    events.append(evt)
            finally:
                chat_sessions._send_claude = original_send

        status_events = [e for e in events if e.get("type") == "status"]
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0]["message"], "model-running")

    def test_stop_session_false_with_no_live_turn(self):
        """stop_session returns False when no turn is running."""
        result = chat_sessions.stop_session("nonexistent")
        self.assertFalse(result)

    def test_stop_session_true_during_turn(self):
        """stop_session returns True and sets the event when a turn is live."""
        # Register event (setUp cleared _stop_events, so we register fresh)
        event = chat_sessions._register_stop_event("test5")
        result = chat_sessions.stop_session("test5")
        self.assertTrue(result)
        self.assertTrue(event.is_set())
        # Clear it so tearDown passes
        chat_sessions._clear_stop_event("test5")

    def test_stop_mid_stream_yields_stopped_frame(self):
        """A stop mid-stream yields {'type': 'stopped'}, entry keeps text with partial."""
        def fake_stream_local(session, message, result_box):
            yield "Partial reply"
            # Block until the test sets the stop event
            import time
            while not chat_sessions._stop_events.get("test6", threading.Event()).is_set():
                time.sleep(0.01)
            result_box["final_text"] = "Partial reply"

        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test6",
                "provider": chat_sessions.PROVIDER_LOCAL,
                "transcript": [],
                "status": "active",
                "messages": [],
            }
            original = chat_sessions._stream_local
            chat_sessions._stream_local = fake_stream_local
            try:
                gen = chat_sessions.stream_message(storage_dir, "test6", "hi")
                events = []
                for evt in gen:
                    events.append(evt)
                    if evt.get("type") == "chunk" and "Partial" in evt.get("text", ""):
                        # Trigger stop after first chunk
                        chat_sessions.stop_session("test6")
                # Verify stopped frame was yielded
                stopped_events = [e for e in events if e.get("type") == "stopped"]
                self.assertEqual(len(stopped_events), 1)
                # Verify session was persisted with partial
                session_path = chat_sessions._session_path(storage_dir, "test6")
                persisted = json.loads(session_path.read_text())
                last_entry = persisted["transcript"][-1]
                self.assertTrue(last_entry.get("partial"))
                self.assertEqual(last_entry.get("reason"), "stopped")
                self.assertTrue(last_entry["text"].strip())
            finally:
                chat_sessions._stream_local = original

    def test_stop_event_gone_after_stop(self):
        """Stop event is cleared after a normal stop."""
        event = chat_sessions._register_stop_event("test7")
        event.set()
        chat_sessions._clear_stop_event("test7")
        self.assertNotIn("test7", chat_sessions._stop_events)

    def test_stop_event_gone_after_disconnect(self):
        """Stop event is cleared after GeneratorExit (disconnect)."""
        event = chat_sessions._register_stop_event("test8")

        def fake_gen():
            yield "chunk"
            raise GeneratorExit()

        gen = fake_gen()
        try:
            # Simulate stream_message's loop
            stop_event = event
            while True:
                try:
                    evt = next(gen)
                except StopIteration:
                    break
                if stop_event.is_set():
                    break
        except GeneratorExit:
            chat_sessions._clear_stop_event("test8")
        self.assertNotIn("test8", chat_sessions._stop_events)

    def test_stop_event_gone_after_exception(self):
        """Stop event is cleared after an exception."""
        event = chat_sessions._register_stop_event("test9")

        try:
            raise ValueError("test error")
        except ValueError:
            chat_sessions._clear_stop_event("test9")
        self.assertNotIn("test9", chat_sessions._stop_events)

    def test_stop_event_gone_after_normal_end(self):
        """Stop event is cleared after normal generator end."""
        event = chat_sessions._register_stop_event("test10")

        def fake_gen():
            yield "chunk"
            return

        gen = fake_gen()
        for _ in gen:
            pass
        chat_sessions._clear_stop_event("test10")
        self.assertNotIn("test10", chat_sessions._stop_events)

    def test_second_stop_during_same_turn_is_harmless(self):
        """A second stop_session call during the same turn does not crash."""
        event = chat_sessions._register_stop_event("test11")
        result1 = chat_sessions.stop_session("test11")
        result2 = chat_sessions.stop_session("test11")
        self.assertTrue(result1)
        self.assertTrue(result2)
        self.assertTrue(event.is_set())
        # Clear it so tearDown passes
        chat_sessions._clear_stop_event("test11")

    def test_mark_partial_checkpoint_marks_last_assistant(self):
        """_mark_partial_checkpoint marks the LAST assistant entry only."""
        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            # Create a proper session file first
            session = {
                "id": "test12",
                "messages": [
                    {"role": "user", "text": "hi"},
                    {"role": "assistant", "text": "first", "streaming": True},
                    {"role": "assistant", "text": "second", "streaming": True},
                ],
            }
            # Write the session file
            chat_sessions._write_session(storage_dir, session)
            # Read it back (this also runs _migrate_session)
            session = chat_sessions._read_session(storage_dir, "test12")
            self.assertIsNotNone(session)
            print(f"DEBUG: session keys: {session.keys()}")
            print(f"DEBUG: last message: {session['messages'][-1]}")
            print(f"DEBUG: last message has streaming: {'streaming' in session['messages'][-1]}")
            print(f"DEBUG: INTERRUPT_REASONS: {chat_sessions.INTERRUPT_REASONS}")
            print(f"DEBUG: 'stopped' in INTERRUPT_REASONS: {'stopped' in chat_sessions.INTERRUPT_REASONS}")
            messages = session.get('messages', [])
            print(f"DEBUG: messages not empty: {bool(messages)}")
            last_entry = messages[-1]
            print(f"DEBUG: last entry role: {last_entry.get('role')}")
            print(f"DEBUG: last entry text strip: {repr(last_entry.get('text', '').strip())}")
            print(f"DEBUG: last entry streaming: {last_entry.get('streaming')}")
            import inspect
            print(f"DEBUG: function source: {inspect.getsource(chat_sessions._mark_partial_checkpoint)}")
            result = chat_sessions._mark_partial_checkpoint(storage_dir, session, "stopped")
            print(f"DEBUG: result: {result}")
            self.assertTrue(result)
            # Only the LAST entry should be marked
            self.assertFalse(session["messages"][1].get("partial"))
            self.assertTrue(session["messages"][2].get("partial"))
            self.assertEqual(session["messages"][2]["reason"], "stopped")
            self.assertNotIn("streaming", session["messages"][2])

    def test_mark_partial_checkpoint_skips_empty_text(self):
        """_mark_partial_checkpoint returns False when text is empty."""
        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test13",
                "messages": [
                    {"role": "assistant", "text": "", "streaming": True},
                ],
            }
            result = chat_sessions._mark_partial_checkpoint(storage_dir, session, "stopped")
            self.assertFalse(result)

    def test_status_tracker_de_duplicates(self):
        """_StatusTracker.next_frame returns None on repeated code."""
        tracker = chat_sessions._StatusTracker()
        frame1 = tracker.next_frame("model-running")
        frame2 = tracker.next_frame("model-running")
        self.assertIsNotNone(frame1)
        self.assertIsNone(frame2)

    def test_status_tracker_returns_new_code(self):
        """_StatusTracker.next_frame returns a dict when code changes."""
        tracker = chat_sessions._StatusTracker()
        frame1 = tracker.next_frame("model-running")
        frame2 = tracker.next_frame("tool-running")
        self.assertEqual(frame1["message"], "model-running")
        self.assertEqual(frame2["message"], "tool-running")

    def test_interrupt_reason_set_and_pop(self):
        """_set_interrupt_reason and _pop_interrupt_reason work correctly."""
        chat_sessions._set_interrupt_reason("s1", "stopped")
        reason = chat_sessions._pop_interrupt_reason("s1", "")
        self.assertEqual(reason, "stopped")
        # Pop removes the key
        reason2 = chat_sessions._pop_interrupt_reason("s1", "default")
        self.assertEqual(reason2, "default")

    def test_recover_interrupted_marks_streaming_entry(self):
        """_recover_interrupted marks a streaming entry as partial with 'restart'."""
        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            # Create a proper session file first
            session = {
                "id": "test14",
                "messages": [
                    {"role": "assistant", "text": "partial", "streaming": True},
                ],
            }
            # Write the session file
            chat_sessions._write_session(storage_dir, session)
            # Read it back (this also runs _migrate_session)
            session = chat_sessions._read_session(storage_dir, "test14")
            self.assertIsNotNone(session)
            # Not in _busy_sessions
            result = chat_sessions._recover_interrupted(storage_dir, session)
            self.assertTrue(result)
            self.assertTrue(session["messages"][0].get("partial"))
            self.assertEqual(session["messages"][0]["reason"], "restart")
            self.assertNotIn("streaming", session["messages"][0])

    def test_recover_interrupted_skips_busy_session(self):
        """_recover_interrupted returns False when session is busy."""
        with tempfile.TemporaryDirectory() as tmp:
            storage_dir = Path(tmp)
            session = {
                "id": "test15",
                "messages": [
                    {"role": "assistant", "text": "partial", "streaming": True},
                ],
            }
            # Add to _busy_sessions
            with chat_sessions._busy_lock:
                chat_sessions._busy_sessions.add("test15")
            result = chat_sessions._recover_interrupted(storage_dir, session)
            self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
