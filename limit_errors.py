"""Limit-error classifier + sample recorder for failed Claude chat turns (HUB0008 8/11).

ASSUMPTION (UNVERIFIED): no recorded provider limit-error text exists in this repo
and none may be invented, so the phrases matched below are ASSUMED only:
  - usage-limit messages mention 'usage limit' or 'limit reached' (often with a
    reset time);
  - rate-limit messages mention '429', 'rate limit' or 'rate_limit'.
`record_sample` exists so the owner can collect REAL error text from live failures
(limit-error-samples.jsonl) and turn it into fixtures once real phrasing is seen.
Stdlib only; both functions take a storage_dir (path or str).
"""
import json
import os
from datetime import datetime, timezone

SAMPLES_FILE = "limit-error-samples.jsonl"
_USAGE_LIMIT_PHRASES = ("usage limit", "limit reached")
_RATE_LIMIT_PHRASES = ("429", "rate limit", "rate_limit")


def classify(error_text):
    """Return {'kind': 'usage_limit' | 'rate_limit' | None} -- case-insensitive over
    the ASSUMED phrases named in the module comment; text matching neither gives
    kind None."""
    text = (error_text or "").lower()
    if any(p in text for p in _USAGE_LIMIT_PHRASES):
        return {"kind": "usage_limit"}
    if any(p in text for p in _RATE_LIMIT_PHRASES):
        return {"kind": "rate_limit"}
    return {"kind": None}


def record_sample(storage_dir, error_text):
    """Append one JSON line {"at": <iso timestamp>, "text": error_text[:500]} to
    <storage_dir>/limit-error-samples.jsonl (file mode 0600). Called only for failed
    Claude turns, and the ERROR TEXT ONLY is stored -- never a prompt."""
    storage_dir = os.path.abspath(str(storage_dir))
    os.makedirs(storage_dir, exist_ok=True, mode=0o700)
    os.chmod(storage_dir, 0o700)  # makedirs' mode is ignored when the dir pre-exists
    line = {
        "at": datetime.now(timezone.utc).isoformat(),
        "text": str(error_text)[:500],
    }
    target = os.path.join(storage_dir, SAMPLES_FILE)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    os.chmod(target, 0o600)
