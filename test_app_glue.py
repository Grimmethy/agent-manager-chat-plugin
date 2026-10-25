"""Tests for app.py's own small glue functions. Covers the pure/file-reading utilities
chat_sessions.py and routes/chat.py rely on via `from app import ...` at call time.

Run: .venv/bin/python -m unittest test_app_glue -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import app  # noqa: E402


class TestReadEnvFile(unittest.TestCase):
    def _write(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "test.env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_parses_key_value_pairs(self):
        p = self._write("A=1\nB=hello world\n")
        self.assertEqual(app.read_env_file(p), {"A": "1", "B": "hello world"})

    def test_skips_comments_and_blank_lines(self):
        p = self._write("# comment\n\nA=1\n  # indented comment\nB=2\n")
        self.assertEqual(app.read_env_file(p), {"A": "1", "B": "2"})

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(app.read_env_file(Path("/does/not/exist.env")), {})

    def test_line_with_no_equals_is_skipped(self):
        p = self._write("A=1\nnotakeyvalue\nB=2\n")
        self.assertEqual(app.read_env_file(p), {"A": "1", "B": "2"})


class TestReadJsonSafe(unittest.TestCase):
    def _write(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "test.json"
        p.write_text(text, encoding="utf-8")
        return p

    def test_valid_json_parses(self):
        p = self._write('{"a": 1}')
        self.assertEqual(app.read_json_safe(p), {"a": 1})

    def test_missing_file_returns_none(self):
        self.assertIsNone(app.read_json_safe(Path("/does/not/exist.json")))

    def test_malformed_json_returns_none(self):
        p = self._write("{not valid")
        self.assertIsNone(app.read_json_safe(p))


class TestParseHbTimestamp(unittest.TestCase):
    def test_none_or_empty_returns_none(self):
        self.assertIsNone(app.parse_hb_timestamp(None))
        self.assertIsNone(app.parse_hb_timestamp(""))

    def test_standard_iso_with_z(self):
        dt = app.parse_hb_timestamp("2026-09-15T10:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_powershell_7_digit_fractional_seconds_truncated(self):
        # PowerShell's Get-Date -Format 'o' emits 7-digit fractional seconds (100ns
        # ticks) -- Python's fromisoformat only accepts 3 or 6 before 3.11.
        dt = app.parse_hb_timestamp("2026-09-15T10:00:33.6859854-06:00")
        self.assertIsNotNone(dt)

    def test_garbage_returns_none(self):
        self.assertIsNone(app.parse_hb_timestamp("not a timestamp"))

    def test_naive_datetime_gets_utc_tzinfo(self):
        dt = app.parse_hb_timestamp("2026-09-15T10:00:00")
        self.assertIsNotNone(dt.tzinfo)


class TestActiveProjectSetting(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def test_env_file_wins_over_os_environ(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"
        env_path.write_text("AGENT_MANAGER_REPO_ROOT=/from/file\n", encoding="utf-8")
        os.environ["AGENT_MANAGER_REPO_ROOT"] = "/from/os-environ"
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertEqual(app.get_active_repo_root(), "/from/file")

    def test_falls_back_to_os_environ_when_file_unset(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"  # does not exist
        os.environ["AGENT_MANAGER_REPO_ROOT"] = "/from/os-environ"
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertEqual(app.get_active_repo_root(), "/from/os-environ")

    def test_pipeline_dir_falls_back_to_repo_root(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"
        env_path.write_text("AGENT_MANAGER_REPO_ROOT=/x/y\n", encoding="utf-8")
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertEqual(app.get_pipeline_dir(), Path("/x/y"))

    def test_get_second_brain_dir_env_file_wins(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"
        env_path.write_text("SECOND_BRAIN_DIR=/from/file/sb\n", encoding="utf-8")
        os.environ["SECOND_BRAIN_DIR"] = "/from/os-environ/sb"
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertEqual(app.get_second_brain_dir(), Path("/from/file/sb"))

    def test_get_second_brain_dir_returns_none_when_unconfigured(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"  # does not exist
        os.environ.pop("SECOND_BRAIN_DIR", None)
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertIsNone(app.get_second_brain_dir())

    def test_queue_dir_and_instances_dir_derive_from_pipeline_dir(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"
        env_path.write_text("AGENT_MANAGER_PIPELINE_DIR=/pipeline\n", encoding="utf-8")
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertEqual(app.queue_dir(), Path("/pipeline/queue"))
            self.assertEqual(app.instances_dir(), Path("/pipeline/instances"))

    def test_queue_dir_is_none_with_no_project_configured(self):
        d = Path(tempfile.mkdtemp())
        env_path = d / "agent-manager.env"  # does not exist, os.environ also empty
        os.environ.pop("AGENT_MANAGER_REPO_ROOT", None)
        os.environ.pop("AGENT_MANAGER_PIPELINE_DIR", None)
        with mock.patch.object(app, "ENV_FILE_PATH", env_path):
            self.assertIsNone(app.queue_dir())
            self.assertIsNone(app.instances_dir())


class TestClaudeDefaults(unittest.TestCase):
    def test_defaults_when_no_settings_file(self):
        with mock.patch.object(app, "DASHBOARD_SETTINGS_PATH", Path("/does/not/exist.json")):
            self.assertEqual(app.claude_defaults(), {"model": "sonnet", "effort": "high"})

    def test_reads_saved_settings(self):
        d = Path(tempfile.mkdtemp())
        p = d / "dashboard-settings.json"
        p.write_text(json.dumps({"claudeDefaultModel": "opus", "claudeDefaultEffort": "max"}), encoding="utf-8")
        with mock.patch.object(app, "DASHBOARD_SETTINGS_PATH", p):
            self.assertEqual(app.claude_defaults(), {"model": "opus", "effort": "max"})


class TestChatRoots(unittest.TestCase):
    """2026-09-15 (Phase 3): _chat_roots() is now a thin wrapper around
    internal_api_client.get_roots() -- the core dashboard resolves the real list itself.
    See internal_api_client's own tests for the HTTP call details this mocks around."""

    def test_returns_the_internal_api_result_when_it_succeeds(self):
        with mock.patch("internal_api_client.get_roots", return_value=["/a", "/b"]):
            self.assertEqual(app._chat_roots(), ["/a", "/b"])

    def test_falls_back_to_core_repo_root_when_the_internal_api_call_fails(self):
        from internal_api_client import InternalApiError
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(app, "CORE_REPO_ROOT", d), \
             mock.patch("internal_api_client.get_roots", side_effect=InternalApiError("no token")):
            self.assertEqual(app._chat_roots(), [os.path.realpath(str(d))])

    def test_falls_back_to_core_repo_root_when_the_internal_api_returns_an_empty_list(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(app, "CORE_REPO_ROOT", d), \
             mock.patch("internal_api_client.get_roots", return_value=[]):
            self.assertEqual(app._chat_roots(), [os.path.realpath(str(d))])


class TestChatExtraRoots(unittest.TestCase):
    """AGENT_MANAGER_CHAT_EXTRA_ROOTS adds repos that are neither plugins nor projects (e.g. crosslister) after core's roots."""

    def _env(self, value):
        return mock.patch.dict(os.environ, {"AGENT_MANAGER_CHAT_EXTRA_ROOTS": value} if value is not None else {}, clear=False)

    def test_unset_leaves_the_core_roots_unchanged(self):
        os.environ.pop("AGENT_MANAGER_CHAT_EXTRA_ROOTS", None)
        with mock.patch("internal_api_client.get_roots", return_value=["/a", "/b"]):
            self.assertEqual(app._chat_roots(), ["/a", "/b"])

    def test_existing_extra_directories_are_appended_after_the_core_roots(self):
        x, y = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        with self._env(os.pathsep.join([str(x), str(y)])), mock.patch("internal_api_client.get_roots", return_value=["/a"]):
            self.assertEqual(app._chat_roots(), ["/a", os.path.realpath(str(x)), os.path.realpath(str(y))])

    def test_missing_paths_blank_entries_and_duplicates_are_skipped(self):
        x = Path(tempfile.mkdtemp())
        value = os.pathsep.join([str(x), "/no/such/dir-xyz", "", "  ", str(x), str(x) + "/../" + x.name])
        with self._env(value), mock.patch("internal_api_client.get_roots", return_value=[os.path.realpath(str(x)), "/a"]):
            self.assertEqual(app._chat_roots(), [os.path.realpath(str(x)), "/a"])

    def test_extras_are_also_added_when_core_is_unreachable(self):
        from internal_api_client import InternalApiError
        core, x = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        with self._env(str(x)), mock.patch.object(app, "CORE_REPO_ROOT", core), \
             mock.patch("internal_api_client.get_roots", side_effect=InternalApiError("down")):
            self.assertEqual(app._chat_roots(), [os.path.realpath(str(core)), os.path.realpath(str(x))])


class TestDiscussProviderArgs(unittest.TestCase):
    def test_defaults_to_local_with_no_model_effort(self):
        provider, model, effort = app._discuss_provider_args({})
        self.assertEqual(provider, "local")
        self.assertIsNone(model)
        self.assertIsNone(effort)

    def test_invalid_provider_falls_back_to_local(self):
        provider, _, _ = app._discuss_provider_args({"provider": "bogus"})
        self.assertEqual(provider, "local")

    def test_claude_provider_fills_in_saved_defaults_when_unset(self):
        with mock.patch.object(app, "claude_defaults", return_value={"model": "opus", "effort": "max"}):
            provider, model, effort = app._discuss_provider_args({"provider": "claude"})
            self.assertEqual((provider, model, effort), ("claude", "opus", "max"))

    def test_claude_provider_explicit_override_wins_over_saved_defaults(self):
        with mock.patch.object(app, "claude_defaults", return_value={"model": "opus", "effort": "max"}):
            provider, model, effort = app._discuss_provider_args({"provider": "claude", "model": "sonnet", "effort": "low"})
            self.assertEqual((provider, model, effort), ("claude", "sonnet", "low"))


if __name__ == "__main__":
    unittest.main()
