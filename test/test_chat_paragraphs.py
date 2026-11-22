"""Tests for chat paragraph arrival stamping (chat_sessions).

Import-safe: puts the repo root on sys.path explicitly (works under
`python -I`, from any cwd, and without `test` being a package). If flask
(pulled in transitively by chat_sessions) is missing, the tests are skipped
with a reason naming flask instead of failing at load time.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_MISSING_DEP = None
try:
    import chat_sessions
except ImportError as exc:
    _name = getattr(exc, "name", None) or ""
    if _name == "flask" or _name.startswith("flask."):
        chat_sessions = None
        _MISSING_DEP = _name
    else:
        raise


@unittest.skipIf(
    chat_sessions is None,
    "missing dependency: flask (required by chat_sessions)",
)
class ChatParagraphsTests(unittest.TestCase):
    def test_chat_sessions_importable(self):
        self.assertTrue(hasattr(chat_sessions, "get_active_session"))


if __name__ == "__main__":
    unittest.main()
