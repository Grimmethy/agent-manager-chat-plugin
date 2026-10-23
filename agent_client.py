"""Client for the local session agent (session_agent.py), which lists the developer's
Claude Code terminal sessions. Entirely optional: the feature is off unless a session
agent is configured (agent_hosts.py: singular AGENT_MANAGER_SESSION_AGENT_URL or the
multi-host AGENT_MANAGER_SESSION_AGENT_URLS). Each host's own token (a token file
holding the token, e.g. from `session_agent.py --token auto`, or a static token) is
sent as a Bearer header to THAT host only; it is added server-side here and never
reaches the browser. Env vars are read per call so tests (and a restarted agent)
need no re-import.
"""
import logging
import os

import requests

import agent_hosts

logger = logging.getLogger(__name__)

# Seconds to wait for a host's /sessions response. Module constant so tests can
# patch it down to exercise the slow-host path without really waiting.
REQUEST_TIMEOUT = 3


class AgentError(RuntimeError):
    """The session agent is unreachable or answered with something unusable."""


def base_url() -> str:
    return os.environ.get("AGENT_MANAGER_SESSION_AGENT_URL", "").strip().rstrip("/")


def enabled() -> bool:
    return bool(agent_hosts.hosts())


def _headers() -> dict:
    token = os.environ.get("AGENT_MANAGER_SESSION_AGENT_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def list_all() -> dict:
    """/sessions from every configured host: {'sessions': [...], 'skipped': [...]}.

    Each row is tagged 'host': <host name> (other fields untouched). A host that
    fails (connection error, timeout, HTTP error, bad JSON, not a list) is reported
    in 'skipped' as {'host': name, 'reason': short text} and never aborts the others.
    Each host's token goes only to that host; a host with no token sends no
    Authorization header. Failures log the host name and exception class only.
    """
    sessions = []
    skipped = []
    for host in agent_hosts.hosts():
        headers = {}
        token = agent_hosts.read_token(host)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = requests.get(host.url + "/sessions", headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("session agent host '%s' /sessions failed: %s",
                           host.name, type(e).__name__)
            skipped.append({"host": host.name, "reason": type(e).__name__})
            continue
        if not isinstance(data, list):
            logger.warning("session agent host '%s' /sessions returned an unexpected shape: %s",
                           host.name, type(data).__name__)
            skipped.append({"host": host.name, "reason": "unexpected shape"})
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            row["host"] = host.name
            sessions.append(row)
    return {"sessions": sessions, "skipped": skipped}


def list_sessions() -> list:
    """All configured hosts' session rows. Raises AgentError when no host is
    configured or every configured host failed -- so a single configured host
    failing behaves exactly as it did before multi-host support."""
    hosts = agent_hosts.hosts()
    if not hosts:
        raise AgentError("session agent is not configured")
    result = list_all()
    if result["skipped"] and len(result["skipped"]) == len(hosts):
        raise AgentError("session agent /sessions failed")
    return result["sessions"]


def open_events():
    """An open streaming response for the agent's /events SSE. The caller must close it.
    Read timeout 60s: the agent sends a keepalive comment every 15s. Singular config only."""
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
