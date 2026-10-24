"""HUB0009 9/13 -- Chat Claude sessions allow the cross-session messaging tools.

CHAT_CLAUDE_ALLOWED_TOOLS must still contain the original six tools in their
original order, with SendMessage and ListAgents appended. claude_client.generate
is replaced by a recorder; no real tokens or credentials appear here.
"""
import contextlib
import io
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import accounts
import chat_sessions
import claude_client
import model_stats_client

ORIGINAL_SIX = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]
ALL_EIGHT = ORIGINAL_SIX + ["SendMessage", "ListAgents"]


def _session():
    return {
        "repoRoot": "/tmp/fake-repo",
        "claudeSessionId": "sess-1",  # not the first turn: skip the preamble path
        "model": "opus",
    }


class AllowedToolsConstantTests(unittest.TestCase):
    def test_constant_has_all_eight_tools_exactly_once(self):
        tools = chat_sessions.CHAT_CLAUDE_ALLOWED_TOOLS.split(",")
        self.assertEqual(len(tools), 8)
        self.assertEqual(Counter(tools), Counter(ALL_EIGHT))

    def test_constant_starts_with_original_six_in_order(self):
        tools = chat_sessions.CHAT_CLAUDE_ALLOWED_TOOLS.split(",")
        self.assertEqual(tools[:6], ORIGINAL_SIX)


class SendClaudeAllowedToolsTests(unittest.TestCase):
    def test_send_claude_passes_allowed_tools_to_generate(self):
        calls = []

        def fake_generate(message, **kwargs):
            calls.append(kwargs)
            return {"sessionId": "sess-1", "model": "opus", "response": "ok"}

        with mock.patch.object(claude_client, "generate", side_effect=fake_generate), \
             mock.patch.object(accounts, "load", return_value=[]), \
             mock.patch.object(accounts, "live_label", return_value=None), \
             mock.patch.object(model_stats_client, "record_call"):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                chat_sessions._send_claude(_session(), "hello", Path("/tmp/fake-storage"))
        self.assertTrue(calls, "claude_client.generate was not called")
        self.assertEqual(
            calls[-1]["allowed_tools"],
            "Read,Grep,Glob,Edit,Write,Bash,SendMessage,ListAgents",
        )


if __name__ == "__main__":
    unittest.main()
