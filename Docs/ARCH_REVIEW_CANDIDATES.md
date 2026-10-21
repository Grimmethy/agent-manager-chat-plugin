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

### AC-15 · AC-2a – Add public `save_template` function to chat_sessions.py
Strength: Strong
Split-Depth: 1
Files: chat_sessions.py

Problem:
The route `api_chat_template_save` in routes/chat.py currently imports the private `_write_session` function across the module boundary (`from chat_sessions import get_session, _write_session`) and pairs it with a direct `template_store.save_proposal(...)` call. This leaks an internal persistence helper into the routing layer. The module `chat_sessions.py` already imports `template_store` at the top level and already defines `_write_session(storage_dir: Path, session: dict)` as a module-level function, so both pieces needed to encapsulate the save-and-flush sequence are already reachable within the module. No public function currently exists that combines the logical mutation (saving the proposal) with the persistence flush (writing the session file).

Solution:
Add a new public module-level function `save_template(storage_dir: Path, session: dict, entry: int, index: int, key=None, text=None)` in chat_sessions.py, placed near the other public entry points (`get_session`, `inject_user_message`, `record_turn_error`). Its body calls `template_store.save_proposal(session, entry, index, key, text)` (which may raise `LookupError` if the proposal is not found), then calls `_write_session(storage_dir, session)` to flush the mutated session to disk, and returns the proposal object. The function's docstring states: 'Persists a template proposal for the given session and flushes the session to storage. Raises LookupError if the proposal does not exist.' No existing function is modified; `_write_session` remains private and unchanged. The new function is the only addition to the file.

Benefits:
Provides a single public API surface for the save-and-flush operation, eliminating the need for any external module to reach into `_write_session`. Keeps the persistence mechanism (atomic temp-file write via `os.replace`) fully internal to chat_sessions.py. The function is small, testable in isolation, and follows the same module-level-function style already used by `get_session`, `inject_user_message`, and `record_turn_error` in the same file.

### AC-16 · AC-2b – Replace cross-module `_write_session` import in routes/chat.py with `save_template`
Strength: Strong
Split-Depth: 1
Files: routes/chat.py
Depends-On: AC-15

Problem:
The `api_chat_template_save` route currently imports the private `_write_session` from chat_sessions (`from chat_sessions import get_session, _write_session`) and also does `import template_store` solely to call `template_store.save_proposal(...)` before calling `_write_session(CHAT_STORAGE_DIR, session)`. This crosses the module boundary into a private helper and duplicates the save-and-flush orchestration that belongs in the session module. After AC-2a lands, a public `save_template` function exists in chat_sessions.py that encapsulates both steps.

Solution:
In `api_chat_template_save`, change the import line from `from chat_sessions import get_session, _write_session` to `from chat_sessions import get_session, save_template`. Remove the `import template_store` line (it is no longer needed in this route). Replace the two-step sequence:

    try:
        prop = template_store.save_proposal(session, entry, index, body.get("key"), body.get("text"))
    except LookupError:
        abort(404, description="no such template proposal")
    _write_session(CHAT_STORAGE_DIR, session)

with a single call:

    try:
        prop = save_template(CHAT_STORAGE_DIR, session, entry, index, body.get("key"), body.get("text"))
    except LookupError:
        abort(404, description="no such template proposal")

The `LookupError` is still raised by `template_store.save_proposal` inside `save_template` before `_write_session` is reached, so the route's error handling is unchanged. The HTTP response (`jsonify({"session": session, "proposal": prop})`), status codes, and all other logic in the route remain identical. No other route in the visible portion of routes/chat.py references `_write_session` or `template_store` for this purpose.

Benefits:
Eliminates the cross-module import of a private helper. The routing layer now depends only on the public API of chat_sessions. If the internal write mechanism changes (e.g., a different flush strategy), only chat_sessions.py needs updating. The route becomes shorter and its intent clearer: one call to save the template, one catch for the not-found case.

### AC-17 · Document idempotency and atomicity contract on set_reserved
Strength: Strong
Split-Depth: 1
Files: chat_sessions.py

Problem:
set_reserved currently reads the session, sets session["reserved"] = reserved, and calls _write_session. The function is already idempotent (calling it twice with the same value converges to the same state without raising) and _write_session already performs an atomic write (tempfile.mkstemp + os.replace). However, nothing in the docstring or code signals this contract to callers. The AC-4 divergence scenario (server-side reservation live, local flag not set) means an operator or future code will retry set_reserved after a partial failure, and there is no documented guarantee that the retry is safe. A future engineer refactoring or wrapping this function has no basis to assume retry-safety.

Solution:
Extend the existing docstring of set_reserved (currently: "Just the persisted flag -- app.py's reservation registry owns actually acquiring/releasing the lock and the idle-timeout watchdog; this only keeps the dashboard's own display of 'is this session reserved' in sync with that.") with two additional lines: (1) an explicit idempotency contract statement -- "Calling this more than once with the same reserved value is a safe no-op; the final persisted state is identical regardless of how many times it is invoked." (2) An atomicity note -- "Persistence is atomic (tempfile + os.replace in _write_session), so a concurrent reader never observes a half-written session file." No behavioural code change is needed; the function body and _write_session already satisfy both properties. This is a documentation-only edit to the docstring string literal.

Benefits:
Any future caller (the reconciliation endpoint, a retry wrapper, an operator script) can rely on set_reserved being retry-safe without re-reading the implementation. The AC-4 reconciliation path (sub-candidate below) and any compensating logic can call it idempotently without a separate guard. Removes the plan's "UNKNOWN whether it currently is" flag on atomicity with a verified, in-code statement.

### AC-18 · Add AC4_DIVERGENCE structured logging and operational note to api_chat_reserve
Strength: Strong
Split-Depth: 1
Files: routes/chat.py

Problem:
In api_chat_reserve, the call sequence is: internal_reserve(session_id, ollama_client.MODEL, want_on) succeeds, then set_reserved(CHAT_STORAGE_DIR, session_id, want_on) is called with no surrounding try/except. If set_reserved raises (disk full, permission error, JSON corruption), the server-side reservation is LIVE but the local session file was never updated. The exception propagates as an unstructured 500 with no log entry that identifies the divergence, making it invisible to operators until a user reports a stuck reservation. There is also no in-code note explaining the known limitation (no compensating release in the error path) or the reconciliation path, so the next engineer touching this function has no context for the gap.

Solution:
In api_chat_reserve, wrap the existing line `session = set_reserved(CHAT_STORAGE_DIR, session_id, want_on)` in a try/except Exception block. In the except clause: (1) log at ERROR level using the standard library logging module (or the existing print-to-stderr pattern used elsewhere in this file, e.g. `print(f"[chat-close] reservation release failed (non-fatal): {e}", file=sys.stderr, flush=True)`), with the message containing the stable tag `AC4_DIVERGENCE`, the session_id, ollama_client.MODEL, the exception type and message, and the human-readable note "Server-side reservation is LIVE but local chat_sessions state was NOT updated. Manual reconciliation required." (2) Re-raise the exception so Flask still returns 500 (no behaviour change). Above the try/except, add a 3-line comment block stating: the known limitation (no compensating release call in this error path), the reconciliation path (use the /reserve/reconcile endpoint), and the longer-term fix (wire internal_reserve(session_id, model, False) -- the same call api_chat_close already uses -- into this except block, or reorder so local state is set first and the remote call is retried asynchronously). Note: the plan assumed no release API exists, but api_chat_close in this same file already calls `internal_reserve(session_id, ollama_client.MODEL, False)` to release; the longer-term fix can use that existing call without modifying internal_api_client.py.

Benefits:
Operators can grep for `AC4_DIVERGENCE` in stderr/logs to find every instance where server and local state diverged, with enough context (session_id, model, exception) to act immediately. The in-code comment makes the known gap and its reconciliation path visible to anyone reading api_chat_reserve, preventing the issue from being re-discovered from scratch. No behavioural change to the happy path or the 500 response shape.

### AC-19 · Add POST /api/chat/<session_id>/reserve/reconcile admin endpoint
Strength: Strong
Split-Depth: 1
Files: routes/chat.py

Problem:
When AC4_DIVERGENCE occurs (server-side reservation live, local flag stale), there is no in-app way to reconcile the local state. An operator must hand-edit the session JSON file in .agent-manager-chat-sessions/<id>.json, which is error-prone and bypasses the _write_session atomicity guarantee. The plan's step 3 calls for an admin endpoint that lets the operator choose which side to trust. The existing codebase has no admin/auth decorator pattern in routes/chat.py (all routes are unauthenticated), so the endpoint should follow the same convention (no additional auth layer) but be clearly named and documented as an operator-only action.

Solution:
Add a new route in routes/chat.py: `@chat_bp.route("/api/chat/<session_id>/reserve/reconcile", methods=["POST"])` with function `api_chat_reserve_reconcile(session_id)`. Body: `{"action": "force_local_reserved" | "force_local_unreserved"}`. Behaviour: (1) Validate action is one of the two strings; if not, abort(400). (2) Look up the session via `get_session(CHAT_STORAGE_DIR, session_id)`; if missing or not active, abort(404). (3) For `force_local_reserved`: call `set_reserved(CHAT_STORAGE_DIR, session_id, True)` (idempotent per the contract documented in chat_sessions.py). For `force_local_unreserved`: call `set_reserved(CHAT_STORAGE_DIR, session_id, False)`, AND also call `internal_reserve(session_id, ollama_client.MODEL, False)` in a best-effort try/except (mirroring the exact pattern in api_chat_close: `try: internal_reserve(session_id, ollama_client.MODEL, False) except InternalApiError as e: print(...)`). This is safe because api_chat_close already uses this call as a release; no new method in internal_api_client.py is needed. (4) Log the reconciliation at WARNING level with the session_id, the action taken, and (for force_local_unreserved) a note that the server-side release was best-effort. (5) Return `jsonify(session)` with the resulting local state so the operator can confirm. Add a docstring noting this is an operator-only reconciliation path for AC4_DIVERGENCE events and that force_local_unreserved does NOT guarantee the server-side reservation is released (the internal_reserve call is best-effort, same as api_chat_close).

Benefits:
Operators can fix a divergence from the dashboard or a curl command without touching the filesystem directly. The force_local_unreserved path releases the server-side reservation using the same existing call api_chat_close already makes, closing the loop without modifying internal_api_client.py. The endpoint is small, follows the file's existing patterns (lazy imports, abort codes, jsonify), and is independently testable (mock set_reserved and internal_reserve, assert 200/400/404 responses).

### AC-20 · Create test_app_integration.py with the seven session-agent route tests
Strength: Strong
Split-Depth: 1
Files: test_app_integration.py

Problem:
test_agent_client.py currently mixes two concerns in a single AgentRouteTests class: seven tests that spin up a real session_agent server on loopback and exercise app.app HTTP routes (test_disabled_without_url, test_lists_sessions, test_wrong_token_is_unreachable_not_an_error, test_agent_down, test_events_passthrough_hides_token, test_events_exclude_transcript_text, test_token_file_wins_and_is_read_per_call), and two pure unit tests of agent_client (test_filter_events_reassembles_split_blocks, test_list_sessions_rejects_wrong_shape). Because the class-level setUp unconditionally starts a threaded HTTP server, even the two unit tests pay the cost of importing app, session_agent, and the fixture tree. The integration tests need a dedicated file that owns the server lifecycle and the app/session_agent imports, so the unit tests can later be isolated in a file that has zero dependency on the server stack.

Solution:
Create a new file test_app_integration.py at the same directory level as test_agent_client.py. It contains: a module docstring describing the integration scope; imports for json, os, shutil, sys, tempfile, threading, unittest, pathlib.Path, unittest.mock, and the three project modules (agent_client, app, session_agent) after a sys.path.insert; the FIXTURE_HOME and TOKEN module-level constants; a single TestCase class (e.g. AgentRouteIntegrationTests) whose setUp copies the fixture tree, constructs session_agent.Agent, starts the make_server on 127.0.0.1:0 in a daemon thread, and creates app.app.test_client(); the _stop and env helper methods; and exactly the seven integration test methods listed above, copied verbatim. The __main__ guard runs unittest.main(). No unit-test methods are included.

Benefits:
The integration tests live in a file whose name and docstring make the server dependency explicit. CI or a developer can run just the fast unit tests without paying server-startup cost, and the integration file can be tagged or excluded in lightweight CI stages. The file is self-contained: it owns the fixture path, the server lifecycle, and the app imports, so nothing is shared across the split.

### AC-21 · Trim test_agent_client.py to the two agent_client unit tests only
Strength: Strong
Split-Depth: 1
Files: test_agent_client.py
Depends-On: AC-20

Problem:
After the integration tests are extracted to test_app_integration.py (candidate 0), test_agent_client.py still contains the full AgentRouteTests class with its server-starting setUp, the app and session_agent imports, the FIXTURE_HOME/TOKEN constants, and the seven integration methods. The two unit tests (test_filter_events_reassembles_split_blocks, test_list_sessions_rejects_wrong_shape) are buried inside that class and inherit the server setUp/tearDown, meaning they cannot run in an environment where app or session_agent are absent. The file's docstring also claims to test 'the terminal-sessions routes, against a REAL session_agent server', which is no longer accurate once the integration tests are gone.

Solution:
Rewrite test_agent_client.py to contain only: an updated module docstring ('Unit tests for agent_client.py — no server, no app import'); imports limited to os, sys, unittest, pathlib.Path, unittest.mock, and agent_client (after sys.path.insert); a single TestCase class (e.g. AgentClientUnitTests) with no setUp/tearDown; the two test methods test_filter_events_reassembles_split_blocks and test_list_sessions_rejects_wrong_shape copied verbatim; and the __main__ guard. Remove the app, session_agent, threading, tempfile, shutil, and json imports; remove FIXTURE_HOME, TOKEN, the env helper, _stop, and all seven integration methods. The file must be importable and runnable in an environment where app and session_agent are not installed.

Benefits:
The unit-test file has a clean, minimal import surface (agent_client + stdlib only), so it can be collected and executed in any environment that has agent_client.py but not the full server stack. Running the unit tests no longer starts a threaded HTTP server, reducing test time from the server-startup baseline to milliseconds. The file name test_agent_client.py now accurately reflects its content: pure unit tests of the agent_client module.

### AC-22 · Extract _safe_internal_call helper for best-effort internal-API failures
Strength: Strong
Split-Depth: 1
Files: routes/chat.py

Problem:
Three chat view functions contain near-identical try/except blocks that catch InternalApiError, log a non-fatal message to sys.stderr, and swallow the exception so the main request path is never blocked. In api_chat_close the block wraps internal_reserve(session_id, ollama_client.MODEL, False); in api_chat_message it wraps internal_preempt() (twice: once for the preempt step and once for the reserved-session refresh). The duplication is mechanical and each site carries its own local import of InternalApiError and the internal call function, making the 'never block the chat on this' policy hard to audit in one place.

Solution:
Add a module-level helper _safe_internal_call(fn, *args, log_prefix, **kwargs) in routes/chat.py that executes fn(*args, **kwargs) inside a try/except InternalApiError, prints f"[{log_prefix}] ..." to sys.stderr with flush=True, and returns None on failure. Move the InternalApiError import to module level. Refactor the three best-effort call sites (api_chat_close's release, api_chat_message's preempt, and api_chat_message's reserved-refresh) to call the helper, passing the existing log prefix ('chat-close', 'chat-preempt', 'chat-reserve') and the exact call arguments. Preserve each site's existing success-path behavior: api_chat_close ignores the return value; api_chat_message assigns the preempt result to preempted and the refresh result is discarded. Leave api_chat_reserve untouched because it aborts(502) on InternalApiError rather than swallowing it, so it does not fit the 'safe' contract.

Benefits:
Single, auditable implementation of the 'best-effort internal API call' policy; removes three duplicated try/except blocks and their local imports; makes it impossible for a future edit to accidentally let an InternalApiError from one of these sites propagate and block a chat turn; keeps the log format and stderr flush behavior identical to the current code.

### AC-23 · Unify local-provider model import and reserved-flag check across chat views
Strength: Strong
Split-Depth: 1
Files: routes/chat.py
Depends-On: AC-22

Problem:
The local-provider model identifier is imported as import ollama_client and referenced as ollama_client.MODEL independently inside api_chat_close, api_chat_message, and api_chat_reserve, so the coupling to ollama_client is scattered across three functions. Separately, the 'is this session reserved?' check is duplicated: api_chat_close uses if session.get("reserved") and api_chat_message uses if existing.get("reserved"). If the reserved flag's name or shape ever changes, both sites must be updated in lockstep.

Solution:
Introduce a single module-level import of ollama_client (or a small _local_model() accessor) and replace the three local import ollama_client statements with references to it. Extract a tiny _is_reserved(session) helper (or inline constant check) used by both api_chat_close and api_chat_message for the reserved-flag test. Do not change any HTTP semantics, log messages, or the abort(502) behavior of api_chat_reserve.

Benefits:
One place defines the local-provider model source, reducing the risk of a stale or divergent import; the reserved-flag check is centralized so a schema change is a one-line fix; both changes are behavior-preserving and keep the diff small and reviewable.
