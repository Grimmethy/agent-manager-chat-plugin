"""HUB0008 3/11 -- _send_claude picks the Claude account per turn.

The chat's own 'account' label wins; a session without one follows the live
account (re-resolved every turn). No registry entry (nothing registered, or an
unknown label) means token_file=None -- today's process-environment behaviour.
All tokens/paths here are fake strings; claude_client.generate is replaced by
a recorder and accounts.load / accounts.live_label are canned.
"""
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import accounts
import chat_sessions
import claude_client
import model_stats_client

TOKEN_A = "/secrets/tokens/claude-a"  # fake -- never a real credential
TOKEN_B = "/secrets/tokens/claude-b"  # fake -- never a real credential


def _registry():
    return [
        {"label": "a", "host": "host-a", "tokenFile": TOKEN_A, "live": True},
        {"label": "b", "host": "host-b", "tokenFile": TOKEN_B, "live": False},
    ]


def _session(account=None):
    session = {
        "id": "chat-test-session",
        "repoRoot": "/tmp/fake-repo",
        "claudeSessionId": "sess-1",  # not the first turn: skip the preamble path
        "model": "opus",
    }
    if account is not None:
        session["account"] = account
    return session


def _send(session, live="a", registry=None):
    """Run one turn of _send_claude with canned registry + a recording generate."""
    registry = [] if registry is None else registry
    calls = []

    def fake_generate(message, **kwargs):
        calls.append(kwargs)
        return {"sessionId": "sess-1", "model": "opus", "response": "ok"}

    with mock.patch.object(claude_client, "generate", side_effect=fake_generate), \
         mock.patch.object(accounts, "load", return_value=registry), \
         mock.patch.object(accounts, "live_label", return_value=live), \
         mock.patch.object(model_stats_client, "record_call"):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            chat_sessions._send_claude(session, "hello", Path("/tmp/fake-storage"))
    assert calls, "claude_client.generate was not called"
    return calls[-1], stderr.getvalue()


class SendClaudeAccountTests(unittest.TestCase):
    def test_session_with_account_b_gets_b_token(self):
        kwargs, out = _send(_session(account="b"), live="a", registry=_registry())
        self.assertEqual(kwargs["token_file"], TOKEN_B)
        self.assertEqual(out, "")  # found: no warning

    def test_session_without_account_gets_live_account_token(self):
        kwargs, out = _send(_session(), live="a", registry=_registry())
        self.assertEqual(kwargs["token_file"], TOKEN_A)
        self.assertEqual(out, "")

    def test_live_change_followed_only_by_sessions_without_account(self):
        follow = _session()
        pinned = _session(account="a")
        kwargs, _ = _send(follow, live="a", registry=_registry())
        self.assertEqual(kwargs["token_file"], TOKEN_A)
        # Live account swaps to b: the no-account session follows, the pinned one does not.
        kwargs, _ = _send(follow, live="b", registry=_registry())
        self.assertEqual(kwargs["token_file"], TOKEN_B)
        kwargs, _ = _send(pinned, live="b", registry=_registry())
        self.assertEqual(kwargs["token_file"], TOKEN_A)

    def test_empty_registry_gives_none(self):
        kwargs, out = _send(_session(), live=None, registry=[])
        self.assertIsNone(kwargs["token_file"])
        self.assertEqual(out, "")  # nothing was set: no warning

    def test_unknown_label_gives_none_and_one_warning_without_path(self):
        kwargs, out = _send(_session(account="ghost"), live="a", registry=_registry())
        self.assertIsNone(kwargs["token_file"])
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)  # exactly ONE warning
        self.assertIn("ghost", lines[0])
        self.assertNotIn("/", lines[0])  # never a path
        self.assertNotIn("secrets", lines[0])


if __name__ == "__main__":
    unittest.main()
