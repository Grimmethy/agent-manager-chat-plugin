"""Client for the local session agent (session_agent.py), which lists the developer's
Claude Code terminal sessions. Entirely optional: the feature is off unless
AGENT_MANAGER_SESSION_AGENT_URL is set. AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE (a file holding
the token, e.g. from `session_agent.py --token auto`) or AGENT_MANAGER_SESSION_AGENT_TOKEN, if
set, is sent as a Bearer token on every call; it is added server-side here and never reaches the browser.
Env vars are read per call so tests (and a restarted agent) need no re-import.
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """The session agent is unreachable or answered with something unusable."""


def base_url() -> str:
    return os.environ.get("AGENT_MANAGER_SESSION_AGENT_URL", "").strip().rstrip("/")


def _urls() -> list:
    """AGENT_MANAGER_SESSION_AGENT_URLS: a JSON list of {"host": ..., "url": ...}
    objects, one per machine (e.g. grimm and wok) -- multiple host agents. Read per
    call like the singular key; invalid JSON falls back to the singular key."""
    raw = os.environ.get("AGENT_MANAGER_SESSION_AGENT_URLS", "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        logger.warning("AGENT_MANAGER_SESSION_AGENT_URLS is not valid JSON; ignoring it")
        return []
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("url"), str) and e["url"].strip():
            out.append({"host": str(e.get("host") or "default"),
                        "url": e["url"].strip().rstrip("/")})
    return out


def configured_agents() -> list:
    """Every configured session agent as {"host", "url"} dicts, in order.
    AGENT_MANAGER_SESSION_AGENT_URLS (one entry per machine) wins; a singular
    AGENT_MANAGER_SESSION_AGENT_URL becomes a single "default" host. Empty if
    none is configured."""
    urls = _urls()
    if urls:
        return urls
    url = base_url()
    return [{"host": "default", "url": url}] if url else []


def enabled() -> bool:
    return bool(base_url() or _urls())


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
    """Sessions from EVERY configured agent (multiple host agents: grimm, wok),
    each row tagged with the `host` name from the config. One agent failing is
    recorded in `skipped` and logged, and never aborts the others; with a single
    configured agent a failure still raises AgentError, exactly as before (the
    route reports `reachable: False`)."""
    agents = configured_agents()
    if not agents:
        raise AgentError("session agent is not configured")
    merged = []
    skipped = []
    for agent in agents:
        host, url = agent["host"], agent["url"]
        try:
            r = requests.get(url + "/sessions", headers=_headers(), timeout=3)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            skipped.append({"host": host, "url": url, "error": str(e)})
            logger.warning("session agent /sessions failed host=%s url=%s: %s", host, url, e)
            continue
        if not isinstance(data, list):
            raise AgentError("session agent /sessions returned an unexpected shape")
        for s in data:
            if isinstance(s, dict):
                s["host"] = host
                merged.append(s)
    if skipped and (len(agents) == 1 or not merged):
        raise AgentError("session agent /sessions failed")
    return merged


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
