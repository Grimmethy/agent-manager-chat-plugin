"""Real send() for the drive relay: one driven chat turn against this repo's sessions.

Sits next to drive_relay.py and imports chat_sessions DIRECTLY (not via drive_relay),
so the relay stays pure/offline-testable while this module owns the live chat call.
"""
import chat_sessions


def default_send(session_id, text, driven_by):
    # This function calls stream_message only and does not call the preempt step used by the message route.
    # CHAT_STORAGE_DIR is imported lazily (routes/chat.py uses the same pattern) because
    # a top-level `from app import ...` would create a circular import: app.py registers
    # the blueprint modules that import this one.
    from app import CHAT_STORAGE_DIR

    last_event = None
    for event in chat_sessions.stream_message(
        CHAT_STORAGE_DIR, session_id, text, driven_by=driven_by
    ):
        last_event = event
    if last_event is None or last_event.get("type") != "final":
        return ""
    transcript = (last_event.get("session") or {}).get("transcript") or []
    if not transcript:
        return ""
    last_row = transcript[-1]
    if last_row.get("error") or last_row.get("role") != "assistant":
        return ""
    return last_row.get("text") or ""
