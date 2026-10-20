import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import session_agent as sa

FIXTURES = Path(__file__).parent / "test_fixtures" / "session_agent"
DEAD_PIDS = {1003}


def alive(pid):
    return pid not in DEAD_PIDS


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Path(self.tmp) / "claude_home"
        shutil.copytree(FIXTURES / "claude_home", self.home)

    def make_agent(self, **kw):
        kw.setdefault("lister", None)
        kw.setdefault("alive", alive)
        kw.setdefault("host", "testhost")
        agent = sa.Agent(self.home, **kw)
        agent.refresh()
        return agent

    def row(self, agent, sid):
        return {r["id"]: r for r in agent.snapshot()}[sid]

    def transcript(self, sid):
        return self.home / "projects" / "-home-user-proj" / f"{sid}.jsonl"


class HelperTests(unittest.TestCase):
    def test_status_mapping(self):
        self.assertEqual(sa.map_status("busy"), "busy")
        self.assertEqual(sa.map_status("WAITING"), "waiting")
        self.assertEqual(sa.map_status(None, "idle"), "idle")
        self.assertEqual(sa.map_status("exploding", None), "idle")
        self.assertEqual(sa.map_status(5, ["x"]), "idle")
        # observed on a real install: bg session CLI status busy + state blocked
        self.assertEqual(sa.map_status("busy", "blocked"), "waiting")
        self.assertEqual(sa.map_status("shell"), "idle")  # seen once, unexplained: not mapped
        self.assertEqual(sa.map_status("busy", "running"), "busy")

    def test_windows_unknown_model_is_null(self):
        table = sa.load_windows(FIXTURES / "windows.json")
        self.assertIsNone(sa.window_for("some-other-model", table))
        self.assertIsNone(sa.window_for(None, table))
        self.assertIsNone(sa.window_for("claude-sonnet-5-5", {}))  # built-in table starts empty

    def test_windows_longest_substring_wins(self):
        table = sa.load_windows(FIXTURES / "windows.json")
        self.assertEqual(sa.window_for("claude-sonnet-5-5", table), 2000)
        self.assertEqual(sa.window_for("claude-sonnet-5-1", table), 1000)

    def test_bad_window_entries_dropped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"ok": 5, "str": "9", "neg": -1, "bool": True}, f)
        self.addCleanup(Path(f.name).unlink)
        self.assertEqual(sa.load_windows(f.name), {"ok": 5})

    def test_loopback_detection(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertTrue(sa.is_loopback(host))
        for host in ("0.0.0.0", "192.0.2.1", "example.invalid"):
            self.assertFalse(sa.is_loopback(host))


class RowTests(AgentCase):
    def test_busy_row_context_step_and_timer(self):
        row = self.row(self.make_agent(), "sess-busy")
        self.assertEqual(row["status"], "busy")
        # latest non-sidechain assistant line: 5 + 200 + 1000; output tokens and sidechain excluded
        self.assertEqual(row["contextTokens"], 1205)
        self.assertEqual(row["step"], "Bash: echo " + "x" * 35)
        self.assertEqual(len(row["step"]) - len("Bash: "), sa.STEP_CAP)
        self.assertEqual(row["statusSince"], "2026-10-18T05:28:20Z")  # from statusUpdatedAt
        self.assertEqual(row["model"], "claude-sonnet-5-5")
        self.assertEqual(row["host"], "testhost")
        self.assertIsNone(row["contextWindow"])
        self.assertEqual(set(row), {"id", "name", "cwd", "kind", "pid", "status", "waitingFor",
                                    "statusSince", "lastActivityAt", "step", "contextTokens", "contextWindow",
                                    "model", "startedAt", "host"})

    def test_window_from_table(self):
        agent = self.make_agent(windows=sa.load_windows(FIXTURES / "windows.json"))
        self.assertEqual(self.row(agent, "sess-busy")["contextWindow"], 2000)
        self.assertIsNone(self.row(agent, "sess-idle")["contextWindow"])  # unknown model

    def test_waiting_step(self):
        row = self.row(self.make_agent(), "sess-wait")
        self.assertEqual((row["status"], row["waitingFor"]), ("waiting", "permission prompt"))
        self.assertEqual(row["step"], "Waiting: permission prompt")

    def test_idle_without_tool_has_null_step_and_transcript_fallback(self):
        row = self.row(self.make_agent(), "sess-idle")
        self.assertIsNone(row["step"])
        self.assertEqual(row["statusSince"], "2026-01-01T00:00:06Z")  # no statusUpdatedAt
        self.assertEqual(row["contextTokens"], 10)

    def test_idle_hides_last_tool_and_kind_is_normalised(self):
        reg = self.home / "sessions" / "1001.json"
        data = json.loads(reg.read_text())
        data.update(status="idle", kind="bg")
        reg.write_text(json.dumps(data))
        row = self.row(self.make_agent(), "sess-busy")
        self.assertEqual((row["status"], row["step"], row["kind"]), ("idle", None, "background"))

    def test_blocked_background_session_is_waiting(self):
        cli = [{"pid": 1001, "sessionId": "sess-busy", "status": "busy", "state": "blocked", "kind": "background"}]
        row = self.row(self.make_agent(lister=lambda: cli), "sess-busy")
        self.assertEqual((row["status"], row["kind"]), ("waiting", "background"))
        self.assertEqual(row["step"], "Bash: echo " + "x" * 35)  # no waitingFor -> last tool

    def test_synthetic_placeholder_lines_do_not_reset_context(self):
        agent = self.make_agent()
        with open(self.transcript("sess-busy"), "a") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:03:00Z", "isSidechain": False,
                                "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "x"}],
                                            "usage": {"input_tokens": 0, "cache_read_input_tokens": 0,
                                                      "cache_creation_input_tokens": 0}}}) + "\n")
        agent.refresh()
        row = self.row(agent, "sess-busy")
        self.assertEqual((row["contextTokens"], row["model"]), (1205, "claude-sonnet-5-5"))

    def test_statusline_window_preferred_and_never_expires(self):
        sl = Path(self.tmp) / "statusline"
        sl.mkdir()
        # observedAt is ancient on purpose: a window does not change within a session
        (sl / "sess-busy.json").write_text(json.dumps({"contextWindowSize": 777, "observedAt": 1,
                                                       "model": "claude-sonnet-5-5"}))
        (sl / "sess-wait.json").write_text(json.dumps({"contextWindowSize": 888, "model": "some-other-model"}))
        (sl / "sess-idle.json").write_text("{nope")
        agent = self.make_agent(statusline_dir=sl, windows={"sonnet": 5})
        self.assertEqual(self.row(agent, "sess-busy")["contextWindow"], 777)
        # recorded model differs from the transcript's latest model -> ignored, table applies
        self.assertEqual(self.row(agent, "sess-wait")["contextWindow"], 5)
        self.assertIsNone(self.row(agent, "sess-idle")["contextWindow"])

    def test_statusline_reader_validation(self):
        sl = Path(self.tmp)
        cases = {"a": {"contextWindowSize": 10},
                 "b": {"contextWindowSize": 10, "model": "m"},
                 "c": {"contextWindowSize": "10"},
                 "d": {"contextWindowSize": True},
                 "e": {"contextWindowSize": -5},
                 "f": [1]}
        for name, body in cases.items():
            (sl / f"{name}.json").write_text(json.dumps(body))
        got = {n: sa.read_statusline_window(sl, n, "m") for n in cases}
        self.assertEqual(got, {"a": 10, "b": 10, "c": None, "d": None, "e": None, "f": None})
        self.assertIsNone(sa.read_statusline_window(sl, "b", "other"))
        self.assertEqual(sa.read_statusline_window(sl, "b", None), 10)  # transcript model unknown
        self.assertIsNone(sa.read_statusline_window(sl, "../a", "m"))
        self.assertIsNone(sa.read_statusline_window(None, "a", "m"))

    def test_last_activity_reported(self):
        self.assertEqual(self.row(self.make_agent(), "sess-busy")["lastActivityAt"], "2026-01-01T00:00:04Z")

    def test_token_file_is_private_and_rotates(self):
        path = Path(self.tmp) / "state" / "agent.token"
        first = sa.write_token_file(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.read_text().strip(), first)
        self.assertGreaterEqual(len(first), 32)
        second = sa.write_token_file(path)
        self.assertNotEqual(first, second)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["agent.token"])  # no temp leftovers

    def test_busy_without_tool_use_is_thinking(self):
        reg = self.home / "sessions" / "1004.json"
        data = json.loads(reg.read_text())
        data["status"] = "busy"
        reg.write_text(json.dumps(data))
        self.assertEqual(self.row(self.make_agent(), "sess-idle")["step"], "Thinking")

    def test_new_prompt_clears_step(self):
        agent = self.make_agent()
        with open(self.transcript("sess-busy"), "a") as f:
            f.write(json.dumps({"type": "user", "timestamp": "2026-01-01T00:01:00Z",
                                "message": {"content": "next"}}) + "\n")
        agent.refresh()
        self.assertEqual(self.row(agent, "sess-busy")["step"], "Thinking")

    def test_dead_pid_omitted(self):
        ids = {r["id"] for r in self.make_agent().snapshot()}
        self.assertEqual(ids, {"sess-busy", "sess-wait", "sess-idle"})

    def test_missing_files_and_garbage_registry_do_not_crash(self):
        (self.home / "sessions" / "2000.json").write_text("{nope")
        (self.home / "sessions" / "2001.json").write_text("[1, 2]")
        self.transcript("sess-wait").unlink()
        agent = self.make_agent()
        row = self.row(agent, "sess-wait")
        self.assertIsNone(row["contextTokens"])
        self.assertEqual(len(agent.snapshot()), 3)
        shutil.rmtree(self.home / "sessions")
        agent.refresh()
        self.assertEqual(agent.snapshot(), [])

    def test_cli_listing_overrides_registry(self):
        cli = [{"pid": 1001, "sessionId": "sess-busy", "status": "idle"}]
        agent = self.make_agent(lister=lambda: cli)
        self.assertEqual(self.row(agent, "sess-busy")["status"], "idle")

    def test_corrupt_oversized_and_partial_lines(self):
        agent = self.make_agent()
        path = self.transcript("sess-busy")
        with open(path, "ab") as f:
            f.write(b'{"type":"assistant","pad":"' + b"y" * (sa.MAX_LINE + 10) + b'"}\n')
            f.write(b"\xff\xfe garbage \"assistant\"\n")
            f.write(b'{"type":"assistant","isSidechain":false,"message":{"usage":{"input_tokens":7}')
        agent.refresh()
        self.assertEqual(self.row(agent, "sess-busy")["contextTokens"], 1205)  # partial line ignored
        with open(path, "ab") as f:
            f.write(b',"model":"m"}}\n')  # line completed later
        agent.refresh()
        self.assertEqual(self.row(agent, "sess-busy")["contextTokens"], 7)


class ProbeTests(AgentCase):
    def test_probe_reports_shapes_only(self):
        rep = sa.probe(self.home, lister=lambda: [{"pid": 1001, "status": "busy", "name": "secret-name"}],
                       alive=alive)
        self.assertEqual(rep["cli"]["status"], ["busy"])
        self.assertEqual(rep["registry"]["files"], 4)
        self.assertEqual(rep["registry"]["pidAlive"], 3)
        busy = [t for t in rep["transcripts"] if t["contextTokens"] == 1205][0]
        self.assertEqual((busy["corrupt"], busy["sidechainAssistantLines"]), (1, 1))
        self.assertEqual(busy["lineTypes"]["assistant"], 3)
        self.assertIn("input_tokens", busy["usageKeys"])
        self.assertEqual(busy["blockKeys"]["tool_use"], ["input", "name", "type"])
        dumped = json.dumps(rep)
        for private in ("sess-busy", "secret-name", "hello synthetic", "/home/user", "first reply"):
            self.assertNotIn(private, dumped)


class TranscriptTests(AgentCase):
    def test_entries_are_summaries_without_sidechain(self):
        entries, nxt = sa.read_transcript(self.transcript("sess-busy"), 0, 200)
        self.assertEqual([e["role"] for e in entries], ["user", "assistant", "tool"])
        self.assertEqual(entries[2]["text"], "Bash: echo " + "x" * 35)
        self.assertNotIn("usage", json.dumps(entries))
        self.assertEqual(nxt, self.transcript("sess-busy").stat().st_size)

    def test_since_cursor_pages(self):
        path = self.transcript("sess-busy")
        first, c1 = sa.read_transcript(path, 0, 1)
        second, c2 = sa.read_transcript(path, c1, 1)
        rest, c3 = sa.read_transcript(path, c2, 200)
        self.assertEqual([first[0]["role"], second[0]["role"], rest[0]["role"]],
                         ["user", "assistant", "tool"])
        self.assertEqual(sa.read_transcript(path, c3, 200), ([], c3))


class ServerCase(AgentCase):
    token = None

    def start(self, **kw):
        self.agent = self.make_agent(token=self.token, **kw)
        self.srv = sa.make_server(self.agent, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.agent.stop.set()
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, path, body, token=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)


class HttpTests(ServerCase):
    def test_endpoints(self):
        self.start()
        self.assertEqual(self.get("/healthz"), {"ok": True})
        self.assertEqual(len(self.get("/sessions")), 3)
        page = self.get("/sessions/sess-busy/transcript?limit=2")
        self.assertEqual(len(page["entries"]), 2)
        rest = self.get("/sessions/sess-busy/transcript?since=" + page["next"])
        self.assertEqual([e["role"] for e in rest["entries"]], ["tool"])
        for bad in ("/sessions/nope/transcript", "/nothing"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(bad)
            self.assertEqual(cm.exception.code, 404)
        for bad in ("since=junk", "since=o" + "9" * 40, "since=o999999999"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get("/sessions/sess-busy/transcript?" + bad)
            self.assertEqual(cm.exception.code, 400, bad)


class HostGuardTests(ServerCase):
    def raw(self, path, host=None, origin=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        headers = {k: v for k, v in (("Host", host), ("Origin", origin)) if v}
        conn.putrequest("GET", path, skip_host=host is not None)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status

    def test_rebinding_hosts_and_foreign_origins_rejected_without_token(self):
        self.start()
        for path in ("/healthz", "/sessions"):
            self.assertEqual(self.raw(path, host="evil.example"), 403)
            self.assertEqual(self.raw(path, host="evil.example:7450"), 403)
            self.assertEqual(self.raw(path, host="127.0.0.1", origin="http://evil.example"), 403)
            for ok in ("127.0.0.1:7450", "localhost", "[::1]:7450"):
                self.assertEqual(self.raw(path, host=ok), 200)
        self.assertEqual(self.raw("/healthz", host="localhost:1", origin="http://localhost:3000"), 200)

    def test_configured_names_allowed_and_no_cors_headers(self):
        self.start(allowed_hosts=["Agent.Internal"])
        self.assertEqual(self.raw("/healthz", host="agent.internal:7450"), 200)
        self.assertEqual(self.raw("/healthz", host="other.internal"), 403)
        req = urllib.request.Request(self.base + "/healthz")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_userinfo_and_odd_hosts_rejected(self):
        self.start()
        for host in ("localhost:80@evil.example", "localhost.", "evil.example@localhost:80x", ""):
            self.assertEqual(self.raw("/healthz", host=host), 403, host)
        self.assertEqual(sa.host_only("[::1]:7450"), "::1")
        self.assertEqual(sa.host_only("http://LocalHost:3000"), "localhost")
        self.assertEqual(self.raw("/healthz", host="localhost", origin="null"), 403)

    def test_host_checked_before_token(self):
        self.token = "synthetic-test-token"
        self.start()
        self.assertEqual(self.raw("/healthz", host="evil.example"), 403)


class AuthTests(ServerCase):
    token = "synthetic-test-token"

    def test_non_loopback_bind_requires_token(self):
        with self.assertRaises(ValueError):
            sa.make_server(sa.Agent(self.home), "0.0.0.0", 0)
        sa.make_server(sa.Agent(self.home, token="t"), "127.0.0.1", 0).server_close()

    def test_token_checked_on_every_request(self):
        self.start()
        for path in ("/healthz", "/sessions", "/events", "/sessions/sess-busy/transcript"):
            for tok in (None, "wrong"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    self.get(path, tok)
                self.assertEqual(cm.exception.code, 401)
        self.assertEqual(self.get("/healthz", self.token), {"ok": True})


class SseTests(ServerCase):
    def read_events(self, resp, want):
        got = []
        event = None
        while len(got) < want:
            line = resp.readline().decode().rstrip("\n")
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                got.append((event, json.loads(line[6:])))
        return got

    def test_snapshot_then_updates_and_appends(self):
        self.start()
        threading.Thread(target=self.agent.poll_forever, args=(0.05,), daemon=True).start()
        resp = urllib.request.urlopen(self.base + "/events", timeout=5)
        self.addCleanup(resp.close)
        snap = self.read_events(resp, 3)
        self.assertEqual({e for e, _ in snap}, {"session.updated"})
        with open(self.transcript("sess-wait"), "a") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:02:00Z",
                                "message": {"model": "m", "usage": {"input_tokens": 42},
                                            "content": [{"type": "text", "text": "more"}]}}) + "\n")
        got = self.read_events(resp, 2)
        by_event = dict(got)
        self.assertEqual(by_event["transcript.append"]["id"], "sess-wait")
        self.assertEqual(by_event["transcript.append"]["entries"][0]["text"], "more")
        self.assertEqual(by_event["session.updated"]["contextTokens"], 42)

    def test_types_session_filters_out_transcript_events(self):
        self.start()
        resp = urllib.request.urlopen(self.base + "/events?types=session", timeout=5)
        self.addCleanup(resp.close)
        self.read_events(resp, 3)  # snapshot
        q = self.agent.hub
        q.publish("transcript.append", {"id": "sess-wait", "entries": [{"role": "user", "text": "private"}]})
        q.publish("session.removed", {"id": "sess-wait"})
        event, data = self.read_events(resp, 1)[0]
        self.assertEqual((event, data), ("session.removed", {"id": "sess-wait"}))

    def test_removed_session_event(self):
        self.start()
        q = self.agent.hub.subscribe()
        (self.home / "sessions" / "1002.json").unlink()
        self.agent.refresh()
        self.assertEqual(q.get(timeout=2), ("session.removed", {"id": "sess-wait"}))


class PeerMessageTests(ServerCase):
    def fake_socket(self, sid):
        """Point the session's registry entry at a fake inbox socket server."""
        path = str(Path(self.tmp) / "inbox.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        self.addCleanup(srv.close)
        reg = self.home / "sessions" / "1001.json"
        data = json.loads(reg.read_text())
        data["messagingSocketPath"] = path
        reg.write_text(json.dumps(data))
        received = []

        def serve():
            conn, _ = srv.accept()
            with conn:
                received.append(conn.makefile("rb").readline())
        threading.Thread(target=serve, daemon=True).start()
        return received

    def test_disabled_by_default(self):
        self.start()
        self.assertEqual(self.post("/sessions/sess-busy/peer-message", {"text": "hi"})[0], 403)

    def test_delivers_one_line_to_fake_socket(self):
        received = self.fake_socket("sess-busy")
        self.start(allow_send=True)
        code, body = self.post("/sessions/sess-busy/peer-message", {"text": "ping"})
        self.assertEqual((code, body["delivered"]), (200, True))
        self.assertIn("cannot approve permission prompts", body["note"])
        deadline = time.time() + 2
        while not received and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(json.loads(received[0])["text"], "ping")
        self.assertTrue(received[0].endswith(b"\n"))

    def test_errors(self):
        self.start(allow_send=True)
        self.assertEqual(self.post("/sessions/sess-busy/peer-message", {"text": "x"})[0], 502)  # socket absent
        self.assertEqual(self.post("/sessions/sess-wait/peer-message", {"text": "x"})[0], 404)  # no socket path
        self.assertEqual(self.post("/sessions/nope/peer-message", {"text": "x"})[0], 404)
        self.assertEqual(self.post("/sessions/sess-busy/peer-message", {"text": ""})[0], 400)


class PeerAuthTests(ServerCase):
    token = "synthetic-test-token"

    def test_post_requires_token(self):
        self.start(allow_send=True)
        self.assertEqual(self.post("/sessions/sess-busy/peer-message", {"text": "x"})[0], 401)


if __name__ == "__main__":
    unittest.main()
