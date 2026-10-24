"""Per-account usage snapshots (multi-chat, HUB0007 1/4 -- the reader for HUB0006 4/7).

statusline_writer.write_limits stores <directory>/limits.<label>.json as
    {"fiveHour": {"usedPercentage": number|null, "resetsAt": epoch seconds|null} | null,
     "weekly":   {... same ...} | null,
     "observedAt": epoch seconds}
read_usage turns that into what the chat UI shows. NOTE the key rename: the file says
`usedPercentage`, the result says `percent`. Stdlib only; no token or path is ever returned.
"""
import json
import os
import re

STALE_AFTER_SECONDS = 6 * 3600
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _number(value):
    """A real number (bool excluded), else None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _window(entry, now):
    """One window of the snapshot -> {'percent', 'resetsAt', 'reset'}. Once `now` is at or past
    resetsAt the window has rolled over: percent becomes None and reset True, so the UI never
    shows a stale percentage. A missing or malformed window is all-None with reset False."""
    if not isinstance(entry, dict):
        return {"percent": None, "resetsAt": None, "reset": False}
    percent = _number(entry.get("usedPercentage"))
    resets_at = _number(entry.get("resetsAt"))
    if resets_at is not None and now >= resets_at:
        return {"percent": None, "resetsAt": resets_at, "reset": True}
    return {"percent": percent, "resetsAt": resets_at, "reset": False}


def read_usage(directory, label, now):
    """Read limits.<label>.json from `directory`. Returns None when the label is not a valid
    account label, or the file is missing, unreadable, not JSON, or not a JSON object.
    Otherwise {'fiveHour': {...}, 'weekly': {...}, 'stale': bool}; `now` is epoch seconds.
    stale is True when the snapshot is more than 6 hours old, or has no usable observedAt."""
    if not isinstance(label, str) or not _LABEL_RE.match(label):
        return None
    try:
        with open(os.path.join(directory, "limits.%s.json" % label), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    observed = _number(data.get("observedAt"))
    stale = observed is None or (now - observed) > STALE_AFTER_SECONDS
    return {
        "fiveHour": _window(data.get("fiveHour"), now),
        "weekly": _window(data.get("weekly"), now),
        "stale": stale,
    }
