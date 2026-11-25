"""ollama_client.generate must never make Ollama reload the model: same num_ctx as the pipeline lanes, and a never-expire keep_alive."""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ollama_client  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _call(env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(json.dumps({"response": "ok"}).encode("utf-8"))

    base = {k: v for k, v in os.environ.items() if k != "AGENT_MANAGER_LANE_NUM_CTX"}
    with mock.patch.dict(os.environ, {**base, **env}, clear=True), mock.patch("urllib.request.urlopen", fake_urlopen):
        ollama_client.generate("hi")
    return seen["body"]


class OllamaClientPinning(unittest.TestCase):
    def test_default_matches_pipeline_pin_and_never_expires(self):
        body = _call({"LOCAL_MODEL": "m"})
        self.assertEqual(body["options"]["num_ctx"], 49152)
        self.assertEqual(body["keep_alive"], "8760h")

    def test_lane_env_overrides_num_ctx(self):
        self.assertEqual(_call({"LOCAL_MODEL": "m", "AGENT_MANAGER_LANE_NUM_CTX": "32768"})["options"]["num_ctx"], 32768)

    def test_bad_lane_env_falls_back_to_default(self):
        for bad in ("", "abc", "0", "-5"):
            self.assertEqual(_call({"LOCAL_MODEL": "m", "AGENT_MANAGER_LANE_NUM_CTX": bad})["options"]["num_ctx"], 49152, bad)


class OllamaClientArbiterHeader(unittest.TestCase):
    """FF-1: generate() must label every Ollama request with X-Arbiter-Class."""

    def _capture_request(self, env):
        """Run generate() and return both the request object and the parsed body."""
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["req"] = req
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp(json.dumps({"response": "ok"}).encode("utf-8"))

        base = {k: v for k, v in os.environ.items() if k not in ("AGENT_MANAGER_LANE_NUM_CTX", "OLLAMA_ARBITER_CLASS")}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True), mock.patch("urllib.request.urlopen", fake_urlopen):
            ollama_client.generate("hi")
        return seen["req"], seen["body"]

    def test_default_header_value(self):
        """No env var -> header is 'interactive'."""
        req, _ = self._capture_request({"LOCAL_MODEL": "m"})
        # urllib.request.Request normalises header names to title-case on the wire
        # but stores them lowercase in req.headers; check the dict directly.
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers.get("x-arbiter-class"), "interactive")

    def test_env_var_override(self):
        """OLLAMA_ARBITER_CLASS=custom -> header value is 'custom'."""
        req, _ = self._capture_request({"LOCAL_MODEL": "m", "OLLAMA_ARBITER_CLASS": "custom"})
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertEqual(headers.get("x-arbiter-class"), "custom")

    def test_empty_env_var_suppresses_header(self):
        """OLLAMA_ARBITER_CLASS='' -> no X-Arbiter-Class header sent."""
        req, _ = self._capture_request({"LOCAL_MODEL": "m", "OLLAMA_ARBITER_CLASS": ""})
        headers = {k.lower(): v for k, v in req.headers.items()}
        self.assertNotIn("x-arbiter-class", headers)

    def test_body_unchanged_with_header(self):
        """Adding the header must not alter the pinned num_ctx/keep_alive or prompt shape."""
        _, body = self._capture_request({"LOCAL_MODEL": "m"})
        self.assertEqual(body["options"]["num_ctx"], 49152)
        self.assertEqual(body["keep_alive"], "8760h")
        self.assertEqual(body["prompt"], "hi")
        self.assertEqual(body["stream"], False)
        self.assertEqual(body["model"], "m")

    def test_server_ignores_header_succeeds(self):
        """A real HTTP server that does not act on the header still gets a successful response."""
        import http.server
        import threading

        class _Handler(http.server.BaseHTTPRequestHandler):
            seen_headers = None

            def do_POST(self):
                _Handler.seen_headers = dict(self.headers)
                length = int(self.headers.get("Content-Length", 0))
                _ = self.rfile.read(length)
                # Deliberately ignore X-Arbiter-Class -- just reply 200.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"response": "from-server"}).encode("utf-8"))

            def log_message(self, *_a, **_kw):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            env = {
                "OLLAMA_URL": f"http://127.0.0.1:{port}",
                "OLLAMA_ARBITER_CLASS": "labeled",
                "LOCAL_MODEL": "m",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                result = ollama_client.generate("probe")
            self.assertEqual(result["response"], "from-server")
            # Confirm the header was actually transmitted (server saw it, chose to ignore).
            self.assertEqual(_Handler.seen_headers.get("X-Arbiter-Class"), "labeled")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
