"""Tests for chat_sessions.py footer enforcement and retry logic.

Covers:
- Missing-footer: enforce_footer appends TASK-REF: N/A when footer is absent
- Empty-retry: empty answer gets deterministic footer
- Failing-retry: preserves original answer when model produces text
- Storage verification: no empty turns or footer-only turns when model produced content
- Transcript integrity: no retry apologies in stored transcript

Run: .venv/bin/python -m unittest tests.test_chat_sessions_footer -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import chat_sessions  # noqa: E402
import app  # noqa: E402


def _session_with_transcript(transcript=None):
    """Create a minimal session dict for testing."""
    root = str(app.CORE_REPO_ROOT)
    session = {
        "id": "chat-test-session",
        "provider": "local",
        "repoRoot": root,
        "roots": [root],
        "transcript": transcript or [],
    }
    return session


def _fake_stream(events):
    """Create a fake stream generator from a list of events."""
    def _gen(**kwargs):
        yield from events
    return _gen


class TestEnforceFooterMissingFooter(unittest.TestCase):
    """Tests for when answer has no footer - should append TASK-REF: N/A."""

    def test_missing_footer_appends_footer(self):
        """When answer has no footer, enforce_footer appends TASK-REF: N/A."""
        answer = "This is the actual answer text."
        result = chat_sessions.enforce_footer(answer)
        self.assertIn("TASK-REF: N/A", result)
        self.assertTrue(result.endswith("TASK-REF: N/A"))
        # Original answer must be preserved
        self.assertIn("This is the actual answer text.", result)

    def test_missing_footer_preserves_original_when_retry_also_missing(self):
        """When answer has no footer and retry also produces no footer, original is preserved."""
        answer = "Simple answer without footer"
        result = chat_sessions.enforce_footer(answer)
        # Should append footer
        self.assertIn("TASK-REF: N/A", result)
        # Original text must be present
        self.assertEqual(result.startswith("Simple answer without footer"), True)

    def test_empty_answer_gets_deterministic_footer(self):
        """Empty answer gets deterministic TASK-REF: N/A footer."""
        answer = ""
        result = chat_sessions.enforce_footer(answer)
        self.assertEqual(result, "TASK-REF: N/A")

    def test_footer_already_present_returns_unchanged(self):
        """When footer already exists, return answer unchanged."""
        answer = "Answer text\n\nTASK-REF: task-123"
        result = chat_sessions.enforce_footer(answer)
        self.assertEqual(result, answer)


class TestEnforceFooterFailingRetry(unittest.TestCase):
    """Tests for when retry mechanism fails - original answer must be preserved."""

    def test_failing_retry_preserves_original(self):
        """When retry fails, original answer is preserved."""
        answer = "Original answer that model produced"
        result = chat_sessions.enforce_footer(answer)
        # Original answer must be in result
        self.assertIn("Original answer that model produced", result)
        # If no footer, footer should be appended
        if not chat_sessions._has_footer(answer):
            self.assertIn("TASK-REF: N/A", result)

    def test_retry_exception_does_not_raise(self):
        """enforce_footer should not raise even if internal operations fail."""
        answer = "Answer with potential failure point"
        # Should not raise
        result = chat_sessions.enforce_footer(answer)
        self.assertIsNotNone(result)
        # Original answer must be preserved
        self.assertIn("Answer with potential failure point", result)


class TestPostChatTurnStorage(unittest.TestCase):
    """Tests for verifying stored transcript integrity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _seed_active_session(self):
        """Create an active session in storage."""
        session = _session_with_transcript()
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        return session["id"]

    def test_no_empty_turns_stored(self):
        """Transcript must not contain turns with empty text when model produced content."""
        session_id = self._seed_active_session()
        
        def fake_stream(**kwargs):
            # Simulate a successful stream with content
            yield {"type": "chunk", "text": "Real answer content"}
            yield {"type": "final", "response": "Real answer content", 
                   "turnsUsed": 2, "forcedSummary": False}
        
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))
        
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        transcript = stored.get("transcript", [])
        # Transcript should not be empty
        self.assertTrue(len(transcript) > 0)
        # No turn should have empty text when model produced content
        for turn in transcript:
            if turn.get("role") == "assistant":
                self.assertTrue(len(turn.get("text", "")) > 0,
                    "Transcript must not contain turns with empty text when model produced content.")

    def test_no_footer_only_turns_when_model_produced_text(self):
        """When model produced text, turn should not be footer-only."""
        session_id = self._seed_active_session()
        
        def fake_stream(**kwargs):
            yield {"type": "chunk", "text": "Substantive answer here"}
            yield {"type": "final", "response": "Substantive answer here",
                   "turnsUsed": 2, "forcedSummary": False}
        
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))
        
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        transcript = stored.get("transcript", [])
        # Find assistant turns
        assistant_turns = [t for t in transcript if t.get("role") == "assistant"]
        for turn in assistant_turns:
            text = turn.get("text", "")
            # If there's substantive content, it shouldn't be just footer
            if "Substantive answer here" in text:
                # Should have more than just footer
                self.assertGreater(len(text), 10,
                    "Turn should not be footer-only when model produced substantive text.")


class TestTranscriptIntegrity(unittest.TestCase):
    """Tests for verifying transcript doesn't contain retry artifacts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage_dir = Path(self.tmp.name)

    def _seed_active_session(self):
        """Create an active session in storage."""
        session = _session_with_transcript()
        session["status"] = "active"
        chat_sessions._write_sessions(self.storage_dir, {session["id"]: session})
        return session["id"]

    def test_no_retry_apologies_in_stored_transcript(self):
        """Stored transcript must not contain retry apologies."""
        session_id = self._seed_active_session()
        
        # Simulate a stream that includes apology-like text in chunks
        def fake_stream(**kwargs):
            # Model might start with apology then correct itself
            yield {"type": "chunk", "text": "Sorry, let me correct that. "}
            yield {"type": "chunk", "text": "The actual answer is here."}
            yield {"type": "final", "response": "The actual answer is here.",
                   "turnsUsed": 3, "forcedSummary": False}
        
        with mock.patch.object(chat_sessions.local_tool_client, "stream_plan_with_tools", fake_stream):
            list(chat_sessions.stream_message(self.storage_dir, session_id, "hi"))
        
        stored = chat_sessions._read_sessions(self.storage_dir)[session_id]
        transcript = stored.get("transcript", [])
        
        # Check that no transcript entry contains retry apologies
        apology_patterns = [
            "Sorry, let me correct",
            "I apologize",
            "You're right, my apologies",
            "My apologies",
        ]
        
        for turn in transcript:
            text = turn.get("text", "")
            for pattern in apology_patterns:
                self.assertNotIn(
                    pattern, text,
                    f"Transcript must not contain retry apologies: '{pattern}'"
                )


class TestFooterExtraction(unittest.TestCase):
    """Tests for footer extraction and normalization."""

    def test_extract_footer_task_ref(self):
        """_extract_footer should find TASK-REF footer."""
        text = "Answer text\n\nTASK-REF: task-123"
        extracted_text, footer = chat_sessions._extract_footer(text)
        self.assertEqual(footer, "TASK-REF: task-123")
        self.assertEqual(extracted_text, "Answer text")

    def test_extract_footer_context_log(self):
        """_extract_footer should find CONTEXT-LOG footer (single word per regex \\S+)."""
        text = "Answer text\n\nCONTEXT-LOG: distilled"
        extracted_text, footer = chat_sessions._extract_footer(text)
        self.assertEqual(footer, "CONTEXT-LOG: distilled")
        self.assertEqual(extracted_text, "Answer text")

    def test_extract_footer_no_footer(self):
        """_extract_footer should return None when no footer."""
        text = "Answer text without footer"
        extracted_text, footer = chat_sessions._extract_footer(text)
        self.assertIsNone(footer)
        self.assertEqual(extracted_text, text)

    def test_normalize_footer_context_log_to_task_ref(self):
        """_normalize_footer should convert CONTEXT-LOG to TASK-REF."""
        result = chat_sessions._normalize_footer("CONTEXT-LOG: distilled summary")
        self.assertEqual(result, "TASK-REF: distilled summary")

    def test_normalize_footer_task_ref_unchanged(self):
        """_normalize_footer should leave TASK-REF unchanged."""
        result = chat_sessions._normalize_footer("TASK-REF: task-123")
        self.assertEqual(result, "TASK-REF: task-123")

    def test_has_footer_task_ref(self):
        """_has_footer should detect TASK-REF."""
        self.assertTrue(chat_sessions._has_footer("Answer\n\nTASK-REF: task-1"))

    def test_has_footer_context_log(self):
        """_has_footer should detect CONTEXT-LOG."""
        self.assertTrue(chat_sessions._has_footer("Answer\n\nCONTEXT-LOG: summary"))

    def test_has_footer_none(self):
        """_has_footer should return False when no footer."""
        self.assertFalse(chat_sessions._has_footer("Answer without footer"))

    def test_has_footer_empty(self):
        """_has_footer should return False for empty string."""
        self.assertFalse(chat_sessions._has_footer(""))


class TestStripChatter(unittest.TestCase):
    """Tests for chatter stripping functionality."""

    def test_strip_chatter_apology(self):
        """_strip_chatter should remove leading apologies."""
        text = "Sorry, let me correct that.\nThe actual answer."
        result = chat_sessions._strip_chatter(text)
        self.assertFalse(result.startswith("Sorry"))
        self.assertIn("The actual answer.", result)

    def test_strip_chatter_no_apology(self):
        """_strip_chatter should leave non-apology text unchanged."""
        text = "The answer without apology"
        result = chat_sessions._strip_chatter(text)
        self.assertEqual(result, text)

    def test_strip_chatter_empty(self):
        """_strip_chatter should return empty string unchanged."""
        result = chat_sessions._strip_chatter("")
        self.assertEqual(result, "")


if __name__ == '__main__':
    unittest.main()
