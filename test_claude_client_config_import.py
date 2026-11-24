import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class ClaudeClientImportsFromConfigTest(unittest.TestCase):
    def _run(self, code):
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_importing_claude_client_does_not_import_app(self):
        result = self._run(
            "import sys\n"
            "import claude_client\n"
            "assert 'app' not in sys.modules, 'claude_client pulled in app'\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_repo_root_matches_config(self):
        result = self._run(
            "import claude_client, config\n"
            "assert claude_client.CORE_REPO_ROOT == config.CORE_REPO_ROOT\n"
            "expected = config.CORE_REPO_ROOT / 'src' / 'claude-client.js'\n"
            "assert claude_client.CLAUDE_CLIENT_JS == expected\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
