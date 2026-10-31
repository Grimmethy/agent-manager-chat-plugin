"""Tests for chat_sessions.py's _stream_local() footer logic -- the three distinct
forced-summary causes (2026-09-15, Grimmethy: "Different stop reasons should absolutely
be distinguishable. It keeps the system understandable.") and the false-message bug this
replaces: 'voluntary-stop' used to share 'turns'' "ran out of its 100-turn tool budget"
text even when the real turnsUsed was nowhere near the cap (confirmed live via
model-stats.db: 2 of 3 real Chat calls in one session showed that exact false message).

Run: .venv/bin/python -m unittest test_chat_sessions -v
"""
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402
import app  # noqa: E402


def _session():
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": "chat-test-session",
        "provider": "local",
        "repoRoot": root,
        "roots": [root],
        "transcript": [],
    }


def _fake_stream(events):
    def _gen(**kwargs):
        yield from events
    return _gen


class StreamLocalFooterTest(unittest.TestCase):
    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)

    def _run(self, events):
        session = _session()
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", _fake_stream(events)):
            return "".join(chat_sessions._stream_local(session, "hi"))

    def test_context_reason_says_context_room(self):
        out = self._run([
            {"type": "chunk", "text": "answer"},
            {"type": "final", "response": "answer", "turnsUsed": 3,
             "forcedSummary": True, "forcedSummaryReason": "context"},
        ])
        self.assertIn("context room", out)
        self.assertNotIn("100-turn", out)

    def test_turns_reason_says_100_turn_budget(self):
        out = self._run([
            {"type": "chunk", "text": "answer"},
            {"type": "final", "response": "answer", "turnsUsed": 100,
             "forcedSummary": True, "forcedSummaryReason": "turns"},
        ])
        self.assertIn("100-turn tool budget", out)
        self.assertNotIn("context room", out)

    def test_voluntary_stop_gets_no_misleading_footer(self):
        # The bug this test guards: turnsUsed=2 (nowhere near the 100-turn cap), but the
        # model just didn't emit a RESOLUTION: line on its first try. No exhaustion
        # occurred at all -- the old code claimed "ran out of its 100-turn tool budget"
        # here, which was simply false.
        out = self._run([
            {"type": "chunk", "text": "pong"},
            {"type": "final", "response": "pong", "turnsUsed": 2,
             "forcedSummary": True, "forcedSummaryReason": "voluntary-stop"},
        ])
        self.assertEqual(out, "pong")
        self.assertNotIn("100-turn", out)
        self.assertNotIn("context room", out)
        self.assertNotIn("Ran out of", out)

    def test_clean_finish_with_no_forced_summary_gets_no_footer(self):
        out = self._run([
            {"type": "chunk", "text": "answer"},
            {"type": "final", "response": "answer", "turnsUsed": 2, "forcedSummary": False},
        ])
        self.assertEqual(out, "answer")


class StreamMessageStoresCleanedTextTest(unittest.TestCase):
    """Real bug, caught live testing the context-log feature: stream_message used to
    persist the raw streamed concatenation as the transcript's final text, never the
    cleaned local-tool-client.js response -- every SIDE-FINDING:/AMPLIFY:/CONTEXT-LOG:
    marker silently leaked into the stored/visible chat transcript. Unnoticed for
    SIDE-FINDING/AMPLIFY (rare opt-in), immediately obvious once CONTEXT-LOG made a
    marker mandatory on every turn."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _seed_active_session(self):
        session = _session()
        session["provider"] = "local"
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        return session["id"]

    def test_stream_message_stores_the_cleaned_response_not_the_raw_stream(self):
        session_id = self._seed_active_session()
        raw = "Real answer.\n\nCONTEXT-LOG: distilled summary\nTASK-REF: t-1"
        cleaned = "Real answer."

        def fake_stream(**kwargs):
            # Simulates local-tool-client.js: streams the RAW text chunk by chunk (as
            # generated, before any marker stripping), then a final event whose
            # `response` field IS the already-cleaned text.
            for ch in raw:
                yield {"type": "chunk", "text": ch}
            yield {"type": "final", "response": cleaned, "turnsUsed": 2, "forcedSummary": False}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        self.assertEqual(stored["transcript"][-1]["text"], cleaned)
        self.assertNotIn("CONTEXT-LOG", stored["transcript"][-1]["text"])

    def test_stream_message_still_appends_the_footer_to_the_cleaned_text(self):
        session_id = self._seed_active_session()

        def fake_stream(**kwargs):
            for ch in "pong":
                yield {"type": "chunk", "text": ch}
            yield {"type": "final", "response": "pong", "turnsUsed": 2,
                   "forcedSummary": True, "forcedSummaryReason": "voluntary-stop"}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        # voluntary-stop gets no footer at all (see StreamLocalFooterTest) -- confirms
        # the fix didn't accidentally start adding one.
        self.assertEqual(stored["transcript"][-1]["text"], "pong")


class StreamLocalContextLogSessionIdTest(unittest.TestCase):
    """Confirms _stream_local threads session['id'] through to
    stream_plan_with_tools(context_log_session_id=...) -- the one wiring hop this
    module itself owns (the rest is internal_api_client.py/local_tool_client.py)."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)

    def test_session_id_passed_through(self):
        session = _session()
        captured = {}

        def fake_stream(**kwargs):
            captured.update(kwargs)
            yield {"type": "final", "response": "ok", "turnsUsed": 1, "forcedSummary": False}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            "".join(chat_sessions._stream_local(session, "hi"))
        self.assertEqual(captured.get("context_log_session_id"), "chat-test-session")


class BuildLocalMessagesTrimmingTest(unittest.TestCase):
    def _session_with_transcript(self, n_turns):
        session = _session()
        session["transcript"] = [
            {"role": "assistant" if i % 2 else "user", "text": f"turn {i}"}
            for i in range(n_turns)
        ]
        return session

    def test_short_transcript_is_replayed_in_full_unaffected(self):
        session = self._session_with_transcript(6)
        messages = chat_sessions._build_local_messages(session, "new message")
        # system + 6 transcript turns + the new message, no trimming block
        self.assertEqual(len(messages), 1 + 6 + 1)
        self.assertNotIn("earlier turn(s)", messages[1]["content"])

    def test_long_transcript_trims_to_the_bound_with_no_note_available(self):
        n = chat_sessions.CHAT_RECENT_TURNS_KEPT + 10
        session = self._session_with_transcript(n)
        with mock.patch.object(chat_sessions, "_read_capped_context_log", return_value=""):
            messages = chat_sessions._build_local_messages(session, "new message")
        # no note available -> no stand-in block gets inserted, just the bounded replay
        self.assertEqual(len(messages), 1 + chat_sessions.CHAT_RECENT_TURNS_KEPT + 1)
        self.assertEqual(messages[1]["content"], "turn 10")  # first of the KEPT window

    def test_long_transcript_trims_and_injects_the_capped_note_as_a_stand_in(self):
        n = chat_sessions.CHAT_RECENT_TURNS_KEPT + 10
        session = self._session_with_transcript(n)
        with mock.patch.object(chat_sessions, "_read_capped_context_log", return_value="compacted prior context here"):
            messages = chat_sessions._build_local_messages(session, "new message")
        self.assertEqual(len(messages), 1 + 1 + chat_sessions.CHAT_RECENT_TURNS_KEPT + 1)
        self.assertIn("10 earlier turn(s)", messages[1]["content"])
        self.assertIn("compacted prior context here", messages[1]["content"])

    def test_real_transcript_array_is_never_mutated_by_trimming(self):
        n = chat_sessions.CHAT_RECENT_TURNS_KEPT + 10
        session = self._session_with_transcript(n)
        with mock.patch.object(chat_sessions, "_read_capped_context_log", return_value=""):
            chat_sessions._build_local_messages(session, "new message")
        self.assertEqual(len(session["transcript"]), n)


class ContextLogNoteReadingTest(unittest.TestCase):
    def test_missing_second_brain_dir_returns_empty_string(self):
        session = _session()
        with mock.patch.object(app, "get_second_brain_dir", return_value=None):
            self.assertEqual(chat_sessions._read_capped_context_log(session), "")

    def test_missing_note_file_returns_empty_string(self):
        session = _session()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(app, "get_second_brain_dir", return_value=Path(tmp)):
                self.assertEqual(chat_sessions._read_capped_context_log(session), "")

    def test_real_note_under_the_cap_is_returned_verbatim(self):
        session = _session()
        with tempfile.TemporaryDirectory() as tmp:
            note_dir = Path(tmp) / "Agent Manager Reports" / "Chat Context Logs"
            note_dir.mkdir(parents=True)
            (note_dir / f"{session['id']}.md").write_text("# chat-test-session\n- entry one\n")
            with mock.patch.object(app, "get_second_brain_dir", return_value=Path(tmp)):
                content = chat_sessions._read_capped_context_log(session)
        self.assertIn("entry one", content)
        self.assertNotIn("truncated", content)

    def test_real_note_over_the_cap_is_tail_kept_and_marked_truncated(self):
        session = _session()
        with tempfile.TemporaryDirectory() as tmp:
            note_dir = Path(tmp) / "Agent Manager Reports" / "Chat Context Logs"
            note_dir.mkdir(parents=True)
            big = "x" * (chat_sessions.CONTEXT_LOG_NOTE_CHAR_CAP + 500) + "THE_TAIL_END"
            (note_dir / f"{session['id']}.md").write_text(big)
            with mock.patch.object(app, "get_second_brain_dir", return_value=Path(tmp)):
                content = chat_sessions._read_capped_context_log(session)
        self.assertIn("truncated", content)
        self.assertIn("THE_TAIL_END", content)
        self.assertLess(len(content), len(big))


class NoGitHandoffPromptTest(unittest.TestCase):
    """Chat must not offer a git hand-off (2026-09-19): the pipeline cannot perform git operations, and a
    premium-priority "commit it and merge to master" task queued from Chat held a GPU lane for an hour."""

    def test_prompt_forbids_queueing_git_operations_and_points_at_unmerged_branches_tab(self):
        prompt = chat_sessions._LOCAL_SYSTEM_PROMPT
        self.assertIn("NEVER queue a task whose deliverable is a git operation", prompt)
        self.assertIn("Unmerged Branches tab", prompt)
        self.assertNotIn("with its own clear error pointing at queue_reviewed_task", prompt)

    def test_prompt_names_current_gpu_lanes_not_the_retired_layout(self):
        prompt = chat_sessions._LOCAL_SYSTEM_PROMPT
        self.assertIn("worker-3090", prompt)
        self.assertNotIn("worker-reasoning", prompt)


class FreshnessRulesPromptTest(unittest.TestCase):
    """2026-10-25: the chat reported months-old sales as 'the most recent' and said 'nothing to do' after silently swapping its data source."""

    def test_rules_name_newest_date_today_could_not_run_and_stale(self):
        rules = chat_sessions._FRESHNESS_RULES
        for phrase in ("newest date", "today's date", "could not run", "possibly stale", "never by row id", "nothing to do"):
            self.assertIn(phrase, rules)

    def test_local_system_prompt_includes_the_rules(self):
        self.assertIn(chat_sessions._FRESHNESS_RULES, chat_sessions._LOCAL_SYSTEM_PROMPT)
        self.assertIn(chat_sessions._FRESHNESS_RULES, chat_sessions._local_system_prompt({"repoRoot": "/x", "roots": ["/x"]}))

    def test_claude_first_turn_preamble_includes_the_rules(self):
        src = inspect.getsource(chat_sessions._send_claude)
        self.assertIn("preamble_parts.append(_FRESHNESS_RULES)", src, "the Claude first-turn preamble carries the same rules")


class ProjectRulesPromptTest(unittest.TestCase):
    """2026-10-25: tasks about the chat plugin repo were filed into agent-manager's queue; the local prompt now tells the chat to use list_projects and `project`."""

    def test_rules_name_the_tools_and_the_project_argument(self):
        rules = chat_sessions._PROJECT_RULES
        for phrase in ("list_projects", "queue_reviewed_task(project=<label>)", "search_tasks accepts project \"all\"", "ask the user", "Never file a task about repo X"):
            self.assertIn(phrase, rules)

    def test_local_system_prompt_includes_the_rules_after_the_freshness_rules(self):
        prompt = chat_sessions._LOCAL_SYSTEM_PROMPT
        self.assertIn(chat_sessions._PROJECT_RULES, prompt)
        self.assertLess(prompt.index(chat_sessions._FRESHNESS_RULES), prompt.index(chat_sessions._PROJECT_RULES))
        self.assertIn(chat_sessions._PROJECT_RULES, chat_sessions._local_system_prompt({"repoRoot": "/x", "roots": ["/x"]}))


class BehavioralRulesPromptTest(unittest.TestCase):
    """2026-10-29 (walkthrough): the chat invented user reactions, narrated protocol,
    and looped on filler. These tests confirm the behavioral guidance rule is in the
    system prompt and the audit module exists."""

    def test_rules_exist_and_contain_key_phrases(self):
        rules = chat_sessions._BEHAVIORAL_RULES
        for phrase in ("Do NOT address the user", "Do NOT apologize", "Do NOT mention internal protocol", "CONTEXT-LOG"):
            self.assertIn(phrase, rules, f"Behavioral rules should contain '{phrase}'")

    def test_local_system_prompt_includes_behavioral_rules_after_project_rules(self):
        prompt = chat_sessions._LOCAL_SYSTEM_PROMPT
        self.assertIn(chat_sessions._BEHAVIORAL_RULES, prompt)
        self.assertLess(prompt.index(chat_sessions._PROJECT_RULES), prompt.index(chat_sessions._BEHAVIORAL_RULES))

    def test_local_system_prompt_joins_the_rules_with_a_real_blank_line(self):
        # The join once used a literal backslash-n pair, so the prompt held the characters \\n\\n instead of a blank line.
        prompt = chat_sessions._LOCAL_SYSTEM_PROMPT
        self.assertIn(chat_sessions._PROJECT_RULES + "\n\n" + chat_sessions._BEHAVIORAL_RULES, prompt)
        self.assertNotIn("\\n", prompt)

    def test_claude_first_turn_preamble_includes_behavioral_rules(self):
        import inspect
        src = inspect.getsource(chat_sessions._send_claude)
        self.assertIn("preamble_parts.append(_BEHAVIORAL_RULES)", src,
                      "the Claude first-turn preamble carries the same rules")


class ChatAuditTest(unittest.TestCase):
    """Tests for chat_audit.py's deterministic audit function.

    This is a model-behavior audit: it counts what the model said, not whether the
    answer was correct. The behavioral guidance rule in chat_sessions.py prevents
    these violations; this module measures compliance.
    """

    def test_counts_youre_right_only_when_no_user_message_follows(self):
        import chat_audit
        # With has_user_message_after_first=False, should count the phrases
        text = "You're right, let me check.\n\nYou're absolutely right to be frustrated."
        result = chat_audit.audit_turn(text, has_user_message_after_first=False)
        self.assertEqual(result['youre_right_count'], 2)
        self.assertTrue(result['has_violations'])

    def test_skips_youre_right_when_user_message_follows(self):
        import chat_audit
        # With has_user_message_after_first=True, should NOT count (user might have said it)
        text = "You're right, let me check.\n\nYou're absolutely right to be frustrated."
        result = chat_audit.audit_turn(text, has_user_message_after_first=True)
        self.assertEqual(result['youre_right_count'], 0)
        self.assertFalse(result['has_violations'])

    def test_flags_context_log_narration(self):
        import chat_audit
        # CONTEXT-LOG mention outside the footer block
        text = "You're right -- I forgot the mandatory CONTEXT-LOG block. Let me add it now.\n\nCONTEXT-LOG: actual footer"
        result = chat_audit.audit_turn(text, has_user_message_after_first=False)
        self.assertEqual(result['context_log_mentions'], 1)  # One outside the footer
        self.assertTrue(result['has_violations'])

    def test_does_not_count_footer_as_mention(self):
        import chat_audit
        # The actual CONTEXT-LOG footer should not be counted
        text = "Here's my answer.\n\nCONTEXT-LOG: distilled summary"
        result = chat_audit.audit_turn(text, has_user_message_after_first=False)
        self.assertEqual(result['context_log_mentions'], 0)
        self.assertFalse(result['has_violations'])

    def test_clean_text_has_no_violations(self):
        import chat_audit
        text = "I checked the branches and found 12 approved, 3 needs-work."
        result = chat_audit.audit_turn(text, has_user_message_after_first=False)
        self.assertEqual(result['youre_right_count'], 0)
        self.assertEqual(result['context_log_mentions'], 0)
        self.assertFalse(result['has_violations'])


class RecordTurnErrorTest(unittest.TestCase):
    """record_turn_error() -- a failed turn's error lands IN the stored transcript (it used
    to exist only as a 3-second toast)."""

    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())

    def _store(self, transcript, status="active"):
        session = _session()
        session["status"] = status
        session["transcript"] = transcript
        chat_sessions._write_sessions(self.storage, {session["id"]: session})
        return session["id"]

    def _stored(self, session_id):
        return chat_sessions.get_session(self.storage, session_id)["transcript"]

    def test_fills_the_empty_assistant_placeholder(self):
        sid = self._store([{"role": "user", "text": "hi"}, {"role": "assistant", "text": ""}])
        chat_sessions.record_turn_error(self.storage, sid, "boom")
        transcript = self._stored(sid)
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[-1]["text"], "Error: boom")
        self.assertTrue(transcript[-1]["error"])

    def test_keeps_partial_reply_text_and_appends_the_error(self):
        sid = self._store([{"role": "user", "text": "hi"}, {"role": "assistant", "text": "half an answer"}])
        chat_sessions.record_turn_error(self.storage, sid, "boom")
        transcript = self._stored(sid)
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[-1]["text"], "half an answer\n\nError: boom")
        self.assertTrue(transcript[-1]["error"])

    def test_appends_a_new_entry_after_a_user_turn(self):
        sid = self._store([{"role": "user", "text": "hi"}])
        chat_sessions.record_turn_error(self.storage, sid, "boom")
        transcript = self._stored(sid)
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[-1]["role"], "assistant")
        self.assertEqual(transcript[-1]["text"], "Error: boom")
        self.assertTrue(transcript[-1]["error"])

    def test_unknown_session_returns_none_and_writes_nothing(self):
        sid = self._store([{"role": "user", "text": "hi"}])
        sdir = chat_sessions.sessions_dir(self.storage)
        before = {p.name: p.read_text(encoding="utf-8") for p in sdir.iterdir()}
        self.assertIsNone(chat_sessions.record_turn_error(self.storage, "chat-no-such-id", "boom"))
        self.assertEqual({p.name: p.read_text(encoding="utf-8") for p in sdir.iterdir()}, before)
        self.assertEqual(len(self._stored(sid)), 1)

    def test_ended_session_returns_none_and_writes_nothing(self):
        sid = self._store([{"role": "user", "text": "hi"}], status="ended")
        self.assertIsNone(chat_sessions.record_turn_error(self.storage, sid, "boom"))
        self.assertEqual(self._stored(sid), [{"role": "user", "text": "hi"}])


class BuildLocalMessagesSkipsErrorsTest(unittest.TestCase):
    def test_error_entries_are_not_replayed_to_the_model(self):
        session = _session()
        session["transcript"] = [
            {"role": "user", "text": "first question"},
            {"role": "assistant", "text": "Error: boom", "error": True},
            {"role": "user", "text": "second question"},
            {"role": "assistant", "text": "a real answer"},
        ]
        messages = chat_sessions._build_local_messages(session, "new message")
        contents = [m["content"] for m in messages[1:]]
        self.assertEqual(contents, ["first question", "second question", "a real answer", "new message"])


class ChosenProjectTest(unittest.TestCase):
    """2026-10-25 (walkthrough): a new chat opened with no indication of its target
    project (the dashboard's active project could silently become the wrong task
    queue). The session now carries chosen_project (None = the active one), it
    round-trips through storage, and the per-turn local prompt makes it the default
    `project` for the task tools."""

    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())

    def _store(self, **extra):
        session = _session()
        session["status"] = "active"
        session.update(extra)
        chat_sessions._write_sessions(self.storage, {session["id"]: session})
        return session["id"]

    def test_new_session_defaults_chosen_project_to_none(self):
        s = chat_sessions._new_session(["/x"], None, "local", "sonnet", "high")
        self.assertIsNone(s["chosen_project"])

    def test_migrate_session_adds_the_key(self):
        migrated = chat_sessions._migrate_session({"id": "chat-old"})
        self.assertIn("chosen_project", migrated)
        self.assertIsNone(migrated["chosen_project"])

    def test_set_chosen_project_round_trips_and_appends_the_note(self):
        sid = self._store()
        updated = chat_sessions.set_chosen_project(self.storage, sid, "agent-manager")
        self.assertEqual(updated["chosen_project"], "agent-manager")
        stored = chat_sessions.get_session(self.storage, sid)
        self.assertEqual(stored["chosen_project"], "agent-manager")
        note = stored["transcript"][-1]
        self.assertEqual(note["role"], "system")
        self.assertIn("Project changed to: agent-manager (chosen)", note["text"])

    def test_set_chosen_project_refuses_unknown_and_ended_sessions(self):
        self.assertIsNone(chat_sessions.set_chosen_project(self.storage, "chat-nope", "x"))
        sid = self._store(status="ended")
        self.assertIsNone(chat_sessions.set_chosen_project(self.storage, sid, "x"))
        self.assertIsNone(chat_sessions.get_session(self.storage, sid).get("chosen_project"))

    def test_local_prompt_names_the_chosen_project_and_that_an_explicit_arg_wins(self):
        session = _session()
        session["chosen_project"] = "agent-manager"
        prompt = chat_sessions._local_system_prompt(session)
        self.assertIn("acting on project: agent-manager (chosen)", prompt)
        self.assertIn("default `project` argument", prompt)
        self.assertIn("explicit `project` argument in a tool call still overrides", prompt)

    def test_local_prompt_omits_it_when_unset(self):
        prompt = chat_sessions._local_system_prompt(_session())
        self.assertNotIn("acting on project", prompt)
        self.assertNotIn("(chosen)", prompt)


class ChosenProjectRouteTest(unittest.TestCase):
    """PATCH /api/chat/<id>/project (2026-10-25 walkthrough): validates the label
    against the same registry list_projects reads, persists chosen_project, and
    GET /api/chat/projects feeds the panel's selector."""

    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        session = _session()
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage, {session["id"]: session})
        self.sid = session["id"]
        self.options = [
            {"label": "agent-manager", "root": "/r/am", "active": True},
            {"label": "agent-manager-chat-plugin-am", "root": "/r/plugin", "active": False},
        ]
        patcher2 = mock.patch("routes.chat._project_options", return_value=self.options)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        self.client = app.app.test_client()

    def test_patch_sets_the_project(self):
        resp = self.client.patch(
            f"/api/chat/{self.sid}/project", json={"project": "agent-manager"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["chosen_project"], "agent-manager")
        self.assertEqual(body["session"]["chosen_project"], "agent-manager")
        stored = chat_sessions.get_session(self.storage, self.sid)
        self.assertEqual(stored["chosen_project"], "agent-manager")

    def test_patch_missing_label_is_400(self):
        resp = self.client.patch(f"/api/chat/{self.sid}/project", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_patch_unknown_label_is_400_listing_the_valid_ones(self):
        resp = self.client.patch(f"/api/chat/{self.sid}/project", json={"project": "nope"})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("agent-manager", body["error"])

    def test_patch_unknown_session_is_404(self):
        resp = self.client.patch(
            "/api/chat/chat-nope/project", json={"project": "agent-manager"})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.get_json()["ok"])

    def test_projects_endpoint_lists_the_registry_and_the_active_one(self):
        body = self.client.get("/api/chat/projects").get_json()
        self.assertEqual([p["label"] for p in body["projects"]],
                         ["agent-manager", "agent-manager-chat-plugin-am"])
        self.assertEqual(body["active"], "agent-manager")


class EnforceFooterTest(unittest.TestCase):
    """Tests for the new enforce_footer() function (HUB0027): retry logic that
    APPENDS instead of replaces, with deterministic fallback."""

    def test_enforce_footer_preserves_existing_task_ref(self):
        """Answer with TASK-REF should be returned unchanged."""
        answer = "Here's the answer.\n\nTASK-REF: AC-123"
        result = chat_sessions.enforce_footer(answer)
        self.assertEqual(result, answer)

    def test_enforce_footer_preserves_existing_context_log(self):
        """Answer with CONTEXT-LOG should be returned unchanged."""
        answer = "Here's the answer.\n\nCONTEXT-LOG: distilled summary"
        result = chat_sessions.enforce_footer(answer)
        self.assertEqual(result, answer)

    def test_enforce_footer_appends_task_ref_when_missing(self):
        """Answer without footer should get TASK-REF: N/A appended (not replaced)."""
        answer = "Here's the answer."
        result = chat_sessions.enforce_footer(answer)
        self.assertIn("Here's the answer.", result)
        self.assertIn("TASK-REF: N/A", result)
        self.assertTrue(result.endswith("TASK-REF: N/A"))

    def test_enforce_footer_preserves_original_content(self):
        """Original answer content must never be discarded."""
        answer = "Long answer with important details.\n\nIt covers X, Y, and Z."
        result = chat_sessions.enforce_footer(answer)
        self.assertIn("Long answer with important details.", result)
        self.assertIn("It covers X, Y, and Z.", result)
        self.assertNotIn("TASK-REF: N/A", answer)  # original didn't have it
        self.assertIn("TASK-REF: N/A", result)  # result should have it

    def test_enforce_footer_handles_empty_string(self):
        """Empty answer should still get footer appended."""
        answer = ""
        result = chat_sessions.enforce_footer(answer)
        self.assertIn("TASK-REF: N/A", result)

    def test_has_footer_detects_task_ref(self):
        """_has_footer() should detect TASK-REF pattern."""
        self.assertTrue(chat_sessions._has_footer("text TASK-REF: 123"))
        self.assertTrue(chat_sessions._has_footer("TASK-REF: abc"))
        self.assertFalse(chat_sessions._has_footer("no footer here"))
        self.assertFalse(chat_sessions._has_footer(""))

    def test_has_footer_detects_context_log(self):
        """_has_footer() should detect CONTEXT-LOG pattern."""
        self.assertTrue(chat_sessions._has_footer("text CONTEXT-LOG: summary"))
        self.assertTrue(chat_sessions._has_footer("CONTEXT-LOG: abc"))
        self.assertFalse(chat_sessions._has_footer("no footer here"))


class ChatRecordCallTest(unittest.TestCase):
    """Chat turns must record with the live session id and a 'chat' instance id."""

    def test_stream_local_passes_session_id_and_chat_instance(self):
        """_stream_local must call record_call with the session id and instance_id='chat'."""
        with mock.patch.object(chat_sessions.model_stats_client, "record_call") as patched:
            session = _session()
            # _stream_local is a generator; must iterate it to reach record_call
            with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools",
                                   lambda **kw: iter([{
                                       "type": "chunk", "text": "x",
                                   }, {
                                       "type": "final", "response": "x", "turnsUsed": 1,
                                       "forcedSummary": False,
                                   }])):
                "".join(chat_sessions._stream_local(session, "hi"))
        patched.assert_called_once()
        call_kwargs = patched.call_args
        # Positional: task_id, model, latency_ms
        task_id = call_kwargs[0][0]
        # Keyword: stage, source, instance_id
        self.assertEqual(task_id, session["id"])
        self.assertEqual(call_kwargs[1].get("instance_id"), "chat")
        self.assertEqual(call_kwargs[1].get("source"), "chat")
        self.assertEqual(call_kwargs[1].get("stage"), "chat")

    def test_record_call_accepts_instance_id(self):
        """model_stats_client.record_call should accept instance_id and forward it."""
        with mock.patch("chat_sessions.model_stats_client._run_event") as patched_run:
            chat_sessions.model_stats_client.record_call(
                "chat-abc", "test-model", 100, instance_id="chat",
            )
        payload = patched_run.call_args[0][1]
        self.assertEqual(payload["taskId"], "chat-abc")
        self.assertEqual(payload["instanceId"], "chat")

    def test_record_call_legacy_call_without_instance_id_still_records(self):
        """A call with no instance_id (legacy path) should still produce a row -- no crash."""
        with mock.patch("chat_sessions.model_stats_client._run_event") as patched_run:
            chat_sessions.model_stats_client.record_call(
                "some-task", "test-model", 100,
            )
        payload = patched_run.call_args[0][1]
        self.assertEqual(payload["taskId"], "some-task")
        # instanceId should be absent (not None) when caller didn't pass one
        self.assertNotIn("instanceId", payload)

    def test_record_call_mutation_safety(self):
        """Mutating the session id source after record_call must not change the recorded task_id."""
        # Hold the session id in a mutable container so we can mutate it after the call.
        session_id_holder = ["chat-test-session"]
        with mock.patch("chat_sessions.model_stats_client._run_event") as patched_run:
            # f-string is evaluated at call time, capturing the string
            chat_sessions.model_stats_client.record_call(
                session_id_holder[0], "test-model", 100, instance_id="chat",
            )
        recorded_task_id = patched_run.call_args[0][1]["taskId"]
        # Now mutate the holder
        session_id_holder[0] = "chat-MUTATED"
        # The recorded value should still be the original captured string
        self.assertEqual(recorded_task_id, "chat-test-session")
        # A fresh call would pick up the mutated value
        with mock.patch("chat_sessions.model_stats_client._run_event") as patched_run2:
            chat_sessions.model_stats_client.record_call(
                session_id_holder[0], "test-model", 100, instance_id="chat",
            )
        self.assertEqual(patched_run2.call_args[0][1]["taskId"], "chat-MUTATED")


class VerdictLedgerStreamMessageIntegrationTest(unittest.TestCase):
    """Integration: real streamed turn through stream_message with a fake _stream_local
    and result_box['tools'] produced by summarize_tool_calls from a realistic core
    toolCallLog. This exercises the real code path, not hand-built fixtures."""

    def setUp(self):
        self._stats_patch = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats_patch.start()
        self.addCleanup(self._stats_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _seed_active_session(self):
        session = _session()
        session["provider"] = "local"
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        return session["id"]

    def test_turn_with_verdicts_includes_ledger(self):
        """A turn with record_branch_verdict calls gets a ledger block appended."""
        session_id = self._seed_active_session()
        # Realistic toolCallLog with a record_branch_verdict call whose args are
        # longer than 200 characters (so clipping would normally lose the data).
        long_branch = "x" * 250
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": long_branch, "project": "testproj", "verdict": "merge"}, "result": {"project": "testproj"}},
            {"tool": "run_bash", "args": {"command": "git show abc123 --stat"}, "result": {"error": "not found"}},
        ]
        
        def fake_stream(**kwargs):
            # Simulate streaming: yield chunks, then final with toolCallLog
            yield {"type": "chunk", "text": "I checked the branches. "}
            yield {"type": "chunk", "text": "Found 1 merge."}
            yield {"type": "final", "response": "I checked the branches. Found 1 merge.", "turnsUsed": 2, "forcedSummary": False, "toolCallLog": tool_call_log}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "check branches"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        final_text = stored["transcript"][-1]["text"]
        self.assertIn("Verdict Ledger:", final_text)
        self.assertIn("merge: 1", final_text)
        self.assertIn("testproj/", final_text)

    def test_turn_with_disagreement_includes_correction(self):
        """Model text that claims wrong counts gets a Correction: line."""
        session_id = self._seed_active_session()
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": "b1", "project": "p1", "verdict": "merge"}, "result": {"project": "p1"}},
            {"tool": "record_branch_verdict", "args": {"branch": "b2", "project": "p1", "verdict": "needs-work"}, "result": {"project": "p1"}},
        ]

        def fake_stream(**kwargs):
            # Model lies: says 2 merge, 0 needs-work (actual is 1/1)
            yield {"type": "chunk", "text": "I found 2 branches approved for merge and 0 needs-work."}
            yield {"type": "final", "response": "I found 2 branches approved for merge and 0 needs-work.", "turnsUsed": 1, "forcedSummary": False, "toolCallLog": tool_call_log}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "check"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        final_text = stored["transcript"][-1]["text"]
        self.assertIn("Correction: the cards show", final_text)
        self.assertIn("merge: 1", final_text)
        self.assertIn("needs-work: 1", final_text)

    def test_turn_with_no_verdict_calls_gets_no_ledger(self):
        """A turn with no record_branch_verdict calls gets no ledger block."""
        session_id = self._seed_active_session()
        tool_call_log = [
            {"tool": "run_bash", "args": {"command": "ls"}, "result": "file1"},
        ]

        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": "Done."}
            yield {"type": "final", "response": "Done.", "turnsUsed": 1, "forcedSummary": False, "toolCallLog": tool_call_log}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        final_text = stored["transcript"][-1]["text"]
        self.assertNotIn("Verdict Ledger:", final_text)
        self.assertNotIn("Correction:", final_text)

    def test_failed_verdict_call_with_ok_false(self):
        """A record_branch_verdict call with ok=false (failed) still records if args present."""
        session_id = self._seed_active_session()
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": "b1", "project": "p1", "verdict": "merge"}, "result": {"error": "network timeout"}},
        ]

        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": "Done."}
            yield {"type": "final", "response": "Done.", "turnsUsed": 1, "forcedSummary": False, "toolCallLog": tool_call_log}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        final_text = stored["transcript"][-1]["text"]
        # The ledger should still be built from args even if the call failed
        self.assertIn("Verdict Ledger:", final_text)
        self.assertIn("merge: 1", final_text)

    def test_verdict_call_with_no_project_uses_result(self):
        """If project not in args, fall back to result.project."""
        session_id = self._seed_active_session()
        tool_call_log = [
            {"tool": "record_branch_verdict", "args": {"branch": "b1", "verdict": "discard"}, "result": {"project": "fallback-proj"}},
        ]

        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": "Done."}
            yield {"type": "final", "response": "Done.", "turnsUsed": 1, "forcedSummary": False, "toolCallLog": tool_call_log}

        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))

        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        final_text = stored["transcript"][-1]["text"]
        self.assertIn("Verdict Ledger:", final_text)
        self.assertIn("fallback-proj", final_text)


if __name__ == "__main__":
    unittest.main()
