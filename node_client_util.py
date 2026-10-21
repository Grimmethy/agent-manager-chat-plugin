"""Shared helper for invoking a Node.js script that reads a JSON payload from a temp file.

This module provides a single function, ``run_node_with_tempfile``, that handles:
  - temp-file creation and writing of the JSON payload,
  - the ``subprocess.run`` call,
  - cleanup (unlink) of the temp file.

It does **not** catch ``subprocess.TimeoutExpired``, does **not** inspect
``returncode``, and does **not** parse ``stdout``/``stderr``.  Callers retain
full responsibility for error handling and output interpretation.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


def run_node_with_tempfile(
    script_path: str,
    extra_args: list[str],
    payload: dict,
    timeout: float,
    *,
    text: bool = False,
) -> subprocess.CompletedProcess:
    """Invoke a Node.js script, passing a JSON payload via a temp file.

    Parameters
    ----------
    script_path:
        Path to the Node.js script to execute.
    extra_args:
        Additional CLI arguments to pass to the script (before the temp-file path).
    payload:
        JSON-serialisable dict to write to the temp file.
    timeout:
        Maximum seconds to wait for the subprocess.
    text:
        If True, decode subprocess output as text (``subprocess.run(text=True)``).

    Returns
    -------
    subprocess.CompletedProcess
        The raw result from ``subprocess.run``.  No error handling is applied.
    """
    tmp_path = Path(tempfile.gettempdir()) / f"node-req-{uuid.uuid4().hex}.json"
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run(
            ["node", str(script_path), *extra_args, str(tmp_path)],
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return result
