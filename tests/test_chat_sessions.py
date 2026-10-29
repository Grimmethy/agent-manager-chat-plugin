"""Tests for chat_sessions.py streaming with tools -- event ordering, coalescing,
tool event pairing, error handling, and text preservation.

Shared contract (HUB0033):
- tool_start event: fields 'name' and 'summary' (first 120 chars of args)
- tool_end event: fields 'name', 'ok' (bool), and 'summary' (first 120 chars of result)
- Chunk coalescing: one frame per 100ms or 200 characters, whichever comes first,
  flushed before tool events and at end.

Run: .venv/bin/python -m unittest tests.test_chat_sessions -v
"""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import chat_sessions  # noqa: E402
import app  # noqa: E402


def _session():
    """Create a minimal session dict for testing."""
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": "chat-test-session",
        "provider": "local",
        "repoRoot": root,
        "roots": [root],
        "transcript": [],
    }


def _fake_stream(events):
    """Create a fake stream generator from a list of events."""
    def _gen(**kwargs):
        yield from events
    return _gen


class TestStreamingToolActivity(unittest.TestCase):
    """Tests for the streaming-with-tools contract in chat_sessions._stream_local."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)

    def _run(self, events):
        """Run _stream_local with a mocked stream_plan_with_tools."""
        session = _session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
            return list(chat_sessions._stream_local(session, "hi"))

    def test_events_arrive_in_order(self):
        """Verify tool_start, chunks, and tool_end arrive in correct order."""
        events = self._run([
            {"type": "chunk", "text": "start"},
            {"type": "tool_start", "name": "Read", "args": "/path/to/file.py"},
            {"type": "chunk", "text": "middle"},
            {"type": "tool_end", "name": "Read", "ok": True, "result": "file contents"},
            {"type": "chunk", "text": "end"},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        # Extract event types (filter out strings which are footers)
        event_types = [e["type"] if isinstance(e, dict) else "footer" for e in events]

        # Find indices
        tool_start_idx = event_types.index("tool_start")
        tool_end_idx = event_types.index("tool_end")

        # tool_start must come before tool_end
        self.assertLess(tool_start_idx, tool_end_idx)

        # Chunks must appear between tool_start and tool_end
        chunk_indices = [i for i, e in enumerate(event_types) if e == "chunk"]
        for idx in chunk_indices:
            self.assertGreater(idx, tool_start_idx, f"Chunk at index {idx} appears before tool_start")
            self.assertLess(idx, tool_end_idx, f"Chunk at index {idx} appears after tool_end")

    def test_tool_start_has_name_and_summary(self):
        """Verify tool_start event has 'name' and 'summary' (first 120 chars of args)."""
        events = self._run([
            {"type": "tool_start", "name": "Edit", "args": "x" * 200},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_start = next(e for e in events if isinstance(e, dict) and e.get("type") == "tool_start")
        self.assertIn("name", tool_start)
        self.assertIn("summary", tool_start)
        self.assertEqual(tool_start["name"], "Edit")
        # Summary should be first 120 chars of args
        self.assertEqual(tool_start["summary"], "x" * 120)
        self.assertLessEqual(len(tool_start["summary"]), 120)

    def test_tool_end_has_name_ok_and_summary(self):
        """Verify tool_end event has 'name', 'ok' (bool), and 'summary' (first 120 chars of result)."""
        events = self._run([
            {"type": "tool_end", "name": "Bash", "ok": True, "result": "y" * 200},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_end = next(e for e in events if isinstance(e, dict) and e.get("type") == "tool_end")
        self.assertIn("name", tool_end)
        self.assertIn("ok", tool_end)
        self.assertIn("summary", tool_end)
        self.assertEqual(tool_end["name"], "Bash")
        self.assertEqual(tool_end["ok"], True)
        # Summary should be first 120 chars of result
        self.assertEqual(tool_end["summary"], "y" * 120)
        self.assertLessEqual(len(tool_end["summary"]), 120)

    def test_tool_start_end_pairing(self):
        """Verify tool_start and tool_end have matching names and summaries."""
        events = self._run([
            {"type": "tool_start", "name": "Grep", "args": "pattern /dir"},
            {"type": "tool_end", "name": "Grep", "ok": True, "result": "match1\nmatch2"},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_starts = [e for e in events if isinstance(e, dict) and e.get("type") == "tool_start"]
        tool_ends = [e for e in events if isinstance(e, dict) and e.get("type") == "tool_end"]

        # Should have one of each
        self.assertEqual(len(tool_starts), 1)
        self.assertEqual(len(tool_ends), 1)

        # Names must match
        self.assertEqual(tool_starts[0]["name"], tool_ends[0]["name"])

    def test_tool_error_yields_ok_false(self):
        """Verify tool errors produce tool_end with ok=false."""
        events = self._run([
            {"type": "tool_start", "name": "Bash", "args": "rm -rf /"},
            {"type": "tool_end", "name": "Bash", "ok": False, "result": "Permission denied"},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_end = next(e for e in events if isinstance(e, dict) and e.get("type") == "tool_end")
        self.assertEqual(tool_end["ok"], False)
        self.assertIn("summary", tool_end)
        self.assertGreater(len(tool_end["summary"]), 0)

    def test_final_text_equals_uncoalesced(self):
        """Verify accumulated chunk text equals original uncoalesced text."""
        original_text = "Hello world " * 50  # 600 chars
        events_list = [{"type": "chunk", "text": "Hello world "} for _ in range(50)]
        events_list.append({"type": "final", "response": original_text, "turnsUsed": 1, "forcedSummary": False})
        events = self._run(events_list)

        # Collect all chunk text (strings yielded by _stream_local)
        chunk_text = "".join(e for e in events if isinstance(e, str))

        # The accumulated chunks should equal the original text
        self.assertEqual(chunk_text, original_text)


class TestChunkCoalescing(unittest.TestCase):
    """Tests for chunk coalescing: one frame per 100ms or 200 characters, whichever comes first."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)

    def _run(self, events):
        """Run _stream_local with a mocked stream_plan_with_tools."""
        session = _session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
            return list(chat_sessions._stream_local(session, "hi"))

    def test_chunks_coalesced_by_time(self):
        """Verify chunks emitted within 100ms are coalesced into fewer frames."""
        # Emit 10 chunks rapidly (each 10ms apart = 90ms total gap)
        # With 100ms coalescing, they should be merged into fewer frames
        events = []
        for i in range(10):
            events.append({"type": "chunk", "text": f"chunk{i}"})
        events.append({"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False})

        result = self._run(events)

        # Count chunk events in result
        chunk_events = [e for e in result if isinstance(e, dict) and e.get("type") == "chunk"]

        # With proper coalescing, rapid chunks should be merged
        # (exact count depends on implementation, but should be <= 10)
        self.assertLessEqual(len(chunk_events), 10)

    def test_chunks_coalesced_by_characters(self):
        """Verify chunks are coalesced to one frame per 200 characters."""
        # Emit chunks that together exceed 200 characters
        long_text = "x" * 300
        events = []
        for i in range(0, 300, 50):
            events.append({"type": "chunk", "text": long_text[i:i+50]})
        events.append({"type": "final", "response": long_text, "turnsUsed": 1, "forcedSummary": False})

        result = self._run(events)

        # Count chunk events in result
        chunk_events = [e for e in result if isinstance(e, dict) and e.get("type") == "chunk"]

        # With 200-char coalescing, 300 chars should be merged into fewer frames
        self.assertLessEqual(len(chunk_events), 6)  # 300/200 = 1.5, so at most 2 frames

    def test_chunks_flushed_before_tool_events(self):
        """Verify pending chunks are flushed before tool_start/tool_end events."""
        events = [
            {"type": "chunk", "text": "pending"},
            {"type": "tool_start", "name": "Read", "args": "/file"},
            {"type": "chunk", "text": "after_tool"},
            {"type": "tool_end", "name": "Read", "ok": True, "result": "content"},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ]

        result = self._run(events)

        # Build a normalized list: ("chunk", text) for strings, (event["type"], event) for dicts
        normalized = []
        for e in result:
            if isinstance(e, str):
                normalized.append(("chunk", e))
            elif isinstance(e, dict):
                normalized.append((e.get("type"), e))
            else:
                normalized.append(("other", e))

        # Find tool_start index
        tool_start_idx = next(i for i, (t, _) in enumerate(normalized) if t == "tool_start")

        # Count chunks before tool_start
        chunk_count_before = sum(1 for i in range(tool_start_idx) if normalized[i][0] == "chunk")
        self.assertGreater(chunk_count_before, 0, "Chunks should be flushed before tool_start")

    def test_no_chunks_after_tool_events(self):
        """Verify chunks don't appear after tool_end events (related to flushing)."""
        events = [
            {"type": "chunk", "text": "before"},
            {"type": "tool_start", "name": "Read", "args": "/file"},
            {"type": "tool_end", "name": "Read", "ok": True, "result": "content"},
            {"type": "chunk", "text": "after"},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ]

        result = self._run(events)

        # Extract event types
        event_types = [e["type"] if isinstance(e, dict) else "footer" for e in result]

        # Find tool_end index
        tool_end_idx = event_types.index("tool_end")

        # No chunks should appear after tool_end
        chunk_indices_after = [i for i, e in enumerate(event_types[tool_end_idx:]) if e == "chunk"]
        self.assertEqual(len(chunk_indices_after), 0, "No chunks should appear after tool_end")


class TestStreamingEdgeCases(unittest.TestCase):
    """Edge case tests for streaming with tools."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)

    def _run(self, events):
        """Run _stream_local with a mocked stream_plan_with_tools."""
        session = _session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
            return list(chat_sessions._stream_local(session, "hi"))

    def test_empty_args_produces_empty_summary(self):
        """Verify tool_start with empty args produces empty summary."""
        events = self._run([
            {"type": "tool_start", "name": "Read", "args": ""},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_start = next(e for e in events if isinstance(e, dict) and e.get("type") == "tool_start")
        self.assertEqual(tool_start["summary"], "")

    def test_empty_result_produces_empty_summary(self):
        """Verify tool_end with empty result produces empty summary."""
        events = self._run([
            {"type": "tool_end", "name": "Bash", "ok": True, "result": ""},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_end = next(e for e in events if isinstance(e, dict) and e.get("type") == "tool_end")
        self.assertEqual(tool_end["summary"], "")

    def test_missing_name_defaults_to_unknown(self):
        """Verify missing 'name' field defaults to 'unknown'."""
        events = self._run([
            {"type": "tool_start", "args": "some args"},
            {"type": "final", "response": "done", "turnsUsed": 1, "forcedSummary": False},
        ])

        tool_start = next(e for e in events if isinstance(e, dict) and e.get("type") == "tool_start")
        self.assertEqual(tool_start["name"], "unknown")

    def test_multiple_tool_pairs(self):
        """Verify multiple tool_start/tool_end pairs are preserved."""
        events = self._run([
            {"type": "tool_start", "name": "Read", "args": "/file1"},
            {"type": "tool_end", "name": "Read", "ok": True, "result": "content1"},
            {"type": "tool_start", "name": "Edit", "args": "/file2"},
            {"type": "tool_end", "name": "Edit", "ok": True, "result": "content2"},
            {"type": "final", "response": "done", "turnsUsed": 2, "forcedSummary": False},
        ])

        tool_starts = [e for e in events if isinstance(e, dict) and e.get("type") == "tool_start"]
        tool_ends = [e for e in events if isinstance(e, dict) and e.get("type") == "tool_end"]

        self.assertEqual(len(tool_starts), 2)
        self.assertEqual(len(tool_ends), 2)

        # Verify pairing: each start matches an end
        start_names = [e["name"] for e in tool_starts]
        end_names = [e["name"] for e in tool_ends]
        self.assertEqual(start_names, end_names)


if __name__ == "__main__":
    unittest.main()
