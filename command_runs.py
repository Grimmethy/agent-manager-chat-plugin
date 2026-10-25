"""Owner-approved write commands on the chat transcript (owner decisions 2026-10-25; contract: docs/write-command-allowlist-design.md section 6).

The chat can only PROPOSE a command (core's propose_command tool); the proposal rides on the turn's final event and is stored on the assistant transcript
entry as `commandProposals`. Nothing runs until the owner presses Confirm in the pane: that persists the decision FIRST (status 'confirmed', `decidedAt`), then
core's restricted runner is called with that timestamp, and the scrubbed, clipped result is stored as `commandExecutions`. Everything lives on the transcript entry
itself, so the audit trail is exactly what the owner sees and survives a reload.

Pure functions over the session dict; the route in routes/chat.py does the storage writes and the call to core.
"""
from datetime import datetime, timezone

import tool_log

STATUSES = ("pending", "confirmed", "denied", "ran")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_proposals(entry: dict, proposals) -> None:
    """Stores core's accepted proposals ([{proposalId, repo, argv, why}]) on the assistant entry as pending; junk entries are skipped."""
    stored = []
    for p in proposals or []:
        if not isinstance(p, dict) or not isinstance(p.get("proposalId"), str) or not p["proposalId"]:
            continue
        if not isinstance(p.get("argv"), list) or not isinstance(p.get("repo"), str):
            continue
        stored.append({
            "proposalId": p["proposalId"], "repo": p["repo"], "argv": [str(a) for a in p["argv"]],
            "why": str(p.get("why") or ""), "status": "pending", "decidedAt": None,
        })
    if stored:
        entry["commandProposals"] = stored


def find_proposal(session: dict, proposal_id: str):
    """(entry, proposal) for a proposalId anywhere in the transcript, else (None, None)."""
    for entry in session.get("transcript", []):
        for proposal in entry.get("commandProposals") or []:
            if proposal.get("proposalId") == proposal_id:
                return entry, proposal
    return None, None


def decide(session: dict, proposal_id: str, action: str, now: str = None) -> dict:
    """Persisted owner decision: a pending proposal becomes 'confirmed' or 'denied' with decidedAt. LookupError when there is no such proposal, ValueError for an
    unknown action or one that is already decided (a second click can never run it twice)."""
    if action not in ("confirm", "deny"):
        raise ValueError("action must be confirm or deny")
    _, proposal = find_proposal(session, proposal_id)
    if proposal is None:
        raise LookupError(proposal_id)
    if proposal.get("status") != "pending":
        raise ValueError(f"this proposal is already {proposal.get('status')}")
    proposal["status"] = "confirmed" if action == "confirm" else "denied"
    proposal["decidedAt"] = now or _now_iso()
    return proposal


def record_execution(session: dict, proposal_id: str, reply) -> dict:
    """Stores core's reply for a confirmed proposal as a scrubbed, clipped `commandExecutions` row on the same entry and marks the proposal 'ran' when the
    command really started (the reply has an exitCode key). A refusal or a transport failure is stored too (ok False, the reason in stderr)."""
    entry, proposal = find_proposal(session, proposal_id)
    if proposal is None:
        raise LookupError(proposal_id)
    reply = reply if isinstance(reply, dict) else {"ok": False, "refused": "core returned no usable reply"}
    ran = "exitCode" in reply
    row = {
        "proposalId": proposal_id, "argv": proposal["argv"], "repo": proposal["repo"], "confirmedAt": proposal.get("decidedAt"),
        "exitCode": reply.get("exitCode") if ran else None, "ok": bool(reply.get("ok")) and ran,
        "stdout": tool_log.scrub_output(reply.get("stdout")),
        "stderr": tool_log.scrub_output(reply.get("stderr") if ran else (reply.get("refused") or "not run")),
        "durationMs": reply.get("durationMs") if isinstance(reply.get("durationMs"), int) else None,
    }
    entry.setdefault("commandExecutions", []).append(row)
    if ran:
        proposal["status"] = "ran"
    return row


def context_note(entry: dict) -> str:
    """One short block for the model's context on later turns: each proposal's state and, for a run, its exit code and the start of its output. '' when the
    entry has no proposals."""
    lines = []
    runs = {r.get("proposalId"): r for r in entry.get("commandExecutions") or []}
    for p in entry.get("commandProposals") or []:
        cmd = " ".join(p.get("argv") or [])
        status = p.get("status")
        if status == "pending":
            lines.append(f"[Proposed command awaiting the owner's confirmation (not run): {cmd}]")
        elif status == "denied":
            lines.append(f"[The owner denied the proposed command (not run): {cmd}]")
        else:
            run = runs.get(p.get("proposalId"))
            if run is None:
                lines.append(f"[The owner confirmed the proposed command; it has not reported back yet: {cmd}]")
            elif run.get("exitCode") is None:
                lines.append(f"[The owner confirmed the command but it did not run: {cmd} -- {run.get('stderr')}]")
            else:
                out = (run.get("stdout") or "").strip()[:600]
                err = (run.get("stderr") or "").strip()[:300]
                lines.append(
                    f"[Command run with the owner's approval: {cmd} -> exit {run['exitCode']}"
                    + (f"; output: {out}" if out else "") + (f"; errors: {err}" if err else "") + "]"
                )
    return ("\n" + "\n".join(lines)) if lines else ""
