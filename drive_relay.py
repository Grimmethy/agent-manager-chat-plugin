"""Bounded relay loop: driver<->target chat turns, budgets enforced by drive_links.consume.

Pure: deterministic and offline, no dependency on the chat-session module. `send(session_id, text, driven_by) -> str`
is injected; `now` is injected for deterministic time budget tests.
"""
import time

import drive_links
import drive_log


def run(storage_dir, link_id, first_message, send, now=time.time):
    """Drive one link to a stop. Returns {'turns': int, 'stoppedBy': reason}."""
    link = drive_links.get(storage_dir, link_id)
    if link is None:
        return {"turns": 0, "stoppedBy": "unknown"}
    sender = link["driver"]
    receiver = link["target"]
    text = first_message
    turns = 0
    while True:
        # Budgets (turn and time) live on the link; consume refuses and records it.
        result = drive_links.consume(storage_dir, link_id, now())
        if not result["ok"]:
            # reason: "stopped" (user pressed Stop), "budget", "time", "unknown"
            return {"turns": turns, "stoppedBy": result["reason"]}
        drive_log.append(storage_dir, sender, receiver, text)
        try:
            reply = send(receiver, text, sender)
        except Exception:
            drive_links.stop(storage_dir, link_id, reason="error")
            return {"turns": turns, "stoppedBy": "error"}
        if not reply:
            return {"turns": turns, "stoppedBy": "empty"}
        turns += 1
        text = reply
        sender, receiver = receiver, sender
