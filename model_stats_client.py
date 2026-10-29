"""Python-side wrapper for the CORE agent-manager repo's src/model-stats-db.js
record-call/record-outcome CLI, so Chat's own calls show up in the same model-stats.db
the core dashboard's Models tab already reads, on both providers.

Same "never let stats recording break the real feature" contract as the JS version:
every function here swallows its own errors.

2026-09-15 (Phase 1 of the Chat-plugin extraction -- see
/home/wok/.claude/plans/immutable-noodling-axolotl.md): not vendored -- model-stats.db
and model-stats-db.js both live in the core repo, so this stays pointed at
AGENT_MANAGER_CORE_REPO_ROOT/src/model-stats-db.js (via app.py's CORE_REPO_ROOT) rather
than a relative __file__ path, which does not hold from this repo's different directory
depth.
"""
import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import CORE_REPO_ROOT
from node_client_util import run_node_with_tempfile

MODEL_STATS_DB_JS = CORE_REPO_ROOT / "src" / "model-stats-db.js"

logger = logging.getLogger(__name__)


def _run_event(event: str, payload: dict):
    try:
        run_node_with_tempfile(MODEL_STATS_DB_JS, [event], payload, 15)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("model-stats event %r failed: %s: %s", event, type(exc).__name__, exc, exc_info=True)


def record_call(task_id: str, model: str, latency_ms: int, stage: str = "discuss",
                 result: dict = None, started_at: str = None, source: str = None,
                 instance_id: str = None) -> str:
    """Call once per interactive-session model call. Returns a callId for a later
    record_outcome() (there usually isn't one for free-form chat -- discuss/grill
    sessions have no approve/reject verdict -- so most callers just keep the callId
    unused; it's still useful as a stable row identifier if a caller wants it)."""
    call_id = str(uuid.uuid4())
    result = result or {}
    payload = {
        "callId": call_id,
        "taskId": task_id,
        "stage": stage,
        "model": model,
        "startedAt": started_at or datetime.now(timezone.utc).isoformat(),
        "latencyMs": latency_ms,
        "evalDurationNs": result.get("eval_duration"),
        "promptEvalCount": result.get("prompt_eval_count"),
        "evalCount": result.get("eval_count"),
        "attempts": None,
        "degenerate": result.get("degenerate"),
        "turnsUsed": result.get("turnsUsed"),
        "source": source,
    }
    if instance_id is not None:
        payload["instanceId"] = instance_id
    _run_event("record-call", payload)
    return call_id


def get_turns_summary() -> dict | None:
    try:
        proc = subprocess.run(
            ["node", "--no-warnings", str(MODEL_STATS_DB_JS), "turns-summary"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        logger.warning("model-stats client failed: %s", exc, exc_info=True)
        return None


def record_outcome(call_id: str, outcome: str, outcome_stage: str = "discuss", outcome_reason: str = None):
    if not call_id:
        return
    _run_event("record-outcome", {
        "callId": call_id,
        "outcome": outcome,
        "outcomeStage": outcome_stage,
        "outcomeReason": outcome_reason,
    })
