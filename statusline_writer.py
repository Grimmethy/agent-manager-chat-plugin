#!/usr/bin/env python3
"""Opt-in Claude Code status line command: records each session's context window for the
session agent (session_agent.py --statusline-dir). Never edits settings; install it yourself:
  "statusLine": {"type": "command", "command": "python3 /path/to/statusline_writer.py"}
Reads the status line JSON on stdin, writes <dir>/<sessionId>.json atomically (mode 0600) only
when contextWindowSize or model changed, and prints a short status text. Never fails the status
line: bad input or an unwritable directory just prints a plain text and exits 0.
Stdlib only, no heavy imports: it runs on every status line refresh.
"""
# Usage limits (piece 3/7): extract_limits()/write_limits() pull the 5-hour and weekly usage
# limits out of the status line payload. ASSUMED shape (UNVERIFIED, from the claude-pulse
# reference; no recorded sample exists in this repo and none may be invented):
#   payload['rate_limits'] = {
#       'five_hour': {'used_percentage': <number>, 'resets_at': <epoch seconds | ISO string>},
#       'seven_day': {'used_percentage': <number>, 'resets_at': <epoch seconds | ISO string>},
#   }
# possibly with null per-model caps. The FIRST time main() sees a rate_limits object it writes
# the RAW object, unmodified, to <directory>/rate_limits.sample.json (mode 0600) and never
# overwrites it, so the owner can confirm the real shape. Until then treat the shape as an
# assumption only; see rate_limits.sample.json once captured.
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
ACCOUNT_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".local/state/agent-manager/statusline")
logger = logging.getLogger(__name__)


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _to_epoch(value):
    """A resets_at value as an int of epoch seconds: numbers pass through, ISO strings are
    parsed (a trailing 'Z' means UTC), anything unexpected is None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


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


def extract_limits(payload):
    """The 5-hour and weekly usage limits from a status line payload, or None if the payload
    has no rate_limits dict. ASSUMED shape (UNVERIFIED; see the module comment and
    rate_limits.sample.json for the real shape once captured): five_hour/seven_day entries with
    used_percentage and resets_at (epoch seconds or an ISO string). Null or non-dict per-model
    caps are ignored (never an error); any missing or non-numeric/non-parseable value becomes
    None. Never raises."""
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    result = {}
    for src_key, dst_key in (("five_hour", "fiveHour"), ("seven_day", "weekly")):
        entry = rl.get(src_key)
        if not isinstance(entry, dict):  # null per-model caps are ignored
            result[dst_key] = None
            continue
        result[dst_key] = {"usedPercentage": _num(entry.get("used_percentage")),
                           "resetsAt": _to_epoch(entry.get("resets_at"))}
    return result


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


def _atomic_write(directory, name, obj):
    """Write <directory>/<name> atomically with mode 0600, creating the directory (mode 0700)."""
    path = os.path.join(directory, name)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_limits(directory, account_label, limits):
    """Write {'fiveHour','weekly','observedAt'} to <directory>/limits.<account_label>.json
    atomically (mode 0600). account_label must match ACCOUNT_LABEL_RE or ValueError is raised."""
    if not isinstance(account_label, str) or not ACCOUNT_LABEL_RE.match(account_label):
        raise ValueError(f"invalid account label: {account_label!r}")
    if not isinstance(limits, dict):
        raise ValueError("limits must be a dict")
    body = {"fiveHour": limits.get("fiveHour"), "weekly": limits.get("weekly"),
            "observedAt": int(time.time())}
    return _atomic_write(directory, f"limits.{account_label}.json", body)


def _write_raw_sample_once(directory, raw_object):
    """Record the first raw rate_limits object, unmodified, to
    <directory>/rate_limits.sample.json (mode 0600) so the owner can confirm the real shape;
    never overwrites an existing sample."""
    path = os.path.join(directory, "rate_limits.sample.json")
    if os.path.exists(path):
        return path
    return _atomic_write(directory, "rate_limits.sample.json", raw_object)


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
    except OSError as exc:
        logger.warning("statusline snapshot write to %r failed: %s", directory, exc)
    try:
        limits = extract_limits(payload)
        if limits is not None:
            # Record the raw object exactly once so the owner can confirm the real shape.
            _write_raw_sample_once(directory, payload["rate_limits"])
            label = os.environ.get("AM_ACCOUNT_LABEL", "").strip()
            if label:
                write_limits(directory, label, limits)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("statusline limits write to %r failed: %s", directory, exc)
    print(status_text(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
