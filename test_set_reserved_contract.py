"""chat_sessions.set_reserved: the retry-safety and atomicity contract its docstring states (AC-17).

Run: .venv/bin/python -m unittest test_set_reserved_contract -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402
import chat_sessions  # noqa: E402

SID = "chat-contract-test"


def _session():
    root = str(app.CORE_REPO_ROOT)
    return {
        "id": SID, "provider": "local", "model": "fake-model", "effort": None,
        "roots": [root], "repoRoot": root, "instancesDir": None, "claudeSessionId": None,
        "transcript": [{"role": "user", "text": "hello"}],
        "status": "active", "startedAt": "2026-10-26T00:00:00Z", "endedAt": None, "reserved": False,
    }


class SetReservedContractTest(unittest.TestCase):
    def setUp(self):
        self.storage = Path(tempfile.mkdtemp())
        chat_sessions._write_sessions(self.storage, {SID: _session()})

    def _stored(self):
        return chat_sessions.get_session(self.storage, SID)

    def test_calling_twice_with_the_same_value_ends_in_the_same_persisted_state(self):
        first = chat_sessions.set_reserved(self.storage, SID, True)
        after_first = self._stored()
        second = chat_sessions.set_reserved(self.storage, SID, True)
        self.assertIs(first["reserved"], True)
        self.assertEqual(first, second)
        self.assertEqual(after_first, self._stored())
        chat_sessions.set_reserved(self.storage, SID, False)
        chat_sessions.set_reserved(self.storage, SID, False)
        self.assertIs(self._stored()["reserved"], False)

    def test_it_changes_only_the_reserved_flag(self):
        before = self._stored()
        chat_sessions.set_reserved(self.storage, SID, True)
        after = self._stored()
        self.assertEqual({k: v for k, v in after.items() if k != "reserved"}, {k: v for k, v in before.items() if k != "reserved"})

    def test_an_unknown_session_returns_none_and_writes_nothing(self):
        with mock.patch.object(chat_sessions, "_write_session") as write:
            self.assertIsNone(chat_sessions.set_reserved(self.storage, "no-such-session", True))
        write.assert_not_called()

    def test_a_failed_write_leaves_the_previous_file_intact_and_no_temp_file_behind(self):
        path = chat_sessions._session_path(self.storage, SID)
        before = path.read_text(encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                chat_sessions.set_reserved(self.storage, SID, True)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertIs(json.loads(before)["reserved"], False)
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
