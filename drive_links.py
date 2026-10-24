"""Drive links: turn/time budgets and a stop switch. Backing file
<storage_dir>/drive-links.json (0600), atomic temp+os.replace write; corrupt or missing file reads as no links."""

import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CHAT_ID_RE = re.compile(r"[A-Za-z0-9._:/-]{1,120}")
_FILENAME = "drive-links.json"


def _links_path(storage_dir):
    return Path(storage_dir) / _FILENAME


def _read_links(storage_dir):
    try:
        data = json.loads(_links_path(storage_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_links(storage_dir, links):
    storage_dir = Path(storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    p = _links_path(storage_dir)
    fd, tmp = tempfile.mkstemp(prefix="drive-links-", suffix=".tmp", dir=storage_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(links, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _as_aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _as_dt(now):
    """UTC datetime or Unix epoch (int/float) -> aware UTC datetime (or None)."""
    if now is None:
        return None
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, timezone.utc)
    return _as_aware(now)


def _now_iso(now=None):
    if now is None:
        return datetime.now(timezone.utc).isoformat()
    return _as_dt(now).isoformat()


def _valid_chat_id(x):
    return isinstance(x, str) and _CHAT_ID_RE.fullmatch(x) is not None


def create(storage_dir, driver, target, turn_budget=10, time_budget_min=60, now=None):
    if not _valid_chat_id(driver):
        raise ValueError("driver is not a valid chat id")
    if not _valid_chat_id(target):
        raise ValueError("target is not a valid chat id")
    if driver == target:
        raise ValueError("driver and target must differ")
    if turn_budget < 1:
        raise ValueError("turn_budget must be at least 1")
    if time_budget_min < 1:
        raise ValueError("time_budget_min must be at least 1")
    link = {
        "id": f"drive-{int(time.time())}-{secrets.token_hex(4)}",
        "driver": driver,
        "target": target,
        "turnBudget": turn_budget,
        "timeBudgetMin": time_budget_min,
        "createdAt": _now_iso(now),
        "turnsUsed": 0,
        "active": True,
        "stoppedAt": None,
        "stopReason": None,
    }
    links = _read_links(storage_dir)
    links.append(link)
    _write_links(storage_dir, links)
    return link


def get(storage_dir, link_id):
    for link in _read_links(storage_dir):
        if link.get("id") == link_id:
            return link
    return None


def list_links(storage_dir, active_only=False):
    links = _read_links(storage_dir)
    if active_only:
        return [l for l in links if l.get("active") is True]
    return links


def stop(storage_dir, link_id, reason="user", now=None):
    links = _read_links(storage_dir)
    link = next((l for l in links if l.get("id") == link_id), None)
    if link is None:
        return None
    link["active"] = False
    link["stoppedAt"] = _now_iso(now)
    link["stopReason"] = reason
    _write_links(storage_dir, links)
    return link


def consume(storage_dir, link_id, now=None):
    now = _as_dt(now)
    links = _read_links(storage_dir)
    link = next((l for l in links if l.get("id") == link_id), None)
    if link is None:
        return {"ok": False, "reason": "unknown", "link": None}
    if link.get("active") is False:
        return {"ok": False, "reason": "stopped", "link": link}
    if link["turnsUsed"] >= link["turnBudget"]:
        link["active"] = False
        link["stoppedAt"] = _now_iso(now)
        link["stopReason"] = "budget"
        _write_links(storage_dir, links)
        return {"ok": False, "reason": "budget", "link": link}
    if now is not None and now > _as_aware(
        datetime.fromisoformat(link["createdAt"])
    ) + timedelta(minutes=link["timeBudgetMin"]):
        link["active"] = False
        link["stoppedAt"] = _now_iso(now)
        link["stopReason"] = "time"
        _write_links(storage_dir, links)
        return {"ok": False, "reason": "time", "link": link}
    link["turnsUsed"] += 1
    _write_links(storage_dir, links)
    return {"ok": True, "reason": None, "link": link}
