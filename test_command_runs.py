"""Owner-approved write commands on the transcript: command_runs.py, the confirm/deny route, the stream wiring and the model-context note (2026-10-25).

Nothing here runs a command: core's runner is replaced by a fake that returns canned replies.

Run: .venv/bin/python -m unittest test_command_runs -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402
import command_runs  # noqa: E402
import internal_api_client  # noqa: E402
import tool_log  # noqa: E402
from test_chat_sessions import _fake_stream, _session  # noqa: E402

SID = "chat-command-test"
ARGV = ["python3", "-m", "inventory", "sales", "email"]
REPO = "/media/model-cache/github/inventory-plugin"


def proposal(pid="cmd-1"):
    return {"proposalId": pid, "repo": REPO, "argv": ARGV, "why": "ingest the newest eBay sale emails"}


def session_with(entry_extra=None):
    entry = {"role": "assistant", "text": "I can ingest them if you confirm."}
    command_runs.record_proposals(entry, [proposal()])
    entry.update(entry_extra or {})
    return {"id": SID, "status": "active", "transcript": [{"role": "user", "text": "ingest"}, entry]}


class RecordProposalsTest(unittest.TestCase):
    def test_accepted_proposals_are_stored_pending_and_junk_is_skipped(self):
        entry = {"role": "assistant", "text": "x"}
        command_runs.record_proposals(entry, [proposal("a"), {"proposalId": ""}, {"proposalId": "b", "argv": "x", "repo": REPO}, None, 5])
        self.assertEqual([p["proposalId"] for p in entry["commandProposals"]], ["a"])
        self.assertEqual((entry["commandProposals"][0]["status"], entry["commandProposals"][0]["decidedAt"]), ("pending", None))
        self.assertEqual(entry["commandProposals"][0]["argv"], ARGV)

    def test_nothing_accepted_leaves_the_entry_alone(self):
        entry = {"role": "assistant", "text": "x"}
        command_runs.record_proposals(entry, [])
        command_runs.record_proposals(entry, None)
        self.assertNotIn("commandProposals", entry)


class DecideTest(unittest.TestCase):
    def test_confirm_and_deny_persist_the_status_and_time(self):
        s = session_with()
        p = command_runs.decide(s, "cmd-1", "confirm", now="2026-10-25T10:00:00+00:00")
        self.assertEqual((p["status"], p["decidedAt"]), ("confirmed", "2026-10-25T10:00:00+00:00"))
        s2 = session_with()
        self.assertEqual(command_runs.decide(s2, "cmd-1", "deny")["status"], "denied")
        self.assertTrue(s2["transcript"][1]["commandProposals"][0]["decidedAt"])

    def test_a_decided_proposal_cannot_be_decided_again_and_unknown_ones_and_actions_are_refused(self):
        s = session_with()
        command_runs.decide(s, "cmd-1", "confirm")
        with self.assertRaisesRegex(ValueError, "already confirmed"):
            command_runs.decide(s, "cmd-1", "confirm")
        with self.assertRaisesRegex(ValueError, "already confirmed"):
            command_runs.decide(s, "cmd-1", "deny")
        with self.assertRaises(LookupError):
            command_runs.decide(s, "nope", "confirm")
        with self.assertRaisesRegex(ValueError, "confirm or deny"):
            command_runs.decide(session_with(), "cmd-1", "run")


class RecordExecutionTest(unittest.TestCase):
    def confirmed(self):
        s = session_with()
        command_runs.decide(s, "cmd-1", "confirm", now="2026-10-25T10:00:00+00:00")
        return s

    def test_a_run_is_stored_scrubbed_and_clipped_and_marks_the_proposal_ran(self):
        s = self.confirmed()
        row = command_runs.record_execution(s, "cmd-1", {
            "ok": True, "exitCode": 0, "durationMs": 1234,
            "stdout": "ingested 2 sales\napi_key=deadbeef Authorization: Bearer abc123\n" + "x" * 9000, "stderr": "password=hunter2",
        })
        entry = s["transcript"][1]
        self.assertEqual(entry["commandExecutions"], [row])
        self.assertEqual((row["exitCode"], row["ok"], row["durationMs"], row["confirmedAt"]), (0, True, 1234, "2026-10-25T10:00:00+00:00"))
        self.assertEqual(entry["commandProposals"][0]["status"], "ran")
        blob = row["stdout"] + row["stderr"]
        for secret in ("deadbeef", "abc123", "hunter2"):
            self.assertNotIn(secret, blob)
        self.assertIn("[hidden]", blob)
        self.assertLessEqual(len(row["stdout"]), tool_log.OUTPUT_MAX)
        self.assertTrue(row["stdout"].endswith("…"))

    def test_a_failing_run_keeps_its_exit_code_and_stderr(self):
        s = self.confirmed()
        row = command_runs.record_execution(s, "cmd-1", {"ok": False, "exitCode": 3, "stdout": "", "stderr": "boom", "durationMs": 5})
        self.assertEqual((row["ok"], row["exitCode"], row["stderr"]), (False, 3, "boom"))
        self.assertEqual(s["transcript"][1]["commandProposals"][0]["status"], "ran")

    def test_a_refusal_or_a_bad_reply_is_stored_as_not_run_and_the_proposal_stays_confirmed(self):
        s = self.confirmed()
        row = command_runs.record_execution(s, "cmd-1", {"ok": False, "refused": "this exact command is not on the allowlist for that repo"})
        self.assertEqual((row["ok"], row["exitCode"]), (False, None))
        self.assertIn("not on the allowlist", row["stderr"])
        self.assertEqual(s["transcript"][1]["commandProposals"][0]["status"], "confirmed")
        row2 = command_runs.record_execution(s, "cmd-1", None)
        self.assertIn("no usable reply", row2["stderr"])
        with self.assertRaises(LookupError):
            command_runs.record_execution(s, "nope", {})


class ContextNoteTest(unittest.TestCase):
    def test_each_state_reads_plainly_for_the_model(self):
        s = session_with()
        entry = s["transcript"][1]
        self.assertIn("awaiting the owner's confirmation (not run)", command_runs.context_note(entry))
        command_runs.decide(s, "cmd-1", "deny")
        self.assertIn("owner denied the proposed command (not run)", command_runs.context_note(entry))
        s = session_with()
        entry = s["transcript"][1]
        command_runs.decide(s, "cmd-1", "confirm")
        self.assertIn("has not reported back yet", command_runs.context_note(entry))
        command_runs.record_execution(s, "cmd-1", {"ok": True, "exitCode": 0, "stdout": "ingested 2 sales", "stderr": ""})
        note = command_runs.context_note(entry)
        self.assertIn("Command run with the owner's approval: python3 -m inventory sales email -> exit 0; output: ingested 2 sales", note)

    def test_an_entry_without_proposals_adds_nothing(self):
        self.assertEqual(command_runs.context_note({"role": "assistant", "text": "x"}), "")

    def test_the_replayed_transcript_carries_the_note_after_the_assistant_text(self):
        s = session_with()
        s.update({"provider": "local", "repoRoot": "/x", "roots": ["/x"]})
        command_runs.decide(s, "cmd-1", "confirm")
        command_runs.record_execution(s, "cmd-1", {"ok": True, "exitCode": 0, "stdout": "ingested 2 sales", "stderr": ""})
        messages = chat_sessions._build_local_messages(s, "how did it go?")
        assistant = [m for m in messages if m["role"] == "assistant"][0]["content"]
        self.assertTrue(assistant.startswith("I can ingest them if you confirm."))
        self.assertIn("exit 0; output: ingested 2 sales", assistant)


class StreamStoresProposalsTest(unittest.TestCase):
    def test_a_final_event_with_commandProposals_lands_pending_on_the_assistant_entry(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(chat_sessions.model_stats_client, "record_call"):
            storage = Path(tmp)
            session = _session()
            session["provider"], session["status"] = "local", "active"
            chat_sessions._write_sessions(storage, {session["id"]: session})
            events = [{"type": "chunk", "text": "Waiting for you."}, {"type": "final", "response": "Waiting for you.", "turnsUsed": 2, "commandProposals": [proposal()]}]
            with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
                list(chat_sessions.stream_message(storage, session["id"], "ingest the sales"))
            entry = chat_sessions.get_session(storage, session["id"])["transcript"][-1]
            self.assertEqual([(p["proposalId"], p["status"]) for p in entry["commandProposals"]], [("cmd-1", "pending")])
            events[1].pop("commandProposals")
            with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
                list(chat_sessions.stream_message(storage, session["id"], "again"))
            self.assertNotIn("commandProposals", chat_sessions.get_session(storage, session["id"])["transcript"][-1])


class CommandRouteTest(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()
        base = _session()
        base.update({"id": SID, "provider": "local", "status": "active"})
        base["transcript"] = session_with()["transcript"]
        chat_sessions._write_sessions(self.storage, {SID: base})

    def post(self, pid, action, sid=SID):
        return self.client.post(f"/api/chat/{sid}/command/{pid}/{action}")

    def stored_entry(self):
        return chat_sessions.get_session(self.storage, SID)["transcript"][1]

    def test_confirm_persists_the_decision_before_running_then_stores_the_result(self):
        seen = {}

        def fake_run(argv, repo, proposal_id, confirmed_at, session_id=None):
            on_disk = chat_sessions.get_session(self.storage, SID)["transcript"][1]["commandProposals"][0]
            seen.update(args=(argv, repo, proposal_id, confirmed_at, session_id), status_on_disk=on_disk["status"])
            return {"ok": True, "exitCode": 0, "stdout": "ingested 2 sales", "stderr": "", "durationMs": 40}

        with mock.patch.object(internal_api_client, "run_command", side_effect=fake_run):
            reply = self.post("cmd-1", "confirm")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(seen["status_on_disk"], "confirmed", "the confirmation was already stored when the runner was called")
        argv, repo, pid, confirmed_at, sid = seen["args"]
        self.assertEqual((argv, repo, pid, sid), (ARGV, REPO, "cmd-1", SID))
        self.assertTrue(confirmed_at)
        entry = self.stored_entry()
        self.assertEqual(entry["commandProposals"][0]["status"], "ran")
        self.assertEqual(entry["commandExecutions"][0]["stdout"], "ingested 2 sales")
        self.assertEqual(reply.get_json()["session"]["transcript"][1]["commandExecutions"][0]["exitCode"], 0)

    def test_deny_never_calls_the_runner(self):
        with mock.patch.object(internal_api_client, "run_command") as run:
            reply = self.post("cmd-1", "deny")
        self.assertEqual(reply.status_code, 200)
        run.assert_not_called()
        self.assertEqual(self.stored_entry()["commandProposals"][0]["status"], "denied")
        self.assertNotIn("commandExecutions", self.stored_entry())

    def test_a_second_click_is_409_and_runs_nothing_twice(self):
        with mock.patch.object(internal_api_client, "run_command", return_value={"ok": True, "exitCode": 0, "stdout": "", "stderr": ""}) as run:
            self.assertEqual(self.post("cmd-1", "confirm").status_code, 200)
            self.assertEqual(self.post("cmd-1", "confirm").status_code, 409)
            self.assertEqual(self.post("cmd-1", "deny").status_code, 409)
        self.assertEqual(run.call_count, 1)

    def test_unknown_proposal_session_and_action_are_refused(self):
        with mock.patch.object(internal_api_client, "run_command") as run:
            self.assertEqual(self.post("nope", "confirm").status_code, 404)
            self.assertEqual(self.post("cmd-1", "confirm", sid="no-such-session").status_code, 404)
            self.assertEqual(self.post("cmd-1", "run").status_code, 400)
        run.assert_not_called()
        self.assertEqual(self.stored_entry()["commandProposals"][0]["status"], "pending")

    def test_a_core_refusal_or_an_unreachable_core_is_stored_and_the_proposal_stays_confirmed(self):
        with mock.patch.object(internal_api_client, "run_command", side_effect=internal_api_client.InternalApiError("core is down")):
            reply = self.post("cmd-1", "confirm")
        self.assertEqual(reply.status_code, 200)
        entry = self.stored_entry()
        self.assertEqual(entry["commandProposals"][0]["status"], "confirmed")
        self.assertIn("core is down", entry["commandExecutions"][0]["stderr"])
        self.assertIsNone(entry["commandExecutions"][0]["exitCode"])


class RunCommandClientTest(unittest.TestCase):
    def test_it_posts_the_contract_body_with_the_internal_token_and_returns_the_reply(self):
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"ok": True, "exitCode": 0}
        with mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"}), mock.patch.object(internal_api_client.requests, "post", return_value=reply) as post:
            out = internal_api_client.run_command(ARGV, REPO, "cmd-1", "2026-10-25T10:00:00+00:00", "s-1")
        self.assertEqual(out, {"ok": True, "exitCode": 0})
        args, kwargs = post.call_args
        self.assertTrue(args[0].endswith("/api/internal/chat/run-command"))
        self.assertEqual(kwargs["json"], {"argv": ARGV, "repo": REPO, "proposalId": "cmd-1", "confirmedAt": "2026-10-25T10:00:00+00:00", "sessionId": "s-1"})
        self.assertEqual(kwargs["headers"]["X-Internal-Token"], "tok")

    def test_a_transport_error_raises_internal_api_error(self):
        with mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"}), mock.patch.object(internal_api_client.requests, "post", side_effect=internal_api_client.requests.ConnectionError("down")):
            with self.assertRaises(internal_api_client.InternalApiError):
                internal_api_client.run_command(ARGV, REPO, "cmd-1", "t")


if __name__ == "__main__":
    unittest.main()
