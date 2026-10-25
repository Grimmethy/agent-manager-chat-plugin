"""HTTP client for agent-manager's internal Chat API (routes/internal_chat.py in the
core repo -- see /home/wok/.claude/plans/immutable-noodling-axolotl.md, Phase 3).

Replaces this repo's own Phase 1 direct filesystem/subprocess coupling
(chat_preempt.py, single_flight_lock.py, and local_tool_client.py's own subprocess call
to the core repo's src/local-tool-client.js) for the local-provider path: preempt,
reserve, local-turn, and roots resolution now go over HTTP to the core dashboard
instead. See the plan's "Why host-side, not direct filesystem/process access" section
for why this exists at all -- the worker-kill/GPU-lock logic stays host-side, with real
authority over the core repo's own daemons/state, rather than being vendored here.

AGENT_MANAGER_CORE_DASHBOARD_URL (default http://localhost:7420) is where the core
dashboard itself listens -- distinct from AGENT_MANAGER_CORE_REPO_ROOT (a filesystem
path, still used by claude_client.py/model_stats_client.py, which were NOT part of the
internal API this phase adds -- see those modules' own headers).
"""
import json
import os

import requests

CORE_DASHBOARD_URL = os.environ.get("AGENT_MANAGER_CORE_DASHBOARD_URL", "http://localhost:7420").rstrip("/")


class InternalApiError(RuntimeError):
    """Raised when a call to the core dashboard's internal Chat API fails -- connection
    refused, a non-2xx response, or a missing/misconfigured token. Callers already know
    to catch this the same way they catch ClaudeClientError/LocalToolClientError."""


def _token() -> str:
    token = os.environ.get("AGENT_MANAGER_INTERNAL_TOKEN")
    if not token:
        raise InternalApiError("AGENT_MANAGER_INTERNAL_TOKEN is not set -- cannot call the core dashboard's internal Chat API")
    return token


def _headers(extra: dict = None) -> dict:
    h = {"X-Internal-Token": _token()}
    if extra:
        h.update(extra)
    return h


def get_roots() -> list:
    try:
        r = requests.get(f"{CORE_DASHBOARD_URL}/api/internal/chat/roots", headers=_headers(), timeout=10)
        r.raise_for_status()
        return r.json().get("roots", [])
    except requests.RequestException as e:
        raise InternalApiError(f"GET /api/internal/chat/roots failed: {e}") from e


def run_command(argv: list, repo: str, proposal_id: str, confirmed_at: str, session_id: str = None) -> dict:
    """Asks core to run ONE owner-confirmed, allowlisted command (POST /api/internal/chat/run-command; core re-validates everything itself). Returns core's reply:
    { ok, exitCode, stdout, stderr, durationMs } for a command that ran, { ok: False, refused } for one that did not. A transport failure raises InternalApiError."""
    try:
        r = requests.post(
            f"{CORE_DASHBOARD_URL}/api/internal/chat/run-command", headers=_headers(),
            json={"argv": argv, "repo": repo, "proposalId": proposal_id, "confirmedAt": confirmed_at, "sessionId": session_id},
            timeout=960,  # core caps a command at 900 s
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        raise InternalApiError(f"POST /api/internal/chat/run-command failed: {e}") from e


def preempt() -> list:
    try:
        r = requests.post(f"{CORE_DASHBOARD_URL}/api/internal/chat/preempt", headers=_headers(), timeout=20)
        r.raise_for_status()
        return r.json().get("preempted", [])
    except requests.RequestException as e:
        raise InternalApiError(f"POST /api/internal/chat/preempt failed: {e}") from e


def reserve(reservation_id: str, model: str = None, on: bool = True) -> dict:
    if model is None:
        model = os.environ.get("LOCAL_MODEL")
    body = {"reservationId": reservation_id, "on": on}
    if model:
        body["model"] = model
    try:
        # Genuinely blocking on the server side (single_flight_lock.acquire() can wait
        # for a worker lane's current call) -- no short client timeout here, mirroring
        # the same reasoning the in-tree feature's own comment gives for why acquire()
        # must never run under a shorter-lived lock.
        r = requests.post(f"{CORE_DASHBOARD_URL}/api/internal/chat/reserve", headers=_headers(),
                           json=body, timeout=300)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise InternalApiError(f"POST /api/internal/chat/reserve failed: {e}") from e


def stream_local_turn(*, messages: list = None, prompt: str = None, max_turns: int = 5,
                       source: str = None, allow_write: bool = False, primary_root: str = None,
                       extra_roots: list = None, force_summary_on_cap: bool = False,
                       allow_amplification: bool = False, context_log_session_id: str = None):
    """Generator sibling of local_tool_client.py's own stream_plan_with_tools -- SAME
    result shape ({"type": "chunk"|"final"|"error", ...}), just sourced from the core
    dashboard's SSE stream instead of a local Node subprocess. Read timeout matches
    local_tool_client.py's own SUBPROCESS_TIMEOUT_S ceiling (6000s) -- a real multi-turn
    call can legitimately run that long."""
    body = {"maxTurns": max_turns}
    if messages is not None:
        body["messages"] = messages
    else:
        body["prompt"] = prompt
    if source:
        body["source"] = source
    if allow_write:
        body["allowWrite"] = True
    if primary_root:
        body["primaryRoot"] = primary_root
    if extra_roots:
        body["extraRoots"] = list(extra_roots)
    if force_summary_on_cap:
        body["forceSummaryOnCap"] = True
    if allow_amplification:
        body["allowAmplification"] = True
    if context_log_session_id:
        body["contextLogSessionId"] = context_log_session_id

    try:
        with requests.post(
            f"{CORE_DASHBOARD_URL}/api/internal/chat/local-turn",
            headers=_headers({"Accept": "text/event-stream"}),
            json=body, stream=True, timeout=(10, 6000),
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                try:
                    yield json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
    except requests.RequestException as e:
        yield {"type": "error", "error": f"internal API local-turn call failed: {e}"}
