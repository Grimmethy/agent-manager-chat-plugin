from flask import Blueprint, Response, abort, jsonify, request, session, stream_with_context

import json
import sys

import requests
import logging

logger = logging.getLogger(__name__)

import agent_client

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
    """Local Claude Code terminal sessions, as reported by the optional session agent
    (agent_client.py). Never an error: 'enabled' says whether the feature is configured,
    'reachable' whether the agent answered. Read-only."""
    if not agent_client.enabled():
        return jsonify({"enabled": False, "reachable": False, "sessions": []})
    try:
        return jsonify({"enabled": True, "reachable": True, "sessions": agent_client.list_sessions()})
    except agent_client.AgentError:
        return jsonify({"enabled": True, "reachable": False, "sessions": []})


@chat_bp.route("/api/chat/terminal-sessions/events", methods=["GET"])
def api_terminal_sessions_events():
    """SSE passthrough of the session agent's /events (the agent token stays server-side).
    The stream just ends if the agent goes away; the browser's EventSource reconnects."""
    if not agent_client.enabled():
        abort(404, description="session agent is not configured")
    try:
        upstream = agent_client.open_events()
    except agent_client.AgentError:
        abort(502, description="session agent is unreachable")

    def relay():
        try:
            yield from agent_client.filter_events(agent_client.iter_chunks(upstream))
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
        import ollama_client
        try:
            internal_reserve(session_id, ollama_client.MODEL, False)
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
            import ollama_client
            internal_reserve(session_id, ollama_client.MODEL, True)
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
    import ollama_client

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
        internal_reserve(session_id, ollama_client.MODEL, want_on)
    except InternalApiError as e:
        abort(502, description=f"reservation call to the core dashboard failed: {e}")

    session = set_reserved(CHAT_STORAGE_DIR, session_id, want_on)
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
