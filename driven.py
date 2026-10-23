"""Admit a driven turn against its drive link.

Identity/existence checks happen here; the budget/time/stopped decision is
made by drive_links.consume and passed through unchanged.
"""

import drive_links


def admit(storage_dir, link_id, driver_id, target_id, now=None):
    link = drive_links.get(storage_dir, link_id)
    if link is None:
        return {"ok": False, "reason": "unknown"}
    if (
        not link.get("active")
        or link.get("driver") != driver_id
        or link.get("target") != target_id
    ):
        return {"ok": False, "reason": "mismatch"}
    # budget/time/stopped semantics are delegated to drive_links.consume and not re-implemented here.
    result = drive_links.consume(storage_dir, link_id, now=now)
    return {"ok": result["ok"], "reason": result["reason"]}
