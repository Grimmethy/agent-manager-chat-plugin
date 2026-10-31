"""Per-turn timings measured inside the chat plugin (2026-10-30), from the events the plugin already receives: the first text chunk, each tool_start to tool_end, and the
whole turn. Core is not involved. Tests drive a REAL streamed turn (chat_sessions.stream_message with local_tool_client.stream_plan_with_tools faked at its boundary,
like test_chat_sessions.py) with a scripted clock, and read the numbers off the stored transcript entry."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402


def _session():
    return {"id": "s1", "title": "t", "repoRoot": "/tmp", "roots": ["/tmp"], "provider": "local", "status": "active", "transcript": [],
            "createdAt": "2026-10-30T00:00:00Z", "updatedAt": "2026-10-30T00:00:00Z"}


class ScriptedClock:
    """Returns the scripted seconds one per call, in order; fails loudly if the code reads the clock more often than the test expects."""
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.values:
            raise AssertionError(f"clock read {self.calls} times, script had fewer")
        return self.values.pop(0)


class TurnTimingsTest(unittest.TestCase):
    def setUp(self):
        self._stats = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats.start()
        self.addCleanup(self._stats.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Path(self.tmp.name)
        chat_sessions._write_sessions(self.storage, {"s1": _session()})

    def turn(self, events, clock):
        def fake_stream(**kwargs):
            for e in events:
                yield e

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream), mock.patch.object(chat_sessions, "_clock", clock):
            list(chat_sessions.stream_message(self.storage, "s1", "hi"))
        return chat_sessions._read_sessions(self.storage)["s1"]["transcript"][-1]

    def test_one_tool_then_an_answer(self):
        # clock reads: turn start 10.0, tool_start 10.5, tool_end 12.5, first chunk 13.0, turn end 14.0
        entry = self.turn([
            {"type": "tool_start", "name": "run_bash", "args": "ls"},
            {"type": "tool_end", "name": "run_bash", "ok": True, "result": "x"},
            {"type": "chunk", "text": "Here "},
            {"type": "chunk", "text": "you go."},
            {"type": "final", "response": "Here you go.", "turnsUsed": 2, "forcedSummary": False, "toolCallLog": [{"tool": "run_bash", "args": {"command": "ls"}, "result": "x"}]},
        ], ScriptedClock(10.0, 10.5, 12.5, 13.0, 14.0))
        self.assertEqual(entry["timings"], {"totalMs": 4000, "firstChunkMs": 3000, "toolMs": [{"name": "run_bash", "ms": 2000}], "toolTotalMs": 2000, "modelMs": 2000})

    def test_two_tools_are_timed_separately_and_summed(self):
        # start 0, tool a 1..4, tool b 5..6, first chunk 7, end 9
        entry = self.turn([
            {"type": "tool_start", "name": "a"}, {"type": "tool_end", "name": "a", "ok": True},
            {"type": "tool_start", "name": "b"}, {"type": "tool_end", "name": "b", "ok": False},
            {"type": "chunk", "text": "done"},
            {"type": "final", "response": "done", "turnsUsed": 3, "forcedSummary": False},
        ], ScriptedClock(0.0, 1.0, 4.0, 5.0, 6.0, 7.0, 9.0))
        t = entry["timings"]
        self.assertEqual(t["toolMs"], [{"name": "a", "ms": 3000}, {"name": "b", "ms": 1000}])
        self.assertEqual((t["totalMs"], t["toolTotalMs"], t["modelMs"], t["firstChunkMs"]), (9000, 4000, 5000, 7000))

    def test_a_turn_without_tools_or_chunks(self):
        entry = self.turn([{"type": "final", "response": "", "turnsUsed": 1, "forcedSummary": False}], ScriptedClock(5.0, 5.25))
        self.assertEqual(entry["timings"], {"totalMs": 250, "firstChunkMs": None, "toolMs": [], "toolTotalMs": 0, "modelMs": 250})

    def test_only_the_first_chunk_reads_the_clock(self):
        clock = ScriptedClock(0.0, 1.0, 2.0)  # start, first chunk, end: a second chunk must not read it
        self.turn([{"type": "chunk", "text": "a"}, {"type": "chunk", "text": "b"}, {"type": "chunk", "text": "c"},
                   {"type": "final", "response": "abc", "turnsUsed": 1, "forcedSummary": False}], clock)
        self.assertEqual(clock.calls, 3)

    def test_a_tool_that_never_ends_is_left_out_and_an_end_without_a_start_is_ignored(self):
        entry = self.turn([
            {"type": "tool_end", "name": "ghost", "ok": True},
            {"type": "tool_start", "name": "hung"},
            {"type": "final", "response": "x", "turnsUsed": 1, "forcedSummary": False},
        ], ScriptedClock(0.0, 1.0, 3.0))
        self.assertEqual(entry["timings"]["toolMs"], [])
        self.assertEqual(entry["timings"]["totalMs"], 3000)

    def test_same_name_tools_pair_in_order(self):
        entry = self.turn([
            {"type": "tool_start", "name": "run_bash"}, {"type": "tool_end", "name": "run_bash", "ok": True},
            {"type": "tool_start", "name": "run_bash"}, {"type": "tool_end", "name": "run_bash", "ok": True},
            {"type": "final", "response": "x", "turnsUsed": 1, "forcedSummary": False},
        ], ScriptedClock(0.0, 1.0, 2.0, 2.0, 5.0, 6.0))
        self.assertEqual([t["ms"] for t in entry["timings"]["toolMs"]], [1000, 3000])

    def test_a_clock_that_goes_backwards_never_stores_a_negative(self):
        entry = self.turn([{"type": "tool_start", "name": "a"}, {"type": "tool_end", "name": "a", "ok": True},
                           {"type": "final", "response": "x", "turnsUsed": 1, "forcedSummary": False}], ScriptedClock(10.0, 9.0, 8.0, 7.0))
        t = entry["timings"]
        self.assertTrue(all(v >= 0 for v in (t["totalMs"], t["toolTotalMs"], t["modelMs"], t["toolMs"][0]["ms"])))

    def test_the_real_clock_gives_whole_non_negative_milliseconds(self):
        entry = self.turn([{"type": "chunk", "text": "x"}, {"type": "final", "response": "x", "turnsUsed": 1, "forcedSummary": False}], chat_sessions._clock)
        t = entry["timings"]
        self.assertIsInstance(t["totalMs"], int)
        self.assertGreaterEqual(t["totalMs"], t["firstChunkMs"])

    def test_the_stored_timings_survive_a_reload_of_the_session(self):
        self.turn([{"type": "final", "response": "x", "turnsUsed": 1, "forcedSummary": False}], ScriptedClock(0.0, 2.0))
        stored = chat_sessions._read_sessions(self.storage)["s1"]["transcript"]
        self.assertEqual(stored[-1]["timings"]["totalMs"], 2000)
        self.assertNotIn("timings", stored[0], "the user entry carries no timings")


if __name__ == "__main__":
    unittest.main()
