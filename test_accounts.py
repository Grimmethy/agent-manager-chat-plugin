import json
import os
import tempfile
import unittest

import accounts


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.storage = self.dir.name
        self.addCleanup(self.dir.cleanup)

    def test_add_and_persist(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-token-alice")
        got = accounts.load(self.storage)
        self.assertEqual(got, [{"label": "alice", "host": "10.0.0.1",
                                "tokenFile": "/tmp/fake-token-alice", "live": True}])
        self.assertEqual(accounts.live_label(self.storage), "alice")

    def test_file_mode_0600_and_dir_0700(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-token-alice")
        self.assertEqual((os.stat(os.path.join(self.storage, "accounts.json")).st_mode & 0o777), 0o600)
        self.assertEqual((os.stat(self.storage).st_mode & 0o777), 0o700)

    def test_duplicate_label_raises(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-token-alice")
        with self.assertRaises(ValueError):
            accounts.add(self.storage, "alice", "10.0.0.2", "/tmp/fake-token-alice2")

    def test_first_becomes_live(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-a")
        accounts.add(self.storage, "bob", "10.0.0.2", "/tmp/fake-b")
        got = accounts.load(self.storage)
        self.assertEqual([a["live"] for a in got], [True, False])
        self.assertEqual(accounts.live_label(self.storage), "alice")

    def test_make_live_switches_and_persists(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-a")
        accounts.add(self.storage, "bob", "10.0.0.2", "/tmp/fake-b")
        accounts.make_live(self.storage, "bob")
        got = accounts.load(self.storage)
        self.assertEqual([a["live"] for a in got], [False, True])
        self.assertEqual(accounts.live_label(self.storage), "bob")

    def test_make_live_unknown_label_keyerror(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-a")
        with self.assertRaises(KeyError):
            accounts.make_live(self.storage, "nobody")

    def test_exactly_one_live_repair_multiple(self):
        os.makedirs(self.storage, exist_ok=True)
        with open(os.path.join(self.storage, "accounts.json"), "w") as f:
            json.dump({"accounts": [
                {"label": "a", "host": "1", "tokenFile": "/tmp/fake-a", "live": True},
                {"label": "b", "host": "2", "tokenFile": "/tmp/fake-b", "live": True},
                {"label": "c", "host": "3", "tokenFile": "/tmp/fake-c", "live": True},
            ]}, f)
        got = accounts.load(self.storage)
        self.assertEqual([a["live"] for a in got], [True, False, False])
        self.assertEqual(accounts.live_label(self.storage), "a")

    def test_exactly_one_live_repair_none(self):
        os.makedirs(self.storage, exist_ok=True)
        with open(os.path.join(self.storage, "accounts.json"), "w") as f:
            json.dump({"accounts": [
                {"label": "a", "host": "1", "tokenFile": "/tmp/fake-a", "live": False},
                {"label": "b", "host": "2", "tokenFile": "/tmp/fake-b", "live": False},
            ]}, f)
        got = accounts.load(self.storage)
        self.assertEqual([a["live"] for a in got], [True, False])
        self.assertEqual(accounts.live_label(self.storage), "a")

    def test_corrupt_json_gives_empty(self):
        os.makedirs(self.storage, exist_ok=True)
        with open(os.path.join(self.storage, "accounts.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(accounts.load(self.storage), [])
        self.assertIsNone(accounts.live_label(self.storage))

    def test_missing_file_gives_empty(self):
        self.assertEqual(accounts.load(self.storage), [])
        self.assertIsNone(accounts.live_label(self.storage))

    def test_invalid_entries_dropped(self):
        os.makedirs(self.storage, exist_ok=True)
        with open(os.path.join(self.storage, "accounts.json"), "w") as f:
            json.dump({"accounts": [
                "not-a-dict",
                {"label": "", "host": "1", "tokenFile": "/tmp/x", "live": True},
                {"label": "bad label!", "host": "1", "tokenFile": "/tmp/x", "live": False},
                {"label": "ok-1", "host": "1", "tokenFile": "/tmp/x", "live": False},
            ]}, f)
        got = accounts.load(self.storage)
        self.assertEqual([a["label"] for a in got], ["ok-1"])
        self.assertEqual([a["live"] for a in got], [True])  # none live -> first becomes live

    def test_public_view_no_tokenfile(self):
        accounts.add(self.storage, "alice", "10.0.0.1", "/tmp/fake-secret-alice")
        view = accounts.public_view(accounts.load(self.storage))
        for row in view:
            self.assertEqual(set(row), {"label", "host", "live"})
            self.assertNotIn("tokenFile", row)
            for value in row.values():
                if isinstance(value, str):
                    self.assertNotIn("fake-secret-alice", value)

    def test_label_validation(self):
        with self.assertRaises(ValueError):
            accounts.add(self.storage, "bad label!", "1", "/tmp/x")
        with self.assertRaises(ValueError):
            accounts.add(self.storage, "a" * 41, "1", "/tmp/x")
        accounts.add(self.storage, "a-b_C9", "1", "/tmp/x")  # valid
        self.assertEqual(accounts.live_label(self.storage), "a-b_C9")


if __name__ == "__main__":
    unittest.main()
