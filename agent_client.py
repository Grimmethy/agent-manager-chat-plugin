"""Client for the local session agent (session_agent.py), which lists the developer's
Claude Code terminal sessions. Entirely optional: the feature is off unless
AGENT_MANAGER_SESSION_AGENT_URL is set. AGENT_MANAGER_SESSION_AGENT_TOKEN, if set, is sent
as a Bearer token on every call; it is added server-side here and never reaches the browser.
Env vars are read per call so tests (and a restarted agent) need no re-import.
"""
import os

import requests


class AgentError(RuntimeError):
    """The session agent is unreachable or answered with something unusable."""


def base_url() -> str:
    return os.environ.get("AGENT_MANAGER_SESSION_AGENT_URL", "").strip().rstrip("/")


def enabled() -> bool:
    return bool(base_url())


def _headers() -> dict:
    token = os.environ.get("AGENT_MANAGER_SESSION_AGENT_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def list_sessions() -> list:
    try:
        r = requests.get(base_url() + "/sessions", headers=_headers(), timeout=3)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise AgentError("session agent /sessions failed") from e
    if not isinstance(data, list):
        raise AgentError("session agent /sessions returned an unexpected shape")
    return [row for row in data if isinstance(row, dict)]


def open_events():
    """An open streaming response for the agent's /events SSE. The caller must close it.
    Read timeout 60s: the agent sends a keepalive comment every 15s."""
    try:
        r = requests.get(base_url() + "/events", headers=_headers(), stream=True, timeout=(3, 60))
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        raise AgentError("session agent /events failed") from e


def iter_chunks(resp):
    """Bytes as they arrive on an open /events response. iter_content(chunk_size=None)
    would block until the (never-ending, close-delimited) body ends, so this reads
    whatever is available (urllib3 2.x read1) and falls back to 1-byte reads."""
    read1 = getattr(resp.raw, "read1", None)
    if read1 is None:
        yield from resp.iter_content(chunk_size=1)
        return
    while True:
        chunk = read1(4096)
        if not chunk:
            return
        yield chunk
