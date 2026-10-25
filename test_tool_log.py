"""tool_log.summarize_tool_calls and its storage on the assistant transcript entry (chat_sessions.stream_message)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_sessions  # noqa: E402
import tool_log  # noqa: E402
from test_chat_sessions import _fake_stream, _session  # noqa: E402


class SummarizeToolCallsTest(unittest.TestCase):
    def test_keeps_name_args_and_outcome(self):
        out = tool_log.summarize_tool_calls([
            {"tool": "run_bash", "args": {"command": "ls /tmp"}, "result": "a\nb\n"},
            {"tool": "read_file", "args": {"path": "/nope"}, "result": {"error": "no such file"}},
        ])
        self.assertEqual(out[0], {"name": "run_bash", "args": '{"command": "ls /tmp"}', "ok": True, "summary": "4 chars returned"})
        self.assertEqual(out[1]["ok"], False)
        self.assertEqual(out[1]["summary"], "no such file")

    def test_long_arguments_and_errors_are_clipped(self):
        out = tool_log.summarize_tool_calls([{"tool": "run_bash", "args": {"command": "x" * 500}, "result": {"error": "e" * 500}}])
        self.assertLessEqual(len(out[0]["args"]), tool_log.MAX_TEXT)
        self.assertLessEqual(len(out[0]["summary"]), tool_log.MAX_TEXT)
        self.assertTrue(out[0]["args"].endswith("…"))

    def test_secret_argument_names_and_inline_secrets_are_hidden(self):
        out = tool_log.summarize_tool_calls([
            {"tool": "t", "args": {"api_token": "abc123", "path": "/x"}, "result": "ok"},
            {"tool": "run_bash", "args": {"command": 'curl -H "Authorization: Bearer abc123" https://h/?password=hunter2'}, "result": "ok"},
        ])
        self.assertNotIn("abc123", out[0]["args"] + out[1]["args"])
        self.assertNotIn("hunter2", out[1]["args"])
        self.assertIn("[hidden]", out[0]["args"])

    def test_a_success_stores_only_its_size_never_the_content(self):
        out = tool_log.summarize_tool_calls([{"tool": "read_file", "args": {"path": "/s"}, "result": "token=SECRETVALUE"}])
        self.assertNotIn("SECRETVALUE", repr(out))

    def test_capped_at_fifty_and_non_lists_and_junk_entries_are_safe(self):
        self.assertEqual(len(tool_log.summarize_tool_calls([{"tool": "t", "args": {}, "result": "x"}] * 80)), tool_log.MAX_TOOLS)
        self.assertEqual(tool_log.summarize_tool_calls(None), [])
        self.assertEqual(tool_log.summarize_tool_calls("nope"), [])
        self.assertEqual(len(tool_log.summarize_tool_calls([None, 3, {"tool": "t"}])), 1)


class StoredOnTheTranscriptTest(unittest.TestCase):
    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _run(self, events):
        session = _session()
        session["provider"] = "local"
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
            list(chat_sessions.stream_message(self.storage_dir, session["id"], "hi"))
        return chat_sessions._read_session(self.storage_dir, session["id"])["transcript"][-1]

    def test_the_assistant_entry_carries_the_tools_the_turn_ran(self):
        entry = self._run([
            {"type": "chunk", "text": "done"},
            {"type": "final", "response": "done", "turnsUsed": 2,
             "toolCallLog": [{"tool": "run_bash", "args": {"command": "ls"}, "result": "a"}]},
        ])
        self.assertEqual(entry["tools"], [{"name": "run_bash", "args": '{"command": "ls"}', "ok": True, "summary": "1 chars returned"}])

    def test_a_turn_without_tool_calls_has_no_tools_key(self):
        entry = self._run([{"type": "chunk", "text": "hi"}, {"type": "final", "response": "hi", "turnsUsed": 1, "toolCallLog": []}])
        self.assertNotIn("tools", entry)


if __name__ == "__main__":
    unittest.main()
