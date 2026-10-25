import json
import os
import subprocess
import unittest
import unittest.mock

import node_client_util


class TestRunNodeWithTempfile(unittest.TestCase):
    """Unit tests for node_client_util.run_node_with_tempfile."""

    def setUp(self):
        self.script_path = "/tmp/test_script.js"
        self.payload = {"key": "value", "num": 42}
        self.extra_args = ["--flag", "--opt"]

    # -- 4a: correct argument list to subprocess.run --

    def test_correct_arg_list(self):
        with unittest.mock.patch("node_client_util.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            node_client_util.run_node_with_tempfile(
                self.script_path, self.payload, self.extra_args
            )
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[0], "node")
            self.assertEqual(cmd[1], self.script_path)
            self.assertEqual(cmd[2 : 2 + len(self.extra_args)], self.extra_args)
            # Last element is the temp-file path (not one of the extra_args).
            temp_path = cmd[-1]
            self.assertNotIn(temp_path, self.extra_args)

    # -- 4b: JSON payload written to the temp file before subprocess.run --

    def test_json_payload_written_to_tempfile(self):
        captured = {}

        def _side_effect(cmd, **kwargs):
            temp_path = cmd[-1]
            with open(temp_path, "rb") as fh:
                captured["content"] = json.loads(fh.read())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with unittest.mock.patch(
            "node_client_util.subprocess.run", side_effect=_side_effect
        ):
            node_client_util.run_node_with_tempfile(
                self.script_path, self.payload, self.extra_args
            )

        self.assertEqual(captured["content"], self.payload)

    def test_json_payload_non_ascii_roundtrip(self):
        payload_with_unicode = {"greeting": "h\u00e9llo", "emoji": "\U0001F600"}
        captured = {}

        def _side_effect(cmd, **kwargs):
            temp_path = cmd[-1]
            with open(temp_path, "rb") as fh:
                captured["content"] = json.loads(fh.read())
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with unittest.mock.patch(
            "node_client_util.subprocess.run", side_effect=_side_effect
        ):
            node_client_util.run_node_with_tempfile(
                self.script_path, payload_with_unicode, []
            )

        self.assertEqual(captured["content"], payload_with_unicode)

    # -- 4c: TimeoutExpired propagates --

    def test_timeout_expired_propagates(self):
        exc = subprocess.TimeoutExpired(cmd="node", timeout=30)
        with unittest.mock.patch(
            "node_client_util.subprocess.run", side_effect=exc
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                node_client_util.run_node_with_tempfile(
                    self.script_path, self.payload, self.extra_args
                )

    # -- 4d: temp file unlinked on success --

    def test_tempfile_unlinked_on_success(self):
        captured_path = {}

        def _side_effect(cmd, **kwargs):
            captured_path["path"] = cmd[-1]
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with unittest.mock.patch(
            "node_client_util.subprocess.run", side_effect=_side_effect
        ):
            node_client_util.run_node_with_tempfile(
                self.script_path, self.payload, self.extra_args
            )

        self.assertFalse(os.path.exists(captured_path["path"]))

    # -- 4e: temp file unlinked on TimeoutExpired --

    def test_tempfile_unlinked_on_timeout(self):
        captured_path = {}

        def _side_effect(cmd, **kwargs):
            captured_path["path"] = cmd[-1]
            raise subprocess.TimeoutExpired(cmd="node", timeout=30)

        with unittest.mock.patch(
            "node_client_util.subprocess.run", side_effect=_side_effect
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                node_client_util.run_node_with_tempfile(
                    self.script_path, self.payload, self.extra_args
                )

        self.assertFalse(os.path.exists(captured_path["path"]))

    # -- 4f: temp file unlinked on generic OSError --

    def test_tempfile_unlinked_on_oserror(self):
        captured_path = {}

        def _side_effect(cmd, **kwargs):
            captured_path["path"] = cmd[-1]
            raise OSError("simulated failure")

        with unittest.mock.patch(
            "node_client_util.subprocess.run", side_effect=_side_effect
        ):
            with self.assertRaises(OSError):
                node_client_util.run_node_with_tempfile(
                    self.script_path, self.payload, self.extra_args
                )

        self.assertFalse(os.path.exists(captured_path["path"]))


if __name__ == "__main__":
    unittest.main()
