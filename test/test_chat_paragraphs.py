"""Tests for chat paragraph arrival stamping (chat_sessions).

Import-safe: puts the repo root on sys.path explicitly (works under
`python -I`, from any cwd, and without `test` being a package). If flask
(pulled in transitively by chat_sessions) is missing, the tests are skipped
with a reason naming flask instead of failing at load time.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


# --- shared helpers -------------------------------------------------------

class FakeClock:
    """Stand-in for chat_sessions._now_iso: distinct, ordered ISO stamps.

    Returns t1, t2, t3 on successive calls, then tEnd forever (never raises).
    NOTE: _touch() also calls _now_iso(); run_turn() patches _touch to a no-op
    so it does not consume ticks. _stream_message_turn itself calls _now_iso
    for the user entry (t1) and the assistant placeholder (t2) first.
    """

    t1 = "2026-01-01T00:00:01Z"
    t2 = "2026-01-01T00:00:02Z"
    t3 = "2026-01-01T00:00:03Z"
    tEnd = "2026-01-01T00:00:59Z"

    def __init__(self):
        self._stamps = [self.t1, self.t2, self.t3]
        self.calls = 0

    def __call__(self):
        i = self.calls
        self.calls += 1
        return self._stamps[i] if i < len(self._stamps) else self.tEnd


def fake_stream_local(chunks):
    """Factory for a replacement of chat_sessions._stream_local.

    Yields every chunk in order (str chunks and dict tool events alike) and
    sets result_box["final_text"] to the joined str chunks.
    """
    def _fake(session, message, result_box=None):
        for chunk in chunks:
            yield chunk
        if result_box is not None:
            result_box["final_text"] = "".join(c for c in chunks if isinstance(c, str))
    return _fake


def run_turn(chunks, clock=None, storage_dir=None, message="hello"):
    """Run chat_sessions._stream_message_turn with fake clock and stream.

    Patches _now_iso, _stream_local and _touch (no-op, so it does not eat
    clock ticks), uses a temp storage_dir unless one is given, drains the
    generator, and returns the stored transcript read back from disk.
    Paragraph-specific storage does not exist in chat_sessions yet; this
    returns the persisted session["transcript"] list -- adjust the key here
    when paragraph storage lands.
    """
    clock = clock if clock is not None else FakeClock()
    own_tmp = None
    if storage_dir is None:
        own_tmp = tempfile.TemporaryDirectory()
        storage_dir = own_tmp.name
    storage_dir = Path(storage_dir)
    session = {
        "id": "chat-1-aaaaaaaa",
        "provider": "local",
        "status": "active",
        "transcript": [],
    }
    try:
        with mock.patch.object(chat_sessions, "_now_iso", clock), \
                mock.patch.object(chat_sessions, "_stream_local", fake_stream_local(chunks)), \
                mock.patch.object(chat_sessions, "_touch", lambda s: None):
            for _ in chat_sessions._stream_message_turn(storage_dir, session, message):
                pass
        stored = chat_sessions.get_session(storage_dir, session["id"])
        return (stored or {}).get("transcript", [])
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()


@unittest.skipIf(
    chat_sessions is None,
    "missing dependency: flask (required by chat_sessions)",
)
class ChatParagraphsTests(unittest.TestCase):
    def test_chat_sessions_importable(self):
        self.assertTrue(hasattr(chat_sessions, "get_active_session"))

    def test_fake_clock_ordered_distinct(self):
        c = FakeClock()
        got = [c(), c(), c(), c()]
        self.assertEqual(got, [FakeClock.t1, FakeClock.t2, FakeClock.t3, FakeClock.tEnd])
        self.assertEqual(len(set(got)), 4)

    def test_fake_stream_local_sets_final_text(self):
        box = {}
        out = list(fake_stream_local(["a", {"type": "tool_start"}, "b"])(None, "m", box))
        self.assertEqual(len(out), 3)
        self.assertEqual(box["final_text"], "ab")

    def test_run_turn_returns_stored_transcript(self):
        transcript = run_turn(["hi ", "there"])
        self.assertEqual(transcript[0]["timestamp"], FakeClock.t1)
        self.assertEqual(transcript[-1]["role"], "assistant")

    # Stamp rule (read from chat_sessions._stream_message_turn): there is no
    # per-paragraph storage. The user entry takes the first clock tick (t1)
    # and the assistant entry takes the second (t2) when its placeholder is
    # appended; later chunks only rewrite the entry's "text", never its
    # "timestamp". Paragraphs are derived from the stored assistant text via
    # split("\n\n") and all share the assistant entry's single stamp (t2). A
    # "\n\n" boundary split across chunks is reassembled in the stored text,
    # so it still yields two paragraphs.
    def test_split_boundary_yields_two_paragraphs_with_shared_stamp(self):
        transcript = run_turn(["first\n", "\nsecond"])
        entry = transcript[-1]
        self.assertEqual(entry["role"], "assistant")
        self.assertEqual(entry["text"].split("\n\n"), ["first", "second"])
        self.assertEqual(entry["timestamp"], FakeClock.t2)

    def test_single_paragraph_spanning_chunks_has_one_stamp(self):
        transcript = run_turn(["one ", "two ", "three"])
        entry = transcript[-1]
        paragraphs = entry["text"].split("\n\n")
        self.assertEqual(paragraphs, ["one two three"])
        # Stamp is fixed at placeholder creation (t2); later chunks leave it.
        self.assertEqual(entry["timestamp"], FakeClock.t2)


if __name__ == "__main__":
    unittest.main()
