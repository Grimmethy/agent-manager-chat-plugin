"""Route tests for routes/chat.py: the template Save button's endpoint and the SSE
message route's error path (a failed turn must leave an error entry in the stored
transcript, not only an SSE event the panel turns into a toast).

Uses a Claude-provider session so the message route skips the local-model GPU preempt.

Run: .venv/bin/python -m unittest test_chat_routes -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402
import template_store  # noqa: E402

SID = "chat-route-test"


def _session(transcript):
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": SID, "provider": "claude", "model": None, "effort": None,
        "roots": [root], "repoRoot": root, "instancesDir": None,
        "claudeSessionId": None, "transcript": transcript,
        "status": "active", "startedAt": "2026-10-18T00:00:00Z", "endedAt": None,
        "reserved": False,
    }


class RouteTestBase(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def _store(self, transcript):
        chat_sessions._write_sessions(self.storage, {SID: _session(transcript)})

    def _stored(self):
        return chat_sessions.get_session(self.storage, SID)


class TemplateSaveRouteTest(RouteTestBase):
    def setUp(self):
        super().setUp()
        self._store([
            {"role": "user", "text": "make a classroom scene"},
            {"role": "assistant", "text": "Here it is.", "templates": [{
                "kind": "scene", "key": "classroom", "status": "proposed", "message": "",
                "text": "empty high school classroom, rows of desks, morning light",
            }]},
        ])

    def _post(self, body, sid=SID):
        return self.client.post(f"/api/chat/{sid}/template/save", json=body)

    def test_non_integer_entry_or_index_is_400(self):
        self.assertEqual(self._post({"entry": "x", "index": 0}).status_code, 400)
        self.assertEqual(self._post({"index": 0}).status_code, 400)

    def test_unknown_session_is_404(self):
        self.assertEqual(self._post({"entry": 1, "index": 0}, sid="chat-no-such-id").status_code, 404)

    def test_missing_proposal_is_404(self):
        self.assertEqual(self._post({"entry": 1, "index": 5}).status_code, 404)
        self.assertEqual(self._post({"entry": 0, "index": 0}).status_code, 404)

    def test_saved_outcome_is_persisted_on_the_proposal(self):
        ok = {"ok": True, "kind": "scene", "key": "classroom 2", "message": "Saved it."}
        with mock.patch.object(template_store, "save_template", return_value=ok) as save:
            resp = self._post({"entry": 1, "index": 0, "key": "classroom 2"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(save.call_args[0][0]["key"], "classroom 2")  # the user's edit is what gets saved
        self.assertEqual(resp.get_json()["proposal"]["status"], "saved")
        stored = self._stored()["transcript"][1]["templates"][0]
        self.assertEqual(stored["status"], "saved")
        self.assertEqual(stored["key"], "classroom 2")
        self.assertEqual(stored["message"], "Saved it.")


class MessageRouteErrorTest(RouteTestBase):
    def test_a_crashed_turn_streams_an_error_event_and_persists_an_error_entry(self):
        self._store([])

        def crashing_stream(storage_dir, session_id, message):
            # Same first step the real stream_message takes: persist the user turn and an
            # empty assistant placeholder, THEN the model call blows up.
            sessions = chat_sessions._read_sessions(storage_dir)
            sessions[session_id]["transcript"] += [
                {"role": "user", "text": message}, {"role": "assistant", "text": ""}]
            chat_sessions._write_sessions(storage_dir, sessions)
            raise RuntimeError("boom")
            yield  # pragma: no cover -- makes this a generator like the real one

        with mock.patch.object(chat_sessions, "stream_message", crashing_stream):
            resp = self.client.post(f"/api/chat/{SID}/message", json={"message": "hello"})
            body = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        events = [json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]
        self.assertEqual(events, [{"type": "error", "error": "RuntimeError: boom"}])
        transcript = self._stored()["transcript"]
        self.assertEqual(transcript[-1], {"role": "assistant", "text": "Error: RuntimeError: boom", "error": True})
        self.assertEqual(transcript[-2]["text"], "hello")


class SessionListAndCloseRouteTest(RouteTestBase):
    def setUp(self):
        super().setUp()
        older = _session([{"role": "user", "text": "first chat"}])
        older.update(id="chat-1-aaaaaaaa", startedAt="2026-10-18T09:00:00Z", lastActivityAt="2026-10-18T09:05:00Z")
        newer = _session([])
        newer.update(id="chat-2-bbbbbbbb", startedAt="2026-10-18T10:00:00Z", lastActivityAt=None)
        ended = _session([])
        ended.update(id="chat-3-cccccccc", status="ended", startedAt="2026-10-18T11:00:00Z")
        chat_sessions._write_sessions(self.storage, {s["id"]: s for s in (older, newer, ended)})

    def test_sessions_lists_active_sessions_most_recent_first(self):
        resp = self.client.get("/api/chat/sessions")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual([r["id"] for r in rows], ["chat-2-bbbbbbbb", "chat-1-aaaaaaaa"])
        self.assertEqual(rows[1]["title"], "first chat")
        for key in ("id", "title", "provider", "model", "startedAt", "lastActivityAt", "turnCount", "busy"):
            self.assertIn(key, rows[0])

    def test_close_ends_one_session_and_leaves_the_others(self):
        resp = self.client.post("/api/chat/chat-1-aaaaaaaa/close")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ended")
        self.assertEqual(chat_sessions.get_session(self.storage, "chat-1-aaaaaaaa")["status"], "ended")
        self.assertEqual(chat_sessions.get_session(self.storage, "chat-2-bbbbbbbb")["status"], "active")
        self.assertEqual([r["id"] for r in self.client.get("/api/chat/sessions").get_json()], ["chat-2-bbbbbbbb"])

    def test_close_unknown_or_already_ended_is_404(self):
        self.assertEqual(self.client.post("/api/chat/chat-9-ffffffff/close").status_code, 404)
        self.assertEqual(self.client.post("/api/chat/chat-3-cccccccc/close").status_code, 404)

    def test_close_releases_a_reservation_first(self):
        s = chat_sessions.get_session(self.storage, "chat-1-aaaaaaaa")
        s["reserved"] = True
        chat_sessions._write_session(self.storage, s)
        import internal_api_client
        with mock.patch.object(internal_api_client, "reserve") as reserve:
            resp = self.client.post("/api/chat/chat-1-aaaaaaaa/close")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(reserve.call_args[0][0], "chat-1-aaaaaaaa")
        self.assertIs(reserve.call_args[0][2], False)

    def test_new_no_longer_ends_the_active_session(self):
        resp = self.client.post("/api/chat/new", json={"provider": "claude"})
        self.assertEqual(resp.status_code, 200)
        new_id = resp.get_json()["id"]
        ids = [r["id"] for r in self.client.get("/api/chat/sessions").get_json()]
        self.assertEqual(ids[0], new_id)
        self.assertIn("chat-1-aaaaaaaa", ids)
        self.assertIn("chat-2-bbbbbbbb", ids)
        self.assertEqual(self.client.get("/api/chat/active").get_json()["id"], new_id)


if __name__ == "__main__":
    unittest.main()
