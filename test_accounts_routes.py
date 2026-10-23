"""Route tests for routes/accounts.py (HUB0006 piece 2/7): GET /api/chat/accounts
and POST /api/chat/accounts/<label>/live.

Flask test client against a tmp CHAT_STORAGE_DIR (same mock.patch.object pattern
as test_chat_routes.py). Only fake token strings are used, and the tests assert
that no response body ever contains 'tokenFile' or the fake token.

Run: python -m unittest test_accounts_routes -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import accounts  # noqa: E402

FAKE_TOKEN = "FAKE-TOKEN-DO-NOT-LEAK-1234"


class AccountsRouteTestBase(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(app, "CHAT_STORAGE_DIR", self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def _get(self):
        return self.client.get("/api/chat/accounts")

    def _make_live(self, label):
        return self.client.post(f"/api/chat/accounts/{label}/live")

    def _registry(self):
        return (self.storage / "accounts.json").read_text(encoding="utf-8")


class EmptyRegistryTest(AccountsRouteTestBase):
    def test_empty_list_and_null_live(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"accounts": [], "live": None})


class TwoAccountsTest(AccountsRouteTestBase):
    def setUp(self):
        super().setUp()
        accounts.add(self.storage, "acct-a", "host-a.example", str(self.storage / "tok-a"))
        accounts.add(self.storage, "acct-b", "host-b.example", str(self.storage / "tok-b"))

    def test_both_accounts_and_first_live(self):
        body = self._get().get_json()
        self.assertEqual([a["label"] for a in body["accounts"]], ["acct-a", "acct-b"])
        self.assertEqual(body["live"], "acct-a")
        self.assertEqual(body["accounts"][0], {"label": "acct-a", "host": "host-a.example", "live": True})
        self.assertEqual(body["accounts"][1], {"label": "acct-b", "host": "host-b.example", "live": False})

    def test_make_live_switches(self):
        r = self._make_live("acct-b")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["live"], "acct-b")
        self.assertEqual(body["accounts"][0]["live"], False)
        self.assertEqual(body["accounts"][1]["live"], True)
        # GET after the switch reflects it too
        self.assertEqual(self._get().get_json()["live"], "acct-b")

    def test_unknown_label_is_404_json(self):
        r = self._make_live("no-such-account")
        self.assertEqual(r.status_code, 404)
        body = r.get_json()
        self.assertIsInstance(body, dict)
        self.assertIn("error", body)

    def test_invalid_label_is_400(self):
        for bad in ("has space!", "x" * 41, "café"):  # path-traversal labels are 404'd by the router itself
            r = self._make_live(bad)
            self.assertEqual(r.status_code, 400, msg=f"expected 400 for {bad!r}")
            self.assertIn("error", r.get_json())
        # the registry must be untouched by a 400/404
        self.assertEqual(self._get().get_json()["live"], "acct-a")


class TokenHygieneTest(AccountsRouteTestBase):
    def test_no_token_or_token_path_in_any_response(self):
        token_path = str(self.storage / "secrets" / FAKE_TOKEN)
        accounts.add(self.storage, "acct-a", "host-a.example", token_path)
        accounts.add(self.storage, "acct-b", "host-b.example", str(self.storage / "other" / FAKE_TOKEN))

        bodies = [self._get().get_data(as_text=True),
                  self._make_live("acct-b").get_data(as_text=True),
                  self._get().get_data(as_text=True),
                  self._make_live("ghost").get_data(as_text=True),
                  self._make_live("bad label!").get_data(as_text=True)]
        for body in bodies:
            self.assertNotIn("tokenFile", body)
            self.assertNotIn(FAKE_TOKEN, body)
        # sanity: the fake token IS in the on-disk registry (so the above is meaningful)
        self.assertIn(FAKE_TOKEN, self._registry())
        self.assertIn("tokenFile", self._registry())


if __name__ == "__main__":
    unittest.main()
