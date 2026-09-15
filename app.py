"""Standalone Chat plugin for agent-manager (Phase 1 of the extraction plan -- see
/home/wok/.claude/plans/immutable-noodling-axolotl.md in the agent-manager checkout that
authored this repo).

Runs as its own process, started/stopped by agent-manager's plugin_process_manager.py
(same lifecycle contract as agent-manager-hardware-plugin: GET /healthz for liveness).

Phase 1 deliberately keeps the SAME direct filesystem/subprocess coupling to the CORE
agent-manager repo the in-tree Chat feature always had (reads its agent-manager.env/
plugins.json/projects.json, instances/, queue/, shells out to its
scripts/gpu-arbiter-cli.js) -- this repo is independently runnable/testable, but not yet
decoupled. Phase 2/3 of the plan replaces this with a small internal API agent-manager
exposes instead, so this repo no longer needs read/write access to the core repo's
runtime state at all.

AGENT_MANAGER_CORE_REPO_ROOT points at that core checkout (defaults to the sibling
directory this repo's own dev machine already uses tonight).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify

# --- Core-repo location + small shared utilities (verbatim ports of app.py's own copies,
# see that file's history for chat_preempt.py's own 2026-09-15 sibling extraction) -------

CORE_REPO_ROOT = Path(os.environ.get(
    "AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager",
)).resolve()
# Name matches what chat_preempt.py's lazy `from app import PACKAGE_ROOT` expects --
# it means "the agent-manager core repo root" there, same meaning this repo gives it.
PACKAGE_ROOT = CORE_REPO_ROOT
ENV_FILE_PATH = PACKAGE_ROOT / "agent-manager.env"
PLUGINS_MANIFEST_PATH = PACKAGE_ROOT / "plugins.json"
PROJECT_REGISTRY_PATH = PACKAGE_ROOT / "projects.json"
DASHBOARD_SETTINGS_PATH = PACKAGE_ROOT / "dashboard-settings.json"

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


def _read_plugins_manifest() -> list:
    if not PLUGINS_MANIFEST_PATH.is_file():
        return []
    try:
        parsed = json.loads(PLUGINS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return parsed if isinstance(parsed, list) else []


def read_project_registry() -> list:
    if not PROJECT_REGISTRY_PATH.is_file():
        return []
    try:
        data = json.loads(PROJECT_REGISTRY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


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
    """Ordered, deduped, existing-on-disk list of every repo Chat can touch. roots[0] is
    always the agent-manager core repo; the rest are each enabled plugin's repo and each
    projects.json repoRoot."""
    raw = [str(PACKAGE_ROOT)]
    for e in _read_plugins_manifest():
        rp = e.get("registerPath")
        if rp and e.get("enabled") is not False:
            raw.append(os.path.dirname(rp))
    for e in read_project_registry():
        if e.get("repoRoot"):
            raw.append(e["repoRoot"])
    seen, out = set(), []
    for p in raw:
        try:
            real = os.path.realpath(p)
        except OSError:
            continue
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        out.append(real)
    return out or [os.path.realpath(str(PACKAGE_ROOT))]


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


# --- Chat reservation registry (process-local, verbatim port) ---------------------------

import threading

_chat_reservations = {}
_chat_reservations_lock = threading.Lock()

# chat_preempt.py's own functions, re-exported into this module's namespace so
# routes/chat.py's `from app import _chat_preempt_enabled, _preempt_pipeline_for_chat`
# (same lazy-import convention the in-tree feature used) resolves here. chat_preempt.py's
# own lazy `from app import PACKAGE_ROOT, ENV_FILE_PATH, read_env_file, instances_dir,
# queue_dir, read_json_safe, parse_hb_timestamp` closes the loop -- every name it expects
# is defined above.
from chat_preempt import _chat_preempt_enabled, _preempt_pipeline_for_chat  # noqa: E402


# --- App + routes ------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


from routes.chat import chat_bp  # noqa: E402

app.register_blueprint(chat_bp)


if __name__ == "__main__":
    port = int(os.environ.get("AGENT_MANAGER_CHAT_PLUGIN_PORT", "7442"))
    app.run(host="127.0.0.1", port=port)
