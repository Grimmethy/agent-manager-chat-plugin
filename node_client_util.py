"""Shared helper for invoking a Node.js script with a JSON payload passed via a temp file."""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path


def run_node_with_tempfile(
    script_path: Path,
    extra_args: list[str],
    payload: dict,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Write `payload` as JSON to a unique temp file, run `node <script_path> <extra_args...> <temp_file>`,
    and always unlink the temp file.

    Raises OSError or subprocess.SubprocessError on failure (does not swallow errors).
    """
    temp_path = Path(tempfile.gettempdir()) / f"node_payload_{uuid.uuid4().hex}.json"
    try:
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        cmd = ["node", str(script_path), *[str(a) for a in extra_args], str(temp_path)]
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass
