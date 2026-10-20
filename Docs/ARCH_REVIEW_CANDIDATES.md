# Architecture Review Candidates

### AC-1 · Header comment overstates Phase-3 scope; `queue_dir`/`instances_dir` usage unverified
Strength: Worth exploring
Files: app.py

Problem:
The module header in `app.py` states that Phase 3 (2026-09-15) "migrated all queue/instance/roots logic to `internal_api_client.py`" and that this repo "no longer reads/writes `instances//queue/`." Yet `app.py` still defines `queue_dir()` and `instances_dir()`. If those two functions are genuinely uncalled after the migration, they are residual code that contradicts the header's blanket claim. However, the shared infrastructure they rely on—`_active_project_setting()`, `ENV_FILE_PATH`, `CORE_REPO_ROOT`—is also used by live code paths: `get_second_brain_dir()` calls `_active_project_setting("SECOND_BRAIN_DIR")`, and `CORE_REPO_ROOT` feeds `ENV_FILE_PATH` → `read_env_file` / `read_dashboard_settings` → `claude_defaults` → `_discuss_provider_args`, as well as `claude_client.py` and `model_stats_client.py`. A blanket removal of "the associated env-file reading logic" would break those live paths.

Solution:
1. Grep for call sites of `queue_dir` and `instances_dir` across the repo (including `routes/`). If they are truly uncalled, remove just those two function definitions (and any now-orphaned imports).
2. If they *are* still called (e.g., by `routes/chat.py` or other modules outside the reviewed file set), update the module header to accurately scope what Phase 3 migrated, rather than claiming "all queue/instance/roots logic" moved.
3. Do **not** remove `_active_project_setting`, `ENV_FILE_PATH`, `CORE_REPO_ROOT`, or `read_dashboard_settings`—these are shared by `get_second_brain_dir`, `claude_defaults`, and the subprocess clients.

Benefits:
Resolves the specific contradiction between the header's blanket claim and the code, without risking breakage of the shared path-resolution and settings-reading infrastructure that multiple live features depend on.

### AC-2 · Private helper imported across module boundary in chat route
Strength: Strong
Files: routes/chat.py, chat_sessions.py

Problem:
The `api_chat_template_save` route in `routes/chat.py` directly imports `_write_session` from `chat_sessions`. In Python, a leading underscore signals a private, module-internal helper, and importing it from a separate Flask route module means the route is reaching into an implementation detail that `chat_sessions` has not committed to exposing as a stable public API. Specifically, the route performs the logical mutation via `template_store.save_proposal` but then bypasses the standard public persistence surface to force a flush by calling the private `_write_session`. This means the route is bypassing the *persistence* step's public contract, not the logic step. Any internal refactor of `chat_sessions`'s state-management internals—renaming the helper, changing its signature, or inlining it—will break `routes/chat.py` with no compile-time or interface-level warning.

Solution:
Introduce a dedicated public method on `chat_sessions` (e.g., `save_template(session_id, payload)`) that encapsulates both the logical mutation (calling `template_store.save_proposal`) and the persistence flush (currently hidden behind `_write_session`). The route in `routes/chat.py` should call this new public method instead of importing the underscore-prefixed helper. If the write requires a specific internal sequence, that sequence lives inside `chat_sessions` and the route remains agnostic to it.

Benefits:
`routes/chat.py` depends only on the stable public contract of `chat_sessions`, so internal refactors of the session store no longer risk silently breaking the route. The public method also becomes a single, discoverable entry point for template persistence, making the module boundary explicit and testable in isolation.

### AC-3 · Inline "find-or-create" session resolution duplicated across chat routes
Strength: Worth exploring
Files: routes/chat.py, chat_sessions.py

Problem:
`api_chat_inject` in `routes/chat.py` implements a multi-step fallback to resolve a `session_id`: it tries a caller-supplied ID, then falls back to `get_active_session`, then to `start_new_conversation`. The route also manually probes both `id` and `session_id` keys in the returned dictionary (`(session or {}).get("id") or (session or {}).get("session_id")`), indicating that the return shapes of `get_active_session` and `start_new_conversation` are not uniform. This "find-or-create" logic is likely replicated (or approximated) in sibling routes such as `api_chat_active` and `api_chat_new`, each with slightly different key-checking or fallback ordering. Crucially, the route has specific error semantics: if a `requested_id` is provided but invalid, it must abort with a 404; if no ID is provided, it must try fallbacks. A simple helper that returns a session or `None` does not capture these distinct error conditions (invalid ID vs. no active session vs. creation failure), meaning the route would still need to implement the error-handling logic to distinguish these cases.

Solution:
Extract a single public helper in `chat_sessions` (e.g., `resolve_session(requested_id=None)`) that encapsulates the full fallback chain and normalises the return shape to a single, documented dictionary with a consistent `id` key. To properly handle the error semantics, this helper should raise specific exceptions (e.g., `SessionNotFound` for invalid IDs, `SessionCreationError` for fallback failures) rather than returning `None`, allowing the route to map these exceptions to the correct HTTP status codes (404 vs 500) without duplicating the fallback logic.

Benefits:
The "find-or-create" contract lives in one place, so a change to session creation or retrieval is a one-line update in `chat_sessions` rather than a multi-route edit. Routes become shorter and more readable, and the uniform return shape removes the class of bugs where a route checks the wrong key and silently gets `None`. By using exceptions for error semantics, the route logic remains clean and correctly maps failures to appropriate HTTP responses.

### AC-4 · Non-transactional reserve leaves local and server state divergent
Strength: Strong
Files: routes/chat.py, chat_sessions.py

Problem:
In `api_chat_reserve`, the route calls `internal_reserve` (the core-dashboard reservation) first and only then calls `set_reserved` to flip the local `chat_sessions` state. `internal_reserve` is a blocking call that can take a significant amount of time. If `set_reserved` raises (filesystem error, race condition, or any unhandled exception in `chat_sessions`), the local session remains `reserved: false` while the core dashboard already records `reserved: true`. The user receives a 500 response, but the reservation is live on the server side with no compensating release. The provided `internal_api_client.py` does not expose a `release` function; it only exposes `reserve(reservation_id, model, on)`. Therefore, a simple "immediate rollback" by calling a non-existent `internal_release` is not possible. The core issue is the lack of a transactional boundary between the remote blocking call and the local state update, leading to permanent inconsistency until manual intervention.

Solution:
Since `internal_api_client.py` does not provide a `release` method, the compensating pattern of calling `internal_release` is not implementable. Instead, the solution should focus on making the local state update atomic with respect to the remote call's success. One approach is to ensure `set_reserved` is idempotent and can be retried safely, but this does not solve the initial divergence. A more robust architectural fix is to modify `internal_api_client.py` to expose a `release` or `cancel` method, or to design the reservation system such that the local state is the source of truth and the remote call is made *after* local state is set, with a background task or retry mechanism to ensure the remote call eventually succeeds. Alternatively, if the remote call is the source of truth, the local state should be updated *only* after the remote call succeeds, and any failure in the local update should trigger a manual reconciliation process or a compensating remote call if available. Given the current API limitations, the most practical immediate fix is to log the divergence prominently and provide an admin endpoint to manually reconcile the state, while planning a longer-term fix to add a `release` method to the client or change the transactional order.

Benefits:
While the ideal transactional atomicity is not achievable with the current API, this approach acknowledges the limitation and provides a path to consistent state through manual reconciliation or future API enhancements. It prevents silent divergence by making the inconsistency visible and actionable, rather than assuming a rollback mechanism that does not exist.

NOTE: Flag 5 was not applied because the code for `api_chat_message` and `internal_preempt` was not provided in the context, so I could not verify the specific resource leak or state inconsistency described.

### AC-5 · test_agent_client.py is an app integration test wearing an agent_client label
Strength: Strong
Files: test_agent_client.py, agent_client.py

Problem:
Seven of the nine test methods in `test_agent_client.py` (`test_disabled_without_url`, `test_lists_sessions`, `test_wrong_token_is_unreachable_not_an_error`, `test_agent_down`, `test_events_passthrough_hides_token`, `test_events_exclude_transcript_text`, `test_token_file_wins_and_is_read_per_call`) exercise `app.app` HTTP routes and require a live `session_agent` threaded server. Only two methods (`test_filter_events_reassembles_split_blocks`, `test_list_sessions_rejects_wrong_shape`) actually call into `agent_client` in isolation. Because the file sits in the same community as `agent_client.py`, is named after it, and imports `app` and `session_agent` as top-level dependencies, any change to `app`'s route logic or `session_agent`'s server API forces a change in this file even though its stated purpose is testing the client. Conversely, a refactor of `agent_client.filter_events` or `agent_client.list_sessions` is buried among integration tests that spin up a threaded HTTP server, making the client's unit-level contract harder to find and run in isolation.

Solution:
Split the file along the actual dependency boundary. Move the two pure `agent_client` unit tests into a dedicated file (e.g. `test_agent_client_unit.py` or simply rename the existing file to reflect its true scope) that imports only `agent_client` and `unittest`. Move the seven server-dependent integration tests into an `app`-level integration test file (e.g. `test_app_integration.py` or a new `tests/app/` subpackage) where they belong alongside other `app` route tests. Each resulting file should import only the module it is testing, so the community boundary in the dependency graph matches the file boundary.

Benefits:
`agent_client` unit tests run in milliseconds without a network server, making them suitable for a fast pre-commit or CI gate. Changes to `app` routes or `session_agent`'s wire protocol no longer touch a file that is co-located with and named after `agent_client`, eliminating the false coupling. A developer refactoring `filter_events` or `list_sessions` finds the relevant tests immediately in a small, focused file rather than scrolling past seven integration tests that require a live server.

### AC-6 · iter_chunks reaches into urllib3's read1 and silently degrades to 1-byte reads
Strength: Strong
Files: agent_client.py

Problem:
`agent_client.iter_chunks` bypasses the `requests` public API by calling `resp.raw.read1`, a urllib3 `HTTPResponse` method, to obtain a non-blocking read of a long-lived SSE body (because `iter_content(chunk_size=None)` would block indefinitely). When `read1` is absent the code falls back to `resp.iter_content(chunk_size=1)`, which reads one byte at a time—an order-of-magnitude throughput penalty with per-byte Python overhead on a long-lived stream—and does so silently, with no log or warning. The `getattr` guard only checks for the attribute's existence; it does not catch `read1` raising an exception. The net effect is that the client's streaming contract is implicitly tied to a specific library's internal object model rather than to a stable, documented interface, and the degradation path is invisible to the operator.

Solution:
Replace the `read1` / `iter_content(chunk_size=1)` pair with a single, stable streaming strategy that works across any `requests` transport adapter. A practical option is to call `resp.iter_content(chunk_size=64)` (or a small constant like 256) inside a `try/except` that treats `BlockingIOError` or a timeout as "no data yet" and simply continues the loop, rather than relying on a non-blocking `read1`. If the project already sets a socket-level timeout on the `requests` session, `iter_content` with a modest chunk size will return as soon as any bytes are available and will raise on timeout, giving the same non-blocking semantics without touching `resp.raw`. Remove the `getattr`/fallback branch entirely. If a truly non-blocking read is still preferred for latency reasons, keep `read1` but wrap the call in `try/except (AttributeError, Exception)` and emit a `logging.warning` the first time the fallback path is taken, so the degradation is observable.

Benefits:
The streaming path no longer depends on urllib3's internal object model, so an HTTP/2 adapter, a custom transport, or a future `requests` release that changes what `resp.raw` exposes will not silently switch the client to 1-byte reads or raise an uncaught `AttributeError`. The fallback (if retained) becomes visible in logs, letting operators diagnose throughput regressions. The function's contract—"yield chunks of SSE data as they arrive, without blocking indefinitely"—is now expressed through the `requests` public API, which is the interface the rest of the codebase already depends on.

### AC-7 · Shallow interface couples callers to internal session structure
Strength: Worth exploring
Files: template_store.py

Problem:
The save_proposal function requires callers to supply a session dictionary and a template index, forcing them to navigate a deeply nested internal structure to locate the specific proposal they want to persist. The function name suggests it operates on a "proposal" as a first-class object, but in practice the caller must already know the exact position of that proposal within the session's transcript. The function also does not update any session-level state (such as a modified flag or timestamp), leaving the caller responsible for bookkeeping that logically belongs to the save operation.

Solution:
Refactor save_proposal to accept the proposal object directly (or a reference that resolves to it) rather than requiring the caller to supply a session and an index. Return a new or updated session object so that state transitions (modified flag, timestamp) are handled inside the function. This makes the interface match its name and removes the need for callers to understand the internal layout of the session structure.

Benefits:
Callers no longer need to know the internal nesting of the session object, reducing the surface area that breaks when the session schema changes. State updates become atomic with the save operation, eliminating a class of bugs where a save succeeds but the session is never marked as modified. The function becomes easier to test in isolation because it no longer depends on a fully constructed session fixture.

### AC-8 · Validation is inseparable from the save operation in save_template
Strength: Worth exploring
Files: template_store.py

Problem:
The save_template function performs validation, existence checking, and persistence as a single monolithic step. There is no public way to run the validation logic against a candidate template without also triggering the save or the existence check. This means a caller who wants to validate a template name or content before deciding whether to save must either call save_template and catch the failure, or duplicate the validation logic. The coupling makes it harder to reuse the validation rules in other contexts (for example, a UI "check name" endpoint) and obscures which part of the operation actually failed.

Solution:
Extract the validation logic into a standalone public function that returns a structured result (valid or invalid, plus any error details) without performing any side effects. save_template then calls this function internally. Callers that only need to check validity can call the validation function directly, and the save path remains unchanged.

Benefits:
Validation becomes independently testable and reusable in any context that needs to check a template before committing. The save path becomes a thin orchestration of validate, check-existence, persist, which is easier to read and to extend (for example, adding a dry-run mode). Tests can exercise the validation rules without mocking the HTTP layer or the storage backend.

### AC-9 · Duplicated session and mock setup across test files
Strength: Worth exploring
Files: test_chat_routes.py, test_template_store.py

Problem:
Both test_chat_routes.py and test_template_store.py construct fake session objects and mock the template-save dependency, but they do so with slightly different fixture shapes and different mocking strategies. This duplication means a change to the session schema or the save interface must be applied in two (or more) places, and the two copies can drift, causing one test file to pass while the other fails for reasons unrelated to the code under test.

Solution:
Introduce a shared test helper (a small factory function or a unittest fixture) that builds a canonical fake session and a canonical mock for the save dependency. Both test files import and use this helper, so the session shape and the mock contract are defined in exactly one place.

Benefits:
A schema change to the session object requires updating a single helper rather than every test file. The two test files become easier to keep in sync, reducing the risk of one passing while the other silently tests a stale shape. New test files that need a fake session can reuse the same helper, keeping the test suite consistent.

### AC-10 · Chat-turn reservation is split across a local JSON flag and a remote lock registry with no reconciliation
Strength: Strong
Files: routes/chat.py, internal_api_client.py

Problem:
A single user-visible chat turn (and its close) is decomposed into three independent HTTP round-trips to the core dashboard (preempt, reserve-refresh, streaming local-turn) plus a local JSON `reserved` flag persisted in `CHAT_STORAGE_DIR`. The authoritative lock lives in the core dashboard's `_internal_chat_reservations` registry, but `api_chat_message` and `api_chat_close` both gate their internal-API calls on the local `session.get("reserved")` flag. There is no reconciliation path: if the core side expires an idle reservation or a reserve-refresh `InternalApiError` is swallowed as non-fatal, this repo keeps believing the session is reserved and keeps refreshing a lock the core side has already released—or, conversely, `api_chat_close` releases a reservation the core side no longer tracks. The close-time release in particular catches `InternalApiError` and writes a single line to stderr, meaning a network blip or core-dashboard restart silently leaks the GPU lock while the log reports it as a non-fatal warning.

Solution:
Collapse the local JSON `reserved` flag into a thin cache of the remote registry's state, with a single reconciliation call (e.g., a `GET /internal/chat/reservations` or a `reserve` that returns the authoritative state) performed at the start of every turn and at close. Make the close-time release retrying (bounded, with backoff) and, on final failure, persist a "pending-release" marker that a background sweep or next-startup hook can drain. If a true single-call atomic "reserve-and-stream" endpoint can be added to the core dashboard, prefer that over the three-round-trip decomposition so the preempt/reserve/stream sequence becomes one transactional operation.

Benefits:
Eliminates the split-brain window where the local flag and the remote registry disagree; converts the close-time GPU-lock leak from a silent stderr line into a bounded, recoverable, and observable failure; reduces the per-turn HTTP fan-out from three calls to one (or at minimum makes the two bookkeeping calls idempotent and state-reading rather than state-writing); and gives operators a concrete "pending-release" artifact to investigate instead of a log line that is easily missed.

### AC-11 · `stream_local_turn` duplicates the SSE streaming protocol of `local_tool_client` with a divergent error contract and is unused by the route that needs it
Strength: Strong
Files: internal_api_client.py, routes/chat.py, local_tool_client.py

Problem:
`stream_local_turn` in `internal_api_client.py` re-implements the same SSE `data:`-frame parsing and `{"type":"chunk"|"final"|"error"}` framing that `local_tool_client.py`'s `stream_plan_with_tools` already provides, but with a subtly different failure contract: it swallows per-line `json.JSONDecodeError` with a bare `continue` and, on a `requests.RequestException`, *yields* an error frame rather than raising. Meanwhile `api_chat_message`'s `generate()` in `routes/chat.py` is built around *exceptions* (`ClaudeClientError`, `LocalToolClientError`, `TimeoutError`, `ConnectionError`, `OSError`) to drive its `fail()` path and to persist the error into the transcript via `record_turn_error`. If the local-provider path were ever routed through `stream_local_turn`, a transport failure would surface as a data frame, silently bypassing the transcript-persistence and error-bookkeeping that `api_chat_message` carefully sets up. Compounding the duplication, neither `routes/chat.py` nor `internal_api_client.py` actually calls `stream_local_turn`; the local turn in `api_chat_message` goes through `chat_sessions.stream_message`, so the community ships a full HTTP+SSE client for the local-turn path that its own message route does not use, splitting the "how do I talk to the core dashboard for a local turn" knowledge across two modules.

Solution:
Delete `stream_local_turn` from `internal_api_client.py` (or, if it is intended as the canonical client, make `chat_sessions.stream_message` a thin wrapper around it and route `api_chat_message` through the same path). Whichever module is kept, align its failure contract with the exception-based model that `api_chat_message`'s `generate()` already expects: raise a typed exception on transport or framing errors rather than yielding an error frame, so that `record_turn_error` and the transcript-persistence path are always exercised. If the SSE framing logic is genuinely shared, extract a single `iter_sse_frames(response)` helper into one module and have both the local-subprocess and core-dashboard paths consume it, eliminating the duplicated parse loop.

Benefits:
Removes a second, divergent implementation of the same streaming protocol, reducing the surface area that must be kept in sync when the core dashboard's SSE contract changes; guarantees that transport failures on the local-turn path are handled by the same exception-driven `fail()` / `record_turn_error` machinery as every other provider path, so the transcript is never silently missing an error; and restores locality by giving the community a single, unambiguous module that owns "how to talk to the core dashboard for a local turn."

### AC-12 · Chat routes reach into `app.py` private helpers via per-function lazy imports to dodge a circular dependency
Strength: Worth exploring
Files: routes/chat.py, app.py

Problem:
`routes/chat.py` imports `_call_chat`, `_chat_roots`, `_discuss_provider_args`, `CHAT_STORAGE_DIR`, and `instances_dir` from `app.py` inside every view function, because a top-level `from app import …` would be circular (app.py imports the blueprint module to register it). The codebase documents this as a deliberate workaround and it works, but the route module's true dependency graph is hidden: the chat routes depend on `app.py`'s private, underscore-prefixed helpers and on module-level constants rather than on a stable public interface. If `app.py` changes its import order, renames a helper, or moves a constant, the failure surfaces at request time (a `NameError` or `ImportError` inside a view) rather than at import time, and the per-function import pattern makes it easy to miss a newly added dependency that was not yet lazy-imported.

Solution:
Extract the shared helpers and constants that `routes/chat.py` actually needs (`_call_chat`, `_chat_roots`, `_discuss_provider_args`, `CHAT_STORAGE_DIR`, `instances_dir`) into a small, import-safe module (e.g., `app_chat_support.py` or a `chat/` package `__init__`) that neither `app.py` nor `routes/chat.py` imports the other from. Both `app.py` and `routes/chat.py` then import from that leaf module at top level, breaking the cycle structurally rather than temporally. The lazy-import workaround and its accompanying header comment can be removed.

Benefits:
Makes the dependency graph visible and static: a rename or removal of a helper is caught at import time by the linter or test suite rather than at the first request after a deploy; eliminates the timing assumption ("by the time a view runs, app.py is fully initialised") that the current code relies on; and gives the chat routes a stable, public interface to depend on, so future refactors of `app.py`'s internal structure do not silently break the blueprint.

### AC-13 · Duplicated internal-API error-handling wrapper across three chat view functions
Strength: Strong
Files: routes/chat.py

Problem:
In `routes/chat.py`, the three view functions `api_chat_close`, `api_chat_message`, and `api_chat_reserve` each independently import `InternalApiError` from `internal_api_client` and wrap their respective internal-API call in a `try/except InternalApiError` block that logs a non-fatal warning to `sys.stderr` and continues. The calls themselves differ—`api_chat_message` invokes `internal_preempt()` (no arguments) and yields an SSE event on success, while `api_chat_close` and `api_chat_reserve` invoke `internal_reserve(session_id, model, on)`—but the surrounding error-handling pattern (import, exception type, logging format, fallback behavior) is replicated verbatim in all three places. This duplication creates a concrete maintenance hazard: any change to the error type, the logging format, or the fallback behavior must be replicated in three places, and a missed copy silently diverges.

Solution:
Extract a small private helper in `routes/chat.py` (or a shared utility module) such as:

```python
def _safe_internal_call(fn, *args, **kwargs):
    """Execute an internal-API call with standard non-fatal error handling."""
    try:
        return fn(*args, **kwargs)
    except InternalApiError as exc:
        print(f"[warn] internal API call failed: {exc}", file=sys.stderr)
        return None
```

Each of the three view functions then calls its specific function through this wrapper (e.g., `_safe_internal_call(internal_preempt)` or `_safe_internal_call(internal_reserve, session_id, model, on)`), reducing the duplicated error-handling surface to one definition while preserving each call's distinct signature and return-value handling.

Benefits:
A single point of change for the import, exception type, logging format, or fallback behavior. New view functions that need the same non-fatal pattern call the helper instead of re-implementing the `try/except` block, reducing the chance of a missed update and making the codebase easier to audit for consistent error handling.

### AC-14 · Undocumented divergence in error severity for the same `InternalApiError`
Strength: Worth exploring
Files: routes/chat.py

Problem:
`api_chat_reserve` treats an `InternalApiError` as fatal (it calls `abort(502)`), while `api_chat_close` and `api_chat_message` treat the identical exception as non-fatal (log to `sys.stderr` and continue). The distinction is intentional—reservation is the primary action in one endpoint and a best-effort side-effect in the others—but nothing in the code documents *why* the handling differs. A future developer refactoring or adding a new endpoint has no in-code signal for which severity is correct and may inadvertently "normalize" the behavior, turning a critical failure into a silent no-op or vice-versa.

Solution:
Add a one-line comment at each `try/except` site explaining the intent (e.g., "Reservation is the primary action here; failure is fatal" vs. "Release is best-effort cleanup; failure is non-fatal"). Alternatively, if a shared helper is introduced per AC-001, give it a `fatal: bool` parameter (or a separate `_fatal_internal_call` variant) so the caller's intent is explicit at the call site rather than implicit in the surrounding function's purpose.

Benefits:
The severity distinction becomes self-documenting and resistant to accidental "fixes." Reviewers can verify at a glance that a change preserves the intended fatal/non-fatal contract, and new contributors understand the design rationale without archaeology.

### AC-29 · Add get_reservation_state() client method to internal_api_client.py
Strength: Strong
Split-Depth: 1
Files: internal_api_client.py

Problem:
The local JSON `reserved` flag (written by `set_reserved` in routes/chat.py) is the sole decision gate for reservation lifecycle. There is no method in internal_api_client.py to query the core dashboard's actual reservation registry for a given session. The existing methods are `get_roots()`, `preempt()`, `reserve(reservation_id, model, on)`, and `stream_local_turn(...)` — all of which either mutate state or serve unrelated data. Without a read-only state query, routes/chat.py cannot verify whether a reservation is truly held server-side before acting, and the local flag can silently drift (expired, released by another path, core dashboard restarted), producing the split-brain that AC-10 targets.

Solution:
Add a new function `get_reservation_state(reservation_id: str) -> dict` to internal_api_client.py. It issues `GET {CORE_DASHBOARD_URL}/api/internal/chat/reservations?reservationId=<id>` with the same `_headers()` auth and a 10-second timeout, calls `r.raise_for_status()`, and returns the parsed JSON body (expected shape: `{"reserved": bool, "owner": str|null, "expires_at": str|null}`). On any `requests.RequestException` it raises `InternalApiError` with a descriptive message — it must NOT catch the error and return a default like `{"reserved": false}`. This mirrors the error-handling pattern already used by `get_roots()` in the same file. Prerequisite: the core dashboard must expose (or be asked to expose) this GET endpoint; if it does not yet exist, coordinate with the core-dashboard team before merging the routes/chat.py change that calls this method.

Benefits:
Provides the single authoritative read primitive that the routes layer needs to reconcile local state against the remote registry. Without this method, any fix in routes/chat.py would still be gated on the local JSON flag and the split-brain persists. The method is small, self-contained, and follows the exact pattern of the existing `get_roots()` function in the same file, so it is low-risk to review and merge independently.

### AC-30 · Replace local-flag gates in routes/chat.py with remote-state reconciliation
Strength: Strong
Split-Depth: 1
Files: routes/chat.py
Depends-On: AC-29

Problem:
In `api_chat_message`, the line `if existing.get("reserved"):` reads the local JSON flag and decides whether to issue a refresh (`internal_reserve(session_id, ollama_client.MODEL, True)`) or a fresh reserve. In `api_chat_close`, the line `if session.get("reserved"):` reads the same flag and decides whether to call `internal_reserve(session_id, ollama_client.MODEL, False)` to release. In both cases the local flag is the sole decision input. If the flag is stale — the reservation expired server-side, the core dashboard restarted, or another code path released it — the message path either skips a needed fresh-reserve or refreshes a reservation that no longer exists, and the close path either skips a release that is still needed or attempts a release on an already-gone reservation. The close path additionally catches `InternalApiError` and writes a single line to stderr (`print(f"[chat-close] reservation release failed (non-fatal): {e}", file=sys.stderr, flush=True)`), silently leaking the GPU lock with no application-log visibility and no retry.

Solution:
In `api_chat_message`: replace the `if existing.get("reserved"):` gate with a call to `get_reservation_state(session_id)` (imported from `internal_api_client`). Decision logic becomes: (a) remote says reserved by this session → issue the existing refresh call; (b) remote says not reserved → issue a fresh preempt + reserve; (c) the GET raises `InternalApiError` → abort the turn with `abort(502, description="cannot confirm reservation state")` — do NOT fall back to the local flag. After a successful reserve/refresh, write the result back to the local flag via `set_reserved` as a cache for debugging only. In `api_chat_close`: replace `if session.get("reserved"):` with `get_reservation_state(session_id)`. Decision logic: (a) remote says not reserved → skip the release call, clear the local flag, log at INFO level; (b) remote says reserved by this session → call `internal_reserve(session_id, ollama_client.MODEL, False)` inside a bounded retry loop (3 attempts, backoff 1s/3s/9s, catching `InternalApiError` each time); (c) remote says reserved by another session → skip release, log WARNING, clear local flag; (d) the GET raises `InternalApiError` → treat as state-unknown, proceed to the retry path in (b). If all 3 retries in (b) fail, write a pending-release marker JSON file to `CHAT_STORAGE_DIR/pending_releases/<session_id>.json` containing `{session_id, failed_at, last_error}`, and log at WARNING level (not stderr). The local flag is still written (as cache) but is never read as a condition that controls whether an internal-API call is made.

Benefits:
Eliminates the split-brain at the root: the local JSON flag is demoted from decision gate to read-through cache, and every turn and every close is verified against the authoritative remote registry before acting. The close path no longer silently leaks a GPU lock — it retries, logs at application level, and persists a recoverable marker on final failure. The message path no longer acts on stale state. Both changes are confined to the two existing view functions and their immediate logic; no new routes, no new persistence layer, no changes to the core dashboard's data structures.
