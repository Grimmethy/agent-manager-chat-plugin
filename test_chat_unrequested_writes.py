"""Unrequested write tool calls in the chat turn audit (2026-10-30, replan of HUB0040).

The earlier attempt scanned the assistant's reply and was never called with the turn's tools, so unrequestedWrites was always 0 in production. These tests drive a REAL
streamed turn (chat_sessions.stream_message with local_tool_client.stream_plan_with_tools faked at its boundary, like test_chat_sessions.py), with a toolCallLog shaped
like core's, and read the audit back off the stored transcript entry."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_audit  # noqa: E402
import chat_sessions  # noqa: E402
import tool_log  # noqa: E402


def _session():
    return {"id": "s1", "title": "t", "repoRoot": "/tmp", "roots": ["/tmp"], "provider": "local", "status": "active", "transcript": [],
            "createdAt": "2026-10-30T00:00:00Z", "updatedAt": "2026-10-30T00:00:00Z"}


def _call(tool, **args):
    return {"tool": tool, "args": args, "result": {"ok": True}}


VERDICT = _call("record_branch_verdict", branch="agent/x", verdict="merge", reasons=["tests pass"], sha="abc")
FILE_TASK = _call("queue_reviewed_task", title="Fix y", description="do it")
WITHDRAW = _call("withdraw_task", taskId="t-1")
LONG_BG = _call("run_bash", command="cd /repo && " + "echo padding && " * 20 + "flock /tmp/l sleep 600 &")
QUICK_BG = _call("run_bash", command="nohup node x.js")
READ_ONLY = _call("run_bash", command="git log --oneline -3")


class PureRulesTest(unittest.TestCase):
    def names(self, message, *calls):
        return chat_audit.unrequested_writes(message, tool_log.summarize_tool_calls(list(calls)))

    def test_prohibition_flags_only_the_forbidden_kind(self):
        self.assertEqual(self.names("Do NOT record any verdict yet, just read it", VERDICT), ["record_branch_verdict"])
        self.assertEqual(self.names("don't record anything", VERDICT, FILE_TASK), ["record_branch_verdict", "queue_reviewed_task"])
        self.assertEqual(self.names("please do not file a task for this", VERDICT, FILE_TASK), ["queue_reviewed_task"])
        self.assertEqual(self.names("verify it without recording", VERDICT), ["record_branch_verdict"])

    def test_only_report_forbids_every_write_tool(self):
        self.assertEqual(self.names("only report what you find", VERDICT, WITHDRAW), ["record_branch_verdict", "withdraw_task"])

    def test_prohibition_without_a_write_is_not_flagged(self):
        self.assertEqual(self.names("do not record anything", READ_ONLY), [])
        self.assertEqual(self.names("do not record anything"), [])

    def test_unprompted_task_filing_or_withdrawal_is_flagged_and_asked_for_filing_is_not(self):
        self.assertEqual(self.names("verify the flock branch", FILE_TASK), ["queue_reviewed_task"])
        self.assertEqual(self.names("verify the flock branch", WITHDRAW), ["withdraw_task"])
        self.assertEqual(self.names("file a task to fix it", FILE_TASK), [])
        self.assertEqual(self.names("please requeue that one", WITHDRAW), [])
        self.assertEqual(self.names("retire the hub", WITHDRAW), [])

    def test_a_verdict_without_a_prohibition_is_not_flagged(self):
        self.assertEqual(self.names("verify the branch and record a verdict", VERDICT), [])
        self.assertEqual(self.names("there is no record of this in the log, check", VERDICT), [], "'no record of' is not a prohibition")

    def test_background_run_bash_is_flagged_unless_the_user_asked_for_it(self):
        self.assertEqual(self.names("verify the flock branch", LONG_BG), ["run_bash"])
        self.assertEqual(self.names("verify the flock branch", QUICK_BG), ["run_bash"])
        self.assertEqual(self.names("start it in the background", LONG_BG), [])
        self.assertEqual(self.names("verify the flock branch", READ_ONLY), [])

    def test_the_replys_words_do_not_matter_only_the_user_message(self):
        self.assertEqual(chat_audit.audit_turn("I queued a task for you", user_message="verify", tools=tool_log.summarize_tool_calls([FILE_TASK]))["unrequestedWrites"], 1)


class ToolLogBackgroundTest(unittest.TestCase):
    def test_trailing_ampersand_is_seen_even_when_the_stored_args_are_clipped(self):
        entry = tool_log.summarize_tool_calls([LONG_BG])[0]
        self.assertTrue(entry["background"])
        self.assertTrue(entry["args"].endswith("…"), "the stored args were clipped, so the flag had to come from the full command")
        self.assertNotIn("&\"", entry["args"][-5:])

    def test_only_a_single_trailing_ampersand_or_nohup_counts(self):
        self.assertTrue(tool_log.is_background_command("sleep 9 &"))
        self.assertTrue(tool_log.is_background_command("nohup sleep 9"))
        self.assertFalse(tool_log.is_background_command("make && make test"))
        self.assertFalse(tool_log.is_background_command("echo a && "))
        self.assertFalse(tool_log.is_background_command("git log --oneline"))
        self.assertFalse(tool_log.is_background_command(None))

    def test_other_tools_and_plain_commands_get_no_background_key(self):
        out = tool_log.summarize_tool_calls([READ_ONLY, FILE_TASK])
        self.assertTrue(all("background" not in e for e in out))


class RealTurnTest(unittest.TestCase):
    def setUp(self):
        self._stats = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats.start()
        self.addCleanup(self._stats.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Path(self.tmp.name)
        chat_sessions._write_sessions(self.storage, {"s1": _session()})

    def turn(self, user_message, tool_calls, reply="Done."):
        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": reply}
            yield {"type": "final", "response": reply, "turnsUsed": 2, "forcedSummary": False, "toolCallLog": tool_calls}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage, "s1", user_message))
        return chat_sessions._read_sessions(self.storage)["s1"]["transcript"][-1]["audit"]

    def test_prohibition_plus_a_recorded_verdict_is_flagged_and_counts_as_a_violation(self):
        audit = self.turn("Do NOT record any verdict yet, read the branch", [READ_ONLY, VERDICT])
        self.assertEqual(audit["unrequestedWrites"], 1)
        self.assertEqual(audit["unrequestedWriteTools"], ["record_branch_verdict"])
        self.assertTrue(audit["has_violations"])

    def test_prohibition_without_a_write_is_clean(self):
        audit = self.turn("Do NOT record any verdict yet, read the branch", [READ_ONLY])
        self.assertEqual(audit["unrequestedWrites"], 0)
        self.assertFalse(audit["has_violations"])

    def test_unprompted_filing_and_background_process_in_one_turn_are_both_flagged(self):
        audit = self.turn("verify the flock branch", [FILE_TASK, LONG_BG])
        self.assertEqual(audit["unrequestedWriteTools"], ["queue_reviewed_task", "run_bash"])
        self.assertTrue(audit["has_violations"])

    def test_asked_for_filing_is_clean(self):
        audit = self.turn("file a task to fix the parser", [FILE_TASK])
        self.assertEqual(audit["unrequestedWrites"], 0)

    def test_a_turn_with_no_tools_keeps_the_existing_audit_keys_and_adds_zero(self):
        audit = self.turn("hello", [], reply="Hi there.")
        self.assertEqual(audit["unrequestedWrites"], 0)
        self.assertEqual(audit["unrequestedWriteTools"], [])
        for key in ("youre_right_count", "context_log_mentions", "has_violations"):
            self.assertIn(key, audit)


class ExistingAuditUnchangedTest(unittest.TestCase):
    def test_the_existing_violations_still_count_and_a_clean_turn_stays_clean(self):
        audit = chat_audit.audit_turn("You're absolutely right.", has_user_message_after_first=False, user_message="hello", tools=[])
        self.assertGreater(audit["youre_right_count"], 0)
        self.assertTrue(audit["has_violations"])
        self.assertFalse(chat_audit.audit_turn("Fine.", user_message="hello", tools=[])["has_violations"])
        self.assertFalse(chat_audit.audit_turn("Fine.")["has_violations"], "old callers that pass neither argument behave as before")


if __name__ == "__main__":
    unittest.main()
