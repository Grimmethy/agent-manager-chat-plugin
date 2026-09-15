"""Standalone Chat plugin for agent-manager -- see
/home/wok/.claude/plans/immutable-noodling-axolotl.md in the agent-manager checkout that
authored this repo.

Runs as its own process, started/stopped by agent-manager's plugin_process_manager.py
(same lifecycle contract as agent-manager-hardware-plugin: GET /healthz for liveness).

Phase 3 (2026-09-15): the local-provider preempt/priority-marker/reservation logic and
roots resolution that used to be vendored here (chat_preempt.py, single_flight_lock.py --
see git history) or read agent-manager's own plugins.json/projects.json directly now go
through internal_api_client.py instead, calling the core dashboard's
/api/internal/chat/* routes over HTTP. This repo no longer reads/writes
instances//queue/ or holds any GPU lock of its own. claude_client.py and
model_stats_client.py still point directly at the core repo's own src/ files (NOT part
of the internal API -- see those modules' own headers for why).

AGENT_MANAGER_CORE_REPO_ROOT points at the core checkout (still needed by
claude_client.py/model_stats_client.py); AGENT_MANAGER_CORE_DASHBOARD_URL (default
http://localhost:7420, see internal_api_client.py) is where the internal API itself is
reached.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template

# --- Core-repo location + small shared utilities ----------------------------------------

CORE_REPO_ROOT = Path(os.environ.get(
    "AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager",
)).resolve()
ENV_FILE_PATH = CORE_REPO_ROOT / "agent-manager.env"
DASHBOARD_SETTINGS_PATH = CORE_REPO_ROOT / "dashboard-settings.json"

# This repo's own state dir (session storage) -- see chat_sessions.py's CHAT_STORAGE_DIR
# usage below. Kept separate from the core repo entirely (2026-09-15 migration, see the
# plan's "Session storage" section): a fresh ~/.local/state subtree, not the core repo's
# own root the in-tree feature used to hardcode.
STATE_DIR = Path(os.environ.get(
    "AGENT_MANAGER_CHAT_STORAGE_DIR",
    str(Path.home() / ".local/state/agent-manager-chat-plugin/sessions"),
)).resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)
CHAT_STORAGE_DIR = STATE_DIR


def read_env_file(env_path: Path) -> dict:
    """Same KEY=VALUE, comment/blank-line-skipping shape agent-manager's own env parser
    reads -- kept as plain text so both stay in sync with one file format."""
    result = {}
    if not env_path.is_file():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def read_json_safe(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# Matches a `.` + at least 7 digits and captures the first 6 -- see app.py's own
# identical constant for the full PowerShell-heartbeat-timestamp rationale.
_EXCESS_FRACTIONAL_SECONDS_RE = re.compile(r"(\.\d{6})\d+")


def parse_hb_timestamp(ts: str):
    if not ts:
        return None
    normalized = _EXCESS_FRACTIONAL_SECONDS_RE.sub(r"\1", ts.replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _active_project_setting(key: str):
    """agent-manager.env FILE wins over os.environ -- same precedence as app.py's own
    _active_project_setting, for the same reason (the core dashboard's Project tab
    switch always writes the file; os.environ can go stale across a reload there)."""
    v = read_env_file(ENV_FILE_PATH).get(key)
    if v:
        return v
    return os.environ.get(key)


def get_active_repo_root():
    return _active_project_setting("AGENT_MANAGER_REPO_ROOT")


def get_pipeline_dir():
    pipeline_dir = _active_project_setting("AGENT_MANAGER_PIPELINE_DIR")
    if pipeline_dir:
        return Path(pipeline_dir)
    repo_root = get_active_repo_root()
    return Path(repo_root) if repo_root else None


def queue_dir():
    d = get_pipeline_dir()
    return (d / "queue") if d else None


def instances_dir():
    d = get_pipeline_dir()
    return (d / "instances") if d else None


def read_dashboard_settings() -> dict:
    if not DASHBOARD_SETTINGS_PATH.is_file():
        return {}
    try:
        data = json.loads(DASHBOARD_SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def claude_defaults() -> dict:
    settings = read_dashboard_settings()
    return {
        "model": settings.get("claudeDefaultModel") or "sonnet",
        "effort": settings.get("claudeDefaultEffort") or "high",
    }


# --- Chat-specific glue (verbatim ports of app.py's own copies) -------------------------

def _chat_roots() -> list:
    """Ordered, deduped, existing-on-disk list of every repo Chat can touch.

    2026-09-15 (Phase 3 of the Chat-plugin extraction): now a thin wrapper around
    internal_api_client.get_roots() -- the core dashboard resolves this itself (it owns
    plugins.json/projects.json), so this repo no longer needs to read those files
    directly. Falls back to just this repo's own CORE_REPO_ROOT if the internal API call
    fails (core dashboard not running, bad token, etc.) -- same fail-open shape the
    in-tree version's own bare-PACKAGE_ROOT fallback had, so a Chat turn still has
    SOMETHING to root itself at rather than erroring outright."""
    from internal_api_client import InternalApiError, get_roots
    try:
        roots = get_roots()
        if roots:
            return roots
    except InternalApiError:
        pass
    return [os.path.realpath(str(CORE_REPO_ROOT))]


def _discuss_provider_args(body: dict = None):
    """{provider, model, effort} for a chat-start call: per-call override from the
    request body, else the Models tab's saved Claude defaults (only consulted when
    provider is actually "claude")."""
    from flask import request
    if body is None:
        body = request.get_json(silent=True) or {}
    provider = (body.get("provider") or "local").strip().lower()
    if provider not in ("local", "claude"):
        provider = "local"
    model = body.get("model")
    effort = body.get("effort")
    if provider == "claude":
        defaults = claude_defaults()
        model = model or defaults["model"]
        effort = effort or defaults["effort"]
    else:
        model = None
        effort = None
    return provider, model, effort


def _call_chat(fn, *args, **kwargs):
    """Turn a known, actionable failure into a clean 4xx/5xx instead of a raw 500."""
    from claude_client import ClaudeClientError
    from local_tool_client import LocalToolClientError
    try:
        return fn(*args, **kwargs)
    except ClaudeClientError as e:
        abort(502, description=str(e))
    except LocalToolClientError as e:
        abort(502, description=str(e))
    except (TimeoutError, ConnectionError, OSError) as e:
        abort(502, description=f"local model call failed ({e}) -- it may be busy with an active worker-lane task; try again shortly.")


# --- App + routes ------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/chat")
def chat_page():
    """The sidebar's actual page -- served at a real, non-root, non-trailing-slash path
    on purpose: the host dashboard's generic proxy route
    (/api/plugins/<name>/proxy/<path:subpath>) requires a non-empty subpath (Werkzeug's
    `path` converter never matches an empty segment), so an iframe src of
    `/api/plugins/agent-manager-chat-plugin/proxy/` (trailing slash, empty subpath) would
    404 against it. The host's sidebar iframe points at
    `/api/plugins/agent-manager-chat-plugin/proxy/chat` instead -- every relative asset/
    API reference inside chat.html/chat.js then resolves correctly back through that same
    proxy path (see chat.html's own comment on why every href/src/fetch() there is
    relative, never absolute)."""
    return render_template("chat.html")


from routes.chat import chat_bp  # noqa: E402

app.register_blueprint(chat_bp)


if __name__ == "__main__":
    port = int(os.environ.get("AGENT_MANAGER_CHAT_PLUGIN_PORT", "7442"))
    app.run(host="127.0.0.1", port=port)
