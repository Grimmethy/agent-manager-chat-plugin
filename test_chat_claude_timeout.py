"""Tests for the Chat Claude turn timeout (2026-11-03): a Chat turn is an agentic session, but claude -p was killed
at the 300 s default ("spawnSync claude ETIMEDOUT" on a repo-trial request). claude_client.generate() takes an
optional timeout_s that reaches both the Node request (timeoutMs) and the wrapping subprocess bound (+30 s), and
chat_sessions passes CHAT_CLAUDE_TIMEOUT_S (default 1200 s, AGENT_MANAGER_CHAT_CLAUDE_TIMEOUT_S override).

subprocess.run and claude_client.generate are mocked, so no node or claude CLI is ever launched.

Run: python -m unittest test_chat_claude_timeout -v
"""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

# claude_client.py does `from app import CORE_REPO_ROOT`, and app.py pulls in flask; only the constant is needed here.
try:
    import app  # noqa: F401
except Exception:
    import types
    _fake_app = types.ModuleType("app")
    _fake_app.CORE_REPO_ROOT = Path(__file__).parent
    sys.modules["app"] = _fake_app

import accounts  # noqa: E402
import chat_sessions  # noqa: E402
import claude_client  # noqa: E402
import model_stats_client  # noqa: E402
from claude_client import ClaudeClientError  # noqa: E402


def _ok_result():
    m = mock.MagicMock()
    m.returncode = 0
    m.stdout = json.dumps({"response": "ok", "thinking": "", "model": "sonnet", "sessionId": "s1"})
    m.stderr = ""
    return m


def _run_generate(**kwargs):
    """Call generate() with subprocess.run mocked; return (run kwargs, request JSON written for node)."""
    seen = {}

    def fake_run(argv, **run_kwargs):
        seen["run_kwargs"] = run_kwargs
        seen["request"] = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        return _ok_result()

    with mock.patch("claude_client.subprocess.run", side_effect=fake_run):
        claude_client.generate("hello", **kwargs)
    return seen["run_kwargs"], seen["request"]


class GenerateTimeout(unittest.TestCase):
    def test_default_path_is_unchanged(self):
        run_kwargs, request = _run_generate()
        self.assertEqual(run_kwargs["timeout"], 330)
        self.assertNotIn("timeoutMs", request)

    def test_timeout_s_reaches_the_request_and_the_subprocess_bound(self):
        run_kwargs, request = _run_generate(timeout_s=1200)
        self.assertEqual(request["timeoutMs"], 1200000)
        self.assertEqual(run_kwargs["timeout"], 1230)

    def test_timeout_error_names_the_real_bound(self):
        expired = subprocess.TimeoutExpired(cmd="node", timeout=1230)
        with mock.patch("claude_client.subprocess.run", side_effect=expired):
            with self.assertRaises(ClaudeClientError) as ctx:
                claude_client.generate("hello", timeout_s=1200)
        self.assertIn("1230s", str(ctx.exception))
        self.assertNotIn("330s", str(ctx.exception))

    def test_default_timeout_error_still_names_330(self):
        expired = subprocess.TimeoutExpired(cmd="node", timeout=330)
        with mock.patch("claude_client.subprocess.run", side_effect=expired):
            with self.assertRaises(ClaudeClientError) as ctx:
                claude_client.generate("hello")
        self.assertIn("within 330s", str(ctx.exception))


class ChatPassesTimeout(unittest.TestCase):
    def test_send_claude_passes_the_chat_timeout(self):
        session = {"id": "chat-test-session", "repoRoot": "/tmp/fake-repo", "claudeSessionId": "sess-1", "model": "sonnet"}
        calls = []

        def fake_generate(message, **kwargs):
            calls.append(kwargs)
            return {"sessionId": "sess-1", "model": "sonnet", "response": "ok"}

        with mock.patch.object(claude_client, "generate", side_effect=fake_generate), \
             mock.patch.object(accounts, "load", return_value=[]), \
             mock.patch.object(accounts, "live_label", return_value=None), \
             mock.patch.object(model_stats_client, "record_call"), \
             contextlib.redirect_stderr(io.StringIO()):
            chat_sessions._send_claude(session, "hello", Path("/tmp/fake-storage"))
        self.assertTrue(calls, "claude_client.generate was not called")
        self.assertEqual(calls[-1]["timeout_s"], chat_sessions.CHAT_CLAUDE_TIMEOUT_S)


class TimeoutSetting(unittest.TestCase):
    def test_default_is_twenty_minutes(self):
        self.assertEqual(chat_sessions.chat_claude_timeout_s({}), 1200)

    def test_valid_override(self):
        self.assertEqual(chat_sessions.chat_claude_timeout_s({"AGENT_MANAGER_CHAT_CLAUDE_TIMEOUT_S": "60"}), 60)

    def test_invalid_values_fall_back(self):
        for bad in ("abc", "-5", "0", "", "inf", "nan"):
            with self.subTest(bad=bad):
                self.assertEqual(chat_sessions.chat_claude_timeout_s({"AGENT_MANAGER_CHAT_CLAUDE_TIMEOUT_S": bad}), 1200)


if __name__ == "__main__":
    unittest.main()
