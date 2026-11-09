"""tool_log.summarize_tool_calls and its storage on the assistant transcript entry (chat_sessions.stream_message)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
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


class VerdictLedgerTest(unittest.TestCase):
    """chat_audit.build_verdict_ledger -- the deterministic ledger that the
    post-turn audit appends to the reply. These tests cover the three
    rejection reasons from the earlier attempts:

    1. ok=false entries must NOT be counted (the ledger must skip them).
    2. Last-wins per (project, branch) with a "changed N times" note.
    3. A model text whose counts disagree gets a "Correction:" line.
    4. A turn with no verdict calls gets no block appended.

    The tests drive a REAL streamed turn through chat_sessions.stream_message
    with a fake _stream_local and a result_box['tools'] produced by
    summarize_tool_calls from a realistic core toolCallLog -- not hand-built
    fixtures that bypass it.
    """

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _session(self):
        return {
            "id": "chat-verdict-test",
            "provider": "local",
            "repoRoot": str(app.CORE_REPO_ROOT),
            "roots": [str(app.CORE_REPO_ROOT)],
            "transcript": [],
            "status": "active",
        }

    def _run_turn(self, tool_call_log, reply="Done."):
        """Run a real streamed turn and return the stored transcript entry."""
        session = self._session()
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})

        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": reply}
            yield {"type": "final", "response": reply, "turnsUsed": 2,
                   "forcedSummary": False, "toolCallLog": tool_call_log}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session["id"], "verify the branches"))
        return chat_sessions._read_session(self.storage_dir, session["id"])["transcript"][-1]

    def test_ok_false_entries_are_skipped(self):
        """The ledger must NOT count a failed record_branch_verdict call.

        A call with ok=false means nothing was recorded -- claiming a verdict
        in the summary would be false. This is the bug that the earlier
        rejection (2026-10-31) identified: build_from_tool_entries never
        checked entry['ok'].
        """
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": "tokenfold/agent/observability-fix-ac-5", "verdict": "merge", "project": "tokenfold"}, "result": {"ok": True}},
            {"tool": "record_branch_verdict", "args": {"branch": "tokenfold/agent/other-fix", "verdict": "merge", "project": "tokenfold"}, "result": {"ok": False, "error": "the branch head moved; nothing was recorded"}},
        ]
        entry = self._run_turn(tool_call_log, reply="All branches approved for merge.")
        tools = entry.get("tools", [])
        # The ledger should only count the ok=true entry.
        self.assertEqual(tools[0]["ok"], True)
        self.assertEqual(tools[1]["ok"], False)
        # The stored text should NOT claim 2 merges -- only 1 (the ok=true one).
        text = entry["text"]
        self.assertIn("Verdict ledger", text)
        # The correction line should appear because the model said "2 merge" but ledger has 1.
        self.assertIn("Correction: the cards show 1 merge, 0 needs-work, 0 discard", text)
        # The failed entry should NOT appear in the ledger.
        self.assertNotIn("other-fix", text)

    def test_last_wins_and_changed_note(self):
        """A branch recorded twice: only the LAST verdict counts, and a
        'changed N times' note appears."""
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": "uxforge/agent/scenario-validator", "verdict": "merge", "project": "uxforge"}, "result": {"ok": True}},
            {"tool": "record_branch_verdict", "args": {"branch": "uxforge/agent/scenario-validator", "verdict": "needs-work", "project": "uxforge"}, "result": {"ok": True}},
        ]
        entry = self._run_turn(tool_call_log, reply="1 branch approved for merge, 0 needs-work.")
        text = entry["text"]
        # The ledger should show needs-work (last value), not merge.
        self.assertIn("uxforge/agent/scenario-validator: needs-work", text)
        self.assertNotIn("uxforge/agent/scenario-validator: merge", text)
        # The changed note should appear.
        self.assertIn("(changed 1 time during this turn)", text)
        # The correction line should appear because the model said "1 merge" but ledger has 0 merge, 1 needs-work.
        self.assertIn("Correction: the cards show 0 merge, 1 needs-work, 0 discard", text)

    def test_no_verdict_calls_means_no_block(self):
        """A turn with no record_branch_verdict calls should get no verdict block appended."""
        tool_call_log = [
            {"tool": "run_bash", "args": {"command": "ls"}, "result": "file1\nfile2"},
        ]
        entry = self._run_turn(tool_call_log, reply="Done.")
        text = entry["text"]
        self.assertNotIn("Verdict ledger", text)
        self.assertNotIn("Correction", text)

    def test_three_verdicts_one_overwritten(self):
        """A realistic scenario: three branches, one overwritten. The ledger
        shows the correct counts and the changed note only on the overwritten one."""
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": "tokenfold/agent/observability-fix-ac-5", "verdict": "merge", "project": "tokenfold"}, "result": {"ok": True}},
            {"tool": "record_branch_verdict", "args": {"branch": "uxforge/agent/scenario-validator", "verdict": "merge", "project": "uxforge"}, "result": {"ok": True}},
            {"tool": "record_branch_verdict", "args": {"branch": "uxforge/agent/scenario-validator", "verdict": "needs-work", "project": "uxforge"}, "result": {"ok": True}},
        ]
        entry = self._run_turn(tool_call_log, reply="2 branches approved for merge, 0 needs-work.")
        text = entry["text"]
        # Counts should be 1 merge, 1 needs-work.
        self.assertIn("Correction: the cards show 1 merge, 1 needs-work, 0 discard", text)
        # uxforge/agent/scenario-validator should be needs-work (last value) with changed note.
        self.assertIn("uxforge/agent/scenario-validator: needs-work (changed 1 time during this turn)", text)
        # tokenfold/agent/observability-fix-ac-5 should be merge with no changed note.
        self.assertIn("tokenfold/agent/observability-fix-ac-5: merge", text)
        self.assertNotIn("observability-fix-ac-5: merge (changed", text)


if __name__ == "__main__":
    unittest.main()
