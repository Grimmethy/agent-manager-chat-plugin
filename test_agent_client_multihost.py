"""Multi-host tests for agent_client.list_all()/list_sessions()/enabled() against REAL
stub HTTP agents on loopback (threading + http.server, no Flask, no mocks of the HTTP
layer). Each stub records the Authorization header and path it served.

Run: python -m unittest test_agent_client_multihost -v
"""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).parent))

import agent_client  # noqa: E402

TOKEN_A = "synthetic-token-host-a"
TOKEN_B = "synthetic-token-host-b"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StubHandler(BaseHTTPRequestHandler):
    """Serves /sessions with a fixed row; records (path, Authorization header)."""

    rows = []  # set per-server in start_stub
    delay = 0.0  # seconds to sleep before responding (slow-host case)

    def do_GET(self):
        self.server.recorded.append((self.path, self.headers.get("Authorization")))
        if self.delay:
            time.sleep(self.delay)
        body = json.dumps(self.rows).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep test output clean
        pass


def start_stub(rows, delay=0.0):
    handler = type("Bound", (StubHandler,), {"rows": rows, "delay": delay})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.recorded = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def write_token_file(text):
    tf = Path(tempfile.mkdtemp()) / "tok"
    tf.write_text(text)
    return str(tf)


def plural_env(*entries):
    return {"AGENT_MANAGER_SESSION_AGENT_URLS": json.dumps(entries),
            "AGENT_MANAGER_SESSION_AGENT_URL": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": ""}


class ListAllTests(unittest.TestCase):
    def tearDown(self):
        for srv in getattr(self, "stubs", []):
            srv.shutdown()
            srv.server_close()

    def add_stub(self, srv):
        self.stubs = getattr(self, "stubs", []) + [srv]
        return self

    def test_both_hosts_listed_with_host_tag(self):
        rows_a = [{"id": "a1", "title": "alpha"}]
        rows_b = [{"id": "b1", "title": "beta"}]
        srv_a, url_a = start_stub(rows_a)
        srv_b, url_b = start_stub(rows_b)
        self.add_stub(srv_a).add_stub(srv_b)
        env = plural_env(
            {"host": "alpha", "url": url_a, "tokenFile": write_token_file(TOKEN_A)},
            {"host": "beta", "url": url_b, "tokenFile": write_token_file(TOKEN_B)},
        )
        with mock.patch.dict(os.environ, env):
            result = agent_client.list_all()
        self.assertEqual([r["id"] for r in result["sessions"]], ["a1", "b1"])
        self.assertEqual(result["sessions"][0]["host"], "alpha")
        self.assertEqual(result["sessions"][1]["host"], "beta")
        self.assertEqual(result["sessions"][0]["title"], "alpha")  # other fields untouched
        self.assertEqual(result["skipped"], [])

    def test_one_host_down_returns_the_other_and_skips(self):
        srv, url = start_stub([{"id": "live-1"}])
        self.add_stub(srv)
        down_url = f"http://127.0.0.1:{_free_port()}"
        env = plural_env(
            {"host": "live", "url": url, "tokenFile": write_token_file(TOKEN_A)},
            {"host": "dead", "url": down_url, "tokenFile": write_token_file(TOKEN_B)},
        )
        with mock.patch.dict(os.environ, env):
            result = agent_client.list_all()
        self.assertEqual([r["id"] for r in result["sessions"]], ["live-1"])
        self.assertEqual(result["sessions"][0]["host"], "live")
        self.assertEqual(result["skipped"], [{"host": "dead", "reason": result["skipped"][0]["reason"]}])
        self.assertEqual(len(result["skipped"]), 1)

    def test_one_host_slow_is_skipped_others_returned(self):
        srv, url = start_stub([{"id": "fast-1"}])
        slow, slow_url = start_stub([{"id": "never-1"}], delay=2.0)
        self.add_stub(srv).add_stub(slow)
        env = plural_env(
            {"host": "fast", "url": url, "tokenFile": write_token_file(TOKEN_A)},
            {"host": "slow", "url": slow_url, "tokenFile": write_token_file(TOKEN_B)},
        )
        with mock.patch.dict(os.environ, env), mock.patch("agent_client.REQUEST_TIMEOUT", 0.2):
            result = agent_client.list_all()
        self.assertEqual([r["id"] for r in result["sessions"]], ["fast-1"])
        self.assertEqual(result["sessions"][0]["host"], "fast")
        self.assertEqual([s["host"] for s in result["skipped"]], ["slow"])

    def test_each_stub_received_only_its_own_token(self):
        srv_a, url_a = start_stub([{"id": "a1"}])
        srv_b, url_b = start_stub([{"id": "b1"}])
        self.add_stub(srv_a).add_stub(srv_b)
        env = plural_env(
            {"host": "alpha", "url": url_a, "tokenFile": write_token_file(TOKEN_A)},
            {"host": "beta", "url": url_b, "tokenFile": write_token_file(TOKEN_B)},
        )
        with mock.patch.dict(os.environ, env):
            agent_client.list_all()
        hdr_a = [h for p, h in srv_a.recorded if p.endswith("/sessions")]
        hdr_b = [h for p, h in srv_b.recorded if p.endswith("/sessions")]
        self.assertEqual(hdr_a, [f"Bearer {TOKEN_A}"])
        self.assertEqual(hdr_b, [f"Bearer {TOKEN_B}"])
        self.assertNotIn(TOKEN_B, " ".join(hdr_a))
        self.assertNotIn(TOKEN_A, " ".join(hdr_b))

    def test_host_without_token_file_gets_no_authorization_header(self):
        srv, url = start_stub([{"id": "open-1"}])
        self.add_stub(srv)
        env = plural_env({"host": "open", "url": url})
        with mock.patch.dict(os.environ, env):
            result = agent_client.list_all()
        self.assertEqual([r["id"] for r in result["sessions"]], ["open-1"])
        paths, hdrs = list(zip(*srv.recorded)) if srv.recorded else ([], ())
        self.assertTrue(any(p.endswith("/sessions") for p in paths))
        self.assertEqual([h for p, h in srv.recorded if p.endswith("/sessions")], [None])

    def test_singular_config_returns_rows_tagged_default(self):
        srv, url = start_stub([{"id": "s1", "title": "solo"}])
        self.add_stub(srv)
        env = {"AGENT_MANAGER_SESSION_AGENT_URLS": "",
               "AGENT_MANAGER_SESSION_AGENT_URL": url,
               "AGENT_MANAGER_SESSION_AGENT_TOKEN": TOKEN_A,
               "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": ""}
        with mock.patch.dict(os.environ, env):
            result = agent_client.list_all()
        self.assertEqual([r["id"] for r in result["sessions"]], ["s1"])
        self.assertEqual(result["sessions"][0]["host"], "default")
        self.assertEqual(result["skipped"], [])
        self.assertEqual([h for p, h in srv.recorded if p.endswith("/sessions")],
                         [f"Bearer {TOKEN_A}"])

    def test_every_host_failing_makes_list_sessions_raise(self):
        down_a = f"http://127.0.0.1:{_free_port()}"
        down_b = f"http://127.0.0.1:{_free_port()}"
        env = plural_env(
            {"host": "a", "url": down_a, "tokenFile": write_token_file(TOKEN_A)},
            {"host": "b", "url": down_b, "tokenFile": write_token_file(TOKEN_B)},
        )
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(agent_client.AgentError):
                agent_client.list_sessions()

    def test_single_failing_host_still_raises(self):
        down = f"http://127.0.0.1:{_free_port()}"
        env = {"AGENT_MANAGER_SESSION_AGENT_URLS": "",
               "AGENT_MANAGER_SESSION_AGENT_URL": down,
               "AGENT_MANAGER_SESSION_AGENT_TOKEN": "",
               "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": ""}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(agent_client.enabled())
            with self.assertRaises(agent_client.AgentError):
                agent_client.list_sessions()

    def test_enabled_follows_configuration(self):
        env_off = {"AGENT_MANAGER_SESSION_AGENT_URLS": "",
                   "AGENT_MANAGER_SESSION_AGENT_URL": "",
                   "AGENT_MANAGER_SESSION_AGENT_TOKEN": "",
                   "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": ""}
        with mock.patch.dict(os.environ, env_off):
            self.assertFalse(agent_client.enabled())
        with mock.patch.dict(os.environ, plural_env({"host": "x", "url": "http://127.0.0.1:1"})):
            self.assertTrue(agent_client.enabled())


if __name__ == "__main__":
    unittest.main()
