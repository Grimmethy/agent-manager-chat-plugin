"""Per-paragraph arrival stamps (HUB0072). Drives _stream_message_turn with a fake _stream_local that yields three paragraphs and an injected clock, and checks
the stored assistant entry's paragraph_times holds each paragraph's own arrival time, not one stamp for the whole turn."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import chat_sessions  # noqa: E402


def _session():
    return {"id": "s1", "title": "t", "repoRoot": "/tmp", "roots": ["/tmp"], "provider": "local", "status": "active", "transcript": [],
            "createdAt": "2026-10-30T00:00:00Z", "updatedAt": "2026-10-30T00:00:00Z"}


class ScriptedClock:
    """Returns the scripted values one per call, in order; fails loudly if read more often than scripted."""
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.values:
            raise AssertionError(f"clock read {self.calls} times, script had fewer")
        return self.values.pop(0)


class ChatParagraphStampsTest(unittest.TestCase):
    def setUp(self):
        self._stats = mock.patch.object(chat_sessions.model_stats_client, "record_call")
        self._stats.start()
        self.addCleanup(self._stats.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Path(self.tmp.name)

    def test_each_paragraph_is_stamped_with_its_own_arrival_time(self):
        paragraphs = ["First paragraph.\n\n", "Second paragraph.\n\n", "Third paragraph."]
        t1, t2, t3, t_end = 100.0, 200.0, 300.0, 400.0

        def fake_stream_local(session, message, result_box=None):
            for p in paragraphs:
                yield p
            if result_box is not None:
                result_box["final_text"] = "".join(paragraphs)

        session = _session()
        clock = ScriptedClock(t1, t2, t3)
        with mock.patch.object(chat_sessions, "_stream_local", fake_stream_local):
            list(chat_sessions._stream_message_turn(self.storage, session, "hi", clock=clock))

        entry = session["transcript"][-1]
        self.assertEqual(entry["role"], "assistant")
        self.assertEqual(entry["paragraph_times"], [t1, t2, t3])
        # The turn end time (t_end, after the last paragraph) must not be what any paragraph was stamped with.
        for stamp in entry["paragraph_times"]:
            self.assertNotEqual(stamp, t_end)
        self.assertEqual(len(set(entry["paragraph_times"])), 3, "paragraphs must not share one stamp")


if __name__ == "__main__":
    unittest.main()
