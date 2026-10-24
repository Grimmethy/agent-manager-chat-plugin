"""Tests for the per-host event stream in agent_client.py: open_events(host=...)
and host tagging in filter_events. Runs against two stub SSE servers on loopback;
each stub records the path and Authorization header it receives so we can assert
each stub saw ONLY its own token. All token strings are fake ("fake-token-a" /
"fake-token-b") -- never real credentials.

Run: python -m unittest test_agent_client_events -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import agent_client  # noqa: E402

TOKEN_A = "fake-token-a"
TOKEN_B = "fake-token-b"

BODY_A = (
    b": keepalive\n\n"
    b"event: transcript.append\ndata: {\"id\": \"a1\", \"entries\": [{\"text\": \"private-a\"}]}\n\n"
    b"event: session.updated\ndata: {\"id\": \"sess-a\", \"status\": \"busy\"}\n\n"
    b"event: session.removed\ndata: {\"id\": \"sess-a\"}\n\n"
)
BODY_B = (
    b": keepalive\n\n"
    b"event: session.updated\ndata: {\"id\": \"sess-b\", \"status\": \"idle\"}\n\n"
    b"event: session.removed\ndata: {\"id\": \"sess-b\"}\n\n"
)


class _SSEHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        srv = self.server
        srv.seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
        if self.path.split("?", 1)[0] != "/events":
            self.send_response(404)
            self.end_headers()
            return
        body = srv.body
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


class _Stub:
    def __init__(self, body):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
        self.server.seen = []
        self.server.body = body
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def events_paths(self):
        return [r for r in self.server.seen if r["path"].split("?", 1)[0] == "/events"]


def _data_payloads(out: bytes) -> list:
    return [json.loads(l[6:]) for l in out.decode().splitlines() if l.startswith("data: ")]


class OpenEventsHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tf_a = self.tmp / "tok_a"
        self.tf_b = self.tmp / "tok_b"
        self.tf_a.write_text(TOKEN_A + "\n")
        self.tf_b.write_text(TOKEN_B + "\n")
        self.a = _Stub(BODY_A)
        self.b = _Stub(BODY_B)
        self.addCleanup(self.a.stop)
        self.addCleanup(self.b.stop)

    def plural_env(self):
        return mock.patch.dict(os.environ, {
            "AGENT_MANAGER_SESSION_AGENT_URL": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": "",
            "AGENT_MANAGER_SESSION_AGENT_URLS": json.dumps([
                {"host": "alpha", "url": self.a.url, "tokenFile": str(self.tf_a)},
                {"host": "beta", "url": self.b.url, "tokenFile": str(self.tf_b)},
            ]),
        })

    def test_two_hosts_each_tagged_each_own_token(self):
        with self.plural_env():
            ra = agent_client.open_events("alpha")
            out_a = b"".join(agent_client.iter_chunks(ra))
            ra.close()
            rb = agent_client.open_events("beta")
            out_b = b"".join(agent_client.iter_chunks(rb))
            rb.close()
        filt_a = b"".join(agent_client.filter_events(iter([out_a]), host="alpha"))
        filt_b = b"".join(agent_client.filter_events(iter([out_b]), host="beta"))
        datas_a = _data_payloads(filt_a)
        datas_b = _data_payloads(filt_b)
        self.assertTrue(datas_a and all(d["host"] == "alpha" for d in datas_a))
        self.assertTrue(datas_b and all(d["host"] == "beta" for d in datas_b))
        # Each stub received only its own token.
        for req in self.a.server.seen:
            self.assertEqual(req["auth"], f"Bearer {TOKEN_A}")
        for req in self.b.server.seen:
            self.assertEqual(req["auth"], f"Bearer {TOKEN_B}")

    def test_only_urls_set_opens_named_host_events(self):
        # Regression: with ONLY AGENT_MANAGER_SESSION_AGENT_URLS set (no singular
        # variable) open_events('<name>') must reach that stub's /events path.
        with self.plural_env():
            rb = agent_client.open_events("beta")
            out = b"".join(agent_client.iter_chunks(rb))
            rb.close()
        self.assertIn(b"event: session.updated", out)
        self.assertTrue(self.b.events_paths())
        self.assertEqual(self.a.events_paths(), [])
        for req in self.b.server.seen:
            self.assertEqual(req["auth"], f"Bearer {TOKEN_B}")
        self.assertEqual(self.a.server.seen, [])  # alpha's stub was never contacted

    def test_session_removed_names_host_and_transcript_dropped(self):
        with self.plural_env():
            ra = agent_client.open_events("alpha")
            out = b"".join(agent_client.iter_chunks(ra))
            ra.close()
        filt = b"".join(agent_client.filter_events(iter([out]), host="alpha"))
        removed = [b for b in filt.split(b"\n\n") if b"session.removed" in b]
        self.assertTrue(removed)
        data = [l for l in removed[0].split(b"\n") if l.startswith(b"data: ")][0][6:]
        self.assertEqual(json.loads(data)["host"], "alpha")
        self.assertNotIn(b"transcript.append", filt)
        self.assertNotIn(b"private-a", filt)
        self.assertIn(b": keepalive", filt)

    def test_no_name_with_two_hosts_raises(self):
        with self.plural_env():
            with self.assertRaises(agent_client.AgentError):
                agent_client.open_events()
            with self.assertRaises(agent_client.AgentError):
                agent_client.open_events(None)

    def test_unknown_host_name_raises(self):
        with self.plural_env():
            with self.assertRaises(agent_client.AgentError):
                agent_client.open_events("nope")

    def test_singular_config_host_none_tags_default(self):
        with mock.patch.dict(os.environ, {
            "AGENT_MANAGER_SESSION_AGENT_URLS": "",
            "AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE": "",
            "AGENT_MANAGER_SESSION_AGENT_URL": self.a.url,
            "AGENT_MANAGER_SESSION_AGENT_TOKEN": TOKEN_A,
        }):
            r = agent_client.open_events()
            out = b"".join(agent_client.iter_chunks(r))
            r.close()
            filt = b"".join(agent_client.filter_events(iter([out])))
        datas = _data_payloads(filt)
        self.assertTrue(datas and all(d["host"] == "default" for d in datas))
        for req in self.a.server.seen:
            self.assertEqual(req["auth"], f"Bearer {TOKEN_A}")
        self.assertEqual(self.b.server.seen, [])

    def test_transcript_dropped_and_non_json_data_unchanged(self):
        body = (
            b": keepalive\n\n"
            b"event: session.updated\ndata: {\"id\": \"x\"}\n\n"
            b"event: transcript.append\ndata: {\"id\": \"x\", \"entries\": [{\"text\": \"secret\"}]}\n\n"
            b"event: session.updated\ndata: not-json\n\n"
        )
        out = b"".join(agent_client.filter_events(iter([body]), host="alpha"))
        self.assertNotIn(b"transcript.append", out)
        self.assertNotIn(b"secret", out)
        self.assertIn(b'"host": "alpha"', out)
        self.assertIn(b"data: not-json", out)


if __name__ == "__main__":
    unittest.main()
