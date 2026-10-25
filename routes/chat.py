from flask import Blueprint, Response, abort, jsonify, request, session, stream_with_context

import json
import os
import sys
from pathlib import Path

import requests
import logging

logger = logging.getLogger(__name__)

import agent_client
import agent_hosts

# The app.py helpers these views call (CHAT_STORAGE_DIR, _call_chat, _chat_roots,
# _discuss_provider_args) are imported lazily inside each view: app.py imports THIS
# module to register the blueprint, so a top-level `from app import ...` is a circular
# import that only fails when app.py is the entrypoint (how the dashboard runs). By the
# time a view runs, app.py is fully initialised and the import is just a dict lookup.
#
# 2026-09-15 (Phase 3 of the Chat-plugin extraction -- see
# /home/wok/.claude/plans/immutable-noodling-axolotl.md): the local-provider
# preempt/priority-marker/reservation logic this file used to call directly (chat_
# preempt.py, single_flight_lock.py) now goes through internal_api_client.py instead --
# see api_chat_message and api_chat_reserve below. This repo no longer touches
# instances//queue/ or holds any GPU lock of its own.

chat_bp = Blueprint("chat-bp", __name__)


def _project_options():
    """2026-10-25 (walkthrough): the project list for the chat panel's "Acting on:"
    selector -- the SAME registry the core dashboard's list_projects tool reads
    (CORE_REPO_ROOT/projects.json: one {label, root} entry per project). The active
    one is the registry's own `active` field, falling back to the AGENT_MANAGER_REPO_ROOT
    setting (same precedence as app.py's _active_project_setting). Fail-open: if the
    registry cannot be read at all (core checkout moved, malformed file), fall back to
    the basename of each chat root so the selector always has real options and a
    Chat turn is never blocked on a cosmetic setting. Never raises.
    Returns [{'label': str, 'root': str|None, 'active': bool}]."""
    from app import CORE_REPO_ROOT, _active_project_setting, _chat_roots
    active_root = None
    try:
        ar = _active_project_setting("AGENT_MANAGER_REPO_ROOT")
        if ar:
            active_root = os.path.realpath(str(ar))
    except Exception:
        active_root = None

    registry = None
    try:
        registry = json.loads((Path(CORE_REPO_ROOT) / "projects.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        registry = None

    entries, active_label = None, None
    if isinstance(registry, list):
        entries = registry
    elif isinstance(registry, dict):
        entries = registry.get("projects") if isinstance(registry.get("projects"), list) else None
        active_label = registry.get("active") or registry.get("activeProject")

    options = []
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            root = e.get("root") or e.get("repo") or e.get("path")
            label = e.get("label") or (str(root).rstrip("/").rsplit("/", 1)[-1] if root else None)
            if not label:
                continue
            active = label == active_label or (
                active_root is not None and root is not None
                and os.path.realpath(str(root)) == active_root)
            options.append({"label": label, "root": root, "active": bool(active)})
    if options:
        return options
    # Fail-open fallback: one option per chat root (basename as its label).
    try:
        roots = _chat_roots() or []
    except Exception:
        roots = []
    for r in roots:
        options.append({
            "label": str(r).rstrip("/").rsplit("/", 1)[-1] or str(r),
            "root": r,
            "active": active_root is not None and os.path.realpath(str(r)) == active_root,
        })
    return options or [{"label": "agent-manager", "root": None, "active": True}]


@chat_bp.route("/api/chat/projects", methods=["GET"])
def api_chat_projects():
    """The "Acting on:" selector's options (2026-10-25 walkthrough): the same
    project registry the core dashboard's list_projects tool reads, tagged with
    which one the dashboard currently has active. See _project_options()."""
    options = _project_options()
    active = next((o["label"] for o in options if o["active"]), None)
    return jsonify({"projects": options, "active": active})


@chat_bp.route("/api/chat/<session_id>/project", methods=["PATCH"])
def api_chat_set_project(session_id):
    """2026-10-25 (walkthrough): change which project this chat acts on BEFORE the
    first message (or between turns). Body: {"project": "<label>"}. The label must be
    one of _project_options() (same registry list_projects reads). Persists
    chosen_project on the session, appends a one-line system note to the transcript,
    and returns the updated session. 400 on a missing/unknown label, 404 on an
    unknown or ended session. The chosen project becomes the default `project` for
    the task tools on every subsequent turn (chat_sessions._local_system_prompt);
    an explicit `project` tool argument still wins."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import set_chosen_project
    data = request.get_json(silent=True) or {}
    label = data.get("project")
    if not label or not isinstance(label, str):
        return jsonify({"ok": False, "error": "Missing or invalid 'project' (expected a non-empty string label)"}), 400
    valid = [o["label"] for o in _project_options()]
    if label not in valid:
        return jsonify({"ok": False, "error": f"Unknown project '{label}'. Valid: {valid}"}), 400
    updated = set_chosen_project(CHAT_STORAGE_DIR, session_id, label)
    if updated is None:
        return jsonify({"ok": False, "error": "Session not found or no longer active"}), 404
    return jsonify({"ok": True, "chosen_project": label, "session": updated})

@chat_bp.route("/api/chat/active", methods=["GET"])
def api_chat_active():
    """Loads (or creates, if none exists yet) the most recently used conversation --
    called on dashboard load so the panel shows where you left off."""
    from app import CHAT_STORAGE_DIR, _call_chat, _chat_roots, instances_dir
    from chat_sessions import get_active_session, PROVIDER_LOCAL
    session = _call_chat(get_active_session, CHAT_STORAGE_DIR, _chat_roots(),
                           instances_dir(), provider=PROVIDER_LOCAL)
    return jsonify(session)


@chat_bp.route("/api/chat/new", methods=["POST"])
def api_chat_new():
    """Starts a fresh conversation alongside the existing ones (it no longer ends the
    active one -- POST /api/chat/<id>/close does that). Body: {provider?, model?,
    effort?} -- same _discuss_provider_args fallback (local by default) every other
    Discuss-family start route already uses."""
    from app import CHAT_STORAGE_DIR, _call_chat, _chat_roots, _discuss_provider_args, instances_dir
    from chat_sessions import start_new_conversation
    provider, model, effort = _discuss_provider_args()
    session = _call_chat(start_new_conversation, CHAT_STORAGE_DIR, _chat_roots(),
                           instances_dir(), provider=provider, model=model, effort=effort)
    return jsonify(session)


@chat_bp.route("/api/chat/sessions", methods=["GET"])
def api_chat_sessions():
    """Every active conversation as a summary row, most recently used first -- the
    workspace's session switcher (multi-chat design doc, Phase 1)."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import list_sessions
    return jsonify(list_sessions(CHAT_STORAGE_DIR))


@chat_bp.route("/api/chat/terminal-sessions", methods=["GET"])
def api_terminal_sessions():
    """Local Claude Code terminal sessions from every configured session agent
    (agent_client.py). Never an error: 'enabled' says whether the feature is configured,
    'reachable' whether at least one host answered, 'sessions' carries one tagged row per
    live host ('host': <name>), 'hosts' the configured host names in config order, and
    'skipped' one {'host','reason'} per failed host. Read-only. No token or token-file
    path ever appears in the body -- each host's own bearer token stays server-side."""
    if not agent_client.enabled():
        return jsonify({"enabled": False, "reachable": False, "sessions": [],
                        "hosts": [], "skipped": []})
    result = agent_client.list_all()
    hosts = [h.name for h in agent_hosts.hosts()]
    skipped = result["skipped"]
    reachable = len(skipped) < len(hosts)
    if not reachable:
        result = {"sessions": [], "skipped": skipped}
    return jsonify({"enabled": True, "reachable": reachable, "sessions": result["sessions"],
                    "hosts": hosts, "skipped": skipped})


@chat_bp.route("/api/chat/terminal-sessions/events", methods=["GET"])
def api_terminal_sessions_events():
    """SSE passthrough of ONE session agent's /events (the agent token stays server-side).
    Query param host=<name> picks the host: optional with a single configured host (that
    one is used and its name tagged onto every event), REQUIRED when several are
    configured (400 when missing, 404 for an unknown name, 502 when that host is
    unreachable). The stream just ends if the agent goes away; the browser's
    EventSource reconnects. No token ever appears in the stream."""
    if not agent_client.enabled():
        abort(404, description="session agent is not configured")
    host = request.args.get("host")
    hosts = agent_hosts.hosts()
    if host is None and len(hosts) != 1:
        abort(400, description=(
            f"{len(hosts)} session agents are configured; pass host=<name> to pick one"))
    if host is None:
        host = hosts[0].name
    elif not any(h.name == host for h in hosts):
        abort(404, description=f"unknown session agent host '{host}'")
    try:
        upstream = agent_client.open_events(host)
    except agent_client.AgentError:
        abort(502, description="session agent is unreachable")

    def relay():
        try:
            yield from agent_client.filter_events(agent_client.iter_chunks(upstream), host=host)
        except (requests.RequestException, OSError) as exc:
            logger.warning("SSE relay stream ended abnormally: %s: %s", type(exc).__name__, exc)
        finally:
            upstream.close()

    return Response(stream_with_context(relay()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_bp.route("/api/chat/<session_id>", methods=["GET"])
def api_chat_get(session_id):
    """The full session JSON for one conversation, so the pane can load a chat that is not
    the 'active' one. 404 if it does not exist or is ended."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import get_session
    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session or session.get("status") != "active":
        abort(404)
    return jsonify(session)


@chat_bp.route("/api/chat/<session_id>/close", methods=["POST"])
def api_chat_close(session_id):
    """Ends one conversation. 404 if it does not exist or is already ended. A reserved
    session's reservation is released first (best-effort) so closing a chat never
    leaves the reasoning model held for a conversation nobody can reach."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import end_session, get_session
    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session or session.get("status") != "active":
        abort(404)
    if session.get("reserved"):
        from internal_api_client import InternalApiError, reserve as internal_reserve
        try:
            internal_reserve(session_id, on=False)
        except InternalApiError as e:  # noqa: BLE001 -- never block closing a chat on this
            print(f"[chat-close] reservation release failed (non-fatal): {e}", file=sys.stderr, flush=True)
    ended = end_session(CHAT_STORAGE_DIR, session_id)
    if not ended:
        abort(404)
    return jsonify(ended)


@chat_bp.route("/api/chat/inject", methods=["POST"])
def api_chat_inject():
    """Appends a chunk of text as a user message into a dashboard chat session for
    follow-up -- the single endpoint the dashboard's "Send to chat" buttons (task
    detail, Brain Dump) POST the task log / brain-dump text to. It only RECORDS the
    user turn (persisted, visible in the panel); it deliberately does NOT call the
    model -- the user reads the injected context and then sends their own follow-up
    question via the normal /api/chat/<session_id>/message route.

    Body: {text: string, sessionId?: string}. Without sessionId: the currently active
    session is used (get_active_session creates one if none exists yet, matching what
    /api/chat/active does on panel load); if that still yields nothing a brand-new
    conversation is started. Either way the resulting session id comes back in the
    response so the button's caller can link back to it."""
    from app import CHAT_STORAGE_DIR, _call_chat, _chat_roots, _discuss_provider_args, instances_dir
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        abort(400, description="text is required")

    from chat_sessions import get_active_session, get_session, inject_user_message
    from chat_sessions import start_new_conversation, PROVIDER_LOCAL

    session_id = (body.get("sessionId") or "").strip()
    if session_id:
        existing = get_session(CHAT_STORAGE_DIR, session_id)
        if not existing or existing.get("status") != "active":
            abort(404, description="no active chat session with that id")
    else:
        session = _call_chat(get_active_session, CHAT_STORAGE_DIR, _chat_roots(),
                             instances_dir(), provider=PROVIDER_LOCAL)
        session_id = (session or {}).get("id") or (session or {}).get("session_id")
        if not session_id:
            provider, model, effort = _discuss_provider_args()
            session = _call_chat(start_new_conversation, CHAT_STORAGE_DIR, _chat_roots(),
                                 instances_dir(), provider=provider, model=model, effort=effort)
            session_id = (session or {}).get("id") or (session or {}).get("session_id")
        if not session_id:
            abort(500, description="could not resolve or start a chat session")

    injected = _call_chat(inject_user_message, CHAT_STORAGE_DIR, session_id, text)
    if not injected:
        abort(404, description="no active chat session with that id")
    return jsonify({"session": injected, "sessionId": injected.get("id") or session_id})


@chat_bp.route("/api/chat/<session_id>/message", methods=["POST"])
def api_chat_message(session_id):
    """The actual chat turn.

    2026-08-24 -- this used to wrap the WHOLE call in the same git-safety mutex the
    merge-branch endpoint uses (_acquire_apply_lock/_release_apply_lock,
    app.py:3979-3999). Caught live within minutes of shipping: a local-provider turn can
    legitimately wait minutes just for the GPU lock (a busy worker lane), and held the
    apply-lock that entire time even though most turns never touch git at all -- a second,
    completely unrelated Chat message (or a real click from Grimmethy, confirmed live)
    got "the pipeline is mid-apply right now" while nothing was actually applying,
    because THIS request was sitting on the mutex for no reason. Exactly the lesson
    single-flight-lock.js's own header already documents from an earlier incident:
    holding a lock across a call's ENTIRE span instead of just the piece that needs it
    turns a narrow, real protection into broad, needless contention.

    Fix: git safety now lives at the point a git-mutating command actually runs, not
    here. local-tool-client.js's runBashTool acquires the SAME apply-task.lock (flock,
    cross-language-compatible, same file apply-task.sh/api_git_merge_branch already use)
    for just the span of each individual command -- see that function's own comment.
    Claude's own Edit/Write/Bash tool calls happen inside the `claude` CLI's own internal
    tool loop, which this codebase has no hook into at the per-call level, so they are
    NOT covered by this -- a known, real, narrower gap (git's own index.lock still turns
    a genuine collision into a clean failure to retry, not silent corruption) rather than
    a solved one; revisit if it causes a real incident."""
    from app import CHAT_STORAGE_DIR
    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        abort(400, description="message is required")

    from chat_sessions import get_session
    existing = get_session(CHAT_STORAGE_DIR, session_id)
    if not existing or existing.get("status") != "active":
        abort(404)

    # 2026-08-26 (Open WebUI investigation, Grimmethy: "vastly improve the chat
    # system... streaming"): this used to be a single blocking send_message() call
    # returning the whole updated session as one JSON body -- the user stared at nothing
    # until the model finished (or the turn budget ran out), which is exactly why the
    # "ran out of its turn budget" explainer had to exist in the first place. Now
    # streamed as SSE: one `data:` frame per {"type":"chunk"} as text arrives, then one
    # {"type":"final","session":{...}} carrying the same shape this route used to return
    # in one shot, so the frontend still ends up with the same authoritative session.
    from chat_sessions import stream_message, PROVIDER_LOCAL, PROVIDER_CLAUDE
    from claude_client import ClaudeClientError
    from local_tool_client import LocalToolClientError
    from internal_api_client import InternalApiError, preempt as internal_preempt

    is_local_turn = existing.get("provider") == PROVIDER_LOCAL

    # Make GPU space for a local-provider turn: the internal API's own /preempt route
    # kills both worker lanes' in-flight draft (chat precludes workers), the reviewer's
    # only if < ~3 min in -- see chat_preempt.py's original header in the core repo for
    # the full rationale, and no-ops server-side if AGENT_MANAGER_CHAT_PREEMPT=false.
    # Synchronous, before the SSE generator runs. Claude-provider turns don't touch the
    # local model, so this is skipped entirely for them. The internal API's own
    # /local-turn route separately holds the priority marker for the WHOLE call (not
    # just this preempt step) -- this repo no longer needs single_flight_lock at all for
    # the local-provider path; local_tool_client.stream_plan_with_tools() below reaches
    # that same route.
    preempted = []
    if is_local_turn:
        try:
            preempted = internal_preempt()
        except InternalApiError as e:  # noqa: BLE001 -- never block a chat turn on this
            print(f"[chat-preempt] failed (non-fatal): {e}", file=sys.stderr, flush=True)

    def generate():
        from chat_sessions import record_turn_error

        def fail(msg):
            # Persist into the transcript first so the error outlives the toast, then
            # tell the client (it re-reads the session and renders the saved entry).
            try:
                record_turn_error(CHAT_STORAGE_DIR, session_id, msg)
            except Exception as werr:  # noqa: BLE001 -- never mask the original error
                print(f"[chat-error-record] failed: {werr}", file=sys.stderr, flush=True)
            event = {'type': 'error', 'error': msg}
            # HUB0008 8/11: a failed CLAUDE-provider turn whose error looks like a
            # limit error (ASSUMED phrases -- see limit_errors.py) gets a swapOffer on
            # the SSE error event and the error text is sampled so the owner can turn
            # real errors into fixtures. Local-provider errors and unclassified Claude
            # errors are untouched. Only account LABELS go into the event -- never a
            # token or a tokenFile (accounts.load entries carry both).
            if existing.get("provider") == PROVIDER_CLAUDE:
                from limit_errors import classify, record_sample
                kind = classify(msg)["kind"]
                if kind is not None:
                    try:
                        record_sample(CHAT_STORAGE_DIR, msg)
                    except Exception as serr:  # noqa: BLE001 -- the offer must not die
                        print(f"[chat-error-sample] failed: {serr}", file=sys.stderr, flush=True)
                    import accounts
                    # The account used for the turn: the chat's pinned account, else
                    # the live one (same resolution as chat_sessions's turn path).
                    used = existing.get("account") or accounts.live_label(CHAT_STORAGE_DIR)
                    options = [{"provider": "claude", "account": a["label"]}
                               for a in accounts.load(CHAT_STORAGE_DIR)
                               if a.get("label") != used]
                    options.append({"provider": "local"})
                    event["swapOffer"] = {"kind": kind, "options": options}
            return f"data: {json.dumps(event)}\n\n"

        if preempted:
            yield f"data: {json.dumps({'type': 'preempt', 'lanes': preempted})}\n\n"
        try:
            for event in stream_message(CHAT_STORAGE_DIR, session_id, message):
                yield f"data: {json.dumps(event)}\n\n"
        except (ClaudeClientError, LocalToolClientError) as e:
            yield fail(str(e))
        except (TimeoutError, ConnectionError, OSError) as e:
            yield fail(f'local model call failed ({e}) -- it may be busy with an active worker-lane task; try again shortly.')
        except Exception as e:  # noqa: BLE001 -- anything else must still land in the history
            yield fail(f'{type(e).__name__}: {e}')

    # Reserved sessions refresh their own idle clock on every real message -- see
    # api_chat_reserve below: calling the internal API's /reserve again with on:true
    # while already reserved is a refresh, not a no-op, on the core-dashboard side.
    if existing.get("reserved"):
        try:
            from internal_api_client import reserve as internal_reserve
            internal_reserve(session_id, on=True)
        except InternalApiError as e:  # noqa: BLE001 -- best-effort refresh, never block the turn
            print(f"[chat-reserve] refresh failed (non-fatal): {e}", file=sys.stderr, flush=True)

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@chat_bp.route("/api/chat/<session_id>/reserve", methods=["POST"])
def api_chat_reserve(session_id):
    """Toggles "fully reserving the reasoning model" (Brain Dump #153) for this session --
    holding the reasoning model's own per-model lock (instances/.pipeline-single-flight.
    <model>.lock -- see single_flight_lock.py's own header for the 2026-08-25 per-model
    keying this relies on) across turns, not just for the span of one call. Since
    2026-08-25 this only idles whatever else is contending for THAT specific model
    (worker-reasoning, local-tool-client.js's arch_discovery/Chat-with-tools calls,
    Discuss's local provider) -- a cheap-model lane like worker-1's brain_dump_sort
    traffic keeps running unaffected, unlike before this locking was split per-model.
    Body: {on: bool}. Only meaningful for the local provider (Claude has no shared-
    resource lock, per the earlier decision not to lock Claude calls against each
    other)."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import get_session, set_reserved, PROVIDER_LOCAL
    from internal_api_client import InternalApiError, reserve as internal_reserve

    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session:
        abort(404)
    if session.get("provider") != PROVIDER_LOCAL:
        abort(400, description="reservation only applies to the local provider")

    body = request.get_json(silent=True) or {}
    want_on = bool(body.get("on"))

    # The real acquire()/release() (and the blocking-for-a-worker-lane's-current-call
    # wait that can entail) now happens entirely on the core dashboard side, in its own
    # _internal_chat_reservations registry -- this repo no longer holds an OS file
    # handle or a lock of its own for this at all, session_id is just an opaque key.
    try:
        internal_reserve(session_id, on=want_on)
    except InternalApiError as e:
        abort(502, description=f"reservation call to the core dashboard failed: {e}")

    # KNOWN LIMITATION (AC-4 divergence): if set_reserved fails after the call above succeeded, the server-side reservation is LIVE while the
    # local session file still has the old flag, and there is no compensating release in this path. The failure is logged with the stable tag
    # AC4_DIVERGENCE so an operator can grep for it and reconcile by hand. Longer-term fix: call internal_reserve(session_id, on=False) from the
    # except block below (as api_chat_close does), or set the local flag first and retry the remote call.
    try:
        session = set_reserved(CHAT_STORAGE_DIR, session_id, want_on)
    except Exception as e:
        import ollama_client
        logger.error("AC4_DIVERGENCE session=%s model=%s want_on=%s error=%s: %s -- Server-side reservation is LIVE but local chat_sessions "
                     "state was NOT updated. Manual reconciliation required.",
                     session_id, ollama_client.MODEL, want_on, type(e).__name__, e)
        raise
    return jsonify(session)


def _public_session(session):
    """The session dict with any token / tokenFile key stripped -- HUB0008 7/11: no
    token or tokenFile ever leaves the server in a swap response. A session should
    only ever hold the account LABEL (accounts.py keeps tokenFile paths server-side),
    but the strip is applied anyway as the belt to that suspenders."""
    if not isinstance(session, dict):
        return {}
    return {k: v for k, v in session.items() if k not in ("token", "tokenFile")}


@chat_bp.route("/api/chat/<session_id>/swap", methods=["POST"])
def api_chat_swap(session_id):
    """HUB0008 7/11 (multi-chat workspace): swap the model behind a live chat,
    mid-conversation.

    Body: {provider: 'local' | 'claude', model?: string, account?: string}.
    For 'claude', `account`, when given, must be the label of an existing account
    (accounts.load against CHAT_STORAGE_DIR); when omitted the session follows the
    live account (account None -- see accounts.live_label). For 'local' the account
    is ignored (local has no account).

    - 404 for an unknown or inactive session; 400 for a bad provider or an unknown
      claude account.
    - If the session currently holds the local reservation and the target is not
      local, the reservation is released FIRST, exactly as api_chat_reserve does
      (internal reserve off, then set_reserved False). A swap TO local NEVER
      acquires a reservation -- the /reserve flow is the only way to get one.
    - When handoff.needs_summary(transcript, target provider) is true, the local
      summarizer builds the older-turns summary (chat_sessions.summarize_older)
      before returning. A summarizer error does NOT block the swap: a warning is
      logged and the turn continues with the verbatim hand-off (piece 6).
    Returns the updated session JSON -- never a token or tokenFile.
    """
    from app import CHAT_STORAGE_DIR
    from chat_sessions import (get_session, swap_session, summarize_older,
                               PROVIDER_LOCAL, PROVIDER_CLAUDE)
    import handoff

    body = request.get_json(silent=True) or {}
    provider = body.get("provider")
    model = body.get("model")
    account = body.get("account")

    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session or session.get("status") != "active":
        abort(404)
    if provider not in (PROVIDER_LOCAL, PROVIDER_CLAUDE):
        abort(400, description="provider must be 'local' or 'claude'")

    if provider == PROVIDER_CLAUDE:
        if account is not None:
            import accounts
            labels = {a.get("label") for a in accounts.load(CHAT_STORAGE_DIR)}
            if account not in labels:
                abort(400, description="unknown account: %s" % account)
        # account None: the session follows the live account.
    else:
        account = None  # local has no account; whatever the body sent is ignored.

    # Leaving local while holding the reservation: release it first, the exact
    # api_chat_reserve release shape (best-effort -- a failed release must not
    # block the swap, mirroring api_chat_close). A swap TO local never acquires.
    if session.get("reserved") and provider != PROVIDER_LOCAL:
        from chat_sessions import set_reserved
        from internal_api_client import InternalApiError, reserve as internal_reserve
        import ollama_client
        try:
            internal_reserve(session_id, ollama_client.MODEL, False)
        except InternalApiError as e:  # noqa: BLE001 -- never block the swap on the release
            logger.warning("[chat-swap] reservation release failed (non-fatal): %s", e)
        set_reserved(CHAT_STORAGE_DIR, session_id, False)

    updated = swap_session(CHAT_STORAGE_DIR, session_id, provider, model, account=account)
    if not updated:
        abort(404)

    # Optional older-turns summary: only when the transcript would overflow the
    # TARGET provider's window (handoff.needs_summary, piece 5). A failure here must
    # not block the swap -- warn and continue with the verbatim hand-off (piece 6).
    try:
        if handoff.needs_summary(updated.get("transcript") or [], provider):
            summarized = summarize_older(CHAT_STORAGE_DIR, session_id)
            if summarized:
                updated = summarized
    except Exception as e:  # noqa: BLE001 -- a summarizer error never fails the swap
        logger.warning("[chat-swap] summarise-older failed (non-fatal, continuing with the "
                       "verbatim hand-off): %s: %s", type(e).__name__, e)
        updated = get_session(CHAT_STORAGE_DIR, session_id) or updated

    return jsonify(_public_session(updated))


@chat_bp.route("/api/chat/<session_id>/command/<proposal_id>/<action>", methods=["POST"])
def api_chat_command_decide(session_id, proposal_id, action):
    """The Confirm / Deny buttons on a proposed command (owner-approved write commands, decisions 2026-10-25). The decision is persisted on the transcript
    entry FIRST; only then, for Confirm, is core's restricted runner called with that decision's timestamp, and its scrubbed result is stored next to the
    proposal. A proposal that is already decided answers 409, so a double click can never run a command twice. Returns {session, proposal}."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import get_session, _write_session
    import command_runs
    import internal_api_client
    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session or session.get("status") != "active":
        abort(404)
    try:
        proposal = command_runs.decide(session, proposal_id, action)
    except LookupError:
        abort(404, description="no such command proposal")
    except ValueError as e:
        abort(409 if "already" in str(e) else 400, description=str(e))
    _write_session(CHAT_STORAGE_DIR, session)  # the decision is on disk before anything runs
    if action == "confirm":
        try:
            reply = internal_api_client.run_command(proposal["argv"], proposal["repo"], proposal_id, proposal["decidedAt"], session_id)
        except internal_api_client.InternalApiError as e:
            reply = {"ok": False, "refused": str(e)}
        command_runs.record_execution(session, proposal_id, reply)
        _write_session(CHAT_STORAGE_DIR, session)
    return jsonify({"session": session, "proposal": proposal})


@chat_bp.route("/api/chat/<session_id>/template/save", methods=["POST"])
def api_chat_template_save(session_id):
    """The Save button on a template proposal card. Body: {entry, index, key?, text?} --
    `entry` is the transcript index of the assistant reply that carries the proposal,
    `index` the proposal within it; key/text are the user's edits from the card. The
    outcome (saved, or why not) is persisted on the proposal so it survives a reload, and
    the updated session comes back so the UI renders it."""
    from app import CHAT_STORAGE_DIR
    from chat_sessions import get_session, _write_session
    import template_store
    body = request.get_json(silent=True) or {}
    try:
        entry, index = int(body["entry"]), int(body["index"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="entry and index are required integers")
    session = get_session(CHAT_STORAGE_DIR, session_id)
    if not session or session.get("status") != "active":
        abort(404)
    try:
        prop = template_store.save_proposal(session, entry, index, body.get("key"), body.get("text"))
    except LookupError:
        abort(404, description="no such template proposal")
    _write_session(CHAT_STORAGE_DIR, session)
    return jsonify({"session": session, "proposal": prop})
