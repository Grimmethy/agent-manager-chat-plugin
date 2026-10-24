"""Append-only drive log: records one JSONL line per message with time/from/to/text.
Stdlib only, no heavy imports."""
import json
import os
import datetime
from pathlib import Path

LOG_FILENAME = "drive-log.jsonl"
MAX_TEXT = 200


def append(storage_dir, frm, to, text, now=None):
    """Append a single record to <storage_dir>/drive-log.jsonl (mode 0600).

    Truncates text to 200 chars. If now is None, uses the current UTC time.
    Returns the record dict that was written.
    """
    storage_dir = Path(storage_dir)
    storage_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    ts = now if now is not None else datetime.datetime.now(datetime.timezone.utc).isoformat()
    if isinstance(ts, datetime.datetime):
        ts = ts.isoformat()

    record = {
        "time": ts,
        "from": frm,
        "to": to,
        "text": str(text)[:MAX_TEXT],
    }
    line = json.dumps(record) + "\n"
    log_path = storage_dir / LOG_FILENAME
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return record


def tail(storage_dir, n=50, pair=None):
    """Return the last n records from <storage_dir>/drive-log.jsonl (newest last).

    If pair is provided, only records where pair matches from or to are returned.
    Malformed lines are skipped. Returns [] if the log file does not exist.
    """
    storage_dir = Path(storage_dir)
    log_path = storage_dir / LOG_FILENAME
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return []

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        if pair is not None:
            if rec.get("from") != pair and rec.get("to") != pair:
                continue
        records.append(rec)

    return records[-n:]
