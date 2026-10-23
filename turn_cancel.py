"""Thread-safe, process-local set of session IDs that have requested cancellation
of a running local-model turn.

State is intentionally process-local: a restart clears all pending cancel flags,
mirroring the design of ``_busy_sessions`` in ``chat_sessions.py``.
"""

import threading

# Process-local on purpose: a restart ends every pending cancel flag.
_cancel_requested = set()
_cancel_lock = threading.Lock()


def request(session_id):
    """Flag *session_id* as having requested cancellation of its current turn."""
    with _cancel_lock:
        _cancel_requested.add(session_id)


def is_requested(session_id):
    """Return True if *session_id* has a pending cancel request."""
    with _cancel_lock:
        return session_id in _cancel_requested


def clear(session_id):
    """Remove the cancel flag for *session_id* (no-op if not present)."""
    with _cancel_lock:
        _cancel_requested.discard(session_id)
