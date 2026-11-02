# Function Length Decomposition Candidates

### AC-1 · Decompose `api_chat_message` into atomic concerns
Strength: Strong
Files: routes/chat.py
Symbol: api_chat_message
Snippet:
```

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
```

Problem:
The function `api_chat_message` orchestrates five distinct concerns — git safety mutex handling, local-provider preempt logic, SSE streaming with error handling, error recording and event formatting, and session reservation refresh — all within a single 127-line block. Each concern has independent logic, side effects, and test requirements, yet they are tightly coupled, forcing readers to mentally track multiple state machines and exception paths. This creates high cognitive load, reduces testability, and invites drift — especially since the git safety lock is now a no-op but still conceptually present, and error handling cascades through multiple layers.

Solution:
Extract five atomic functions: `acquire_git_safety_lock()` (conceptually present, now no-op), `handle_local_preempt()` (conditional on provider), `stream_chat_turn()` (core SSE generator with error handling), `refresh_session_reservation()` (conditionally executed), and `record_error_and_format_event()` (Claude-specific error formatting). Each function should accept only the data it needs, return only its result, and be testable in isolation. The main function becomes a simple orchestrator that delegates to these units.

Benefits:
Decomposition isolates responsibilities, enabling unit tests for each concern without mocking the entire system. It reduces cognitive load for developers reading or modifying the code, and makes future changes (e.g., adding new error types, swapping providers) safer and less invasive. The codebase becomes more maintainable and less prone to drift — especially as the function’s logic evolves or is extended.

VERDICT CONSTRAINT:
Genuine
