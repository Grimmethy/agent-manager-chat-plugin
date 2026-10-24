"""Account registry for the multi-chat plugin: label, host, token file path and a
single "live" flag. Stdlib only; every function takes a storage_dir (pathlib.Path).

The registry is <storage_dir>/accounts.json, written 0600 (its directory 0700 when
created) via a tmp file + os.replace. A tokenFile is only an opaque PATH: this
module never opens it. Exactly one account is live at any time.
"""
import json
import os
import re
import tempfile
from pathlib import Path

_ACCOUNTS_FILE = "accounts.json"
_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _valid_label(label) -> bool:
    return isinstance(label, str) and bool(_LABEL_RE.match(label))


def _enforce_one_live(accounts: list) -> list:
    """Invariant: exactly one account is live; if zero or several are, keep only
    the first live one (or the first account when none is live)."""
    if not accounts:
        return accounts
    live_idx = [i for i, a in enumerate(accounts) if a.get("live")]
    if len(live_idx) != 1:
        keeper = live_idx[0] if live_idx else 0
        for i, a in enumerate(accounts):
            a["live"] = (i == keeper)
    return accounts


def _save(storage_dir: Path, accounts: list) -> None:
    storage_dir = Path(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(storage_dir, 0o700)
    target = storage_dir / _ACCOUNTS_FILE
    fd, tmp = tempfile.mkstemp(prefix=".accounts.", suffix=".tmp", dir=str(storage_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, indent=2)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(storage_dir) -> list:
    """Return the account list; a missing or corrupt file gives []. Entries
    without a valid label are dropped."""
    storage_dir = Path(storage_dir)
    try:
        data = json.loads((storage_dir / _ACCOUNTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("accounts")
    if not isinstance(raw, list):
        return []
    accounts = [a for a in raw if isinstance(a, dict) and _valid_label(a.get("label"))]
    return _enforce_one_live(accounts)


def add(storage_dir, label: str, host: str, token_file: str) -> None:
    """Register an account; the first account added becomes live. A duplicate
    label (or an invalid label) raises ValueError."""
    if not _valid_label(label):
        raise ValueError(f"invalid label: {label!r}")
    accounts = load(storage_dir)
    if any(a.get("label") == label for a in accounts):
        raise ValueError(f"duplicate label: {label!r}")
    accounts.append({"label": label, "host": host, "tokenFile": token_file,
                     "live": not accounts})
    _save(storage_dir, accounts)


def make_live(storage_dir, label: str) -> None:
    """Make one account the single live one; an unknown label raises KeyError."""
    accounts = load(storage_dir)
    match = next((a for a in accounts if a.get("label") == label), None)
    if match is None:
        raise KeyError(label)
    for a in accounts:
        a["live"] = (a is match)
    _save(storage_dir, accounts)


def public_view(accounts: list) -> list:
    """Only label, host and live -- never tokenFile, never a token."""
    return [{"label": a["label"], "host": a["host"], "live": a["live"]} for a in accounts]


def live_label(storage_dir) -> "str | None":
    """The label of the single live account, or None when there are no accounts."""
    for a in load(storage_dir):
        if a.get("live"):
            return a["label"]
    return None
