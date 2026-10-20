"""Client for the local session agent (session_agent.py), which lists the developer's
Claude Code terminal sessions. Entirely optional: the feature is off unless
AGENT_MANAGER_SESSION_AGENT_URL is set. AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE (a file holding
the token, e.g. from `session_agent.py --token auto`) or AGENT_MANAGER_SESSION_AGENT_TOKEN, if
set, is sent as a Bearer token on every call; it is added server-side here and never reaches the browser.
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


def _token():
    """AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE (read on every call, so an agent restarted with
    `--token auto` is picked up) wins over AGENT_MANAGER_SESSION_AGENT_TOKEN."""
    token_file = os.environ.get("AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE", "").strip()
    if token_file:
        try:
            with open(token_file, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    return os.environ.get("AGENT_MANAGER_SESSION_AGENT_TOKEN")


def _headers() -> dict:
    token = _token()
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
        r = requests.get(base_url() + "/events", headers=_headers(), params={"types": "session"},
                         stream=True, timeout=(3, 60))
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


def filter_events(chunks, allowed=("session.updated", "session.removed")):
    """Pass through only whole SSE blocks whose event name is in `allowed` (plus comment
    keepalives). transcript.append carries private text the UI never shows, so it is dropped
    here even though the agent is also asked (types=session) not to send it."""
    buf = b""
    for chunk in chunks:
        buf += chunk
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            lines = block.split(b"\n")
            if all(l.startswith(b":") for l in lines):
                yield block + b"\n\n"
                continue
            name = next((l[7:].strip() for l in lines if l.startswith(b"event:")), b"")
            if name.decode("utf-8", "replace") in allowed:
                yield block + b"\n\n"
