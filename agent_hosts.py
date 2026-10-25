"""Multi-host session-agent configuration. Reads env vars on every call (no caching).

Plural form AGENT_MANAGER_SESSION_AGENT_URLS (JSON list) wins over the singular
AGENT_MANAGER_SESSION_AGENT_URL when set and non-empty.
"""
import json
import logging
import os
from collections import namedtuple

logger = logging.getLogger(__name__)

Host = namedtuple("Host", "name url token_file token")


def hosts() -> list:
    """Return the list of configured session-agent Hosts (reads env per call)."""
    raw = os.environ.get("AGENT_MANAGER_SESSION_AGENT_URLS", "").strip()
    if raw:
        return _parse_plural(raw)
    return _singular()


def _parse_plural(raw: str) -> list:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("AGENT_MANAGER_SESSION_AGENT_URLS is not valid JSON; ignoring")
        return []
    if not isinstance(data, list):
        logger.warning("AGENT_MANAGER_SESSION_AGENT_URLS must be a JSON list; ignoring")
        return []
    seen = set()
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-object entry in AGENT_MANAGER_SESSION_AGENT_URLS")
            continue
        name = entry.get("host")
        url = entry.get("url")
        if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
            logger.warning(
                "Skipping entry missing 'host' or 'url' in AGENT_MANAGER_SESSION_AGENT_URLS"
            )
            continue
        url = url.rstrip("/")
        if name in seen:
            logger.warning(
                "Duplicate host name '%s' in AGENT_MANAGER_SESSION_AGENT_URLS; keeping first",
                name,
            )
            continue
        seen.add(name)
        tf = entry.get("tokenFile")
        token_file = tf if isinstance(tf, str) and tf else None
        out.append(Host(name=name, url=url, token_file=token_file, token=None))
    return out


def _singular() -> list:
    url = os.environ.get("AGENT_MANAGER_SESSION_AGENT_URL", "").strip().rstrip("/")
    if not url:
        return []
    tf = os.environ.get("AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE", "").strip() or None
    tok = os.environ.get("AGENT_MANAGER_SESSION_AGENT_TOKEN", "").strip() or None
    return [Host(name="default", url=url, token_file=tf, token=tok)]


def read_token(host: Host):
    """Return the token for a host: token_file (re-read at call time) wins, else static token."""
    if host.token_file:
        try:
            with open(host.token_file, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            return None
    return host.token if host.token else None


def get_host(name: str):
    """Return the Host with the given name, or None."""
    for h in hosts():
        if h.name == name:
            return h
    return None
