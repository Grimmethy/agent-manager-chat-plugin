"""Tests for claude_client.generate()'s per-call Claude account selection via a token
file (HUB0008 2/11): token_file, when given, is read at call time and handed to the
`node` child as CLAUDE_CODE_OAUTH_TOKEN in the CHILD env only -- never os.environ,
never the request JSON, never a log line; when None, the call is byte-identical to
today's (no env keyword at all).

Uses a fake token string only; subprocess.run is mocked, so no node or claude CLI is
ever launched.

Run: python -m unittest test_claude_client_account -v
"""
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

# claude_client.py does `from app import CORE_REPO_ROOT`, and app.py pulls in flask.
# In environments without flask we still only need that one constant (the JS path is
# never executed here -- subprocess.run is mocked), so fall back to a minimal stand-in.
try:
    import app  # noqa: F401
except Exception:
    import types
    _fake_app = types.ModuleType("app")
    _fake_app.CORE_REPO_ROOT = Path(__file__).parent
    sys.modules["app"] = _fake_app

import claude_client  # noqa: E402
from claude_client import ClaudeClientError  # noqa: E402

FAKE_TOKEN = "fake-oauth-token-abc123"  # fake string, never a real credential


def _ok_result():
    """A subprocess.run result good enough for generate()'s happy path."""
    m = mock.MagicMock()
    m.returncode = 0
    m.stdout = json.dumps({"response": "ok", "sessionId": "sess-1"})
    m.stderr = ""
    return m


class TokenFileAccountTests(unittest.TestCase):
    def _token_file(self, content=FAKE_TOKEN + "\n"):
        f = tempfile.NamedTemporaryFile(delete=False, mode="w", suffix=".tok")
        f.write(content)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_child_env_carries_token_and_os_environ_unchanged(self):
        tf = self._token_file()
        before = dict(os.environ)
        with mock.patch("claude_client.subprocess.run", return_value=_ok_result()) as run:
            claude_client.generate("hello", token_file=tf)
        kwargs = run.call_args[1]
        self.assertEqual(kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"], FAKE_TOKEN)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", before)  # pre-test precondition
        self.assertEqual(dict(os.environ), before)  # generate() never mutates os.environ

    def test_request_json_does_not_contain_token(self):
        tf = self._token_file()
        seen = {}

        def capture(args, **kwargs):
            req_text = Path(args[2]).read_text(encoding="utf-8")  # file still on disk here
            seen["req"] = req_text
            return _ok_result()

        with mock.patch("claude_client.subprocess.run", side_effect=capture):
            claude_client.generate("hello", token_file=tf)
        self.assertNotIn(FAKE_TOKEN, seen["req"])
        json.loads(seen["req"])  # still valid JSON, just without any token

    def test_none_token_file_keeps_call_unchanged(self):
        with mock.patch("claude_client.subprocess.run", return_value=_ok_result()) as run:
            claude_client.generate("hello")  # token_file omitted entirely
            claude_client.generate("hello", token_file=None)
        for call in run.call_args_list:
            self.assertNotIn("env", call[1])  # no env keyword at all -- child inherits, as today

    def test_unreadable_token_file_raises_without_path(self):
        missing = "/nonexistent/claude-account-token-file-xyz"
        with self.assertRaises(ClaudeClientError) as ctx:
            claude_client.generate("hello", token_file=missing)
        msg = str(ctx.exception)
        self.assertEqual(msg, "account token file is not readable")
        self.assertNotIn(missing, msg)

    def test_empty_token_file_raises_without_path_or_token(self):
        tf = self._token_file(content="   \n\t\n")
        with self.assertRaises(ClaudeClientError) as ctx:
            claude_client.generate("hello", token_file=tf)
        msg = str(ctx.exception)
        self.assertEqual(msg, "account token file is not readable")
        self.assertNotIn(tf, msg)
        self.assertNotIn(FAKE_TOKEN, msg)

    def test_token_never_appears_in_logs(self):
        tf = self._token_file()

        class _Sink(logging.Handler):
            def __init__(self):
                super().__init__(level=logging.DEBUG)
                self.lines = []

            def emit(self, record):
                self.lines.append(self.format(record))

        sink = _Sink()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(sink)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, sink)
        self.addCleanup(root.setLevel, old_level)
        try:
            with mock.patch("claude_client.subprocess.run", return_value=_ok_result()):
                claude_client.generate("hello", token_file=tf)
        finally:
            for line in sink.lines:
                self.assertNotIn(FAKE_TOKEN, line)


if __name__ == "__main__":
    unittest.main()
