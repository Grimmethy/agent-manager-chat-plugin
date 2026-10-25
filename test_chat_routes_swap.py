"""HUB0008 7/11 (multi-chat workspace): route tests for POST /api/chat/<id>/swap.

Covers: local -> claude with a known account; an unknown claude account -> 400;
a bad provider -> 400; an unknown/inactive session -> 404; leaving local while
reserved releases the reservation (internal reserve off + set_reserved False);
swapping TO local never acquires a reservation; the older-turns summary runs
only when handoff.needs_summary is true; a summarizer error still swaps (200);
the response carries the marker row and a swaps entry and never a token or
tokenFile.

All internals are faked: the internal reserve call, the summarizer, and (where
it matters) handoff.needs_summary -- no network, no model, no real credentials
(tokenFile paths are fake strings and are never opened).

Run: python -m unittest test_chat_routes_swap test_chat_routes -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import accounts  # noqa: E402
import chat_sessions  # noqa: E402
import handoff  # noqa: E402
import internal_api_client  # noqa: E402
import ollama_client  # noqa: E402

SID = "chat-swap-test"

FAKE_TOKEN_FILE = "/secrets-outside-repo/fake-token-file"  # opaque fake path, never opened


def _session(provider="local", reserved=False, status="active"):
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": SID, "provider": provider, "model": "fake-model", "effort": None,
        "roots": [root], "repoRoot": root, "instancesDir": None,
        "claudeSessionId": None,
        "transcript": [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "hi, how can I help?"},
        ],
        "status": status, "startedAt": "2026-10-18T00:00:00Z", "endedAt": None,
        "reserved": reserved,
    }


class SwapRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

        # Default: no summary needed, and the real summarizer (which would call
        # the local model) is faked away so no test ever touches a model.
        self.needs_patcher = mock.patch.object(handoff, "needs_summary", return_value=False)
        self.needs = self.needs_patcher.start()
        self.addCleanup(self.needs_patcher.stop)
        self.summarize_patcher = mock.patch.object(chat_sessions, "summarize_older")
        self.summarize = self.summarize_patcher.start()
        self.addCleanup(self.summarize_patcher.stop)

    def _store(self, provider="local", reserved=False, status="active"):
        chat_sessions._write_sessions(self.storage, {SID: _session(provider, reserved, status)})

    def _stored(self):
        return chat_sessions.get_session(self.storage, SID)

    def _post(self, body, sid=SID):
        return self.client.post(f"/api/chat/{sid}/swap", json=body)

    def _add_account(self, label="acct-1"):
        accounts.add(self.storage, label, "example.invalid", FAKE_TOKEN_FILE)


class SwapValidationTest(SwapRouteTestBase):
    def test_local_to_claude_with_a_known_account(self):
        self._add_account()
        self._store("local")
        resp = self._post({"provider": "claude", "account": "acct-1"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["provider"], "claude")
        self.assertEqual(body["account"], "acct-1")
        self.assertEqual(self._stored()["provider"], "claude")
        self.assertEqual(self._stored()["account"], "acct-1")
        # local had no reservation and a swap TO claude never acquires one
        self.assertFalse(self._stored()["reserved"])

    def test_claude_account_omitted_follows_the_live_account(self):
        self._add_account()
        self._store("local")
        resp = self._post({"provider": "claude"})
        self.assertEqual(resp.status_code, 200)
        # account None: the session follows whatever account is live
        self.assertIsNone(resp.get_json()["account"])
        self.assertIsNone(self._stored()["account"])

    def test_unknown_claude_account_is_400(self):
        self._add_account("acct-1")
        self._store("local")
        resp = self._post({"provider": "claude", "account": "no-such-account"})
        self.assertEqual(resp.status_code, 400)
        # nothing was mutated
        self.assertEqual(self._stored()["provider"], "local")

    def test_bad_or_missing_provider_is_400(self):
        self._store("local")
        self.assertEqual(self._post({"provider": "openai"}).status_code, 400)
        self.assertEqual(self._post({}).status_code, 400)
        self.assertEqual(self._stored()["provider"], "local")

    def test_unknown_or_inactive_session_is_404(self):
        self._store("local")
        self.assertEqual(self._post({"provider": "claude"}, sid="no-such-id").status_code, 404)
        self._store("local", status="ended")
        self.assertEqual(self._post({"provider": "claude"}).status_code, 404)


class SwapReservationTest(SwapRouteTestBase):
    def test_leaving_local_while_reserved_releases_the_reservation(self):
        self._add_account()
        self._store("local", reserved=True)
        with mock.patch.object(internal_api_client, "reserve") as reserve:
            resp = self._post({"provider": "claude", "account": "acct-1"})
        self.assertEqual(resp.status_code, 200)
        # released exactly as api_chat_reserve does: internal reserve OFF...
        self.assertEqual(reserve.call_args[0], (SID, ollama_client.MODEL(), False))
        # ...and the dashboard's display flag cleared
        self.assertFalse(self._stored()["reserved"])
        self.assertEqual(self._stored()["provider"], "claude")

    def test_leaving_local_not_reserved_makes_no_reserve_call(self):
        self._add_account()
        self._store("local", reserved=False)
        with mock.patch.object(internal_api_client, "reserve") as reserve:
            resp = self._post({"provider": "claude", "account": "acct-1"})
        self.assertEqual(resp.status_code, 200)
        reserve.assert_not_called()

    def test_swapping_to_local_never_acquires_a_reservation(self):
        self._store("claude", reserved=False)
        with mock.patch.object(internal_api_client, "reserve") as reserve:
            resp = self._post({"provider": "local"})
        self.assertEqual(resp.status_code, 200)
        reserve.assert_not_called()  # the /reserve flow is the only way to reserve
        self.assertEqual(self._stored()["provider"], "local")
        self.assertIsNone(self._stored()["account"])  # account ignored for local


class SwapSummaryTest(SwapRouteTestBase):
    def test_summary_runs_only_when_needed(self):
        self._add_account()
        self._store("local")

        self.needs.return_value = False
        self.assertEqual(self._post({"provider": "claude", "account": "acct-1"}).status_code, 200)
        self.summarize.assert_not_called()

        self.needs.return_value = True
        self.summarize.reset_mock()
        self.summarize.return_value = dict(self._stored(), summary={
            "upToIndex": 1, "text": "earlier: hello", "at": "2026-10-18T00:00:01Z"})
        resp = self._post({"provider": "local"})
        self.assertEqual(resp.status_code, 200)
        self.summarize.assert_called_once_with(self.storage, SID)

    def test_a_summarizer_error_does_not_block_the_swap(self):
        self._add_account()
        self._store("local")
        self.needs.return_value = True
        self.summarize.side_effect = RuntimeError("summariser blew up")
        resp = self._post({"provider": "claude", "account": "acct-1"})
        self.assertEqual(resp.status_code, 200)  # the swap itself succeeded
        body = resp.get_json()
        self.assertEqual(body["provider"], "claude")
        self.assertEqual(body["account"], "acct-1")
        self.assertEqual(self._stored()["provider"], "claude")


class SwapResponseShapeTest(SwapRouteTestBase):
    def test_response_has_the_marker_row_and_a_swaps_entry(self):
        self._add_account()
        self._store("local")
        resp = self._post({"provider": "claude", "account": "acct-1"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(len(body["swaps"]), 1)
        entry = body["swaps"][0]
        self.assertEqual(entry["from"]["provider"], "local")
        self.assertEqual(entry["to"]["provider"], "claude")
        self.assertEqual(entry["to"]["account"], "acct-1")
        markers = [t for t in body["transcript"] if t.get("marker") is True]
        self.assertEqual(len(markers), 1)
        # every pre-swap row is still there, verbatim, before the marker
        self.assertEqual(body["transcript"][:2], _session()["transcript"])
        self.assertIn("Switched from", body["transcript"][-1]["text"])

    def test_response_contains_no_token_or_tokenfile(self):
        self._add_account()
        self._store("local")
        # even if a secret somehow lived on the session, it must never be echoed
        s = self._stored()
        s["token"] = "fake-token-string"
        s["tokenFile"] = FAKE_TOKEN_FILE
        chat_sessions._write_session(self.storage, s)

        resp = self._post({"provider": "claude", "account": "acct-1"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertNotIn("token", body)
        self.assertNotIn("tokenFile", body)
        self.assertNotIn("fake-token-string", resp.get_data(as_text=True))
        self.assertNotIn(FAKE_TOKEN_FILE, resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
