"""A local turn whose last model message was only the TASK-REF/CONTEXT-LOG footer must not be
stored as a normal reply (seen live 2026-11-01: 102 streamed chunks, stored 'TASK-REF: HUB0293').
The answer streamed before the footer is recovered deterministically (no second model call, so no
write tool can re-run); if there is none the turn stores an explicit notice. Either way the turn
carries `recovered_reply`.

Run: .venv/bin/python -m unittest tests.test_chat_sessions_recovered_reply -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402

ANSWER = "The branch adds 3 files and the tests import candidate-dedupe.js, which exports dedupe."


def _run(events):
    with tempfile.TemporaryDirectory() as tmp:
        storage = Path(tmp)
        root = str(app.CORE_REPO_ROOT)
        session = {"id": "chat-t", "provider": "local", "repoRoot": root, "roots": [root],
                   "transcript": [], "status": "active"}
        chat_sessions._write_sessions(storage, {"chat-t": session})

        def fake(**kwargs):
            yield from events
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake):
            list(chat_sessions.stream_message(storage, "chat-t", "q"))
        turns = chat_sessions._read_sessions(storage)["chat-t"]["transcript"]
        return [t for t in turns if t["role"] == "assistant"][-1]


def _final(response, **kw):
    return {"type": "final", "response": response, "turnsUsed": 3, "forcedSummary": False, **kw}


class FooterOnlyReply(unittest.TestCase):
    def test_answer_after_last_tool_is_recovered(self):
        t = _run([
            {"type": "chunk", "text": "Let me look at the diff."},
            {"type": "tool_start", "name": "run_bash", "args": "git diff"},
            {"type": "tool_end", "name": "run_bash", "ok": True, "result": "x"},
            {"type": "chunk", "text": ANSWER},
            {"type": "chunk", "text": "CONTEXT-LOG: investigated the branch.\nTASK-REF: HUB0293"},
            _final("TASK-REF: HUB0293"),
        ])
        self.assertIn(ANSWER, t["text"])
        self.assertNotIn("Let me look at the diff", t["text"])   # pre-tool narration is not the answer
        self.assertNotIn("investigated the branch", t["text"])    # CONTEXT-LOG body is not the answer
        self.assertEqual(t["recovered_reply"], "stream")
        self.assertEqual(t["display_text"], ANSWER)               # footer stripped for display

    def test_no_answer_anywhere_stores_notice_not_bare_footer(self):
        t = _run([
            {"type": "tool_start", "name": "run_bash", "args": "ls"},
            {"type": "tool_end", "name": "run_bash", "ok": True, "result": "x"},
            _final("TASK-REF: HUB0293"),
        ])
        self.assertEqual(t["recovered_reply"], "empty")
        self.assertIn(chat_sessions.EMPTY_REPLY_NOTICE, t["text"])
        self.assertEqual(t["display_text"], chat_sessions.EMPTY_REPLY_NOTICE)

    def test_wholly_empty_final_also_flagged(self):
        t = _run([_final("")])
        self.assertEqual(t["recovered_reply"], "empty")

    def test_normal_reply_untouched_and_unflagged(self):
        t = _run([{"type": "chunk", "text": ANSWER}, _final(ANSWER + "\n\nTASK-REF: N/A")])
        self.assertNotIn("recovered_reply", t)
        self.assertIn(ANSWER, t["text"])

    def test_recovery_makes_no_second_model_call(self):
        calls = []

        def fake(**kwargs):
            calls.append(1)
            yield {"type": "chunk", "text": ANSWER}
            yield _final("TASK-REF: x")
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            root = str(app.CORE_REPO_ROOT)
            chat_sessions._write_sessions(storage, {"chat-t": {
                "id": "chat-t", "provider": "local", "repoRoot": root, "roots": [root],
                "transcript": [], "status": "active"}})
            with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake):
                list(chat_sessions.stream_message(storage, "chat-t", "q"))
        self.assertEqual(len(calls), 1)


class Helpers(unittest.TestCase):
    def test_visible_text(self):
        self.assertEqual(chat_sessions._visible_text("TASK-REF: A"), "")
        self.assertEqual(chat_sessions._visible_text("hi\n\nCONTEXT-LOG: x\nTASK-REF: y"), "hi")
        self.assertEqual(chat_sessions._visible_text(""), "")

    def test_bare_footer_is_stripped_from_display(self):
        self.assertEqual(chat_sessions._strip_footer("TASK-REF: HUB0293").strip(), "")

    def test_recover_cuts_at_first_marker_even_without_newline(self):
        self.assertEqual(chat_sessions._recover_from_stream("done.CONTEXT-LOG: z"), "done.")


if __name__ == "__main__":
    unittest.main()
