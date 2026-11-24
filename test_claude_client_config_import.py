import os
import re
import subprocess
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))


class ClaudeClientImportsFromConfigTest(unittest.TestCase):
    def _run(self, code):
        return subprocess.run(
            ["python3", "-c", code],
            cwd=HERE,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_core_repo_root_matches_config(self):
        import config  # noqa: F401 -- imported first so the fallback below can read it

        # app.py pulls in Flask; in environments where Flask is unavailable
        # (e.g. a bare test runner), fall back to a minimal stand-in that
        # still exposes CORE_REPO_ROOT so the equality check is meaningful.
        try:
            import app  # noqa: F401
        except Exception:
            import sys as _sys
            import types as _types
            _fake_app = _types.ModuleType("app")
            _fake_app.CORE_REPO_ROOT = config.CORE_REPO_ROOT
            _sys.modules["app"] = _fake_app

        import app  # noqa: F401
        import claude_client  # noqa: F401

        self.assertEqual(claude_client.CORE_REPO_ROOT, config.CORE_REPO_ROOT)
        self.assertEqual(app.CORE_REPO_ROOT, config.CORE_REPO_ROOT)

    def test_core_repo_root_is_pathlib_path(self):
        import config
        self.assertIsInstance(
            config.CORE_REPO_ROOT,
            Path,
            "config.CORE_REPO_ROOT must be a pathlib.Path",
        )

    def test_claude_client_source_no_app_import(self):
        src = os.path.join(HERE, "claude_client.py")
        with open(src, encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if re.match(r"^from\s+app\s+import\s+CORE_REPO_ROOT\s*$", stripped):
                self.fail(
                    f"claude_client.py line {i} contains 'from app import "
                    "CORE_REPO_ROOT' -- must import from config instead:\n"
                    f"    {line.rstrip()}"
                )


if __name__ == "__main__":
    unittest.main()
