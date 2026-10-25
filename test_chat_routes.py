"""Route tests for routes/chat.py: the template Save button's endpoint and the SSE
message route's error path (a failed turn must leave an error entry in the stored
transcript, not only an SSE event the panel turns into a toast).

Uses a Claude-provider session so the message route skips the local-model GPU preempt.

Run: .venv/bin/python -m unittest test_chat_routes -v
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class SwapOfferErrorTest(RouteTestBase):
    """HUB0008 8/11: a failed claude-provider turn whose error looks like a limit
    error (ASSUMED phrases) gets a swapOffer on the SSE error event plus one 0600
    sample line; local-provider and unclassified errors stay the old event. Fake
    token file paths only -- never a real token."""

    def _crash_with(self, msg):
        def crashing(storage_dir, session_id, message):
            raise RuntimeError(msg)
            yield  # pragma: no cover -- generator like the real stream_message
        return crashing

    def _post_message(self, msg):
        with mock.patch.object(chat_sessions, "stream_message", self._crash_with(msg)):
            resp = self.client.post(f"/api/chat/{SID}/message", json={"message": "hello"})
        body = resp.get_data(as_text=True)
        events = [json.loads(line[len("data: "):]) for line in body.splitlines()
                  if line.startswith("data: ")]
        return resp, body, events

    def _register_two_accounts(self):
        import accounts
        accounts.add(self.storage, "acct-a", "https://api.example", "/tmp/fake-token-a")
        accounts.add(self.storage, "acct-b", "https://api.example", "/tmp/fake-token-b")

    def test_classified_claude_error_gets_swapoffer_and_a_0600_sample(self):
        self._register_two_accounts()  # acct-a is live (first added), i.e. the one used
        self._store([])
        resp, body, events = self._post_message("You have hit your usage limit, resets tomorrow")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(events[-1]["type"], "error")
        offer = events[-1]["swapOffer"]
        self.assertEqual(offer["kind"], "usage_limit")
        # the live (used) account is excluded; the offer always ends with local
        self.assertEqual(offer["options"],
                         [{"provider": "claude", "account": "acct-b"}, {"provider": "local"}])
        self.assertEqual(offer["options"][-1], {"provider": "local"})
        self.assertNotIn("tokenFile", body)
        self.assertNotIn("fake-token", body)
        sample = self.storage / "limit-error-samples.jsonl"
        self.assertEqual(sample.stat().st_mode & 0o777, 0o600)
        lines = sample.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertIn("at", rec)
        self.assertEqual(rec["text"], "RuntimeError: You have hit your usage limit, resets tomorrow")

    def test_swapoffer_excludes_the_pinned_account_not_the_live_one(self):
        self._register_two_accounts()  # live = acct-a
        s = _session([])
        s["account"] = "acct-b"  # the chat is pinned to acct-b, so acct-b is the one used
        chat_sessions._write_sessions(self.storage, {SID: s})
        _, _, events = self._post_message("HTTP 429 Too Many Requests")
        offer = events[-1]["swapOffer"]
        self.assertEqual(offer["kind"], "rate_limit")
        self.assertEqual(offer["options"],
                         [{"provider": "claude", "account": "acct-a"}, {"provider": "local"}])

    def test_unclassified_claude_error_is_unchanged_and_not_sampled(self):
        self._register_two_accounts()
        self._store([])
        _, body, events = self._post_message("connection refused by the provider")
        self.assertEqual(events, [{"type": "error",
                                   "error": "RuntimeError: connection refused by the provider"}])
        self.assertNotIn("swapOffer", body)
        self.assertFalse((self.storage / "limit-error-samples.jsonl").exists())

    def test_local_provider_error_is_unchanged_and_not_sampled(self):
        s = _session([])
        s["provider"] = "local"
        chat_sessions._write_sessions(self.storage, {SID: s})
        import internal_api_client
        with mock.patch.object(internal_api_client, "preempt", return_value=[]), \
                mock.patch.object(chat_sessions, "stream_message",
                                  self._crash_with("usage limit reached")):
            resp = self.client.post(f"/api/chat/{SID}/message", json={"message": "hi"})
        body = resp.get_data(as_text=True)
        self.assertNotIn("swapOffer", body)
        self.assertNotIn("token", body)
        self.assertFalse((self.storage / "limit-error-samples.jsonl").exists())


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
        # the route now passes `on` by keyword and lets reserve() default the model
        self.assertIs(reserve.call_args.kwargs["on"], False)

    def test_get_returns_the_full_session(self):
        resp = self.client.get("/api/chat/chat-1-aaaaaaaa")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["id"], "chat-1-aaaaaaaa")
        self.assertEqual(body["transcript"], [{"role": "user", "text": "first chat"}])

    def test_get_unknown_or_ended_is_404(self):
        self.assertEqual(self.client.get("/api/chat/chat-9-ffffffff").status_code, 404)
        self.assertEqual(self.client.get("/api/chat/chat-3-cccccccc").status_code, 404)

    def test_get_does_not_shadow_the_fixed_routes(self):
        self.assertIsInstance(self.client.get("/api/chat/sessions").get_json(), list)
        self.assertEqual(self.client.get("/api/chat/active").get_json()["id"], "chat-2-bbbbbbbb")

    def test_new_no_longer_ends_the_active_session(self):
        resp = self.client.post("/api/chat/new", json={"provider": "claude"})
        self.assertEqual(resp.status_code, 200)
        new_id = resp.get_json()["id"]
        ids = [r["id"] for r in self.client.get("/api/chat/sessions").get_json()]
        self.assertEqual(ids[0], new_id)
        self.assertIn("chat-1-aaaaaaaa", ids)
        self.assertIn("chat-2-bbbbbbbb", ids)
        self.assertEqual(self.client.get("/api/chat/active").get_json()["id"], new_id)


class TerminalSessionsMultiHostRouteTest(unittest.TestCase):
    """Multi-host /api/chat/terminal-sessions and per-host /events, via the Flask test
    client against two REAL stub HTTP agents on loopback (same pattern as
    test_agent_client_multihost / test_agent_client_events). All token strings are fake
    ("fake-token-a" / "fake-token-b") -- never real credentials."""

    TOKEN_A = "fake-token-a"
    TOKEN_B = "fake-token-b"

    BODY_A = (
        b": keepalive\n\n"
        b"event: session.updated\ndata: {\"id\": \"sess-a\", \"status\": \"busy\"}\n\n"
        b"event: session.removed\ndata: {\"id\": \"sess-a\"}\n\n"
    )
    BODY_B = (
        b": keepalive\n\n"
        b"event: session.updated\ndata: {\"id\": \"sess-b\", \"status\": \"idle\"}\n\n"
        b"event: session.removed\ndata: {\"id\": \"sess-b\"}\n\n"
    )

    class _SessionsHandler(BaseHTTPRequestHandler):
        rows = []

        def do_GET(self):
            body = json.dumps(self.rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    class _SSEHandler(BaseHTTPRequestHandler):
        body = b""

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
            self.wfile.flush()

        def log_message(self, *a):
            pass

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @staticmethod
    def _write_token(text):
        tf = Path(tempfile.mkdtemp()) / "tok"
        tf.write_text(text)
        return str(tf)

    def _stub_sessions(self, rows):
        handler = type("Bound", (self._SessionsHandler,), {"rows": rows})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.recorded = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def _stub_sse(self, body):
        handler = type("Bound", (self._SSEHandler,), {"body": body})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.seen = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def _plural_env(self, url_a, url_b):
        return {
            "AGENT_MANAGER_SESSION_AGENT_URL": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": "",
            "AGENT_MANAGER_SESSION_AGENT_URLS": json.dumps([
                {"host": "alpha", "url": url_a, "tokenFile": self._write_token(self.TOKEN_A)},
                {"host": "beta", "url": url_b, "tokenFile": self._write_token(self.TOKEN_B)},
            ]),
        }

    def _singular_env(self, url):
        return {
            "AGENT_MANAGER_SESSION_AGENT_URLS": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": "",
            "AGENT_MANAGER_SESSION_AGENT_URL": url,
            "AGENT_MANAGER_SESSION_AGENT_TOKEN": self.TOKEN_A,
        }

    def _no_hosts_env(self):
        return {"AGENT_MANAGER_SESSION_AGENT_URLS": "", "AGENT_MANAGER_SESSION_AGENT_URL": "",
                "AGENT_MANAGER_SESSION_AGENT_TOKEN": "",
                "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": ""}

    def setUp(self):
        self.client = app.app.test_client()

    def test_two_hosts_listed_with_host_tags_and_hosts_skipped_keys(self):
        _, url_a = self._stub_sessions([{"id": "sess-a", "status": "busy"}])
        _, url_b = self._stub_sessions([{"id": "sess-b", "status": "idle"}])
        with mock.patch.dict(os.environ, self._plural_env(url_a, url_b)):
            body = self.client.get("/api/chat/terminal-sessions").get_json()
        self.assertTrue(body["enabled"])
        self.assertTrue(body["reachable"])
        self.assertEqual(body["hosts"], ["alpha", "beta"])
        self.assertEqual(body["skipped"], [])
        self.assertEqual([r["id"] for r in body["sessions"]], ["sess-a", "sess-b"])
        self.assertEqual(body["sessions"][0]["host"], "alpha")
        self.assertEqual(body["sessions"][1]["host"], "beta")
        self.assertNotIn(self.TOKEN_A, json.dumps(body))
        self.assertNotIn(self.TOKEN_B, json.dumps(body))

    def test_one_host_down_appears_in_skipped_reachable_stays_true(self):
        _, url_a = self._stub_sessions([{"id": "sess-a"}])
        down_b = f"http://127.0.0.1:{self._free_port()}"
        with mock.patch.dict(os.environ, self._plural_env(url_a, down_b)):
            body = self.client.get("/api/chat/terminal-sessions").get_json()
        self.assertTrue(body["reachable"])
        self.assertEqual([r["id"] for r in body["sessions"]], ["sess-a"])
        self.assertEqual(body["hosts"], ["alpha", "beta"])
        self.assertEqual([s["host"] for s in body["skipped"]], ["beta"])
        self.assertTrue(body["skipped"][0]["reason"])

    def test_all_hosts_down_is_unreachable_with_empty_sessions(self):
        down_a = f"http://127.0.0.1:{self._free_port()}"
        down_b = f"http://127.0.0.1:{self._free_port()}"
        with mock.patch.dict(os.environ, self._plural_env(down_a, down_b)):
            body = self.client.get("/api/chat/terminal-sessions").get_json()
        self.assertTrue(body["enabled"])
        self.assertFalse(body["reachable"])
        self.assertEqual(body["sessions"], [])
        self.assertEqual(body["hosts"], ["alpha", "beta"])
        self.assertEqual([s["host"] for s in body["skipped"]], ["alpha", "beta"])

    def test_no_host_configured_keeps_disabled_shape(self):
        with mock.patch.dict(os.environ, self._no_hosts_env()):
            body = self.client.get("/api/chat/terminal-sessions").get_json()
            events = self.client.get("/api/chat/terminal-sessions/events")
        self.assertEqual(body, {"enabled": False, "reachable": False, "sessions": [],
                                "hosts": [], "skipped": []})
        self.assertEqual(events.status_code, 404)

    def test_events_missing_host_with_two_hosts_is_400(self):
        _, url_a = self._stub_sse(self.BODY_A)
        _, url_b = self._stub_sse(self.BODY_B)
        with mock.patch.dict(os.environ, self._plural_env(url_a, url_b)):
            resp = self.client.get("/api/chat/terminal-sessions/events")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("host=", resp.get_data(as_text=True))

    def test_events_unknown_host_is_404(self):
        _, url_a = self._stub_sse(self.BODY_A)
        _, url_b = self._stub_sse(self.BODY_B)
        with mock.patch.dict(os.environ, self._plural_env(url_a, url_b)):
            resp = self.client.get("/api/chat/terminal-sessions/events?host=nope")
        self.assertEqual(resp.status_code, 404)

    def test_events_unreachable_host_is_502(self):
        _, url_a = self._stub_sse(self.BODY_A)
        down_b = f"http://127.0.0.1:{self._free_port()}"
        with mock.patch.dict(os.environ, self._plural_env(url_a, down_b)):
            resp = self.client.get("/api/chat/terminal-sessions/events?host=beta")
        self.assertEqual(resp.status_code, 502)

    def test_events_stream_only_the_requested_host_tagged(self):
        _, url_a = self._stub_sse(self.BODY_A)
        _, url_b = self._stub_sse(self.BODY_B)
        with mock.patch.dict(os.environ, self._plural_env(url_a, url_b)):
            resp = self.client.get("/api/chat/terminal-sessions/events?host=alpha")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        stream = iter(resp.response)
        text = b""
        while b"session.removed" not in text:
            text += next(stream)
        resp.close()
        self.assertIn(b'"host": "alpha"', text)
        self.assertNotIn(b"sess-b", text)
        self.assertNotIn(self.TOKEN_A.encode(), text)
        self.assertNotIn(self.TOKEN_B.encode(), text)

    def test_singular_config_events_work_without_host_param(self):
        _, url_a = self._stub_sse(self.BODY_A)
        with mock.patch.dict(os.environ, self._singular_env(url_a)):
            resp = self.client.get("/api/chat/terminal-sessions/events")
        self.assertEqual(resp.status_code, 200)
        stream = iter(resp.response)
        text = b""
        while b"session.updated" not in text:
            text += next(stream)
        self.assertIn(b'"host": "default"', text)
        self.assertNotIn(self.TOKEN_A.encode(), text)
        resp.close()


if __name__ == "__main__":
    unittest.main()
