"""Chat panel session logic -- Brain Dump #153 (Grimmethy): "a hideable panel on the
right side of the app for a conversational AI, very similar to the claude terminal I have
been using externally. It should be able to access the project files and make edits to
the system... the chat in the machine."

2026-08-31 (Grimmethy: "It should be rooted in agent manager always, and have access to
all active plugins"): the panel is no longer scoped to whichever project the pipeline is
pointed at. It is one global, system-wide conversation ROOTED at the agent-manager repo,
with real Read/Grep/Glob/Edit/Write/Bash access to agent-manager PLUS every registered
plugin (plugins.json) and project (projects.json). `session["roots"]` carries that list,
`roots[0]` (agent-manager) is the primary/`cwd`; app.py's `_chat_roots()` builds it and
the storage dir is fixed at the agent-manager repo root, not the active pipelineDir.

Deliberately NOT built on discuss_sessions.py's shape despite some surface similarity:
Discuss is per-subject (a task/note/brain-dump entry), read-only, and rebuilds a fresh
prompt+transcript every turn because neither provider has real session continuity wired
in there. Chat is global (one ongoing conversation per project, not tied to any subject),
has real Edit/Write/Bash access on both providers, and -- for Claude -- uses the CLI's own
`--resume` session continuity (see claude_client.py's own header) instead of replaying a
transcript. The local provider still has no native session-resume (Ollama's /api/generate
is stateless), so it keeps Discuss's proven transcript-replay approach -- but now through
local_tool_client.py's tool-calling loop (allow_write=True) instead of a bare completion,
since Chat's local provider needs real Read/Grep/Glob/Edit/Write/Bash tool access, not
just text.

Git safety (edits landing on the LIVE working tree, not an isolated worktree) is handled
by the CALLER, not here -- app.py wraps each send_message() call in the exact same
_acquire_apply_lock()/_release_apply_lock() pair already used by the merge-branch
endpoint, so this module never needs its own git-mutex logic.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import claude_client
import local_tool_client
import model_stats_client
import ollama_client
import template_store

PROVIDER_LOCAL = "local"
PROVIDER_CLAUDE = "claude"

# Real Edit/Write/Bash, unlike Discuss's deliberately read-only CLAUDE_DISCUSS_ALLOWED_TOOLS
# -- this is the whole point of the Chat panel (Brain Dump #153: "make edits to the
# system"). cwd is the live repoRoot, not an isolated worktree -- see this module's own
# header on the trust model this mirrors (this actual terminal session).
CHAT_CLAUDE_ALLOWED_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
CHAT_CLAUDE_MAX_TURNS = 30
# 2026-08-26, Grimmethy: "Seems like qwen got hung up" -- root-caused live, not actually
# hung: model-stats.db showed 4 real completed calls (65s/16s/75s/102s), zero errors, zero
# degenerate flags. It genuinely ran out of its 5-turn budget mid-investigation ("look at
# AC-3, what went wrong" needs locate-doc -> find-AC-3 -> check-git-log -> read-source,
# comfortably more than 5 read-only-plus-bash tool calls for a model this size) and
# returned whatever text happened to be attached to its LAST tool-calling turn -- "I'll
# check X next," not a real answer, since local-tool-client.js's own loop only returns a
# synthesized final response when a turn comes back with NO tool calls at all. Raised
# from 5 to 15 first, then to 100 the same night after a live AC-3 test still ran out of
# the 15-turn budget mid-investigation without reaching a conclusion (a multi-hop
# investigation -- locate doc, read doc, read referenced source, read a second file,
# check a third -- can legitimately need more than 15 read-only-plus-bash calls for a
# model this size). See local_tool_client.py's own SUBPROCESS_TIMEOUT_S comment for the
# matching wall-clock headroom this needed too -- that value MUST scale with this one.
CHAT_LOCAL_MAX_TURNS = 100


# 2026-10-18 (multi-chat workspace, Phase 1): one JSON file PER SESSION instead of one
# file holding every session. The old single file was read and rewritten whole on every
# streamed chunk, so a turn in session A held a stale copy of session B and erased B's
# progress the moment both streamed at once -- the design doc's "several chats open at
# once" cannot work on top of that. Every writer below now reads and rewrites only its
# own session's file. _read_sessions/_write_sessions keep their names and shapes for the
# callers that want all sessions, but _write_sessions can only touch the sessions it is
# handed.
SESSIONS_DIRNAME = ".agent-manager-chat-sessions"
# 2026-10-19 (multi-chat workspace): the on-disk session schema version. v1 (master)
# sessions have no schemaVersion/account/swaps/summary keys; _migrate_session() below
# adds exactly those four with their defaults when a session reads in without them.
SCHEMA_VERSION = 2
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_migrate_lock = threading.Lock()


def chat_sessions_path(storage_dir: Path) -> Path:
    """The LEGACY single-file store -- read only by the one-time migration below."""
    return storage_dir / ".agent-manager-chat-sessions.json"


def sessions_dir(storage_dir: Path) -> Path:
    return storage_dir / SESSIONS_DIRNAME


def _session_path(storage_dir: Path, session_id: str) -> Path:
    # Ids become file names: refuse anything that could leave the sessions dir.
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid chat session id: {session_id!r}")
    return sessions_dir(storage_dir) / f"{session_id}.json"


def _migrate_legacy_store(storage_dir: Path):
    """Splits the legacy single-file store into per-session files, once. Ids that already
    have a file keep it (never overwritten); the legacy file is renamed to .migrated, never
    deleted. An unreadable legacy file is left where it is and reported on stderr."""
    legacy = chat_sessions_path(storage_dir)
    if not legacy.exists():
        return
    with _migrate_lock:
        if not legacy.exists():
            return
        try:
            sessions = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[chat-sessions] legacy store {legacy} unreadable, not migrated: {e}", file=sys.stderr, flush=True)
            return
        sessions_dir(storage_dir).mkdir(parents=True, exist_ok=True)
        for sid, session in (sessions or {}).items():
            try:
                if not _session_path(storage_dir, sid).exists():
                    _write_session(storage_dir, session)
            except ValueError as e:
                print(f"[chat-sessions] skipped legacy session during migration: {e}", file=sys.stderr, flush=True)
        legacy.rename(legacy.with_name(legacy.name + ".migrated"))


def _migrate_session(session: dict) -> dict:
    """Lossless v1 -> v2 schema upgrade (2026-10-19, multi-chat workspace). A session
    without a schemaVersion (or below SCHEMA_VERSION) gains exactly the four v2 fields
    with their defaults -- nothing else is changed or dropped: the transcript is never
    touched and any unknown extra keys are preserved. Pure: it returns the session and
    does NOT rewrite the file on read (the next normal _write_session persists the new
    shape). Idempotent -- a v2 (or newer) session is returned exactly as-is."""
    if not isinstance(session, dict):
        return session
    if session.get("schemaVersion", 0) >= SCHEMA_VERSION:
        return session
    session.setdefault("schemaVersion", SCHEMA_VERSION)
    # Label of a Claude account in accounts.json; None = the live account at turn time.
    session.setdefault("account", None)
    session.setdefault("swaps", [])
    # Later shape: {'upToIndex': int, 'text': str, 'at': iso}
    session.setdefault("summary", None)
    return session


def _read_session(storage_dir: Path, session_id: str):
    _migrate_legacy_store(storage_dir)
    try:
        p = _session_path(storage_dir, session_id)
    except ValueError:
        return None
    try:
        session = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _migrate_session(session)


def _write_session(storage_dir: Path, session: dict):
    """Writes ONE session atomically: a unique temp file in the same directory, then
    os.replace, so a reader never sees half a file and two writers never share a temp."""
    p = _session_path(storage_dir, session["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{session['id']}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session, f, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_sessions(storage_dir: Path) -> dict:
    _migrate_legacy_store(storage_dir)
    d = sessions_dir(storage_dir)
    if not d.is_dir():
        return {}
    sessions = {}
    for p in d.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(s, dict) and s.get("id"):
            sessions[s["id"]] = s
    return sessions


def _write_sessions(storage_dir: Path, sessions: dict):
    for session in sessions.values():
        _write_session(storage_dir, session)


# Sessions with a turn streaming right now, in THIS process (stream_message adds on entry
# and removes in its finally). Process-local on purpose: a restart ends every stream.
_busy_sessions = set()
_busy_lock = threading.Lock()


def _touch(session: dict):
    session["lastActivityAt"] = _now_iso()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_session(roots: list, instances_dir, provider: str, model: str, effort: str) -> dict:
    session_id = f"chat-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    roots = [r for r in (roots or []) if r] or ["."]
    return {
        "id": session_id,
        "provider": provider,
        "model": model,
        "effort": effort,
        # roots[0] is the primary (agent-manager) repo -- Claude's cwd, and where a
        # relative path resolves for the local provider. roots[1:] are the additional
        # plugin/project repos, all read/write. See app.py:_chat_roots().
        "roots": roots,
        "repoRoot": roots[0],
        "instancesDir": str(instances_dir) if instances_dir else None,
        # Only meaningful for the Claude provider (claude_client.generate()'s own
        # `resume` param) -- the CLI's real session id, captured from the first reply and
        # threaded into every call after. Local provider continuity comes from replaying
        # `transcript` instead (see this module's own header).
        "claudeSessionId": None,
        "transcript": [],
        "status": "active",
        "startedAt": _now_iso(),
        # Last time anything was appended to the transcript -- what "most recently used"
        # means for get_active_session() and list_sessions(). None until the first entry.
        "lastActivityAt": None,
        "endedAt": None,
        # "fully reserving the reasoning model" (Brain Dump #153) -- whether this
        # session currently holds .pipeline-single-flight.lock across turns, not just for
        # the span of one call. The actual held file handle lives in a process-local
        # registry (app.py), NOT here -- an open file object isn't JSON-serializable and
        # wouldn't survive a process restart meaningfully anyway.
        "reserved": False,
        # --- schema v2 fields (see _migrate_session, which adds the same four to v1 files) ---
        "schemaVersion": SCHEMA_VERSION,
        # Label of a Claude account in accounts.json; None = the live account at turn time.
        "account": None,
        "swaps": [],
        # Later shape: {'upToIndex': int, 'text': str, 'at': iso}
        "summary": None,
    }


def _last_used(session: dict) -> str:
    # ISO-8601 UTC strings compare correctly as text.
    return max(session.get("lastActivityAt") or "", session.get("startedAt") or "")


def get_active_session(storage_dir: Path, roots: list, instances_dir=None,
                        provider: str = PROVIDER_LOCAL, model: str = None, effort: str = None) -> dict:
    """The conversation the single-pane sidebar shows (Brain Dump #153: "hideable panel
    on the right side of the app" -- global, not per-subject like Discuss): the most
    recently used active session, so a chat just made with "New" (startedAt = now) wins
    until another one is used. Creates one if none is active. Does NOT re-resolve
    provider/model/effort for an existing active session -- same "fixed for the
    session's lifetime" reasoning discuss_sessions.py already documents (a
    mid-conversation provider switch would hand the new provider/model a transcript or
    --resume history it didn't generate)."""
    active = [s for s in _read_sessions(storage_dir).values() if s.get("status") == "active"]
    if active:
        return max(active, key=_last_used)
    session = _new_session(roots, instances_dir, provider, model, effort)
    _write_session(storage_dir, session)
    return session


def start_new_conversation(storage_dir: Path, roots: list, instances_dir=None,
                            provider: str = PROVIDER_LOCAL, model: str = None, effort: str = None) -> dict:
    """Starts a fresh conversation ALONGSIDE the existing ones (2026-10-18: it used to end
    every active session first, so "New" destroyed the chat you were in). Use
    end_session() to close one."""
    session = _new_session(roots, instances_dir, provider, model, effort)
    _write_session(storage_dir, session)
    return session


def end_session(storage_dir: Path, session_id: str):
    """Closes exactly one active session. Returns it, or None if it is missing or already
    ended."""
    session = _read_session(storage_dir, session_id)
    if not session or session.get("status") != "active":
        return None
    session["status"] = "ended"
    session["endedAt"] = _now_iso()
    _write_session(storage_dir, session)
    return session


def _session_title(session: dict) -> str:
    for turn in session.get("transcript") or []:
        if turn.get("role") == "user" and (turn.get("text") or "").strip():
            text = " ".join(turn["text"].split())
            return text if len(text) <= 60 else text[:59] + "…"
    return "New chat"


def list_sessions(storage_dir: Path) -> list:
    """Summary rows for every active session, most recently used first -- what the
    workspace's session switcher lists. `busy` = a turn is streaming in this process."""
    with _busy_lock:
        busy = set(_busy_sessions)
    rows = [{
        "id": s["id"],
        "title": _session_title(s),
        "provider": s.get("provider"),
        "model": s.get("model"),
        "status": s.get("status"),
        "startedAt": s.get("startedAt"),
        "lastActivityAt": s.get("lastActivityAt"),
        "turnCount": len(s.get("transcript") or []),
        "busy": s["id"] in busy,
        "reserved": bool(s.get("reserved")),
    } for s in _read_sessions(storage_dir).values() if s.get("status") == "active"]
    rows.sort(key=lambda r: max(r["lastActivityAt"] or "", r["startedAt"] or ""), reverse=True)
    return rows


def _roots_blurb(session: dict) -> str:
    roots = session.get("roots") or [session["repoRoot"]]
    lines = [f"- {roots[0]}  (agent-manager -- primary; relative paths resolve here)"]
    for r in roots[1:]:
        lines.append(f"- {r}")
    return "\n".join(lines)


# 2026-09-05, Grimmethy: "Have it read agents.md for sure. I'd like to set that up as the
# de facto starting point" -- confirmed live that neither provider ever read it before:
# the local system prompt only announced accessible repos, and the Claude provider's CLI
# auto-reads CLAUDE.md from cwd (a different file, a different convention) but never
# AGENTS.md. A generous cap, not a tight one -- the real file here is a few KB; this only
# guards against a pathological one blowing the prompt.
AGENTS_MD_MAX_CHARS = 20000


def _read_agents_md(session: dict) -> str:
    """The primary repo's (roots[0]) own AGENTS.md, best-effort -- a project without one
    just gets no section, never an error that would block a chat turn."""
    roots = session.get("roots") or [session["repoRoot"]]
    try:
        content = (Path(roots[0]) / "AGENTS.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content:
        return ""
    if len(content) > AGENTS_MD_MAX_CHARS:
        content = content[:AGENTS_MD_MAX_CHARS] + "\n...[truncated]"
    return content


def _agents_md_blurb(agents_md: str) -> str:
    return (
        "The primary repo's own AGENTS.md -- its de facto starting point. Read this "
        "before anything else: it documents repo-specific hazards and conventions "
        "(e.g. what's safe to hand-edit vs. what needs a real mechanism) that a generic "
        "coding assistant would not otherwise know.\n\n" + agents_md
    )


def _send_claude(session: dict, message: str) -> str:
    started = time.time()
    roots = session.get("roots") or [session["repoRoot"]]
    # First turn only: the CLI's --add-dir makes the extra repos reachable but doesn't
    # announce them, and nothing else primes Claude with this repo's own AGENTS.md (the
    # CLI's own auto-read only covers CLAUDE.md). Tell it both on turn one; --resume
    # carries all of this forward on every later turn, same as before.
    if not session.get("claudeSessionId"):
        preamble_parts = []
        if len(roots) > 1:
            preamble_parts.append(
                "You are rooted at the agent-manager repo and also have full read/write "
                "access to these additional repos:\n" + _roots_blurb(session)
            )
        agents_md = _read_agents_md(session)
        if agents_md:
            preamble_parts.append(_agents_md_blurb(agents_md))
        if preamble_parts:
            message = "\n\n---\n\n".join(preamble_parts) + "\n\n---\n\n" + message
    result = claude_client.generate(
        message, model=session.get("model"), effort=session.get("effort"),
        cwd=session["repoRoot"], allowed_tools=CHAT_CLAUDE_ALLOWED_TOOLS,
        max_turns=CHAT_CLAUDE_MAX_TURNS, resume=session.get("claudeSessionId"),
        add_dirs=roots[1:], allow_amplification=True,
    )
    if result.get("sessionId"):
        session["claudeSessionId"] = result["sessionId"]
    model_stats_client.record_call("chat-session", result["model"], int((time.time() - started) * 1000),
                                    stage="chat", result=result, source="chat")
    return result["response"].strip()


_LOCAL_SYSTEM_PROMPT = (
    "You are a coding assistant embedded in the agent-manager system's own dashboard, "
    "with real Read/Grep/Glob access and READ-ONLY Bash access to live repositories via "
    "your tools -- investigate and diagnose freely. You do NOT have write_file/edit_file, "
    "and run_bash cannot write anything (every repo is mounted read-only for you "
    "specifically -- a real filesystem-level restriction, not a request you're expected "
    "to self-police).\n\n"
    "2026-09-15 (Grimmethy: \"I'd like to see the local chat stick to the task system "
    "when making these fixes... build it in such a way that it honors the task log "
    "system and pushes the fix to unmerged branches for review\"): earlier, Chat had "
    "direct write access and was asked, unsupervised, to build and wire up a whole new "
    "pipeline mechanism live -- the auto-mode classifier itself correctly flagged that as "
    "creating an unsafe unsupervised agent, and the write tools were removed entirely as "
    "a result. For ANY real change now -- from a one-line fix to a whole new mechanism -- "
    "investigate and design it as normal, then YOU MUST ACTUALLY CALL THE "
    "queue_reviewed_task TOOL yourself, in this same turn, with a title and a description "
    "detailed enough (files, the specific behavior, acceptance criteria, any known risks) "
    "for the pipeline to implement it without further back-and-forth. Do not just "
    "describe, promise, or narrate the change in your text response and stop there -- a "
    "text description of the fix is not the hand-off; only a real queue_reviewed_task "
    "tool call is. Only after that call succeeds (you'll get a real queuedTaskId back) "
    "should you tell the user it's been queued. This lands EVERY real change on the same "
    "reviewed pipeline (implement/critique/review/majority-vote) every other pipeline "
    "change goes through, landing as a real unmerged branch for a human to review -- "
    "never applied directly by you.\n\n"
    "For a task that's already stuck -- sitting in Needs Clarification or Blocked in Job "
    "Status -- you have four more tools to actually unstick it, reusing the exact same "
    "pipeline requeue mechanisms a human uses from the dashboard (never a new write "
    "path): answer_task_clarification (the task asked an open design question -- use "
    "once the human has actually told you their decision in this conversation, never "
    "invent one yourself), resolve_task_clarification (the task needs a file/path "
    "picked, not a design decision), mark_task_clarification_done (the human says the "
    "work is already done -- this does NOT requeue, it's terminal), and "
    "requeue_blocked_task (send a blocked/done/archived task back to pending/ for a "
    "fresh attempt). Always read the task first (read_task) and discuss the actual "
    "blocker/question with the human before calling any of these -- they mutate real "
    "task state, not source code, but they are NOT reversible from Chat once called (the "
    "underlying task file moves). Summarize what you're about to do and why in your "
    "reply before or immediately after calling one of these, the same way you already "
    "confirm a queue_reviewed_task hand-off, so the human always knows a real state "
    "change happened, not just a description of one. If requeue_blocked_task reports a "
    "repeatedBlocker match (this rejection looks like the same underlying problem "
    "recurring), tell the human plainly and ask before retrying with force:true -- do "
    "not silently force it.\n\n"
    "You are ROOTED at the agent-manager repo (relative paths resolve there) and also have "
    "read access to every registered plugin and project repo. Call list_roots to see "
    "them; use an absolute path (or grep_codebase's `root` argument) to reach a "
    "non-primary repo.\n\n"
    "IMPORTANT -- run_bash cannot see real host processes: your run_bash tool executes "
    "inside a bwrap sandbox with its own isolated PID namespace (--unshare-pid, its own "
    "/proc), by design, for containment -- NOT the real host. `ps aux` (or any process "
    "listing) run there will show only the sandbox's own tiny process tree and can NEVER "
    "see the worker lanes (one per GPU, e.g. worker-3090/worker-p40)/reviewer/queue-watchdog even when every one "
    "of them is genuinely alive on the host. Confirmed live 2026-09-06: this produced a "
    "confident, completely false \"the pipeline is down, no live processes\" conclusion "
    "while 5 real worker/reviewer processes were actively running. NEVER use run_bash's "
    "`ps`/`pgrep`/`top` output as evidence the pipeline is up or down. The real, correct "
    "liveness signal is the heartbeat file each instance writes on every status change -- "
    "read `instances/<instanceId>.json` (one worker lane per GPU named for it, e.g. "
    "worker-3090 and worker-p40, plus reviewer and watchdog) and check `lastHeartbeat` is recent (a live "
    "instance heartbeats roughly every tick, well under a minute stale) -- the exact same "
    "signal this codebase's own Test-InstanceLiveness/check_instance_liveness and "
    "dead-process-check.js already use as their sole source of truth.\n\n"
    "Also: watchdog's own sweeps (coordinator-sweep.js, decompose-loop-autoroute.js, "
    "blocked-drain.js, staleness/context-trim sweeps, ...) are NOT separate daemons with "
    "their own instanceId -- watchdog invokes each as a plain one-shot `node <script>.js` "
    "subprocess once per tick, then it exits. `ps aux | grep decompose` (or any sweep name) "
    "will ALWAYS show nothing between ticks even when the sweep is working perfectly -- see "
    "CONTEXT.md's own \"Watchdog sweep\" glossary entry. If watchdog's heartbeat is fresh, "
    "every sweep it owns is exactly that alive; to confirm one actually ran recently and "
    "what it did, read its own log under ~/.local/state/agent-manager/logs/ (e.g. "
    "coordinator-sweep.log) or the task-level evidence it leaves (a stalenessFlag, an "
    "advisory history entry, a moved queue file) -- never a process-name search.\n\n"
    "Also: run_bash additionally refuses any `git merge` or `git push` command by name, "
    "even disguised inside a compound `&&` chain -- on top of the blanket read-only mount "
    "above (belt and braces: a clear, specific error beats a generic permission-denied "
    "one). The pipeline does not perform git operations either, so there is NO hand-off "
    "for a merge, push or commit: investigate and recommend, then tell the user to merge "
    "the branch themselves from the dashboard's Unmerged Branches tab, which checks for "
    "conflicts first. NEVER queue a task whose deliverable is a git operation (\"commit X\", "
    "\"merge Y to master\", \"push Z\") -- queue_reviewed_task refuses them, and a drafting "
    "sandbox cannot write git history anyway (confirmed live 2026-09-19: a premium-priority "
    "\"commit the fix and merge to master\" task queued from Chat could never be performed "
    "and held a GPU lane for an hour). queue_reviewed_task is only for describing a code "
    "CHANGE; the pipeline commits by itself and lands an unmerged branch. This rule dates to "
    "a real incident: asked to review a batch of unmerged branches, this same assistant "
    "correctly diagnosed which ones had real work, but recommended merging one without ever "
    "checking the dashboard's own /api/git/unmerged-branches API, which already knew that "
    "branch would conflict. Read-only git commands (log/diff/show/status/branch/fetch) are "
    "completely unaffected -- keep using them to investigate.\n\n"
    "Also: end EVERY response with exactly one block -- CONTEXT-LOG: <one tight "
    "paragraph distilling what this exchange covered or decided>, then optionally "
    "TASK-REF: <task id> on its own line ONLY when this exchange concerned a specific "
    "task (omit that line otherwise). This is mandatory, not optional, but keep it "
    "tight -- never pad it. It becomes a durable, cross-referenced record of this "
    "conversation that survives even after older turns are trimmed from what you can "
    "see, so a wrong or invented TASK-REF actively misleads a later reader -- only name "
    "a task you actually discussed.\n\n"
    "Also: you can PROPOSE a reusable template for the user to save with one click. Only "
    "when the user asks you to create or save a template/scene, end the reply (before the "
    "CONTEXT-LOG block) with exactly:\n"
    "SAVE-TEMPLATE: scene | <short lowercase name>\n"
    "<one paragraph of comma-separated image-generation tags: setting first, then props, "
    "then lighting/mood>\n"
    "END-TEMPLATE\n"
    "The chat shows it as a card with a Save button -- you cannot and must not write the "
    "file yourself. Never claim a template is saved; the user saves it. Do not wrap the "
    "block in a code fence."
)


def _local_system_prompt(session: dict) -> str:
    prompt = _LOCAL_SYSTEM_PROMPT + "\n\nAccessible repos:\n" + _roots_blurb(session)
    agents_md = _read_agents_md(session)
    if agents_md:
        prompt += "\n\n---\n\n" + _agents_md_blurb(agents_md)
    return prompt


# Bounded message-window trimming (2026-09-15, see context-log-marker.js/
# context-log-sweep.js in the core repo, and the plan's Phase 4): only kicks in once a
# session's transcript exceeds this many entries (one entry per user OR assistant turn,
# so this is ~10 exchanges) -- a short conversation is completely unaffected, matching
# the existing reactive context-budget check's own "don't change behavior until actually
# needed" spirit. Older entries are replaced by one capped block pulled from this
# session's own Second Brain context-log note (the durable backstop
# context-log-sweep.js has already been building turn by turn) rather than silently
# dropped -- this is the piece that makes trimming safe: what the live window loses is
# never truly gone, just no longer replayed verbatim.
CHAT_RECENT_TURNS_KEPT = 20

# Same bounded-tail-keep shape as `clu`'s own compact_old_logs() (see the plan's Context
# section) -- keeps the MOST RECENT portion of the note, not the oldest, since that's
# what's most relevant to a conversation resuming right now.
CONTEXT_LOG_NOTE_CHAR_CAP = 3000


def _context_log_note_path(session: dict):
    from app import get_second_brain_dir
    second_brain_dir = get_second_brain_dir()
    if not second_brain_dir:
        return None
    safe_id = "".join(c for c in session["id"] if c.isalnum() or c == "-")
    return second_brain_dir / "Agent Manager Reports" / "Chat Context Logs" / f"{safe_id}.md"


def _read_capped_context_log(session: dict) -> str:
    """Best-effort: a missing note (nothing swept yet, or SECOND_BRAIN_DIR unconfigured)
    is a normal, expected state, not an error -- returns "" so the caller can skip
    injecting anything rather than fail the turn over an optional backstop."""
    path = _context_log_note_path(session)
    if not path or not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(content) > CONTEXT_LOG_NOTE_CHAR_CAP:
        content = "...(truncated)...\n" + content[-CONTEXT_LOG_NOTE_CHAR_CAP:]
    return content


def _build_local_messages(session: dict, message: str) -> list:
    """2026-08-26 (Open WebUI investigation, Grimmethy): replaces the old approach of
    flattening the whole conversation into one giant string inside a single
    {role:'user'} message -- Ollama's /api/chat (like every real chat frontend, Open
    WebUI included) expects the actual conversation as a per-turn array of
    {role, content} objects, not narrated "You:"/"User:" text glued together. This also
    incidentally means only a session's very first-ever message is ever a length-1
    request now -- see local-tool-client.js's own comment on why that shape is the one
    that can hit TokenFold's flaky single-message cache path.

    2026-09-15: transcript replay is now bounded (see CHAT_RECENT_TURNS_KEPT above) --
    session["transcript"] itself is NEVER trimmed (still the full, permanent record this
    module has always kept), only what gets REPLAYED to the model each turn is capped.
    When older entries exist beyond the bound, one extra message stands in for them,
    built from this session's own context-log note (context-log-sweep.js) rather than
    just dropping that history outright."""
    messages = [{"role": "system", "content": _local_system_prompt(session)}]
    transcript = session["transcript"]
    trimmed_count = max(0, len(transcript) - CHAT_RECENT_TURNS_KEPT)
    if trimmed_count:
        capped_note = _read_capped_context_log(session)
        if capped_note:
            messages.append({
                "role": "user",
                "content": (
                    f"[{trimmed_count} earlier turn(s) of this conversation were trimmed "
                    "from what you can see directly -- here is a compacted record of "
                    f"what they covered, from this session's own context log:]\n\n{capped_note}"
                ),
            })
    recent = transcript[len(transcript) - CHAT_RECENT_TURNS_KEPT:] if trimmed_count else transcript
    for turn in recent:
        if turn.get("error"):
            continue  # recorded failure notices are for the human, never replayed to the model
        role = "assistant" if turn["role"] == "assistant" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": message})
    return messages


def _stream_local(session: dict, message: str, result_box: dict = None):
    """Yields plain text chunks as the local model generates them (Open WebUI-style live
    streaming, see this module's own recent header note) -- the caller accumulates them
    into the final transcript entry for real-time display.

    2026-09-15 (real bug, caught live testing the context-log feature): the caller used
    to treat that raw streamed concatenation as the FINAL stored transcript text too --
    but those chunks are the model's UNCLEANED output, streamed token-by-token, before
    local-tool-client.js's own SIDE-FINDING:/AMPLIFY:/CONCEPT-BUILD:/CONTEXT-LOG: marker
    stripping ever runs (that only happens on the fully-accumulated response, inside
    local-tool-client.js's withUsage()). So every marker block has been silently leaking
    into the visible, PERSISTED chat transcript this whole time streaming has existed
    here -- unnoticed for SIDE-FINDING/AMPLIFY because they're rare opt-in flags, made
    obvious immediately once CONTEXT-LOG made a marker mandatory on every single turn.

    `result_box`, when given, is populated with `{"final_text": ...}` -- the real,
    authoritative text (cleaned model response + any footer this function itself adds
    below) -- once the turn is fully done, so the caller can OVERWRITE what it stored
    with the correct value instead of trusting the raw stream. Real-time DISPLAY during
    the turn is intentionally unaffected (still raw, unavoidably, for genuine
    token-by-token streaming) -- this only fixes what ends up permanently stored."""
    started = time.time()
    messages = _build_local_messages(session, message)
    roots = session.get("roots") or [session["repoRoot"]]
    result = None
    raw_chunks = []
    for event in local_tool_client.stream_plan_with_tools(
        messages=messages, max_turns=CHAT_LOCAL_MAX_TURNS, source="chat", allow_write=True,
        primary_root=roots[0], extra_roots=roots[1:], force_summary_on_cap=True,
        allow_amplification=True, context_log_session_id=session["id"],
    ):
        if event.get("type") == "chunk":
            raw_chunks.append(event["text"])
            yield event["text"]
        elif event.get("type") == "final":
            result = event
    model_stats_client.record_call("chat-session", ollama_client.MODEL, int((time.time() - started) * 1000),
                                    stage="chat", result=result or {}, source="chat")
    final_text = (result or {}).get("response")
    if final_text is None:
        final_text = "".join(raw_chunks)
    # 2026-09-05, Grimmethy: force_summary_on_cap=True is now passed above, so a
    # turn-cap hit, a context-budget hit, AND a voluntary early stop with no RESOLUTION:
    # line (see local-tool-client.js's own RESERVED_RESPONSE_TOKENS/
    # estimateMessagesTokens check and its runForcedSummaryTurn call sites) all force one
    # extra no-tools turn that produces a real considered answer with a RESOLUTION: line,
    # rather than returning whatever rode along with the run's last tool call.
    # `forcedSummary`/`forcedSummaryReason` are the real signal for that now -- prefer
    # them over the old turnsUsed >= max_turns heuristic below (kept only as a fallback
    # for the exceedingly rare case where forcedSummary itself never got set, e.g. the
    # tools-disabled kill-switch path).
    #
    # 2026-09-15 (Grimmethy: "Different stop reasons should absolutely be
    # distinguishable. It keeps the system understandable."): these three reasons used to
    # collapse into one of two footers, and 'voluntary-stop' -- by far the MOST common
    # one, since Chat's own system prompt never asks for a RESOLUTION: line on an
    # ordinary reply -- shared 'turns'' "ran out of its 100-turn tool budget" text even
    # when the real turnsUsed was 2. Confirmed live via model-stats.db: two of three real
    # Chat calls in one session showed that exact false message. 'voluntary-stop' gets NO
    # footer at all: nothing was actually exhausted, the model gave a complete answer and
    # was only asked to append the sentinel line the pipeline's resolver parses.
    footer = None
    if result and result.get("forcedSummary") and result.get("forcedSummaryReason") == "context":
        footer = "\n\n*(Ran out of context room mid-investigation, so it gave its best final answer using what it had already learned.)*"
    elif result and result.get("forcedSummary") and result.get("forcedSummaryReason") == "turns":
        footer = (
            f"\n\n*(Ran out of its {CHAT_LOCAL_MAX_TURNS}-turn tool budget mid-investigation, "
            "so it gave its best final answer using what it had already learned.)*"
        )
    elif result and result.get("turnsUsed") is not None and result["turnsUsed"] >= CHAT_LOCAL_MAX_TURNS:
        footer = (
            f"\n\n*(Ran out of its {CHAT_LOCAL_MAX_TURNS}-turn tool budget mid-investigation -- "
            "the above may be a status update rather than a finished answer. Ask again, or "
            "narrow the question, to let it continue.)*"
        )
    if footer:
        final_text += footer
        yield footer
    if result_box is not None:
        result_box["final_text"] = final_text


def stream_message(storage_dir: Path, session_id: str, message: str):
    """Generator sibling of the old send_message -- yields {"type": "chunk", "text"} as
    the reply generates, then exactly one {"type": "final", "session": {...}} carrying
    the same updated-session shape send_message() used to return in one shot, once the
    transcript is persisted. Claude sessions still resolve in a single blocking
    _send_claude() call (Claude-side token streaming isn't wired up here) but go through
    this same generator shape so app.py's SSE route doesn't need to special-case either
    provider.

    Persists only THIS session's file (2026-10-18), and marks the session busy in
    list_sessions() for exactly the life of the generator."""
    session = _read_session(storage_dir, session_id)
    if not session or session["status"] != "active":
        yield {"type": "final", "session": session}
        return
    with _busy_lock:
        _busy_sessions.add(session_id)
    try:
        yield from _stream_message_turn(storage_dir, session, message)
    finally:
        with _busy_lock:
            _busy_sessions.discard(session_id)


def _stream_message_turn(storage_dir: Path, session: dict, message: str):
    # 2026-09-01 (Grimmethy: "time stamps to the second programmed into each
    # thought segment"): capture the turn's wall-clock time so the transcript
    # render (grillRenderTranscript in index.html) can stamp each segment. Old
    # persisted entries simply lack the key -- the renderer falls back to no
    # stamp, so this is safe against existing sessions.
    session["transcript"].append({"role": "user", "text": message, "timestamp": _now_iso()})
    # In-progress placeholder, rewritten and PERSISTED after every chunk (not just once
    # at the end) -- 2026-08-26, Grimmethy: "It thought a lot and then the chat record
    # just disappeared." Root-caused live: a 30+-minute real investigation was killed
    # outright when the dashboard's own file-watcher reloader restarted this process
    # mid-stream (chat_sessions.py itself got edited while that turn was still running)
    # -- the old persist-once-at-the-end design meant the ENTIRE turn vanished, not just
    # whatever hadn't streamed yet. Any interruption now loses at most the last unwritten
    # chunk, regardless of cause (a reloader restart, a crash, a dropped connection) --
    # the whole point is not depending on reaching a specific line of code at the end.
    session["transcript"].append({"role": "assistant", "text": "", "timestamp": _now_iso()})
    _touch(session)
    _write_session(storage_dir, session)

    reply_parts = []
    result_box = {}
    if session["provider"] == PROVIDER_CLAUDE:
        reply = _send_claude(session, message)
        reply_parts.append(reply)
        session["transcript"][-1]["text"] = reply
        _write_session(storage_dir, session)
        yield {"type": "chunk", "text": reply}
    else:
        for chunk in _stream_local(session, message, result_box):
            reply_parts.append(chunk)
            session["transcript"][-1]["text"] = "".join(reply_parts)
            _write_session(storage_dir, session)
            yield {"type": "chunk", "text": chunk}

    # 2026-09-15: prefer _stream_local's own authoritative final_text (the model's
    # response with every SIDE-FINDING:/AMPLIFY:/CONCEPT-BUILD:/CONTEXT-LOG: marker
    # actually stripped) over the raw streamed concatenation -- see _stream_local's own
    # header for the real bug this closes. Falls back to the raw join for the Claude
    # path (no result_box there) and for the rare case _stream_local ran into an error
    # before ever setting it.
    final_text = result_box.get("final_text")
    reply = (final_text if final_text is not None else "".join(reply_parts)).strip()
    # SAVE-TEMPLATE blocks become Save-button proposals (template_store.py); never auto-saved.
    reply, proposals = template_store.extract_proposals(reply)
    session["transcript"][-1]["text"] = reply.strip()
    if proposals:
        session["transcript"][-1]["templates"] = proposals
    _write_session(storage_dir, session)
    yield {"type": "final", "session": session}


def get_session(storage_dir: Path, session_id: str):
    return _read_session(storage_dir, session_id)


def inject_user_message(storage_dir: Path, session_id: str, text: str):
    """Appends `text` as a user turn to an existing active session and persists it --
    NO model call, NO assistant placeholder. Same {role, text} turn shape stream_message
    uses when it records the user side of a turn, so the session's transcript is
    indistinguishable from a normal chat that hasn't received its reply yet. Returns the
    updated session dict, or None if the session doesn't exist / isn't active. This is
    what app.py's POST /api/chat/inject endpoint calls so dashboard "Send to chat"
    buttons (task detail, Brain Dump) can push a task log / brain-dump dump into the
    conversation for follow-up without triggering a model turn."""
    session = _read_session(storage_dir, session_id)
    if not session or session.get("status") != "active":
        return None
    session["transcript"].append({"role": "user", "text": text, "timestamp": _now_iso()})
    _touch(session)
    _write_session(storage_dir, session)
    return session


def record_turn_error(storage_dir: Path, session_id: str, error_text: str):
    """Persists a failed turn's error INTO the transcript so it survives the toast.

    stream_message() writes an empty assistant placeholder before the model runs; when
    the turn then raises, that placeholder used to stay empty and the only trace of the
    failure was a 3-second toast. This fills the trailing assistant entry (or appends a
    new one) with the error and flags it `error: True` so the UI can style it and the
    local-model replay can skip it. Returns the updated session or None."""
    session = _read_session(storage_dir, session_id)
    if not session or session.get("status") != "active":
        return None
    text = f"Error: {error_text}"
    transcript = session["transcript"]
    last = transcript[-1] if transcript else None
    if last and last["role"] == "assistant" and not last.get("error"):
        partial = (last.get("text") or "").strip()
        last["text"] = f"{partial}\n\n{text}" if partial else text
        last["error"] = True
    else:
        transcript.append({"role": "assistant", "text": text, "timestamp": _now_iso(), "error": True})
    _touch(session)
    _write_session(storage_dir, session)
    return session


def set_reserved(storage_dir: Path, session_id: str, reserved: bool):
    """Just the persisted flag -- app.py's reservation registry owns actually
    acquiring/releasing the lock and the idle-timeout watchdog; this only keeps the
    dashboard's own display of "is this session reserved" in sync with that."""
    session = _read_session(storage_dir, session_id)
    if not session:
        return None
    session["reserved"] = reserved
    _write_session(storage_dir, session)
    return session
