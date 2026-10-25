"""docs/write-command-allowlist-design.md records the owner's 2026-10-25 decisions and the contract the implementation pieces share."""
import re
import unittest
from pathlib import Path

DOC = (Path(__file__).resolve().parent / "docs" / "write-command-allowlist-design.md").read_text(encoding="utf-8")


class WriteCommandDesignDocTest(unittest.TestCase):
    def test_status_is_approved_with_the_three_decisions_and_nothing_pending(self):
        head = DOC[:1800]
        self.assertIn("APPROVED by the owner, 2026-10-25", head)
        for phrase in ("(A)", "(B)", "(C)", "restricted host-side runner", "central config", "never a write-tool unlock"):
            self.assertIn(phrase, head)
        self.assertNotIn("pending owner approval", DOC)
        self.assertNotIn("Decision questions for the owner", DOC)

    def test_section_6_holds_the_contract_names(self):
        section = DOC[DOC.index("## 6. Implementation contract and pieces"):]
        for name in (
            "AGENT_MANAGER_CHAT_COMMAND_ALLOWLIST_PATH", "command-allowlist.json", "propose_command", "commandProposals",
            "commandExecutions", "/api/internal/chat/run-command", "api/chat/<session id>/command/<proposalId>/<confirm|deny>",
            "awaiting-confirmation", "timeoutSeconds",
        ):
            self.assertIn(name, section)
        self.assertEqual(len(re.findall(r"^\d\. ", section, re.M)), 5, "five numbered pieces")

    def test_runner_limits_and_the_chosen_option_are_stated_in_section_4(self):
        section4 = DOC[DOC.index("## 4. How it stays off"):DOC.index("## 5.")]
        for phrase in ("CHOSEN, decision A", "no shell", "120 s", "900 s", "20000 characters", "chat-command-audit.jsonl", ".chat-write-tools-disabled"):
            self.assertIn(phrase, section4)


if __name__ == "__main__":
    unittest.main()
