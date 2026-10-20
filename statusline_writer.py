#!/usr/bin/env python3
"""Opt-in Claude Code status line command: records each session's context window for the
session agent (session_agent.py --statusline-dir). Never edits settings; install it yourself:
  "statusLine": {"type": "command", "command": "python3 /path/to/statusline_writer.py"}
Reads the status line JSON on stdin, writes <dir>/<sessionId>.json atomically (mode 0600) only
when contextWindowSize or model changed, and prints a short status text. Never fails the status
line: bad input or an unwritable directory just prints a plain text and exits 0.
Stdlib only, no heavy imports: it runs on every status line refresh.
"""
import json
import os
import re
import sys
import time

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".local/state/agent-manager/statusline")


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def extract(payload):
    """The recorded fields from a status line payload, or None without a usable session id."""
    if not isinstance(payload, dict) or not isinstance(payload.get("session_id"), str):
        return None
    if not SESSION_ID_RE.match(payload["session_id"]):
        return None
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    ctx = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    size = ctx.get("context_window_size")
    return {"sessionId": payload["session_id"],
            "contextWindowSize": size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else None,
            "usedPercentage": _num(ctx.get("used_percentage")),
            "model": model.get("id") if isinstance(model.get("id"), str) else None}


def write_snapshot(directory, rec):
    """Write when contextWindowSize/model differ from the file on disk (usedPercentage and
    observedAt are a snapshot as of the last write). Returns True if it wrote."""
    if rec is None or rec["contextWindowSize"] is None:
        return False
    path = os.path.join(directory, rec["sessionId"] + ".json")
    try:
        with open(path, "rb") as f:
            old = json.loads(f.read(4096))
        if old.get("contextWindowSize") == rec["contextWindowSize"] and old.get("model") == rec["model"]:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({**rec, "observedAt": int(time.time())}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def status_text(payload):
    """Short text for the status line, e.g. 'Sonnet 14%'; just the model name early in a session."""
    model = payload.get("model") if isinstance(payload, dict) and isinstance(payload.get("model"), dict) else {}
    ctx = payload.get("context_window") if isinstance(payload, dict) and isinstance(payload.get("context_window"), dict) else {}
    name = model.get("display_name") or model.get("id") or "claude"
    pct = _num(ctx.get("used_percentage"))
    return f"{name} {round(pct)}%" if pct is not None else str(name)


def main(argv=None, stdin=None):
    argv = sys.argv[1:] if argv is None else argv
    directory = (argv[0] if argv else None) or os.environ.get("AM_STATUSLINE_DIR") or DEFAULT_DIR
    try:
        payload = json.loads((stdin or sys.stdin).read(65536))
    except ValueError:
        payload = None
    try:
        write_snapshot(directory, extract(payload))
    except OSError:
        pass
    print(status_text(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
