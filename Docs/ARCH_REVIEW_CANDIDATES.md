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

### AC-26 · ~~Duplicated SSE block-parsing logic between client and test~~
Strength: Strong

**Retracted.**

The original draft asserted that `test_filter_events_reassembles_split_blocks` re-implements the SSE parsing state machine (partial-line buffers, delimiter tracking, block-boundary logic) to construct its expected output. The provided source for `test_agent_client.py` does not support this claim. The test:

1. Defines a single hardcoded byte string containing a few SSE blocks.
2. Splits it into fixed-size (7-byte) chunks.
3. Feeds those chunks to `agent_client.filter_events`.
4. Asserts that the returned value equals a hardcoded expected byte string.

There is no duplicated parsing logic, no re-derivation of intermediate buffer state, and no coupling to internal delimiter-handling mechanics. The test is already a black-box exercise of `filter_events`'s observable contract. Because the stated problem does not exist, the proposed rewrite is unnecessary and the finding is withdrawn in its entirety.

NOTE: Flag 1 was applied in full; the finding is retracted because its core premise (that the test re-implements the parsing state machine) is contradicted by the actual source code, making the entire problem/solution/benefits chain inapplicable.

### AC-27 · Clients import a config constant from the Flask entry-point module
Strength: Strong
Files: claude_client.py, model_stats_client.py, app.py

Problem:
Both `claude_client.py` and `model_stats_client.py` execute `from app import CORE_REPO_ROOT` at module level. Because `app.py` is the Flask application entry point, importing a single path constant forces the entire application module to be loaded and initialized. This inverts the expected dependency direction—leaf utility modules should not depend on the root entry point. It also creates a latent circular-import hazard: if `app.py` ever adds a top-level import of either client (today it uses lazy imports inside functions, which masks the coupling), the import cycle will crash at startup. The clients also cannot be imported independently in tests, scripts, or tooling without pulling in the full Flask app.

Solution:
Extract `CORE_REPO_ROOT` (and any other shared path constants such as `ENV_FILE_PATH`) into a new lightweight module, e.g. `config.py` or `paths.py`, that carries no dependency on `app.py`. Update `app.py` to import from this new module, and change `claude_client.py` and `model_stats_client.py` to import `CORE_REPO_ROOT` from the same neutral module. The new module should contain only constant definitions and, at most, a small `os.path` computation—no Flask, no business logic.

Benefits:
The dependency graph becomes acyclic and unidirectional: `config.py` ← `app.py`, `config.py` ← `claude_client.py`, `config.py` ← `model_stats_client.py`. The client modules become independently importable for unit tests, CLI scripts, and CI tooling without booting the Flask app. Adding or removing a top-level import in `app.py` can no longer trigger a circular-import crash, and the single source of truth for the repo-root path is obvious and easy to audit.

### AC-28 · Duplicated Node.js temp-file subprocess invocation in two client modules
Strength: Strong
Files: claude_client.py, model_stats_client.py

Problem:
Both `claude_client.py` and `model_stats_client.py` independently implement the same multi-step pattern: generate a unique temporary file path, serialize a JSON payload to that file, invoke `subprocess.run` with `node` and the target script, handle timeout and non-zero-exit errors, and remove the temp file in a `finally` block. The logic is nearly line-for-line identical. This is a missing-locality defect: if the invocation strategy must change (switch to stdin pipes, alter the cleanup mechanism, add retry logic, or change the error-reporting format), both modules must be updated in lockstep, and a missed update silently produces divergent behavior.

Solution:
Introduce a shared utility function—e.g. `run_node_script(script_path, payload, timeout)` in a new `node_client_util.py` (or alongside the config constants in `config.py`)—that encapsulates the full write-JSON-to-temp-file → `subprocess.run(node, …)` → cleanup-in-`finally` pattern, including timeout handling and error normalization. Both `claude_client.py` and `model_stats_client.py` then call this single function, passing their respective script path, JSON payload, and timeout. The utility owns the temp-file lifecycle and the subprocess contract, so the client modules focus solely on constructing their payloads and interpreting results.

Benefits:
The temp-file lifecycle, timeout handling, and error-reporting logic live in exactly one place, eliminating the risk of the two implementations drifting apart. A future change to the invocation mechanism (pipes, a different runtime, structured error codes) is a single-file edit. The client modules become shorter and more readable, and the shared utility is trivially unit-testable in isolation using `unittest` (mocking `subprocess.run`) without importing either client or the Flask app.

### AC-31 · AC-27a – Create config.py with CORE_REPO_ROOT
Strength: Strong
Split-Depth: 1
Files: .venv/lib/python3.12/site-packages/flask/config.py

Problem:
Both claude_client.py and model_stats_client.py import CORE_REPO_ROOT from app (the Flask entry-point module), which forces a dependency on the entire Flask application just to obtain a single path constant. app.py defines it as `CORE_REPO_ROOT = Path(os.environ.get("AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager")).resolve()` at module level. There is no standalone module that holds this value, so any non-Flask consumer must pull in Flask transitively.

Solution:
Create a new file `config.py` at the repository root containing exactly: a one-line module docstring (e.g. `"""Shared path constants for the Chat plugin."""`), the two stdlib imports needed (`import os` and `from pathlib import Path`), and the single constant definition copied verbatim from app.py: `CORE_REPO_ROOT = Path(os.environ.get("AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager")).resolve()`. The file must not import Flask, app, any client module, or any other project-internal module. No functions, no classes, no side effects beyond the Path.resolve() call.

Benefits:
Provides a zero-dependency (stdlib-only) module that any consumer can import without pulling in Flask or the rest of the application. Establishes the single source of truth for the core-repo path that the next two sub-candidates will point at.

### AC-32 · AC-27b – app.py imports CORE_REPO_ROOT from config
Strength: Strong
Split-Depth: 1
Files: app.py
Depends-On: AC-31

Problem:
app.py currently defines `CORE_REPO_ROOT = Path(os.environ.get("AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager")).resolve()` at module level (line ~30, after the Flask import). The derived constants `ENV_FILE_PATH` and `DASHBOARD_SETTINGS_PATH` are computed from it in the same module. Once config.py exists (AC-27a), app.py should import the constant rather than define it, so there is exactly one definition in the codebase.

Solution:
In app.py, replace the three-line `CORE_REPO_ROOT = Path(os.environ.get(...)).resolve()` statement with `from config import CORE_REPO_ROOT`. Place this import alongside the other top-level imports (after the stdlib imports, before or after the Flask import—either is fine since config has no Flask dependency). The subsequent lines `ENV_FILE_PATH = CORE_REPO_ROOT / "agent-manager.env"` and `DASHBOARD_SETTINGS_PATH = CORE_REPO_ROOT / "dashboard-settings.json"` remain unchanged; they now reference the imported name. Remove the now-redundant `import os` and `from pathlib import Path` ONLY if no other code in app.py uses them (os is still used by `_active_project_setting` and `_chat_roots`; Path is still used by `get_pipeline_dir`, `get_second_brain_dir`, `STATE_DIR`, etc.—so in practice both stay). No other line in app.py changes.

Benefits:
Eliminates the duplicate definition. app.py's namespace still exposes `CORE_REPO_ROOT` (via the from-import), so any existing `app.CORE_REPO_ROOT` access pattern or `__all__` export continues to work. The single source of truth now lives in config.py.

### AC-33 · AC-27c – claude_client.py imports CORE_REPO_ROOT from config
Strength: Strong
Split-Depth: 1
Files: claude_client.py
Depends-On: AC-31

Problem:
claude_client.py has the line `from app import CORE_REPO_ROOT` (after the long comment block about claude-client.js not being vendored). This forces importing the entire Flask application module just to get a path constant. The only use is `CLAUDE_CLIENT_JS = CORE_REPO_ROOT / "src" / "claude-client.js"`. No other symbol is imported from app in this file.

Solution:
Change the single import line `from app import CORE_REPO_ROOT` to `from config import CORE_REPO_ROOT`. No other line in claude_client.py changes. The `CLAUDE_CLIENT_JS` assignment and all function bodies remain identical.

Benefits:
claude_client.py no longer depends on the Flask entry-point module. It can be imported in isolation (e.g. in a test or a standalone script) without Flask being installed or the Flask app object being constructed. The dependency graph becomes: claude_client → config (stdlib only), not claude_client → app → Flask.

### AC-34 · AC-27d – model_stats_client.py imports CORE_REPO_ROOT from config
Strength: Strong
Split-Depth: 1
Files: model_stats_client.py
Depends-On: AC-31

Problem:
model_stats_client.py has the line `from app import CORE_REPO_ROOT` (after the module docstring and stdlib imports). This forces importing the entire Flask application module just to get a path constant. The only use is `MODEL_STATS_DB_JS = CORE_REPO_ROOT / "src" / "model-stats-db.js"`. No other symbol is imported from app in this file.

Solution:
Change the single import line `from app import CORE_REPO_ROOT` to `from config import CORE_REPO_ROOT`. No other line in model_stats_client.py changes. The `MODEL_STATS_DB_JS` assignment and all function bodies remain identical.

Benefits:
model_stats_client.py no longer depends on the Flask entry-point module. It can be imported in isolation without Flask. The dependency graph becomes: model_stats_client → config (stdlib only), not model_stats_client → app → Flask. Together with AC-27c, both clients are now decoupled from the Flask app for their sole shared dependency.

### AC-35 · Create node_client_util.py with shared temp-file subprocess helper
Strength: Strong
Split-Depth: 1
Files: node_client_util.py

Problem:
claude_client.py's generate() and model_stats_client.py's _run_event() each independently implement the same mechanical pattern: generate a unique temp-file path via tempfile.gettempdir() + uuid4, serialize a dict as JSON into it, invoke node <script> [extra-args] <temp-file> via subprocess.run with a timeout, and unlink the temp file in a finally block. Any fix to the temp-file lifecycle (encoding, race on cleanup, missing kwarg) must be made in two places. The two implementations also differ in subtle ways (claude_client passes text=True and no extra args; model_stats passes an event string as an extra arg and omits text=True), making a blind copy-paste refactor unsafe without a single parameterized function.

Solution:
Create node_client_util.py at the project root (same directory as the two client modules). Expose one public function: run_node_with_tempfile(script_path: str, extra_args: list[str], payload: dict, timeout: float, *, text: bool = False) -> subprocess.CompletedProcess. The function body: (1) build a unique temp path as Path(tempfile.gettempdir()) / f"node-req-{uuid.uuid4().hex}.json"; (2) write json.dumps(payload) to it with encoding="utf-8"; (3) call subprocess.run(["node", str(script_path), *extra_args, str(tmp_path)], capture_output=True, text=text, timeout=timeout); (4) in a finally block, unlink the temp file suppressing OSError. The function does NOT catch TimeoutExpired, does NOT check returncode, and does NOT parse stdout — those remain caller responsibilities so each module keeps its own error contract (ClaudeClientError vs. log-and-swallow). Add a module docstring stating the contract.

Benefits:
Single implementation of the temp-file create/write/invoke/cleanup lifecycle; both callers become thinner; a future change to the invocation mechanism (e.g. stdin pipes) touches one function instead of two; the parameterized extra_args and text kwargs capture the real differences between the two call sites without forcing them into a false common shape.

### AC-36 · Refactor claude_client.py to use run_node_with_tempfile
Strength: Strong
Split-Depth: 1
Files: claude_client.py
Depends-On: AC-35

Problem:
generate() inlines the full temp-file → subprocess → cleanup block: it builds tmp_path via tempfile.gettempdir() + uuid4, writes json.dumps(request), calls subprocess.run(["node", str(CLAUDE_CLIENT_JS), str(tmp_path)], capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S), catches subprocess.TimeoutExpired to raise ClaudeClientError, and unlinks in a finally. This is the same mechanical pattern as model_stats_client.py and should be delegated to the shared utility.

Solution:
Replace the inline block (from tmp_path = Path(tempfile.gettempdir()) … through the finally: tmp_path.unlink() / except OSError: pass) with: result = run_node_with_tempfile(CLAUDE_CLIENT_JS, [], request, SUBPROCESS_TIMEOUT_S, text=True). Keep the surrounding logic exactly as-is: the try/except subprocess.TimeoutExpired → raise ClaudeClientError(...) from e wrapper, the if result.returncode != 0: raise ClaudeClientError(...) check, and the json.loads(result.stdout) parsing. Remove imports that are now unused in this file: tempfile and uuid (neither is referenced anywhere else in claude_client.py). Keep json (still used for json.dumps in payload construction and json.loads for result parsing), os (used for os.environ.get), subprocess (still needed for the TimeoutExpired exception type in the except clause), and Path (still used for CLAUDE_CLIENT_JS). Add from node_client_util import run_node_with_tempfile.

Benefits:
claude_client.py is shorter and no longer owns the temp-file lifecycle; the ClaudeClientError contract (timeout, non-zero exit, bad JSON) stays in the caller where it belongs; removing tempfile and uuid imports reduces the module's surface area for accidental misuse.

### AC-37 · Refactor model_stats_client.py _run_event to use run_node_with_tempfile
Strength: Strong
Split-Depth: 1
Files: model_stats_client.py
Depends-On: AC-35

Problem:
_run_event() inlines the same temp-file → subprocess → cleanup block: it builds tmp_path via tempfile.gettempdir() + uuid4, writes json.dumps(payload), calls subprocess.run(["node", str(MODEL_STATS_DB_JS), event, str(tmp_path)], capture_output=True, timeout=15), catches (OSError, subprocess.SubprocessError) to log a warning, and unlinks in a finally. This duplicates the pattern in claude_client.py. Note: get_turns_summary() does NOT use a temp file (it passes no payload, just a subcommand) and must be left untouched.

Solution:
Replace the inline block in _run_event() (from tmp_path = Path(tempfile.gettempdir()) … through the finally: tmp_path.unlink() / except OSError: pass) with: run_node_with_tempfile(MODEL_STATS_DB_JS, [event], payload, 15). Keep the surrounding try/except (OSError, subprocess.SubprocessError) → logger.warning(...) wrapper in _run_event() itself. Remove the tempfile import (it is used only in _run_event). Keep subprocess (still used in get_turns_summary), uuid (still used in record_call for call_id), json (still used in get_turns_summary for json.loads), and Path (still used for MODEL_STATS_DB_JS). Add from node_client_util import run_node_with_tempfile.

Benefits:
model_stats_client.py's _run_event is shorter and no longer owns the temp-file lifecycle; the 'swallow all errors and log a warning' contract stays in the caller; removing the tempfile import reduces surface area; get_turns_summary is untouched since it uses a different (no-payload) invocation pattern.

### AC-38 · Add unit tests for node_client_util.run_node_with_tempfile
Strength: Strong
Split-Depth: 1
Files: test_node_client_util.py
Depends-On: AC-35

Problem:
The new shared function run_node_with_tempfile in node_client_util.py has no test coverage. Both client modules now depend on it for their temp-file lifecycle, so a regression (wrong arg order, missing cleanup, lost encoding) would silently break both Claude and model-stats calls with no unit-level signal.

Solution:
Create test_node_client_util.py in the project's test directory using unittest (the project's existing Python test framework). Write tests that mock subprocess.run (via unittest.mock.patch) and verify: (a) the correct argument list ["node", script_path, *extra_args, temp_path] is passed to subprocess.run; (b) the JSON payload is written to the temp file before the subprocess call (assert file contents via a mock or a real temp dir); (c) on subprocess.TimeoutExpired the exception propagates to the caller (assertRaises); (d) the temp file is unlinked in all paths — success, TimeoutExpired, and a generic OSError from subprocess.run. These tests must import only node_client_util and standard-library modules; they must NOT import claude_client, model_stats_client, or the Flask app.

Benefits:
The shared utility is covered independently of either caller; regressions in the temp-file lifecycle (missing cleanup, wrong arg order, lost encoding) are caught at the unit level before they affect either client module.

### AC-39 · Route handlers directly import model client to satisfy reserve() parameter
Strength: Strong
Files: routes/chat.py, internal_api_client.py, ollama_client.py

Problem:
In routes/chat.py, the api_chat_close, api_chat_message, and api_chat_reserve handlers each execute a local import of ollama_client solely to pass ollama_client.MODEL into internal_api_client.reserve(). This forces the routing layer to know which specific model provider is active and to resolve the model name itself, creating a hidden coupling between the chat routes and the provider configuration module. If the model source changes (different provider, config-driven selection, or a renamed constant), every handler in chat.py that calls reserve() must be updated, even though "which model is active" is a concern of the provider/client layer, not the routing layer. The principle of least knowledge is violated: the route handler should not need to dereference a constant from a sibling client module to fulfill a parameter that the internal API client expects.

Solution:
Move model-name resolution out of the route handlers. Either have internal_api_client.reserve() resolve the model internally by importing or receiving the provider configuration, or inject the active model name into the route context (e.g., via a dependency-injection container or a request-scoped config object) so that reserve() receives it without the handler reaching into ollama_client. In either case, routes/chat.py calls reserve() with a value obtained from the application's configuration layer rather than from a specific client module, and the coupling to ollama_client is eliminated from the routing code.

Benefits:
The routing layer becomes provider-agnostic: swapping ollama for another local model backend, or moving the model name into a central config, requires no changes in routes/chat.py. The reserve() call site reads as a simple API invocation rather than a hidden cross-module lookup. Future refactors of the provider layer (renaming the constant, making it dynamic, supporting multiple models) are contained within the provider/client modules, reducing the blast radius of configuration changes and making the dependency graph between routing and provider layers explicit and auditable.

### AC-40 · Duplicated HTTP error-handling pattern across internal_api_client functions
Strength: Strong
Files: internal_api_client.py

Problem:
internal_api_client.py defines four public functions—get_roots, preempt, reserve, and stream_local_turn—that each independently construct a URL, issue a requests call, catch requests.RequestException, and raise InternalApiError from the caught exception. The try/except/raise block and the URL-construction idiom are repeated across all four functions. This is a missing-locality defect: the error-reporting contract (exception type, message format, whether the HTTP status code is included, whether a log line is emitted) is defined in four places rather than one. If the error format needs to change—for example, to include the response status code, to add structured logging, or to switch to a different exception hierarchy—all four functions must be updated in lockstep, and any missed update silently produces inconsistent error reporting to callers.

Solution:
Introduce a private helper, e.g. `_request(method, url, **kwargs)`, that encapsulates the requests call, the try/except requests.RequestException block, and the raise InternalApiError(...) from e normalization, then returns the raw Response object. The three request-response functions (get_roots, preempt, reserve) reduce to constructing their URL and payload, calling `_request`, and parsing the returned response (e.g. `r.json()`).

For stream_local_turn, the initial POST call (with `stream=True`) shares the same connection-level error contract and can call the same `_request` helper to obtain the open Response object. However, the subsequent iteration over `r.iter_lines()` and the per-chunk `yield` logic are structurally different from the request-response pattern: the function must remain a generator, and mid-stream read errors (broken pipe, timeout during transfer) occur after the helper has returned. Therefore stream_local_turn retains its own try/except around the iteration loop (or wraps it in a small `_iter_stream(r)` generator helper if the mid-stream error contract also needs centralizing), while the initial-connection error handling is unified with the other three functions via `_request`.

In both cases, the public function signatures and the module's public surface remain unchanged; no caller code needs modification.

Benefits:
The initial-connection error-handling contract (exception type, message format, logging) lives in exactly one place for all four functions, making it trivial to audit and to evolve (add status codes, structured logging, circuit-breaker logic) without hunting through four functions. New internal API endpoints added to the module automatically inherit the correct connection-level error behavior by calling `_request`. The three non-streaming functions become shorter and more readable, each focusing on its unique payload/response logic. The streaming function's iteration logic remains explicit and generator-appropriate, avoiding a forced abstraction that would obscure the yield contract. Cross-cutting concerns that apply to all four (connection timeout, retry policy, metrics) are centralized in `_request`; streaming-specific concerns (chunk framing, mid-stream error reporting) stay where they logically belong.

### AC-41 · Resolve model name inside reserve() so callers need not supply it
Strength: Strong
Split-Depth: 1
Files: internal_api_client.py

Problem:
reserve(reservation_id, model, on) requires the caller to pass the model name explicitly. The only callers in routes/chat.py obtain it via a local `import ollama_client` + `ollama_client.MODEL`, which is itself just `os.environ.get("LOCAL_MODEL")`. This forces the routing layer to import the Ollama provider module solely to read an environment variable, coupling routes to a specific provider implementation.

Solution:
Change the `reserve()` signature to `def reserve(reservation_id: str, model: str = None, on: bool = True) -> dict:`. At the top of the function body, add: `if model is None: model = os.environ.get("LOCAL_MODEL")`. The existing `if model:` guard before adding it to the JSON body already handles the falsy case correctly (omits the key), so no further body changes are needed. This makes the model argument truly optional and resolves it from the same env var `ollama_client.MODEL` reads, without importing that module.

Benefits:
Callers can omit the model argument entirely. The routing layer no longer needs to know where the model name originates. The env-var read is co-located with the HTTP call that actually consumes the value, making the dependency explicit in one place rather than scattered across route handlers.

### AC-42 · Remove ollama_client import from chat route handlers
Strength: Strong
Split-Depth: 1
Files: routes/chat.py
Depends-On: AC-41

Problem:
Each of the three handlers that release or acquire a reservation (api_chat_close, api_chat_message, api_chat_reserve) performs a local `import ollama_client` and passes `ollama_client.MODEL` as the second positional argument to `internal_api_client.reserve()`. This is the only reference to `ollama_client` in the routing layer, and it exists purely to supply a value that `reserve()` can now resolve on its own (see the internal_api_client.py change).

Solution:
In each of the three handlers, delete the `import ollama_client` line and change the `reserve()` call to omit the model argument. For example, in api_chat_close the current line `internal_reserve(session_id, ollama_client.MODEL, False)` becomes `internal_reserve(session_id, None, False)` (or equivalently `internal_reserve(session_id, on=False)` using the keyword). Repeat the same two-line edit in api_chat_message and api_chat_reserve. After the edits, confirm no `ollama_client` identifier remains anywhere in routes/chat.py.

Benefits:
The routing layer is now provider-agnostic: it depends only on internal_api_client and the shared app helpers, not on a specific model-client module. Adding or swapping the local model provider later requires no changes to route files.

### AC-43 · Centralize the Node CLI subprocess wrapper into a shared helper
Strength: Strong
Files: claude_client.py, model_stats_client.py

Problem:
Both `claude_client.py` and `model_stats_client.py` independently implement the identical subprocess execution pattern for invoking Node.js CLI scripts: generating a UUID, serializing a JSON payload to a temporary file, constructing and executing a `node` command with a timeout, and cleaning up the temporary file in a `finally` block. This duplication means any change to the execution protocol—such as adding a retry mechanism, altering the payload schema, adjusting timeout defaults, or switching the cleanup strategy—must be made in two places, and a miss in either file silently diverges behavior between the two clients.

Solution:
Extract the mechanical subprocess-execution steps into a single shared utility function (for example, `_run_node_cli(script_path, args, json_payload, timeout)`) placed in a common module that both clients already depend on. Each client then calls this helper, passing only its own script path, argument list, and payload, while the helper owns UUID generation, temp-file I/O, the `subprocess.run` call with timeout, and guaranteed cleanup in a `finally` block.

Benefits:
Eliminates the duplicated logic entirely, so there is exactly one code path for Node CLI invocation. Cross-cutting concerns—structured logging, metrics emission, error-classification, or future migration to a different transport—can be added in a single location. The two client modules become thinner, more readable, and easier to unit-test by mocking one helper rather than two near-identical blocks.

### AC-44 · Decouple client modules from `app.py`'s module-level state
Strength: Strong
Files: claude_client.py, model_stats_client.py, app.py

Problem:
Both `claude_client.py` and `model_stats_client.py` import `CORE_REPO_ROOT` directly from `app.py`, which is the Flask application entry point. This forces any consumer of the client modules to import the entire Flask app—its routes, its extensions, its configuration side-effects—just to obtain a filesystem path. It makes the clients untestable in isolation (a unit test must boot or at least import the Flask app), and it inverts the natural dependency direction: low-level utility modules should not depend on the web-application layer.

Solution:
Move the `CORE_REPO_ROOT` resolution logic (the `os.path` / `pathlib` computation that determines the repository root) into a small, dependency-free module such as `paths.py` or `config.py`. Both `app.py` and the two client modules then import `CORE_REPO_ROOT` from that shared module instead of from `app.py`. The Flask app retains its role as the HTTP entry point, while the path constant lives in a layer that has no web-framework dependency.

Benefits:
The client modules become importable and testable without pulling in Flask, its extensions, or any route registration. The dependency graph becomes acyclic and directionally correct (clients → shared config; app → shared config), which simplifies refactoring, static analysis, and future extraction of the clients into a standalone package. It also removes a hidden coupling that would break if `app.py` were ever renamed, split, or replaced by a different web framework.

### AC-45 · Duplicated atomic-write protocol between `write_snapshot` and `_atomic_write`
Strength: Strong
Files: (module containing `write_snapshot` and `_atomic_write`; test file exercising `_write_raw_sample_once`)

Problem:
`write_snapshot` and `_atomic_write` implement the identical atomic-write protocol line-for-line: `os.makedirs` → `os.open(tmp, …O_EXCL, 0o600)` → `json.dump` → `os.replace` → `unlink`-on-failure. The only difference in the *write* portion is the object handed to `json.dump`—`write_snapshot` builds it inline (`{**rec, "observedAt": int(time.time())}`) while `_atomic_write` receives a pre-built `obj`. This means any protocol-level change (adding `os.fsync(fd)` before `os.replace` for crash-durability, switching the tmp suffix from `.{pid}` to a UUID for multi-process safety, adjusting the `O_EXCL` flag, or adding a `"schema_version"` key) must be made in two places in the same file, and a missed copy silently leaves one path with the old, weaker guarantee.

Solution:
Keep `write_snapshot`'s pre-write idempotency check (read the existing file, compare `contextWindowSize` and `model`, return `False` early if unchanged) in place. Replace only the duplicated *write* portion—the `os.makedirs` → `os.open` → `json.dump` → `os.replace` → `unlink`-on-failure block—with a single call to `_atomic_write`. Concretely, the tail of `write_snapshot` (after the diff-check and the `return False` early-exit) becomes:

```python
return _atomic_write(directory, rec["sessionId"] + ".json",
                     {**rec, "observedAt": int(time.time())})
```

This eliminates the ~12 lines of duplicated protocol code while preserving `write_snapshot`'s full observable contract: it still returns `False` (without touching the filesystem) when the window size and model are unchanged, and it still performs the atomic write and returns `True` (or the path, matching `_atomic_write`'s return) when a change is detected. `_atomic_write` becomes the single owner of the write protocol; `write_snapshot` remains a thin adapter that adds the snapshot-specific diff-check and payload construction.

Benefits:
A single implementation of the atomic-write protocol means any future durability, concurrency, or schema change is made in exactly one place. The subtle divergence risk (one path gets a new key or flag, the other doesn't) is eliminated. The module's public surface and test coverage remain unchanged—`write_snapshot` still accepts the same arguments, still returns `False` on a no-op (as asserted by `test_writes_only_when_window_or_model_changed`), and still produces the same on-disk result on a real write—so no caller or test needs modification.

### AC-46 · Centralize the Node subprocess invocation pattern in a shared helper
Strength: Strong
Files: claude_client.py, model_stats_client.py

Problem:
Both `claude_client.py` and `model_stats_client.py` independently implement the identical "write JSON payload to a temp file, execute `node <script> <file>`, and clean up the temp file" pattern. Each module duplicates the `tempfile` creation, `json.dumps` serialization, `subprocess.run` configuration (including timeout and argument assembly), and the `finally`-block cleanup of the temporary file. This duplication means any change to the invocation contract—adding a timeout, switching to stdin instead of a file, adjusting environment variables—must be made in two places, and the two copies can silently drift apart.

Solution:
Introduce a single shared utility function (e.g., `run_node_cli(script_path, args, payload)`) in a new `node_bridge.py` module (or within `app.py` if a separate module is undesirable). This function owns the full temp-file lifecycle: it serializes the payload, writes it to a temp file, invokes the Node script via `subprocess.run` with a configurable timeout, captures and parses stdout, and guarantees temp-file cleanup in a `finally` block. Both `claude_client.py` and `model_stats_client.py` are refactored to call this helper instead of inlining the pattern.

Benefits:
Eliminates duplicated boilerplate across the two modules, ensures consistent timeout, environment, and cleanup behavior for every Node bridge call, and provides a single choke point where cross-cutting concerns—structured logging, metrics emission, or future transport changes—can be added once and apply everywhere.

### AC-47 · Unify the error-handling contract for Node subprocess failures
Strength: Strong
Files: claude_client.py, model_stats_client.py

Problem:
`claude_client.py` raises a custom `ClaudeClientError` with distinct messages for timeouts and non-zero exits, while `model_stats_client.py` silently swallows all errors (logging only a warning) so that stats recording never breaks the main feature. The two modules thus present fundamentally different failure semantics for the same underlying operation. The deliberate "swallow" choice in `model_stats_client.py` is implicit—there is no shared location documenting why it differs from `claude_client.py`—making it easy for a future contributor to "fix" the inconsistency by propagating the error and inadvertently breaking the stats path, or to miss a real subprocess failure because the logging level is insufficient.

Solution:
Define a standardized `NodeBridgeError` in the shared `node_bridge.py` helper (from AC-001) that is raised for every failure mode: timeout, non-zero exit, and JSON parse error on stdout. `claude_client.py` catches `NodeBridgeError` and re-raises it as `ClaudeClientError`, preserving its current caller-facing contract. `model_stats_client.py` catches `NodeBridgeError`, logs a warning with the original message, and returns a safe default. The intentional divergence is now explicit, localized to the two call-sites, and the shared helper guarantees that no failure mode can slip through unhandled.

Benefits:
Provides a single, auditable point for Node subprocess error handling. Makes the deliberate choice to swallow errors in `model_stats_client.py` explicit rather than implicit, reducing the risk of accidental error propagation or inconsistent logging. Any new consumer of the Node bridge inherits a well-defined error type and must consciously choose its propagation strategy, preventing future silent-failure regressions.

### AC-48 · Decouple save_proposal from the session object
Strength: Worth exploring
Files: template_store.py

Problem:
The `save_proposal` function in `template_store.py` accepts `session`, `entry_index`, and `tpl_index` to locate and mutate a specific proposal within the nested session structure. This requires `template_store` to understand the internal layout of the session object (specifically the path `session["transcript"][entry_index]["templates"][tpl_index]`). While the function does not persist the session (the caller handles that), it is tightly coupled to the session's structure for the mutation step. If the session layout changes (e.g., `transcript` is renamed or `templates` is moved), `save_proposal` will break. The multi-index access pattern is also prone to `IndexError` or `KeyError` if indices are stale or incorrect, and the function’s responsibility is mixed: it both navigates the session structure and updates the proposal.

Solution:
Change `save_proposal` to accept the `proposal` dictionary directly instead of the `session` and indices. The caller (the route handler) should extract the proposal from the session using the indices it already has from the UI context, then pass the proposal object to `save_proposal`. The function updates the proposal's status and message fields in place and returns the updated proposal. This decouples `template_store` from the session's internal structure and shifts the index-lookup responsibility to the caller, which already has the indices and is responsible for session persistence.

Benefits:
The `template_store` module becomes more modular and easier to unit-test in isolation, since a test can construct a minimal proposal dict without fabricating an entire session tree. The routing layer retains sole responsibility for session structure navigation and persistence, which is its domain. The function signature is cleaner, and the index-lookup logic is centralized in the caller, reducing the risk of `template_store` breaking due to session layout changes.

### AC-49 · Replace check-then-add with an atomic save in save_template
Strength: Worth exploring
Files: template_store.py

Problem:
The `save_template` function performs a two-step process: it first issues a `GET`-like request (`action: "get"`) to check whether the key already exists, then issues a `POST` request (`action: "add"`) to create it. Between these two round-trips there is a race window in which another process could insert the same key, causing the subsequent `add` to fail or silently overwrite. The pattern also doubles the network overhead for what is logically a single operation. The "already exists" handling is in the first interaction (the `get` call), but the overall flow is not atomic.

Solution:
If the target `/api/entry` endpoint supports an atomic upsert or `add_if_not_exists` action, replace the two-step sequence with that single call. If the API does not offer such an action, the current two-step flow should be explicitly documented as a known limitation with a TODO referencing the API gap. The draft’s proposed alternative of relying on a 409 Conflict error from the `add` call is not recommended because the `add` endpoint may not return a 409 for "already exists"—it may return a different error or succeed with an overwrite (which the current code explicitly prevents).

Benefits:
Eliminates the race condition window, reducing the chance of a spurious failure under concurrent writes. Cuts one network round-trip from the happy path, lowering latency. Consolidates the "already exists" decision into a single atomic operation, making the function easier to reason about and test.

### AC-50 · Extract duplicated SSE event parsing into a shared test helper
Strength: Worth exploring
Files: test_chat_routes.py

Problem:
Multiple test methods in `test_chat_routes.py` (e.g. `test_a_crashed_turn_streams_an_error_event_and_persists_an_error_entry`, `test_classified_claude_error_gets_swapoffer_and_a_0600_sample`) each manually split the response body, strip the `data: ` SSE prefix, and `json.loads` each line to extract events. This parsing logic is copy-pasted across several tests. While the logic is trivial and identical in all copies, a change to the SSE wire format (e.g. adding an `event:` field or changing the delimiter) would require touching every test individually. Centralizing the parsing logic reduces the risk of divergence and makes the tests easier to maintain.

Solution:
Introduce a small helper function (e.g. `parse_sse_events(response) -> list[dict]`) at the top of `test_chat_routes.py`. The helper accepts the raw response object, splits on the SSE line delimiter, strips the `data: ` prefix, deserialises each payload, and returns a list of event dictionaries. Every test that currently inlines this logic is refactored to call the helper. Since the tests are in a single file, a shared test-utility module is not necessary.

Benefits:
Removes the duplicated parsing code, making each test body shorter and focused on its actual assertion. A future change to the SSE format requires updating exactly one function rather than N test methods. Reduces the risk of a test passing while inadvertently skipping or mis-parsing an event due to a subtle copy-paste divergence in the inline logic.

### AC-51 · Duplicated Node.js subprocess and temp-file lifecycle logic
Strength: Strong
Files: claude_client.py, model_stats_client.py

Problem:
Both `claude_client.py` and `model_stats_client.py` independently implement the same low-level pattern for invoking Node.js scripts: generating a UUID-based temporary file, serializing a JSON payload into it, calling `subprocess.run` with a timeout, and attempting to unlink the file in a `finally` block. Because the mechanics are copy-pasted rather than shared, any change to the timeout strategy, the temp-file naming scheme, the cleanup path, or the subprocess arguments must be manually replicated in both modules. This duplication increases the surface area for subtle divergence—for example, one module might tighten its timeout or add a retry while the other is left behind—making the two integration points harder to audit and more likely to drift out of sync over time.

Solution:
Extract the shared subprocess-and-temp-file mechanics into a single utility function (e.g., `run_node_script(script_path, payload, timeout)`) that encapsulates UUID temp-file creation, JSON serialization, `subprocess.run` invocation with the given timeout, and guaranteed cleanup in a `finally` block. The utility should return the raw subprocess result (return code, stdout, stderr) without performing any error handling or exception raising. Both `claude_client.py` and `model_stats_client.py` would then delegate to this one utility for the execution mechanics, while retaining their own distinct logic for interpreting the result: `claude_client.py` would raise `ClaudeClientError` with specific stderr text on failure, and `model_stats_client.py` would log and return `None` on failure. This preserves the distinct operational contracts of each module while eliminating the duplicated low-level code.

Benefits:
Eliminates the duplicated file-lifecycle and process-execution code, ensuring that timeout handling, temp-file cleanup, and subprocess invocation behave identically across all Node.js integrations. Future changes to the execution strategy (e.g., switching to `asyncio.create_subprocess_exec`, adding a retry loop, or changing the temp-file directory) require editing a single function rather than hunting for every copy, reducing the risk of one integration silently diverging from the others. The distinct error-handling contracts of each module remain intact and explicit, as the utility does not impose a unified error policy.

### AC-52 · Inconsistent exception contracts for Node.js subprocess failures
Strength: Strong
Files: claude_client.py, model_stats_client.py

Problem:
The two modules treat subprocess failures in fundamentally different ways. `claude_client.py` raises a custom `ClaudeClientError` with descriptive messages for timeouts, non-zero exits, and non-JSON output, surfacing the failure to the caller. `model_stats_client.py`, by contrast, catches the same class of failure, logs a warning, and returns `None`, deliberately hiding the error so that a stats-collection hiccup never breaks the surrounding feature. A caller that has interacted with one client and then encounters the other must maintain two separate mental models of what "the Node script failed" means, and a developer debugging a silent `None` return in the stats path has no exception to inspect—only a log line that may have been lost in volume.

Solution:
Refactor the error-handling logic in both modules to be explicit and localized, without introducing a new shared exception type. In `claude_client.py`, ensure that all failure paths (timeout, non-zero exit, unparseable output) consistently raise `ClaudeClientError` with the specific stderr text from the Node script, preserving the module's stated intent to surface the Node script's own error messages directly. In `model_stats_client.py`, ensure that all failure paths consistently log a structured warning and return `None`, preserving its "never break the real feature" contract. This makes the distinct error contracts of each module explicit and easy to audit, without forcing them into a unified exception hierarchy that would contradict their design rationale.

Benefits:
Makes the distinct error-handling contracts of each module explicit and intentional, rather than implicit side-effects of duplicated code. Developers can easily understand and audit why `claude_client.py` fails loudly with specific error messages while `model_stats_client.py` swallows errors and returns `None`. This clarity reduces the risk of accidental divergence or misunderstanding of the modules' behavior, and it preserves the design rationale of each module without introducing unnecessary indirection.

NOTE: Flag 1 was addressed by removing the proposal to introduce `NodeScriptError` and instead preserving `claude_client.py`'s direct use of `ClaudeClientError` with specific stderr text. Flag 2 was addressed by ensuring the `run_node_script` utility does not impose a unified error policy, allowing each module to retain its distinct operational contract.

### AC-53 · Duplicated SSE event-parsing comprehension across test methods
Strength: Worth exploring
Files: test_chat_routes.py

Problem:
The extraction of SSE `data:` lines from a raw HTTP response body is copy-pasted verbatim in two separate test methods (`MessageRouteErrorTest.test_a_crashed_turn_streams_an_error_event_and_persists_an_error_entry` and `SwapOfferErrorTest._post_message`). Both use the identical list comprehension `[json.loads(line[len("data: "):]) for line in body.splitlines() if line.startswith("data: ")]`. Because the logic is not isolated in a shared helper, any future change to the SSE framing convention (e.g., a different prefix, multi-line data payloads, or a `event:` header) would require locating and updating every copy by hand, and a missed copy would silently produce wrong assertions rather than a clear failure.

Solution:
Introduce a small module-level helper in `test_chat_routes.py` (e.g., `def _parse_sse_events(body: str) -> list[dict]:`) that encapsulates the line-splitting, prefix-stripping, and JSON-decoding steps. Replace both inline comprehensions with calls to this helper. If the SSE contract is already defined elsewhere in the project, the helper can also be the single place where framing assumptions are documented.

Benefits:
A single source of truth for SSE parsing in the test suite means a framing change is a one-line edit. New tests that need to decode a streamed response can reuse the helper instead of re-deriving the logic, reducing the chance of a subtle mismatch (e.g., off-by-one in the prefix length) going unnoticed.

### AC-54 · `save_proposal` reaches into a hard-coded session sub-path
Strength: Strong
Files: template_store.py

Problem:
`save_proposal` in `template_store.py` navigates directly into `session["transcript"][entry_index]["templates"][tpl_index]` with no intermediate accessor. The function therefore assumes a very specific nested shape of the chat-session object. If the transcript becomes a different container type, templates are relocated to a sibling key, or the index is out of range, the call raises an unhandled `KeyError` or `IndexError` that propagates to the caller with no context about which structural assumption was violated. The template store is thus tightly coupled to one particular session layout, making it fragile to any refactoring of the session model.

Solution:
Introduce a thin accessor (or a small dataclass / protocol) that the template store depends on instead of the raw session dict. For example, define a `TemplateSlot` protocol with `get_template(entry_index, tpl_index)` and `set_template(entry_index, tpl_index, value)` methods, and have the chat-session object implement it. `save_proposal` then calls through the protocol, and the session object is responsible for validating indices. On failure the accessor should raise `LookupError` (or a subclass thereof) with a descriptive message naming the missing key or out-of-range index—this preserves the existing contract that `test_bad_index_raises` in `test_template_store.py` asserts (`pytest.raises(LookupError)`). The current implementation already catches `IndexError`, `KeyError`, and `TypeError` and re-raises them as `LookupError`; the protocol simply moves that responsibility to the session object so the template store no longer needs to know which sub-path to walk.

Benefits:
The template store no longer breaks when the session's internal representation changes; only the session object's accessor implementation needs updating. Errors remain `LookupError` (satisfying the existing test suite) but become self-describing (naming the missing key or out-of-range index) rather than bare `KeyError`/`IndexError` traces. The protocol also makes the contract explicit and testable in isolation, and it opens the door to alternative session backends (e.g., a database-backed transcript) without touching template-store code.

### AC-55 · Inconsistent timestamp comparison in session ordering
Strength: Strong
Files: (single file containing `secondsSince`, `orderSessions`, `nextSelectionAfterClose`)

Problem:
The file defines two different interpretations of "which timestamp is later." `secondsSince` correctly normalizes timestamps through `Date.parse`, handling ISO strings with arbitrary UTC offsets, `Date` objects, and raw millisecond values. However, `orderSessions`—which `nextSelectionAfterClose` delegates to for selecting the most recent remaining chat after a session close—sorts using raw lexicographic string comparison on the timestamp key. When two ISO strings carry different UTC offsets (e.g. `…T10:00:00+02:00` vs `…T09:00:00Z`), the string comparator and `Date.parse` disagree on ordering, causing `nextSelectionAfterClose` to potentially select the wrong row.

Solution:
Route the sort key in `orderSessions` through the same `Date.parse`-based numeric comparison that `secondsSince` already establishes. Concretely, replace the lexicographic `key(a) < key(b)` comparison with a numeric comparison of `Date.parse(key(a))` versus `Date.parse(key(b))`, falling back to the existing behavior for unparseable values. This gives the file a single, consistent interpretation of timestamp ordering.

Benefits:
`nextSelectionAfterClose` and any other consumer of `orderSessions` will always agree with `secondsSince` on which timestamp is more recent, eliminating a class of wrong-row-selection bugs that surface whenever session timestamps carry mixed UTC offsets. The fix is localized to the sort comparator and introduces no new API surface.

### AC-56 · Centralize the Node CLI temp-file JSON invocation contract
Strength: Strong
Files: claude_client.py, model_stats_client.py, node_utils.py (new)

Problem:
`claude_client.py` and `model_stats_client.py` each independently implement the identical subprocess pattern: generate a UUID, serialize a JSON payload to a temporary file, invoke a `node` script located under `CORE_REPO_ROOT`, and unlink the file in a `finally` block. This duplication means any change to the temp-file lifecycle—adding a `0600` permission to prevent a local user from reading the payload before deletion, switching from a file-path argument to a stdin pipe, or adding structured logging—must be made in two places with no compiler or test to catch a missed update. The two implementations can silently drift, producing inconsistent behavior or a security gap in one path while the other is patched.

Solution:
Extract the shared pattern into a single utility function (e.g. `_run_node_cli(script_path, args, payload)`) placed in a new `node_utils.py` module. The function owns temp-file creation, the `subprocess.run` call, and the `unlink` cleanup. `claude_client.py` and `model_stats_client.py` then call this utility, passing only their specific script path and payload. The security hardening (file permissions, timeout enforcement) and any future protocol change (stdin pipe, structured logging) live in exactly one function.

Benefits:
The temp-file lifecycle is managed in a single location, eliminating the risk of divergent implementations. A protocol change to the Node CLI contract requires editing one function rather than hunting for every call site. Security best practices such as `os.chmod` on the temp file and subprocess timeout enforcement have one canonical home, and the two client modules become thinner, easier to review, and easier to unit-test by mocking a single seam. Placing the utility in a dedicated `node_utils.py` (rather than in `app.py`) keeps it free of Flask application dependencies, consistent with the decoupling goal in AC-002.

### AC-57 · Decouple claude_client.py and model_stats_client.py from app.py's module-level state
Strength: Strong
Files: claude_client.py, model_stats_client.py, app.py, .venv/lib/python3.12/site-packages/flask/config.py

Problem:
Both `claude_client.py` and `model_stats_client.py` import `CORE_REPO_ROOT` directly from `app.py` (`from app import CORE_REPO_ROOT`). This forces the entire Flask application module to be imported—and its module-level side effects (such as `STATE_DIR.mkdir`, Flask app instantiation, and blueprint registration) to execute—before either low-level client can be used. A utility module that merely needs a filesystem path is thereby coupled to the application's initialization sequence, making it impossible to import, test, or reuse these clients in isolation (e.g. in a CLI tool, a test harness, or a different service) without triggering the full Flask app setup.

Solution:
Move the `CORE_REPO_ROOT` resolution logic into a small, dedicated `config.py` module. This module must encapsulate the *complete* resolution chain that currently lives in `app.py`: reading the `AGENT_MANAGER_CORE_REPO_ROOT` environment variable, applying the documented fail-open fallback to the hardcoded path when the variable is unset, and performing any file-existence checks that the current code performs. `config.py` exposes the resolved path as a module-level constant or a lazily-evaluated property. Both `app.py` and the two client modules import from `config.py` rather than one importing from the other. `app.py` still serves as the Flask entry point and blueprint loader, but the clients no longer depend on it for a configuration value.

Benefits:
`claude_client.py` and `model_stats_client.py` become standalone, importable modules with no Flask side effects, simplifying unit tests and enabling reuse outside the Flask process. The dependency graph becomes explicit: the clients depend on configuration, not on the application. It also removes the implicit requirement that `app.py`'s module-level code (directory creation, Flask app instantiation, blueprint registration) must succeed before any client function can be called, reducing the blast radius of a misconfigured environment. Because the full resolution logic (including the fail-open fallback) is preserved in `config.py`, the documented resilience contract is unchanged.

### AC-58 · Make the fail-open CORE_REPO_ROOT fallback visible to operators
Strength: Worth exploring
Files: app.py (or config.py, per AC-002), claude_client.py, model_stats_client.py

Problem:
`app.py` defines `CORE_REPO_ROOT` with a hardcoded fallback path (`/media/model-cache/github/agent-manager`) when the `AGENT_MANAGER_CORE_REPO_ROOT` environment variable is unset. The source code explicitly documents this as an intentional fail-open design: the comment states the fallback exists so "a Chat turn still has SOMETHING to root itself at rather than erroring outright," mirroring the in-tree version's bare `PACKAGE_ROOT` fallback. This resilience contract is correct and must be preserved. However, when the fallback *is* engaged, there is no visible signal to the operator. The system silently runs in a degraded, machine-specific configuration. If the fallback directory does not exist on the current host, the failure surfaces later as an obscure `FileNotFoundError` or a `subprocess` error deep inside `claude_client.py` or `model_stats_client.py`, far from the root cause, and the operator has no prior indication that the configuration was not ideal.

Solution:
Preserve the fail-open fallback exactly as documented. In the `config.py` module (introduced by AC-002), when the environment variable is unset and the hardcoded fallback path is selected, emit a prominent one-time warning log (or structured event) at the point of resolution. The message should clearly state: the variable name that is unset, the fallback path being used, and that the system is operating in a degraded, non-portable configuration. For example: `"WARNING: AGENT_MANAGER_CORE_REPO_ROOT is unset; falling back to /media/model-cache/github/agent-manager. This is a machine-specific path and may not exist on this host."` The log fires once at import/first-use time, not on every call. No exception is raised; the system continues to function per the fail-open contract.

Benefits:
The documented fail-open resilience is fully preserved—the system still degrades gracefully and a Chat turn still has a root path to work with. Operators gain an immediate, self-explanatory signal that the configuration is not ideal, turning a silent, machine-specific assumption into a visible, actionable warning. When the fallback directory does not exist, the operator has already seen the warning and can diagnose the missing directory rather than encountering an opaque `FileNotFoundError` deep in a subprocess call. The error-handling philosophy is aligned: the system does not crash, but it does not hide the degraded state either.

NOTE: Flag #3 was not applied because it explicitly states the issue is "not a problem" and requires no change. Flag #4 was addressed by removing the "or in `app.py`" alternative from AC-001's solution and committing to `node_utils.py`, which is consistent with AC-002's decoupling goal.

### AC-59 · Redundant and divergent token-resolution logic
Strength: Strong
Files: agent_client.py

Problem:
Both `list_all` and `open_events` independently read the token from the `agent_hosts` object and construct the `Authorization` header with identical inline logic. In addition, the module defines a `_headers()` helper that reads the token from `os.environ` rather than from the host object, creating a third, inconsistent code path that is currently unused by the main functions. This duplication means any future change to token handling (e.g., adding a refresh step, switching to a different credential source, or adding a scope parameter) must be applied in two or three places, and the divergent `_headers()` helper is a latent trap for a developer who assumes it is the canonical helper.

Solution:
Extract a single private helper, e.g. `_auth_headers(host)`, that reads the token from the `agent_hosts` object and returns a fully-populated headers dict (or an empty dict when no token is present). Replace the inline token logic in both `list_all` and `open_events` with a call to this helper, and remove the unused `_headers()` function (or, if it is intentionally kept for a different credential source, rename it clearly and document why it differs).

Benefits:
Token handling lives in exactly one place, eliminating the risk of the two call sites drifting apart. Removing the inconsistent, unused helper reduces cognitive load and prevents a future caller from accidentally using the environment-variable path. Any future change to authentication (rotation, multi-tenant tokens, etc.) is a one-line edit.

### AC-60 · Inconsistent error handling and logging between `list_all` and `open_events`
Strength: Strong
Files: agent_client.py

Problem:
`list_all` catches connection and JSON errors, logs a warning that includes the exception class name, and appends a structured `skipped` entry so the caller can see which host failed and why. `open_events`, by contrast, catches `requests.RequestException`, logs nothing, and raises a generic `AgentError` with a static message that omits the host name and the underlying exception type. A failure to connect to the SSE stream is therefore significantly harder to diagnose: the operator sees only a bare "agent error" with no indication of which host was unreachable or whether the failure was a DNS timeout, a TLS handshake error, or a 502 from a proxy.

Solution:
Align `open_events`'s error handling with `list_all`'s pattern: catch the specific exception, log a warning that includes the host identifier and the exception class (and ideally a short message), and then raise `AgentError` with a message that embeds the host name and the original exception's `str()`. Alternatively, if the design intent is for `open_events` to be retryable by the caller, let the original exception propagate (or wrap it in a typed `AgentConnectionError`) so the caller can distinguish transient from permanent failures.

Benefits:
Operators and on-call engineers can immediately identify the failing host and the nature of the failure from the log line, reducing mean-time-to-diagnosis. The caller receives a consistent error contract across both functions, making it safe to write a single error-handling path. Removing the silent context loss eliminates a class of "works in dev, unexplained failure in prod" tickets.

### AC-61 · `save_template` bakes a two-step get-then-add protocol into the save path, entangling the "never overwrite" guarantee with its mechanism
Strength: Worth exploring
Files: template_store.py

Problem:
`save_template` enforces its "never overwrite" contract by issuing two sequential `requests.post` calls to the same `/api/entry` endpoint — first an `action: "get"` to read the current value, then an `action: "add"` to write. This creates two coupled frictions: (a) the atomicity of the no-overwrite guarantee depends on an unstated single-writer assumption, because there is a TOCTOU window between the two HTTP calls during which another actor (a concurrent chat session, a shared entry store, or the app's own "add entry" box) can create the key; and (b) the two-step get-then-add protocol is hard-coded into the function body rather than being a property of the target app's API, so if a future kind's target exposes a single atomic "add-if-absent" endpoint, the module cannot use it without restructuring `save_template`. The "never overwrite" guarantee and the "how to save" mechanism are entangled in one place, and the concurrency assumption is implicit rather than named.

Solution:
Introduce a private helper, e.g. `_save_if_absent(base_url, target, key, value)`, that owns the two-step protocol and returns a normalized outcome (saved / already-exists / refused / unreachable). `save_template` calls this helper instead of inlining the two `requests.post` calls. The public signature of `save_template` and the shape of its result dict are unchanged, so `save_proposal` and the existing tests remain untouched. The helper becomes the single place where the "never overwrite" guarantee is implemented and where the concurrency assumption is documented. If a target app later offers an atomic add-if-absent endpoint, a per-kind flag in the KINDS registry can select the atomic path, and only `_save_if_absent` changes.

Benefits:
The "never overwrite" guarantee is localized to one function, making the concurrency assumption explicit and auditable in a single place rather than scattered across the body of `save_template`. The save mechanism (two-step vs. atomic) becomes swappable per-kind without touching validation, the result-dict contract, or the caller (`save_proposal`). The TOCTOU window still exists (it is a property of the target app's API, not something this module can fully remove), but it is now named and contained, so a future maintainer can see exactly where the assumption lives and add a server-side atomic path when a target app supports one.

### AC-62 · Status-word mapping in `static/sessions-view.js`
Strength: No issue found
Files: static/sessions-view.js

Assessment:
The module defines two status-word helpers: `statusWord(row)` maps the boolean `row.busy` to `'Busy'`/`'Idle'` for chat-session rows, and `terminalStatusWord(status)` maps the string `row.status` through the `STATUS_WORDS` constant to `'Busy'`/`'Waiting'`/`'Idle'` for terminal-session rows. These are **not** two encodings of the same concept that must be kept in sync. They operate on different data shapes (boolean vs. string), different state spaces (two states vs. three), and different domains (chat sessions have no `waiting` state; terminal sessions do). The `waiting` state is a legitimate, domain-specific terminal concept, not an orphan or a synchronization hazard.

Because the two functions serve independent domains with independent vocabularies, there is no single source of truth to unify and no risk of them drifting out of sync. Renaming `Busy` in the chat context is a one-line edit to `statusWord`; renaming it in the terminal context is a one-line edit to `STATUS_WORDS`. A hypothetical global rename across both is a trivial find-and-replace, not an architectural defect.

No change is warranted. The existing separation is appropriate.

### AC-63 · Centralize HTTP error-handling in a single `_request` helper
Strength: Strong
Files: internal_api_client.py

Problem:
The four public functions (`get_roots`, `preempt`, `reserve`, `stream_local_turn`) each independently wrap their `requests` calls in a `try/except requests.RequestException` block, call `raise_for_status()`, and raise `InternalApiError` with a hand-crafted message. This shallow-interface duplication means that any cross-cutting transport concern—retries with back-off, circuit breaking, centralized request logging, or a unified timeout policy—must be added in four separate places, and a change to the error-handling contract (e.g., adding a `request_id` to every raised exception) requires touching every public function. The transport layer is effectively re-implemented per call site rather than being a single, testable seam.

Solution:
Introduce a private `_request(method, url, **kwargs)` helper inside `internal_api_client.py` that performs the `requests` call, wraps it in a single `try/except requests.RequestException`, calls `raise_for_status()`, and raises `InternalApiError` with a consistent message that includes the URL and status code. Each public function then delegates its HTTP call to `_request` and focuses solely on parsing the response payload. For `stream_local_turn`, the helper can expose a `_stream_request` variant that yields the response iterator, keeping the same error-raising contract at the connection-establishment phase while allowing the generator to handle mid-stream errors separately.

Benefits:
Adding retries, a circuit breaker, or structured logging becomes a one-line change in `_request` rather than a four-function refactor. The error contract is uniform and easier to test: a single unit test on `_request` covers the connection-failure and HTTP-error paths for all four public functions. New public endpoints added to the client automatically inherit the same error-handling behavior without copy-paste risk.

### AC-64 · Unify the error-propagation contract between streaming and non-streaming calls
Strength: Strong
Files: internal_api_client.py

Problem:
`get_roots`, `preempt`, and `reserve` signal internal-API failures by raising `InternalApiError`, while `stream_local_turn` catches the same class of `requests.RequestException` and instead yields a `{"type": "error", ...}` dictionary into the generator stream. Consumers of this module therefore face a split-brain contract: for three functions they must `try/except InternalApiError`, but for the fourth they must inspect every yielded dict for an error type. The routing layer that dispatches to these functions must maintain two separate failure-handling paths for what is logically the same "internal API is unreachable or returned an error" condition, and a new caller who is unfamiliar with the module will inevitably miss the yield-based error path.

Solution:
Adopt a single contract: all four functions raise `InternalApiError` for connection and HTTP-status failures (i.e., failures that occur before or at the moment the response is established). For `stream_local_turn`, the helper establishes the connection and raises `InternalApiError` on failure before the generator begins yielding; once the stream is open, mid-stream transport errors can still be surfaced as yielded error dicts (since the caller is already inside a `for` loop), but the initial "cannot reach the service" case follows the same raise-based contract as the other three functions. Document this two-phase contract (raise-before-yield vs. yield-during-stream) in the module docstring.

Benefits:
Callers can wrap every call to the client in a single `try/except InternalApiError` block for the "service is down" case, reducing the routing layer's branching logic. The contract is easier to communicate in code review and onboarding. It also makes it straightforward to add a global retry decorator that operates on the raised-exception path without needing to inspect generator yields.

### AC-65 · Decouple `test_claude_client_account.py` from `app.py` module internals
Strength: Worth exploring
Files: test_claude_client_account.py, app.py

Problem:
The test file imports `app` solely to read the `CORE_REPO_ROOT` constant, and includes a fallback that fabricates a fake `app` module if the import fails. This couples the test to the internal structure of `app.py`—specifically, that `CORE_REPO_ROOT` exists as a module-level attribute at import time. If `app.py` reorders its initialization, moves the constant to a sub-module, or guards it behind a lazy property, the test's fallback silently activates and the test exercises a fake path that may not reflect the real value, masking a genuine regression. The test is no longer independent of application internals, violating the principle that tests should inject their dependencies rather than reach into production module state.

Solution:
Extract `CORE_REPO_ROOT` (or the path it represents) into a small, importable configuration module (e.g., `config.py` or a `paths.py`) that has no side effects and no dependency on the Flask/Django app lifecycle. Both `app.py` and `test_claude_client_account.py` import from that shared module. In the test, if the value must be overridden for a specific scenario, use `unittest.mock.patch` on the configuration module's attribute rather than constructing a fake `app` module. Remove the fallback import logic entirely.

Benefits:
The test no longer depends on `app.py`'s import order or internal structure; it depends only on a stable, side-effect-free configuration module. The fake-module fallback is eliminated, removing a silent-failure mode where a broken import is masked by the fallback. Refactoring `app.py` (splitting it, renaming it, changing its init sequence) can no longer break the test in a non-obvious way.

### AC-66 · Row-filtering predicate duplicated across handoff and chat_sessions
Strength: Worth exploring (conditional — see caveat)
Files: handoff.py, chat_sessions.py, test_chat_routes_swap.py

**Caveat:** `chat_sessions.py` is not included in the files provided for this review. The duplication claim rests on `handoff.py`'s module docstring (which describes the two-skip rule and references the chat-sessions path) and on comments in `test_chat_routes_swap.py`. Before acting on this item, confirm that `chat_sessions._build_local_messages` (or an equivalent function) actually exists and implements the same two-skip logic. The proposal below assumes that verification passes.

Problem:
`handoff._is_plain_row` implements a two-skip rule (drop rows where `"error"` is truthy, drop rows where `"marker" is True`). `handoff.py`'s docstring states that the same rule is applied in the chat-sessions message-construction path, but the rule lives in two separate code paths held together only by that prose comment. There is no shared predicate, no single test that asserts the two filters agree on an arbitrary row, and no import-time link between them. If a third row type (e.g. a `"system"` notice) is introduced and one filter is updated without the other, the handoff transcript and the local-model message construction will silently diverge, producing inconsistent token counts or missing context with no error.

Solution:
Extract the predicate into a single public helper (e.g. `handoff.is_plain_row(row) -> bool`) that `handoff.transcript_tokens` / `build_handoff` call directly. If the chat-sessions module does contain a duplicate of the same two-skip logic, have that function import and call `handoff.is_plain_row` rather than re-implementing the check. Add a unit test (using the existing `unittest` convention, as in `test_chat_routes_swap.py`) that feeds a row containing a novel field through both call sites and asserts they produce the same keep/drop decision, so any future divergence is caught at test time.

Benefits:
The "which rows count as plain content" rule now has exactly one definition. Adding a new row type requires touching one function instead of two, and the agreement test makes any accidental asymmetry a red build rather than a silent runtime divergence. The docstring comment in `handoff.py` can be shortened to point at the shared helper (`"See is_plain_row for the canonical rule"`) rather than restating the rule in prose, reducing the chance that the comment and the code drift apart.

### AC-67 · model_stats_client duplicates its error-swallowing wrapper twice
Strength: Worth exploring
Files: model_stats_client.py

Problem:
Two public functions in `model_stats_client.py`—`_run_event` and `get_turns_summary`—each independently wrap their `subprocess.run` call in its own try/except, with slightly different exception tuples (one includes `json.JSONDecodeError`, the other does not) and each calling `logger.warning` with its own message. A third function, `record_outcome`, delegates to `_run_event` rather than calling `subprocess.run` directly, so it inherits the wrapper rather than duplicating it. The module docstring states the contract—"never let stats recording break the real feature"—but that contract is implemented in two separate try/except blocks. A new event type added later will either copy-paste one of the two patterns (drifting the exception tuple or the log message) or omit an exception type, turning a non-fatal stats failure into an unhandled exception that propagates into the feature code.

Solution:
Introduce a single `_safe_run(cmd, *, timeout, description, cleanup=None)` helper inside `model_stats_client.py` that owns the `subprocess.run` invocation, the full exception tuple (`OSError`, `subprocess.SubprocessError`, `json.JSONDecodeError`), and the `logger.warning` call. The optional `cleanup` parameter (a callable or a temp-file path) lets `_run_event` pass its temp-file removal logic without forcing `get_turns_summary` to deal with a file it never creates. Have `_run_event` and `get_turns_summary` delegate to `_safe_run`, passing only their specific command, description string, and (for `_run_event`) the cleanup callback. `record_outcome` continues to call `_run_event` as before. The "never break the feature" contract then lives in exactly one function.

Benefits:
Adding a new stats event is a one-line call to `_safe_run` with no risk of forgetting an exception type or duplicating cleanup logic. The exception tuple is defined once, so a new failure mode (e.g. a `TimeoutError` variant) is added in one place. Reviewers can verify the "stats never crash the feature" guarantee by reading a single function rather than auditing two near-identical blocks.

### AC-68 · Inconsistent env-var read timing between ollama_client and handoff
Strength: Worth exploring
Files: ollama_client.py, handoff.py

Problem:
`ollama_client.py` captures `MODEL = os.environ.get("LOCAL_MODEL")` at module import time, freezing the value for the process lifetime. `handoff.py`'s `window_tokens` reads `AGENT_MANAGER_CHAT_*_WINDOW_TOKENS` on every call, explicitly documented as "read per call." Both are configuration for the same chat-swap feature and are consumed by the same code paths (the swap route calls `handoff.needs_summary` and passes `ollama_client.MODEL` to the reserve call). An operator or test that changes `LOCAL_MODEL` after import will see `handoff` pick up new window sizes while `ollama_client` still uses the old model name, with no error, no log, and no indication that the two modules disagree.

Solution:
Pick one env-var semantics for the chat-swap feature and apply it consistently. If per-call is chosen (matching `handoff`'s existing documented behavior), change `ollama_client` to read `LOCAL_MODEL` inside `generate()` rather than at module load. If at-import is chosen, change `handoff.window_tokens` to read once at module load and document the reload requirement. Add a `unittest`-based test that (1) sets the env var, (2) imports both modules, (3) changes the env var again, (4) calls `importlib.reload(ollama_client)` to re-execute its module-level code (necessary because `MODEL` is bound at import time and a plain re-import would return the cached module), and (5) asserts that both `ollama_client.MODEL` and `handoff.window_tokens(...)` observe the same updated value, locking the contract in code.

Benefits:
Operators and tests get a single, predictable rule for how configuration changes propagate: either both modules see the new value immediately or both require a process restart. The inconsistency is eliminated at the source, and the agreement test (using `importlib.reload` to force re-evaluation of the import-time binding) makes any future regression—e.g. someone re-introducing a module-level constant in one file—a red build rather than a silent mismatch in production.

### AC-69 · Divergent provider fallback logic across session and swap views
Strength: Moderate
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `sessions-view.js` and `swap-view.js` define a local function named `providerLabel` that maps a provider identifier to a human-readable string. However, they encode different fallback rules for unrecognized providers: `sessions-view.js` defaults to the literal string `'Local'`, while `swap-view.js` defaults to `'Unknown'`. This divergence creates a consistency hazard: if a session with a non-standard provider (e.g., a typo or a newly added provider not yet mapped) is displayed in both the session list and the swap menu, the user will see contradictory labels (`'Local'` vs. `'Unknown'`) for the same underlying data. Additionally, the identical function name in two separate modules creates a naming collision that can confuse developers, particularly when searching for or refactoring the provider-labeling logic, as it is unclear which implementation is the "canonical" one or whether they are intended to be kept in sync.

Solution:
Extract the provider-to-label mapping logic into a single shared utility function (e.g., `getProviderLabel(provider)` in a new `utils.js` or similar module) that defines one consistent fallback rule (e.g., always `'Unknown'` for unrecognized providers, or a configurable default). Both `sessions-view.js` and `swap-view.js` should import and use this shared function. If the two views require different display formats (e.g., one includes an account suffix and the other does not), the shared utility should handle the core provider-to-label mapping, and each view can append its specific context-specific suffixes (like account names) separately. This ensures the core provider label is consistent across the UI while allowing for view-specific formatting.

Benefits:
Ensures consistent user experience by preventing contradictory labels for the same provider across different UI components. Reduces code duplication and eliminates the risk of the two implementations diverging further over time. Clarifies the codebase structure by establishing a single source of truth for provider labeling, making it easier for developers to understand and maintain the logic. Removes the naming collision, improving code readability and reducing the chance of import or reference errors.

NOTE: The critique regarding `chat.js` was not applied because the file was not provided in the inputs, and the corrected draft removes all unverified claims about `chat.js` imports or aliases, focusing instead on the verifiable divergence in fallback logic and naming collision between the two provided files.

### AC-70 · Duplicated atomic-write protocol in `write_snapshot` and `_atomic_write`
Strength: Strong
Files: (single module containing `write_snapshot` and `_atomic_write`; path not confirmed in pre-check)

Problem:
`write_snapshot` and `_atomic_write` each independently implement the same seven-step atomic-write sequence—`os.makedirs`, build a `.{pid}.tmp` path, `os.open` with `O_WRONLY|O_CREAT|O_EXCL` and mode `0o600`, `os.fdopen` + `json.dump`, `os.replace`, and `os.unlink` cleanup on failure. The only differences are the serialized object (`{**rec, "observedAt": …}` vs. a caller-supplied `obj`) and the return value (`True`/`False` vs. the destination path). Because the protocol is copy-pasted rather than shared, any future change to the write protocol—adding an `fsync` before `os.replace`, switching the tmp-name scheme to a UUID to avoid PID-reuse collisions on long-lived systems, adding a retry on `EINTR`, or changing the directory-creation mode—must be made in two places in the same file, and a change to one will silently desynchronize the other.

Solution:
Collapse the duplication by having `write_snapshot` delegate its write step to `_atomic_write`. Concretely, after the existing read-compare guard, `write_snapshot` would call `_atomic_write(directory, rec["sessionId"] + ".json", {**rec, "observedAt": int(time.time())})` and return `True` on success or `False` if the target already existed with identical content (preserving its current short-circuit). `_atomic_write` remains the single implementation of the open-write-replace-cleanup sequence, and `write_snapshot` retains only its domain-specific read-compare-then-decide logic.

Benefits:
The atomic-write protocol lives in exactly one place, so protocol changes (fsync, tmp-naming, retry, permission bits) are a single edit. The read-compare flow in `write_snapshot` is preserved unchanged, so callers see identical behavior. The file shrinks by roughly the length of the duplicated block, and the two functions now have a clear single-responsibility boundary: `_atomic_write` owns the I/O ceremony, `write_snapshot` owns the snapshot-specific policy.

### AC-71 · Duplicated per-host token resolution in agent_client.py
Strength: Strong
Files: agent_client.py

Problem:
In `agent_client.py`, the logic for resolving a per-host authentication token and constructing the `Authorization: Bearer …` header is duplicated across two functions. `list_all()` iterates over `agent_hosts.hosts()`, calls `agent_hosts.read_token(host)` for each host, and manually builds the `headers` dictionary. `open_events()` calls `_resolve_host(host)` and then repeats the same token-reading and header-construction steps. Meanwhile, the module-level `_headers()` helper exists but is never called by either of these multi-host-aware functions, leaving it as dead code and creating a confusing parallel path for header construction.

Solution:
Extract the repeated token-resolution-and-header-construction logic into a single internal helper (e.g., `_host_headers(host)`) that calls `agent_hosts.read_token(host)` and returns the fully formed `headers` dict. Both `list_all()` and `open_events()` would then call this helper instead of duplicating the steps. The existing unused `_headers()` function can either be deleted or repurposed as this shared helper, eliminating the dead code path.

Benefits:
Removes the duplicated token-resolution logic so that the two functions cannot diverge in how they handle missing tokens, token format, or header construction. Cleans up the module by eliminating (or repurposing) the unused `_headers()` function, reducing cognitive load for future maintainers and making the single source of truth for Bearer-header construction explicit.

### AC-72 · Route handler reaches into accounts.py private validation helper
Strength: Strong
Files: routes/accounts.py, accounts.py

Problem:
The `api_chat_accounts_make_live` handler in `routes/accounts.py` calls `accounts._valid_label(label)` to decide whether to return a 400 Bad Request before attempting the state change. This crosses the module boundary: `accounts.py`'s docstring defines its public surface as persistence operations (every function takes a `storage_dir`), and `_valid_label` is an internal helper with no contract guarantee. The route is forced to pre-validate because `accounts.make_live` only raises `KeyError` for unknown labels, so an invalid label currently falls through to a 404 instead of a 400. If the label-validation rules in `accounts.py` ever change (e.g., allowing Unicode, changing max length), the route handler silently diverges and misclassifies errors.

Solution:
Have `accounts.make_live` (and any other public mutation that accepts a label) raise `ValueError` when the label fails validation, in addition to raising `KeyError` for an unknown-but-valid label. The route handler then simply catches `ValueError` → 400 and `KeyError` → 404, removing the need to call `_valid_label` directly. No new public function is added to `accounts.py`; the existing public mutation API gains a more precise exception contract, and the routing layer interprets exceptions rather than reaching into private helpers.

Benefits:
The routing layer no longer depends on a private symbol, so refactoring `accounts.py`'s internals (renaming, splitting, or changing `_valid_label`) cannot silently break the route. Error semantics become self-documenting: the domain module declares *what* is wrong (invalid vs. missing) via exception type, and the routing layer maps that to HTTP status codes. A single source of truth for label validation lives in `accounts.py`, eliminating the risk of the two modules drifting out of sync.

### AC-73 · Consolidate duplicated provider-labeling logic into a shared module
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `static/sessions-view.js` and `static/swap-view.js` export a `providerLabel` function that maps a session's `provider` and `model` fields to a display string, but the two implementations have diverged. `sessions-view.js` checks `row.provider === 'claude'` and returns either `'Claude'` or `'Local'`, treating every non-`'claude'` value as Local. `swap-view.js` checks `session.provider === 'claude'` or `'local'` and returns `'Claude · <model> · <account>'` or `'Local · <model>'`, explicitly distinguishing the `'local'` string from other unknown values. The same conceptual question—"what provider and model is this session on?"—is answered in two places with two different output shapes and two different sets of recognized provider values, creating a shallow-interface duplication that will silently drift as providers are added or renamed.

Solution:
Extract the provider/model identification logic into a single shared module (e.g. `static/provider-label.js`) that exports one `providerLabel(session)` function returning a **structured result object** (e.g. `{ provider: 'claude' | 'local' | <unknown>, model, account }`). The shared module is the single authority on which provider strings are recognized and how they are normalized.

Each view module then imports this function and performs its own **view-specific string formatting** from the structured result:

- `sessions-view.js` composes a short label (e.g. `result.provider === 'claude' ? 'Claude' : 'Local'`).
- `swap-view.js` composes the longer form (e.g. `` `${result.provider} · ${result.model} · ${result.account}` ``).

This keeps the **exported public API of each view module unchanged**—both still export a `providerLabel` that accepts a session object and returns a display **string**, so any downstream caller that imports and renders that string sees the same type and the same (now-corrected) value. What changes is the **internal implementation** of those two view modules: the duplicated branching over raw provider strings is replaced by a call to the shared module plus a thin, view-specific format step.

Benefits:
A single source of truth governs which provider strings are valid and how they are normalized. Adding a new provider (e.g. `'openai'`) requires editing the shared module once; both view modules automatically pick up the new value through the structured result, eliminating the `'local'`-vs-"everything-else-is-Local" discrepancy that already exists. The view-specific formatting (short label vs. long label with account) remains a one-line composition in each view file rather than a re-implemented branch, so the two files can no longer silently disagree about which providers are recognized. Downstream callers of the view modules' `providerLabel` continue to receive a string and need no code changes; the only files that change are the two view modules (internal rewrite) and the new shared module.

### AC-74 · Dead auth helpers shadow duplicated inline header construction
Strength: Strong
Files: agent_client.py, agent_hosts.py

Problem:
`agent_client.py` defines `base_url()` and `_headers()`—both relics of a singular-host era that read environment variables directly. Neither function is called anywhere in the community. Meanwhile, the actual "resolve this host's token and build the Authorization header" logic is copy-pasted inline in two call sites (`list_all()` and `open_events()`), each independently calling `agent_hosts.read_token(host)` and assembling a Bearer header. The dead `_headers()` is actively misleading: a developer adding a new auth header or switching schemes would naturally edit it, only to discover it is unused and that the real logic lives in two separate inline blocks. Updating one inline site without the other silently breaks one code path.

Solution:
Delete both `base_url()` and `_headers()` from `agent_client.py`. Extract the token-to-header step into a single private helper (e.g. `_auth_headers(host)`) that calls `agent_hosts.read_token(host)` and returns the fully-formed header dict. Both `list_all()` and `open_events()` then call `_auth_headers(host)` instead of duplicating the two-line block.

Benefits:
A single canonical site owns the auth-header construction, eliminating the risk of the two call sites drifting apart. Removing the dead functions removes the false affordance that a developer would edit `_headers()` and see no effect. Future changes to the auth scheme (new header, mTLS, per-host scheme) touch exactly one function.

### AC-75 · `_resolve_host` re-implements `agent_hosts.get_host` lookup
Strength: Strong
Files: agent_client.py, agent_hosts.py

Problem:
`agent_hosts.py` already exposes `get_host(name)` which iterates the host list and returns the matching `Host` or `None`. `agent_client._resolve_host(name)` re-does that same iteration by hand. The only added value of `_resolve_host` is the `name is None` → "pick the single host or raise" branch and the `AgentError` wrapping. As written, the named-lookup branch is a duplicate of `agent_hosts.get_host` with a different return convention (raise vs. `None`). If `agent_hosts` later adds host-aliasing, case-folding, or a deprecation warning inside `get_host`, `_resolve_host` will silently diverge because it bypasses that logic.

Solution:
Replace the manual loop inside `_resolve_host` with a call to `agent_hosts.get_host(name)`. If the result is `None`, raise `AgentError` with a descriptive message. Keep the `name is None` auto-select branch (pick the single host or raise) local to `agent_client`, since that is a client-level policy decision rather than a host-list concern.

Benefits:
Host-lookup logic lives in exactly one place (`agent_hosts.get_host`), so any future normalization, aliasing, or deprecation rules are applied uniformly. `agent_client` retains only the policy it uniquely owns (auto-select when no name is given, error wrapping for the caller). The two modules no longer need to be kept in lockstep by convention.

### AC-76 · Minor host-lookup consolidation opportunity
Strength: Minor
Files: agent_client.py, agent_hosts.py

Problem:
`agent_client._resolve_host(name)` and `agent_hosts.get_host(name)` both contain a short `for host in hosts(): if host.name == name` loop. The two functions are **not** the same lookup duplicated: `get_host` handles the single case "find a named host or return `None`," while `_resolve_host` additionally handles the `name=None` single-host convenience and raises a helpful multi-host-ambiguity error. Those extra cases are legitimate HTTP-layer policy and belong in `agent_client`. The only shared logic is the three-line name-match loop, which is idiomatic to repeat in each module that needs it, and the match key (`host.name`) is a stable field on the `Host` namedtuple, so independent drift is unlikely. This is a small consolidation opportunity, not an architectural defect.

Solution (optional, low-priority):
If the team prefers a single source for the name-match, `agent_client._resolve_host` can delegate the `name=<string>` branch to `agent_hosts.get_host(name)` and keep the `name=None` convenience and the `AgentError` raise as a thin wrapper. The `for`-loop in `get_host` remains the one place that implements "match by name." This is a two-line change in `_resolve_host` and is safe to defer.

Benefits:
Marginal: one fewer copy of a three-line pattern to update if the match key ever changes. The error-contract split (`None` vs. `AgentError`) and the `name=None` policy remain where they belong. No behavioral change.

---

*AC-001 from the prior draft has been removed.* Its central claim—that `agent_client._headers()` is dead code "never called anywhere in the community"—is incorrect: the function is invoked by `app.py` on the singular-config path (exercised by `test_wrong_token_is_unreachable_not_an_error`), and in that path the global token is correctly scoped to the single configured host, so the "wrong credential to the wrong host" hazard does not materialise. The proposed solution (delete `_headers()`, re-source the token exclusively through `agent_hosts.read_token`) would break the `app.py` caller without specifying how it would obtain the Authorization header, making the fix internally inconsistent. The residual observation (two modules each call `os.environ.get` for the same variable) is a one-liner idiom that does not rise to a "Strong" finding.

### AC-77 · Centralize the "plain transcript row" predicate across handoff and chat_sessions
Strength: Worth exploring
Files: handoff.py, chat_sessions.py

Problem:
The predicate that identifies a valid, non-error, non-marker transcript row is implemented independently in two modules. `handoff.py` defines `_is_plain_row()` to check for the absence of `error` and `marker` keys. The review notes that `chat_sessions.py` (specifically the logic within `_build_local_messages`) re-implements this same two-skip logic inline. Because the definition of a "plain row" is scattered, any future change to the transcript schema (for example, introducing a new system-row type that must be skipped) requires a synchronized edit in both files. This violates DRY and creates a tight, implicit coupling between the two modules: a developer updating one side may overlook the other, leading to silent divergence in which rows are processed.

Solution:
Extract the row-filtering logic into a single shared utility function (e.g., `is_plain_transcript_row(row)`) placed in a common module such as `handoff.py` or a new `transcript_utils.py`. Both `handoff.py` and `chat_sessions.py` then import and call this one function to decide which rows to process. The concept of a "plain row" gains a single, named home, and the two call sites become thin consumers of a well-defined contract.

Benefits:
A change to the transcript schema is made in exactly one place, eliminating the risk of the two implementations drifting apart. The named function makes the intent at each call site immediately readable. Maintenance cost drops because there is no longer a second, implicit copy of the skip-logic to keep in sync.

### AC-78 · Decouple model_stats_client from the app entry-point module
Strength: Strong
Files: model_stats_client.py, app.py

Problem:
`model_stats_client.py` performs a module-level import of `CORE_REPO_ROOT` from `app.py` (`from app import CORE_REPO_ROOT`). This inverts the expected layering: a low-level client module depends on the high-level application entry point. This coupling means `model_stats_client.py` cannot be imported, unit-tested, or reused in a different context without pulling in the entire `app` module and its side effects. Furthermore, if `app.py` ever imports `model_stats_client.py` directly or transitively (a common pattern in Flask apps where the app module initializes clients), the result is a circular-import error at startup. Even without a present-day cycle, the dependency direction is inverted and fragile.

Solution:
Move the `CORE_REPO_ROOT` constant into a small, dependency-free configuration module (e.g., `config.py` or `paths.py`). Both `app.py` and `model_stats_client.py` then import `CORE_REPO_ROOT` from that shared module. The client no longer depends on the application entry point, restoring a clean dependency direction (client → config, app → config).

Benefits:
The potential for circular-import hazards is removed. `model_stats_client.py` becomes independently importable and testable without loading the Flask application. The configuration concern is isolated in one place, making it straightforward to override or parameterize in tests.

### AC-79 · Standardize Ollama client error handling to prevent silent or unhandled failures
Strength: Worth exploring
Files: ollama_client.py

Problem:
The `generate()` function in `ollama_client.py` issues an HTTP request via `urllib.request.urlopen` and parses the response with `json.loads`, but does not wrap these calls in explicit exception handling for `urllib.error.URLError`, `json.JSONDecodeError`, or `OSError`. If the Ollama server is unreachable, times out, or returns malformed JSON, the exception propagates uncaught to the caller. In a standard Flask application, this results in a 500 Internal Server Error response, which is a "loud" failure but provides no structured context to the frontend or logging system. More critically, the existing caller code in `chat_sessions.py` (and its test mocks) expects `generate` to return a dictionary containing a `"response"` key. Currently, if an exception is raised, the caller crashes with a `500` error. If the caller is modified to handle errors gracefully, it currently lacks a standardized contract to check against.

Solution:
Wrap the `urlopen` and `json.loads` calls in a `try-except` block that catches `urllib.error.URLError`, `json.JSONDecodeError`, and `OSError`. On failure, log the exception via the `logging` module and return a standardized error dictionary (e.g., `{"error": "Ollama request failed", "details": str(exc)}`). **Crucially, this change must be accompanied by an update to the caller logic in `chat_sessions.py` (specifically `_send_claude` or equivalent) to explicitly check for the `"error"` key before accessing `"response"`.** Without this caller-side update, the new return contract will cause a `KeyError` or `TypeError` when the caller attempts to access `data["response"]` on an error payload.

Benefits:
The client presents a uniform error contract that callers can rely on, matching the defensive posture of `model_stats_client.py`. Logged details make it straightforward to diagnose connectivity or server-side issues. By explicitly updating the caller to handle the `"error"` key, the system degrades gracefully (showing a user-facing message) rather than failing with an opaque 500 error or an unhandled exception.

NOTE: Flag 3 was not applied to AC-001 or AC-002 because the critique acknowledges that the core problem descriptions (duplicated logic in AC-001, inverted dependency in AC-002) are valid refactoring targets even if the specific file contents were not fully provided in the input; the flags regarding "unverifiable" content do not invalidate the architectural issues identified, and the draft's solution remains the correct architectural response to the described coupling.

### AC-80 · Eliminate redundant registry I/O in the "make live" route
Strength: Strong
Files: routes/accounts.py, accounts.py

Problem:
In `routes/accounts.py`, the `api_chat_accounts_make_live` handler calls `accounts.make_live()`, which internally executes `load()` and `_save()`. Immediately following this, the handler calls `_accounts_body()`, which executes `accounts.load()` and `accounts.live_label()` (which in turn calls `load()` again). This results in reading and parsing the `accounts.json` file three times and writing it once for a single logical operation, all within one request cycle.

Solution:
Refactor `accounts.py` to provide a combined `make_live_and_get_view(storage_dir, label)` function that loads the accounts file once, applies the `make_live` mutation, saves the file, and returns the `public_view` list and `live_label` string directly from the in-memory object it just modified. The route handler in `routes/accounts.py` then calls this single function to obtain the view data, and **separately** iterates over the returned `public_view` to attach the per-account `usage` key (calling `usage.read_usage(usage_dir, account["label"], now)`), since `usage_dir` / `AM_STATUSLINE_DIR` is a route-layer concern that `accounts.py` does not import or know about. This keeps the `usage` logic in the view layer where it belongs while eliminating the redundant `load()` calls that were the source of the triple-read.

Benefits:
Reduces disk I/O and JSON parsing overhead by two-thirds for this endpoint (three `load()` calls become one). It also enforces the invariant that the state returned to the client exactly matches the state that was just written to disk, removing the theoretical risk of a race condition where another process modifies the file between the write and the subsequent read. The `usage` attachment remains a pure in-memory step over the already-loaded view, so no additional file I/O is introduced.

### AC-81 · Decouple route handlers from the `app` module to resolve circular import risk
Strength: Worth exploring
Files: routes/accounts.py, app.py

Problem:
`routes/accounts.py` uses lazy imports (`from app import CHAT_STORAGE_DIR` inside the view functions) to avoid a circular import, as `app.py` imports the blueprint from this module. This pattern is a code smell indicating tight coupling between the routing layer and the application entry point. It also makes the code harder to test in isolation, as the tests must mock `app.CHAT_STORAGE_DIR` rather than injecting the dependency at a higher level.

Solution:
Move the `CHAT_STORAGE_DIR` definition out of `app.py` and into a shared configuration module (e.g., `config.py` or `settings.py`) that both `app.py` and `routes/accounts.py` can import at the top level without circularity. Alternatively, pass the storage directory as a dependency to the blueprint or use a context variable, so the route module never needs to reach back into the application entry point.

Benefits:
Removes the need for lazy imports, making the dependency graph explicit and easier to understand. It improves testability by allowing the storage path to be injected or mocked at a higher level (the config module) rather than patching an attribute on the `app` object. It also prevents potential subtle bugs related to import order or module initialization state.

### AC-82 · Eliminate the `local_tool_client` re-export shim
Strength: Worth exploring
Files: local_tool_client.py, app.py, internal_api_client.py

Problem:
`local_tool_client.py` contains no logic of its own: it re-exports `stream_local_turn` from `internal_api_client` and aliases `InternalApiError` as `LocalToolClientError`. Its sole purpose is to preserve the Phase-1 name `stream_plan_with_tools` so that existing callers (e.g. `app.py`'s `_call_chat` and any other module that imports the shim) don't need to change. This creates a confusing two-name surface for one capability: a reader of `app.py` sees `LocalToolClientError` and infers a separate "local tool" subsystem, while the actual HTTP error is raised as `InternalApiError` inside `internal_api_client`. Because the alias is literally the same class (not a subclass), the catch works, but nothing in the code communicates that equivalence, and the "local tool client" name now misrepresents what is a pure HTTP client. The module adds an indirection hop and a misleading name for no behavioral gain, and it couples `app.py`'s error handling to a shim whose entire content is a re-export.

Solution:
1. Grep the repository for every import of `local_tool_client` (e.g. `grep -rn "local_tool_client\|LocalToolClientError\|stream_local_turn" --include="*.py"`) to enumerate all consumers before deleting the module.
2. In `app.py`, change the import to pull `stream_local_turn` and `InternalApiError` directly from `internal_api_client`, and update the `except LocalToolClientError` clause to `except InternalApiError`.
3. In any other file identified in step 1, update the import to source `stream_local_turn` / `InternalApiError` from `internal_api_client` and rename the error reference accordingly.
4. Delete `local_tool_client.py`.

This leaves exactly one name for the error type and one import target for the streaming function across the codebase.

Benefits:
The dependency graph becomes honest: `app.py` and any other consumer import from the module that actually performs the HTTP call. A reader no longer has to trace through a one-line re-export to discover the real source, and the error type has a single, unambiguous name. Future changes to the client (retry logic, new endpoints) live in one file without a second file that must be kept in sync.

### AC-83 · Extract the repeated connection/error-handling block in `internal_api_client`
Strength: Strong
Files: internal_api_client.py

Problem:
`get_roots`, `preempt`, `reserve`, and `stream_local_turn` each repeat the identical pattern: build a `requests` call with the token header and timeout, call `raise_for_status()`, and wrap any `requests.RequestException` into `InternalApiError` (or, for the streaming function, yield an error dict). The connection-level contract—auth header, timeout policy, exception translation—is stated four times. A change to timeout policy, addition of a retry decorator, or a shift in error-shape (e.g. including the HTTP status code in the message) must be made in four spots, and it is easy to miss one, leaving an inconsistent error surface.

Solution:
Introduce a small private helper, e.g. `_request(method, path, *, json_body=None, stream=False)`, that encapsulates the token header, timeout, `raise_for_status()`, and the `except requests.RequestException → InternalApiError` translation. For the streaming path, a companion `_stream_request(path, json_body)` that yields the response iterator and translates errors into the existing error-dict shape. The four public functions then reduce to one-liners that call the helper and shape the return value.

Benefits:
The connection and error-handling invariant lives in exactly one place. A timeout-policy change, a retry wrapper, or an error-shape update is a single edit. New endpoints added to the internal API inherit the correct contract by construction rather than by copy-paste. The public function signatures and the `InternalApiError` type remain unchanged, so no downstream code is affected.

### AC-84 · Distinguish failure origins in `_call_chat`'s 502 response
Strength: Worth exploring
Files: app.py, internal_api_client.py

Problem:
`_call_chat` catches `ClaudeClientError`, `LocalToolClientError` (i.e. `InternalApiError`), and `(TimeoutError, ConnectionError, OSError)` and collapses all of them into `abort(502, description=...)`. The only differentiator is the human-readable string in `description`, which the frontend must pattern-match on to show a meaningful message. A caller hitting a 502 cannot programmatically distinguish "the core dashboard's internal API is down" from "a Claude account call failed" from "a raw socket timeout slipped past `requests`." The `OSError` branch is effectively redundant because `requests` already wraps socket-level failures into `RequestException`→`InternalApiError`, so it only catches edge cases that should not occur in practice.

Solution:
Assign each failure origin a stable machine-readable code (e.g. `"internal_api"`, `"claude"`, `"network"`) and include it as a structured field in the 502 response body alongside the human-readable `description`. Remove the redundant `(TimeoutError, ConnectionError, OSError)` catch since `requests` already translates those into `InternalApiError`. Update the frontend to read the code field for routing its error UI, falling back to the description string for display.

Benefits:
The frontend can branch on a stable identifier rather than parsing English prose, making error handling resilient to wording changes. Operators debugging a 502 can immediately tell which upstream dependency failed without reading logs. The redundant `OSError` catch is removed, simplifying the exception hierarchy the reader must track.

### AC-85 · ~~Asymmetric error handling between streaming and non-streaming paths~~
Strength: Strong

**Status: Withdrawn**

The original draft asserted that `stream_local_turn`'s use of a `{"type": "error", ...}` yield was a silent-failure bug and that callers would "silently miss a network failure." This is incorrect. The module's own docstring for `stream_local_turn` explicitly enumerates `"error"` as a valid yield type (`"chunk"|"final"|"error"`), and `local_tool_client.py` documents the generator contract that `chat_sessions.py` consumes: yield chunks, then a final, or an error dict. The caller is designed to inspect the yielded dictionary and react accordingly. Changing the function to raise `InternalApiError` mid-iteration would violate the documented generator contract, break the existing consumer logic in `chat_sessions.py`, and conflate a recoverable per-turn error with an unrecoverable transport failure. The current design is intentional and correct.

### AC-86 · ~~Redundant pass-through wrapper module adds indirection without value~~
Strength: Strong

**Status: Withdrawn**

The original draft characterized `local_tool_client.py` as a valueless shim that should be deleted. In fact, the module's header states explicitly that it preserves the public name and signature (`local_tool_client.stream_plan_with_tools(...)`) that `chat_sessions.py` and other external callers already invoke, so that "nothing else [needs] to change." It is a deliberate compatibility layer maintaining API stability during the Phase 3 migration. Deleting it would force every external caller to update its import path and function name, which is the opposite of the stability the module provides. The indirection is a feature, not a defect.

### AC-87 · ~~Error handling in the app layer depends on an alias that will vanish with the shim~~
Strength: Strong

**Status: Withdrawn**

This finding was predicated on AC-002's recommendation to delete `local_tool_client.py`. Since that recommendation is withdrawn (see above), the premise that `LocalToolClientError` "will vanish" no longer holds. The alias is a clear, semantic name for the error type in the context of local-tool calls, and `app.py`'s `except (ClaudeClientError, LocalToolClientError)` clause is a valid, explicit dependency on a stable public name. Renaming it to `InternalApiError` would couple the app layer to an internal implementation name and provide no correctness benefit.

---

**Summary:** All three original findings were based on misreadings of the code's design intent. The streaming error-dict contract, the `local_tool_client.py` compatibility layer, and the `LocalToolClientError` alias are each intentional, documented, and load-bearing. No code change is warranted.

### AC-88 · `providerLabel` export-name collision between `sessions-view.js` and `swap-view.js`
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js, static/chat.js

Problem:
Both `sessions-view.js` and `swap-view.js` export a function named `providerLabel`, but the two implementations differ in signature (they accept `row` vs `session`), in output format (the sessions version yields `"Claude · model"`; the swap version yields `"Claude · model · live account"` and also handles an `"Unknown"` provider), and in the "no model" fallback rule. Because `chat.js` is the documented consumer of both modules, it must alias one of the imports (e.g. `import { providerLabel as swapProviderLabel } from './swap-view.js'`), and every reader of `chat.js` must track which `providerLabel` is in scope at each call site. The two functions are not interchangeable even after disambiguation, so the shared name actively misleads rather than merely requiring a rename.

Solution:
Rename the export in `swap-view.js` to a name that reflects its distinct responsibility—e.g. `swapProviderLabel` or `accountLabel`—and update the single import site in `chat.js` accordingly. The function body and its internal logic remain unchanged; only the exported identifier and the one import binding are touched.

Benefits:
The import block in `chat.js` becomes self-documenting: each name at the call site immediately signals which module's formatting rules apply, eliminating the need for an alias and removing a class of copy-paste or shadowing bugs. Future contributors adding a third view module no longer face the temptation (or pressure) to reuse the same generic name, and the two modules' distinct contracts are visible at the module boundary rather than only discoverable by reading both implementations.

### AC-89 · Duplicated filter scaffolding in `filterRows` / `filterChats`
Strength: Worth exploring
Files: static/sessions-view.js, static/swap-view.js

Problem:
The first three lines of `filterRows` and `filterChats` (needle normalisation, empty-query short-circuit, and the inner `has` helper) are textually identical; only the set of fields checked in the predicate differs (`name/cwd/host` vs `title/providerLabel/model`). This is a small amount of repetition in an otherwise well-factored module, but it is the one place where the "same logic, different field list" pattern is likely to grow—e.g. adding a `model` field to the terminal filter or a `cwd` field to the chat filter—and the two copies will drift independently.

Solution:
Extract the shared scaffolding into a small helper, e.g. `filterByFields(rows, query, fields)`, that performs needle normalisation, the empty-query short-circuit, and the `has` check over a caller-supplied field list. `filterRows` and `filterChats` each become a one-line delegation passing their respective field arrays.

Benefits:
Adding a new searchable field to either filter is a one-element change to an array literal rather than a copy of the helper body. The two filter functions can no longer silently diverge in normalisation or short-circuit behaviour, and the shared helper has a single, testable location for edge-case logic (unicode normalisation, case-folding, etc.).

### AC-90 · Inconsistent error-propagation contract between streaming and non-streaming internal-API calls
Strength: Strong
Files: internal_api_client.py, app.py

Problem:
The non-streaming functions in `internal_api_client.py` (`get_roots`, `preempt`, `reserve`) all raise `InternalApiError` on failure, and `app.py` is explicitly designed to catch that exception and return a clean HTTP 502. The streaming function `stream_local_turn`, however, catches `requests.RequestException` and yields a JSON `{"type": "error", ...}` object into the response body instead of raising. This creates a split-brain error contract: a connection failure before the stream begins is reported through the data stream (yielded payload) rather than through the exception path that every other function in the module uses. As a result, `app.py`'s `except InternalApiError` handler never fires for an initial-connection failure in the streaming path, and the client receives a 200 OK response whose body contains an error object rather than the expected 502.

Solution:
Modify `stream_local_turn` in `internal_api_client.py` so that if the initial `requests.post` call or the subsequent `raise_for_status()` fails (i.e., before the generator begins yielding any data), it raises `InternalApiError` just like the non-streaming functions. Only when a failure occurs *during* iteration of `r.iter_lines()` (mid-stream, after the response headers have already been sent) should the function yield the `{"type": "error", ...}` JSON object, since at that point the HTTP status line can no longer be changed. This preserves the ability to report a dropped stream to the client while aligning the "server unreachable" case with the rest of the module.

Benefits:
Callers in `app.py` no longer need to inspect yielded data to determine whether the initial connection failed; the existing `except InternalApiError` block correctly catches initial streaming failures and returns a consistent HTTP 502. The error-handling surface is reduced to two well-defined cases—exception for "cannot reach the provider" and in-stream error object for "provider connection dropped mid-response"—making the contract easier to reason about, test, and document.

### AC-91 · Redundant `LocalToolClientError` alias obscures the true error source
Strength: Worth exploring
Files: local_tool_client.py, app.py, internal_api_client.py

Problem:
`local_tool_client.py` imports `InternalApiError` and re-exports it under the name `LocalToolClientError`. `app.py` then imports `LocalToolClientError` from `local_tool_client` and catches it as though it were a distinct exception type. Because the alias is literally the same class object as `InternalApiError`, and `internal_api_client` is the sole origin of local-provider errors, the indirection adds a second name for the same concept without adding any wrapping, context, or differentiation. It also makes the dependency graph misleading: a reader of `app.py` sees a dependency on `local_tool_client` for error handling, when the actual source of truth is `internal_api_client`.

Solution:
Remove the `as LocalToolClientError` alias in `local_tool_client.py` (or, if external consumers might import the old name, keep it only as a deprecated re-export with a comment). Update `app.py` to import `InternalApiError` directly from `internal_api_client` and catch that name in `_call_chat` and any other relevant handlers. This makes the import graph reflect the actual data flow: `app.py` depends on `internal_api_client` for local-provider error types.

Benefits:
Eliminates a redundant class name that forces readers to mentally resolve the alias to understand which errors are actually being caught. Clarifies the dependency graph so that `app.py`'s imports directly name the module that defines the error. Removes the risk of future divergence if `local_tool_client` were ever refactored to wrap or re-raise errors differently, at which point the alias would silently change semantics for `app.py`'s catch blocks.

### AC-92 · Divergent providerLabel implementations produce contradictory labels for the same session
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `sessions-view.js` and `swap-view.js` export a function named `providerLabel` consumed by the same caller, but they encode different provider taxonomies. The sessions-view version is binary: `provider === 'claude'` yields "Claude" and *everything else* yields "Local". The swap-view version is ternary: `'claude'` → "Claude · …", `'local'` → "Local · …", and *anything else* → "Unknown". A session whose `provider` field is neither `'claude'` nor `'local'` (a new backend, a misconfigured row, a future provider) is therefore labelled "Local" in the session switcher but "Unknown" in the swap menu—two contradictory labels for the same session in the same UI. The name collision also means any future change to display rules (adding a third provider, renaming a label) must be made in two places with the risk of them drifting further apart.

Solution:
Extract a single `providerDisplay(session, { includeAccount })` helper into a small shared module (or into one of the two files with the other importing it) that owns the provider→label mapping, the model-append rule, and the optional account-append rule. Both `formatRow` in sessions-view and the swap-menu code in swap-view call this one helper. The account suffix is only relevant in the swap context, so the helper accepts an `includeAccount` flag (or the swap code post-processes). The "what is a valid provider and what do we call it" decision then lives in exactly one place.

Benefits:
A single source of truth for provider display eliminates the contradictory-label bug for any non-`claude`/non-`local` provider, and ensures that adding a new provider or changing label wording is a one-line change in one file rather than a two-file edit that can silently diverge.

### AC-93 · Duplicate filter logic in sessions-view.js states matching semantics twice
Strength: Worth exploring
Files: static/sessions-view.js

Problem:
`filterRows` and `filterChats` in `sessions-view.js` are structurally identical six-line patterns—trim/lowercase the query, early-return on empty, build a `has` closure, filter—differing only in the list of fields they probe (`name`/`cwd`/`host` vs. `title`/`providerLabel(r)`/`model`). The "how do we match a type-to-filter query" logic (case-insensitivity, whitespace handling, null-safety) is stated twice in the same file. If the matching semantics ever change (adding a fuzzy-match option, changing how `providerLabel` is folded into the search), both copies must be updated in lockstep or the two filters will silently disagree.

Solution:
Introduce a single `filterByFields(rows, query, fields)` helper where `fields` is an array of accessor functions. `filterRows` becomes `filterByFields(rows, query, [r => r.name, r => r.cwd, r => r.host])` and `filterChats` becomes `filterByFields(rows, query, [r => r.title, r => providerLabel(r), r => r.model])`. The shared helper owns the trim/lowercase/empty/null logic exactly once.

Benefits:
Matching semantics (case-folding, whitespace trimming, null-safety) are defined in one place, eliminating the risk of the two filters drifting apart if the matching rules evolve. The code is slightly shorter and the intent of each filter is expressed purely by its field list, making the difference between the two filters immediately visible.

### AC-94 · ~~Centralize the "plain row" predicate to eliminate duplicated skip-logic~~ — **RETRACTED**
Strength: Strong

The original draft claimed `test_chat_routes_swap.py` re-implements the `_is_plain_row` filter via inline list comprehensions. On re-reading the test, it does not. `test_response_has_the_marker_row_and_a_swaps_entry` asserts the *presence* of exactly one marker row (`len(markers) == 1`) and that the pre-swap transcript rows appear verbatim (`body["transcript"][:2] == …`). It never filters rows by the "plain vs. annotated" predicate, so there is no duplicated skip-logic to centralize. The proposed public `is_plain_row` / `get_plain_rows` refactor has no consumer in the test and solves a problem that is not present in the code.

**Action:** No change. The existing private helper in `handoff.py` and the test's presence-assertion are orthogonal concerns; no coupling exists to break.

---

### AC-95 · ~~Decouple model_stats_client from the top-level app module~~ — **RETRACTED**
Strength: Strong

The original draft flagged `from app import CORE_REPO_ROOT` as an inverted dependency and proposed an environment variable or a `paths.py` shim. The module docstring in `model_stats_client.py` explicitly documents *why* the import exists:

> "not vendored – model-stats.db and model-stats-db.js both live in the core repo, so this stays pointed at `AGENT_MANAGER_CORE_REPO_ROOT/src/model-stats-db.js` (via `app.py`'s `CORE_REPO_ROOT`) rather than a relative `__file__` path, which does not hold from this repo's different directory depth."

The path is resolved relative to the **core** repo root, which is a runtime value established by the app bootstrap. A static env-var default or a new `paths.py` module would either (a) duplicate the same resolution logic without eliminating the dependency, or (b) hard-code a path that is wrong at the Chat-plugin repo's directory depth—exactly the failure mode the comment warns about. The circular-import risk cited in the draft is speculative: `model_stats_client` is a leaf in the import graph shown and no reverse import chain was identified.

**Action:** No change. The current `from app import CORE_REPO_ROOT` is the documented, intentional mechanism for cross-repo path resolution. Introducing an indirection layer would add complexity without removing the underlying dependency and risks breaking the path at the plugin repo's depth.

---

### Summary

Both ACs in the original draft were based on misreadings of the provided source. After correction, **no refactoring action is recommended** for the two files under review. The private `_is_plain_row` helper and the `app.CORE_REPO_ROOT` import are each appropriate to their context, and the tests / consumers interact with them in ways that do not create the coupling or dependency problems the draft described.

### AC-96 · `providerLabel` name collision between sibling view helpers
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `static/sessions-view.js` and `static/swap-view.js` export a function named `providerLabel`, yet the two implementations differ in both parameter shape (`row` vs. `session`) and output format (sessions-view produces `"Claude · <model>"` while swap-view produces `"Claude · <model> · <account>"`). Because they live in the same directory, share the same "DOM-free helpers consumed by chat.js" contract, and cross-reference each other in comments, a reader of the single consumer (`chat.js`) must maintain a manual import alias to disambiguate the two, and every call-site becomes a small puzzle: which `providerLabel` is in scope here, and does this code path need the short form or the account-aware form? The shared name also creates a maintenance hazard—any future edit applied to one function (e.g., adding a new field to the label) can be accidentally mirrored onto the other simply because they look like the same helper.

Solution:
Rename one of the two exports to make the distinction self-evident at the import and call-site level. The natural choice is to rename the swap-view export to `swapProviderLabel` (or, if the sessions-view form is considered the "default", rename that one to `rowProviderLabel`). Update the single import in `chat.js` to use the new name directly, removing the alias. Add a one-line comment in each file noting the sibling's export name so the pair remains discoverable without a cross-file search.

Benefits:
The import in `chat.js` becomes unambiguous and self-documenting—no alias, no mental bookkeeping about which `providerLabel` is which. New contributors reading either file immediately see a name that encodes the context (swap vs. session row), reducing the chance that a label-format change intended for one view is accidentally applied to the other. The cross-referencing comments in the two files become less load-bearing, since the names themselves now carry the disambiguation.

### AC-97 · Inconsistent error propagation in local-turn streaming path
Strength: Strong
Files: internal_api_client.py, app.py, routes/chat.py

Problem:
The internal API client establishes a clear error-handling contract: `get_roots`, `preempt`, and `reserve` all raise `InternalApiError` (aliased as `LocalToolClientError`) on transport failures, and the `_call_chat` wrapper in `app.py` is purpose-built to catch that exception and translate it into a clean HTTP 502 response. `stream_local_turn`, however, violates this contract by catching `requests.RequestException` and yielding a `{"type": "error", "error": ...}` dictionary into the stream instead of raising. Because the exception never propagates, `_call_chat` cannot intercept it, and the route handler in `routes/chat.py` is forced to manually inspect each yielded dictionary for an `"error"` type to detect transport failures. This conflates in-payload data errors (which are legitimate stream content) with transport-level failures (which should surface as HTTP errors), breaking the unified error-handling pattern the rest of the client relies on.

Solution:
Modify `stream_local_turn` in `internal_api_client.py` so that when a `requests.RequestException` is raised during the initial connection or stream setup, the function raises `InternalApiError` (or `LocalToolClientError`) rather than yielding an error dictionary. The existing `_call_chat` wrapper in `app.py` will then catch the exception and return the standard HTTP 502 response, exactly as it does for the other client methods. In-payload error events that arrive *after* the stream has successfully started can still be yielded as dictionaries, preserving the distinction between transport failures and application-level stream content.

Benefits:
The error-handling contract for the internal API client becomes uniform across all endpoints, eliminating the need for special-case dictionary inspection in `routes/chat.py`. Transport failures during the local-turn stream are reported with the correct HTTP status code (502) and consistent error body, making monitoring, logging, and client-side retry logic simpler and more predictable. The routing layer no longer needs to distinguish between "the stream contained an error object" and "the connection itself failed," reducing cognitive load and the surface area for missed error paths.

### AC-98 · Duplicate provider-labeling logic across view modules
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js, static/chat.js

Problem:
Both `sessions-view.js` and `swap-view.js` export a function named `providerLabel` that performs nearly identical work: checking whether the provider is `'claude'`, checking for a model string, and concatenating them with a middle-dot separator. The only substantive difference is that the `swap-view.js` variant also appends an account string. Because both are exported and consumed by `chat.js`, the rendering layer must import two modules and manage two separate functions that do 90 % of the same job. This violates locality and creates a maintenance hazard: any UI-design change (a different separator, a new provider type, a formatting tweak) requires the developer to remember to update both files in lockstep, and a missed update silently produces inconsistent labels across panels.

Solution:
Extract the shared provider-labeling logic into a single utility module (e.g. `provider-format.js`) that exports one `providerLabel` function accepting the provider, model, and an optional account string. Both `sessions-view.js` and `swap-view.js` (and any future view module) import from that one location. The `chat.js` consumer no longer needs to import two modules just to get a label; it can call the shared function directly or receive already-formatted strings from the views.

Benefits:
A single source of truth for provider display formatting eliminates the risk of the two panels drifting apart after a UI change. Adding a new provider type or changing the separator is a one-line edit in one file. The import graph in `chat.js` simplifies, and future view modules can adopt the same label format without copy-pasting logic.

### AC-99 · Copy-pasted case-insensitive substring filtering in sessions-view
Strength: Strong
Files: static/sessions-view.js

Problem:
`sessions-view.js` contains two functions—`filterRows` (for terminal sessions) and `filterChats` (for chat sessions)—that implement the exact same case-insensitive substring-matching algorithm. The only difference is the set of fields each checks: `filterRows` inspects `name`, `cwd`, and `host`, while `filterChats` inspects `title` and the result of `providerLabel(r)`. The core logic (trimming the query, lowercasing, empty-string short-circuit, substring test) is duplicated verbatim. A bug in the matching algorithm—Unicode normalization, special-character handling, or a future requirement like diacritic-insensitive search—must be discovered and fixed in two places, and a fix applied to only one will leave the other panel behaving inconsistently.

Solution:
Introduce a single `filterByFields(rows, query, fields)` helper inside `sessions-view.js` (or in a shared utility if other modules need it). The helper trims and lowercases the query, short-circuits on empty input, and returns the subset of rows where at least one of the caller-supplied field accessors produces a string containing the query. `filterRows` becomes `filterByFields(rows, q, [r => r.name, r => r.cwd, r => r.host])` and `filterChats` becomes `filterByFields(chats, q, [r => r.title, r => providerLabel(r)])`.

Benefits:
The matching algorithm lives in exactly one place, so any correctness fix or enhancement (Unicode normalization, fuzzy matching, etc.) is applied uniformly to both panels. Adding a new filterable field to either panel is a one-element change to the field list rather than a copy-paste of the entire loop. The two public functions become thin, readable one-liners that make the intent obvious.

### AC-100 · Duplicated and Divergent Provider Labeling Logic
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both files export a function named `providerLabel`, but they implement different logic and return different string formats for the same underlying data concepts (provider, model, account). `sessions-view.js` returns `"Claude · <model>"` or `"Local · <model>"` and does not handle accounts, while `swap-view.js` returns `"Claude · <model> · <account>"` or `"Local · <model>"` and explicitly handles the `account` field, distinguishing between `null` (live account) and specific labels. This creates a tight coupling to specific string formats in the UI layer: if the display format needs to change (adding a status indicator, changing the separator, standardizing how "Local" vs "Claude" is presented), a developer must find and update both functions. The core provider-detection logic (`provider === 'claude'`, `provider === 'local'`) is duplicated, violating DRY and creating a risk of divergence where one file adds a new provider type or adjusts its format and the other does not, leading to inconsistent UI labels for the same session depending on which view is active.

Solution:
Extract the provider/model/account labeling logic into a shared utility module (e.g., `static/labels.js` or `static/format.js`) that exports a single `formatProviderLabel(session)` function handling all provider types, model presence, and account details. Both `sessions-view.js` and `swap-view.js` should import and use this shared function. If `sessions-view.js` specifically needs a shorter label without the account segment, it can pass a configuration flag (e.g., `{ includeAccount: false }`) or call a separate `formatShortProviderLabel` exported from the same shared module, ensuring the core provider detection and base string construction remain centralized in one place.

Benefits:
Provider labeling logic lives in a single source of truth, making it easier to maintain and audit. The same session is labeled consistently across different views (session switcher vs. swap menu), eliminating the risk of one view showing a stale or divergent format. UI components depend on a shared formatting utility rather than duplicating business logic about how to interpret provider/model/account data, and adding new providers or changing label formats requires updating only one module.

### AC-101 · Duplicated `providerLabel` with divergent semantics across view modules
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `static/sessions-view.js` and `static/swap-view.js` export a function named `providerLabel` that formats a provider/model pair, but they implement different rules and return different shapes. `sessions-view.js` returns a simple display string (e.g., `"Claude · gpt-4"` or `"Local"`) with no account information, while `swap-view.js` returns a richer string (e.g., `"Claude · gpt-4 · live account"`) and handles account-pinning logic. Both accept a single object argument (`row` / `session`) and extract `provider`, `model`, and (in the swap case) `account` internally.

The practical cost is twofold. First, the core "how do we describe a provider/model pair" logic is duplicated: if the naming convention changes (a new provider type, a different separator, a locale requirement), both files must be updated independently, and a missed update in one produces inconsistent labels across the UI. Second, any consumer that needs both variants (such as a shared `chat.js` orchestrator) must import them under distinct local names (`import { providerLabel as sessionLabel } from './sessions-view.js'`), which is a small but recurring source of confusion and typos, and it obscures the fact that the two functions are supposed to be the same concept.

Solution:
Extract a single shared utility module (e.g., `static/provider-label.js`) that exposes one function with the same single-object signature the existing code already uses:

```js
// static/provider-label.js
export function providerLabel(row) {
  // row: { provider, model, account? }
  const base = formatBase(row.provider, row.model); // e.g. "Claude · gpt-4"
  if (row.account != null && row.account !== "") {
    return `${base} · ${row.account}`;
  }
  return base;
}
```

Because the signature is identical to what both existing exports already accept (a single object with `provider`, `model`, and optionally `account`), the change is a drop-in replacement: each call site that currently writes `providerLabel(row)` or `providerLabel(session)` simply changes its import source from the local module to `./provider-label.js`. No caller needs to destructure or re-shape its argument. The old per-module `providerLabel` exports are removed.

Benefits:
A single source of truth for provider/model display strings means a convention change is made in exactly one place, guaranteeing consistent labels across every view. The need to alias two same-named imports disappears, so any module can `import { providerLabel } from './provider-label.js'` unambiguously. The shared function is trivially unit-testable in isolation, and the view modules become thinner, focusing solely on DOM/rendering concerns rather than string-formatting policy.

### AC-102 · Inconsistent "no account" representation (null vs. empty string) in swap-view
Strength: Worth exploring
Files: static/swap-view.js

Problem:
In `static/swap-view.js`, the concept of "no pinned account" (i.e., use the live/default account) is represented as `null` or `undefined` on `session.account`. The `swapOptions` filter (`o.account !== current`) and the `swapBody` construction (omitting `account` when it is `null`/`undefined`) both treat `null`/`undefined` as the sentinel. However, if `session.account` ever arrives as an empty string `""`—for example from a form field that was cleared but not set to `null`, or from a deserialized API response that uses `""` as a default—the filter becomes `o.account !== ""` (which silently drops any account whose label is empty, an unintended side-effect) and `swapBody` will include `account: ""` in the POST payload, potentially tripping backend validation or creating a phantom "empty-string account" record. The module has no single normalization point that maps all "no account" representations to one canonical value before the filter and body-construction logic runs.

Solution:
Introduce a small normalizer at the top of the `swapOptions` and `swapBody` code paths (or as a shared helper in the same module): `const current = (session.account == null || session.account === "") ? null : session.account;`. Use this normalized `current` value for both the filter comparison and the body-omission check. Additionally, add a brief comment (or a tiny `normalizeAccount` helper) documenting that `null`, `undefined`, and `""` are all treated as "no pinned account," so future contributors understand the contract. If the backend API contract specifies that `account` must be absent (not `""`) when no account is pinned, the normalizer guarantees the client never sends the empty-string variant.

Benefits:
The UI and API contract align regardless of which "no account" representation arrives from the session object, eliminating a class of subtle bugs where an empty-string account leaks into filter logic or POST bodies. The normalization is a one-line change with no behavioral impact on the already-correct `null`/`undefined` paths, so the risk of introducing a regression is minimal. Future code that reads `session.account` can rely on the documented invariant rather than re-deriving the sentinel semantics locally.

### AC-103 · Centralize Internal API error-handling in a single helper
Strength: Strong
Files: internal_api_client.py

Problem:
The three non-streaming public functions in `internal_api_client.py` (`get_roots`, `preempt`, `reserve`) each independently wrap their `requests` call in a `try/except requests.RequestException` block that raises `InternalApiError`. This duplicates the connection-level error-handling contract—exception type, message format, and any status-code checking—across three call sites within the same module. Any future change to the error contract (adding retries, circuit-breaking, or altering the message format) requires editing three separate blocks and risks them drifting out of sync.

Solution:
Introduce a private helper (e.g. `_request(method, url, **kwargs)`) that performs the HTTP call, inspects the response status, and wraps any `requests.RequestException` into `InternalApiError` with a consistent message. Each of the three non-streaming public functions then calls `_request` and focuses exclusively on building its specific payload and parsing the response body. The public signatures remain unchanged, so no caller code is affected.

`stream_local_turn` is deliberately excluded from this helper. It uses `requests.post(..., stream=True)` and iterates over `r.iter_lines()` inside a generator, yielding chunks to the caller. Forcing that flow through a uniform `_request` wrapper would either break the generator contract or require a complex abstraction that obscures the yield semantics. Its initial-connection `try/except` remains in place, co-located with the streaming iteration where it belongs.

Benefits:
The error-handling contract for the three non-streaming endpoints lives in exactly one place, making it trivial to audit and to evolve (retries, timeouts, circuit-breakers) without touching the public functions. The module's public API is unchanged, so downstream callers in `routes/chat.py` and elsewhere require no modification. The streaming function retains an explicit, generator-appropriate error path that does not fight the yield contract.

### AC-104 · Resolve the model name inside `reserve()` to decouple routes from the provider module
Strength: Strong
Files: internal_api_client.py, routes/chat.py

Problem:
Three handlers in `routes/chat.py` (`api_chat_close`, `api_chat_message`, `api_chat_reserve`) each perform a local `import ollama_client` solely to read `ollama_client.MODEL` (which is just `os.environ.get("LOCAL_MODEL")`) and pass it as a positional argument to `internal_api_client.reserve()`. This forces the routing layer to import a specific provider module just to supply an environment variable, coupling route handlers to one particular model-client implementation.

Solution:
Change the `reserve()` signature to `def reserve(reservation_id: str, model: str = None, on: bool = True) -> dict` and, at the top of the body, add `if model is None: model = os.environ.get("LOCAL_MODEL")`. The existing `if model:` guard before serializing into the JSON body already handles the falsy case. Then, in each of the three handlers in `routes/chat.py`, delete the `import ollama_client` line and call `reserve(session_id, on=…)` without the model argument.

Benefits:
The routing layer becomes provider-agnostic: it depends only on `internal_api_client` and shared app helpers, not on a specific model-client module. Swapping or adding a local model provider later requires no changes to route files. The env-var read is co-located with the HTTP call that actually consumes the value, making the dependency explicit in one place rather than scattered across three handlers.

### AC-105 · Provider-Specific Reservation Logic in Generic Routing
Strength: Strong
Files: test_chat_routes_swap.py, internal_api_client.py

Problem:
The `POST /api/chat/<id>/swap` endpoint embeds provider-specific reservation-release logic directly in the generic routing layer. The test `test_leaving_local_while_reserved_releases_the_reservation` in `test_chat_routes_swap.py` asserts that the swap route calls `internal_api_client.reserve(SID, ollama_client.MODEL, False)` when a session leaves the `local` provider. This means the route must import `ollama_client` solely to obtain the model name and must know the positional shape of the `reserve` API, tightly coupling a provider-agnostic routing endpoint to one specific provider's client module.

Solution:
Move the model-name resolution inside `internal_api_client.reserve()` itself. The method should accept the session ID and a boolean flag (e.g., `reserve(SID, release=True)`) and resolve the model name internally via `os.environ.get("LOCAL_MODEL")`, which is already the canonical source for the local model identifier. The swap route then calls `internal_api_client.reserve(SID, release=True)` and no longer imports `ollama_client` or references `ollama_client.MODEL` at all. This keeps the fix within a module that is already part of the current architecture (`internal_api_client.py`) and aligns with the resolution strategy already outlined in AC-41/AC-42.

Benefits:
The routing layer no longer depends on a specific provider's client module or its exported constants. Adding a new provider that manages its own reservations (e.g., a GPU pool or a rate-limited remote service) requires no changes to the swap route—only a new branch inside `internal_api_client.reserve()` or a provider-specific override. The "what model to release" knowledge is co-located with the reservation mechanism that actually uses it, improving locality, testability, and long-term maintainability.

### AC-106 · Duplicated and divergent provider-labeling logic across view modules
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `static/sessions-view.js` and `static/swap-view.js` independently define a `providerLabel` function that maps a `provider` field to a human-readable display string, but they handle the `claude` provider differently: `sessions-view.js` renders `'Claude'` or `'Claude · <model>'`, while `swap-view.js` renders `'Claude · <model> · <account>'` or `'Claude · live account'`. Beyond the formatting divergence, the two implementations also guard against `null`/`undefined` inputs with different patterns (`row &&` vs. `session || {}`), so a change to the backend's data shape requires touching both files independently and keeping them in sync by hand. The split logic creates a tight coupling to each file's assumed data shape and invites subtle inconsistency the moment the provider or account fields evolve.

Solution:
Extract the provider-labeling logic into a single shared utility module (e.g. `static/provider-label.js`) that exports one `providerLabel(session, options)` function. The function should:

1. Normalize the input with a single guard (`const s = session || {}`).
2. Accept an `options` object (defaulting to `{ includeAccount: false }`) that controls whether the `account` field is appended to the display string.
3. Build the display string from whichever of `provider`, `model`, and `account` are present, including `account` **only** when `options.includeAccount` is truthy.

Call sites:

- `static/sessions-view.js` calls `providerLabel(row)` (or `providerLabel(row, { includeAccount: false })`), preserving its existing `'Claude · <model>'` format without an account segment.
- `static/swap-view.js` calls `providerLabel(session, { includeAccount: true })`, preserving its existing `'Claude · <model> · <account>'` format.

Both files import the shared function, replacing their local definitions. The shared module becomes the single source of truth for the base mapping (provider → display name, model suffix), while the `includeAccount` flag lets each view opt in or out of the account segment without duplicating the core logic. The null/undefined guard is written once and applied uniformly.

Benefits:
A future change to how the backend reports provider or model requires editing exactly one file instead of two, eliminating the risk that the two views drift apart. The unified null-guard removes a class of subtle bugs where one view crashes or renders `undefined` while the other silently defaults. The explicit `includeAccount` option makes the formatting contract at each call site self-documenting: a reader of `sessions-view.js` sees that it intentionally omits account, and a reader of `swap-view.js` sees that it intentionally includes it. New view modules that need a provider label can import the same function and pass the appropriate option rather than re-implementing the mapping, keeping the display contract consistent across the application.

### AC-107 · Centralize "Plain Row" Filtering in handoff.py
Strength: Strong
Files: handoff.py, chat_sessions.py

Problem:
The rule for which transcript rows count as "plain" conversational turns (excluding error notices and swap audit markers) is implemented independently in two modules. `handoff.py` defines `_is_plain_row` as a pure, I/O-free helper, and its docstring explicitly notes that it mirrors the logic in `chat_sessions._build_local_messages`. Because `chat_sessions.py` carries its own copy of the same classification logic, the definition of a valid transcript row is split across a pure-logic module and an I/O-heavy session-management module. If the criteria change—for example, a new system-message type that should be excluded from handoff summaries but retained in local context, or the inverse—a developer must remember to update both locations. Divergence between the two implementations would cause the handoff text and the local model context to include or exclude different rows during provider swaps, producing inconsistent model behavior.

Solution:
Refactor `chat_sessions.py` to import and call `handoff._is_plain_row` (or a thin public wrapper such as `handoff.is_plain_row`) inside `_build_local_messages`, removing the duplicated inline filtering. `handoff.py` already owns token-counting and handoff-text construction and is described as a "pure hand-off helper" with no I/O, making it the natural single home for the row-classification domain rule. No new dependency is introduced because `chat_sessions.py` already depends on `handoff.py` for swap operations.

Benefits:
The "plain row" rule now exists in exactly one place, eliminating the risk of silent logic drift between handoff calculation and local message building. Future changes to transcript row types (e.g., introducing "tool" or "system" rows) require a single edit in `handoff.py` and propagate automatically to `chat_sessions.py`. The pure-logic module retains full ownership of the domain rule, while the I/O-heavy session module simply consumes it, improving locality of behavior and reducing the cognitive load on developers touching either file.

### AC-108 · Inconsistent error-handling contract between streaming and non-streaming paths in internal_api_client
Strength: Strong
Files: internal_api_client.py

Problem:
The module exposes both non-streaming helpers (`get_roots`, `preempt`, `reserve`) and a streaming helper (`stream_local_turn`). The non-streaming functions follow a uniform contract: they catch `requests.RequestException` and re-raise a domain-specific `InternalApiError`, so callers need only one `except` clause to detect failure. `stream_local_turn`, however, catches the same exception class and instead yields a dictionary `{"type": "error", ...}` into the iterator. A caller that has already written `try/except InternalApiError` around the non-streaming calls must now also inspect every yielded item for an error payload when consuming the stream, effectively forcing two different failure-detection strategies inside the same module. This splits the module's public error contract in two and makes it easy for a new caller to handle one path correctly and miss the other.

Solution:
Unify the contract by having `stream_local_turn` raise `InternalApiError` (or a dedicated `InternalApiStreamError` subclass) on transport-level failures before the generator begins yielding, rather than encoding the failure as a data item. If the failure can only be detected mid-stream (e.g., the connection drops after the first chunk), the generator should raise the exception at that point rather than yielding an error dict. Callers that need to distinguish "stream ended normally" from "stream failed" can then rely on a single exception type, and the `{"type": "error"}` payload convention can be reserved for application-level errors that are part of the protocol rather than transport failures.

Benefits:
Callers need only one `except` clause regardless of whether they invoke a non-streaming or streaming helper, reducing the chance of a silently swallowed transport error. New contributors can rely on a single, documented error contract for the entire module. It also makes it straightforward to add cross-cutting concerns—retry logic, structured logging, metrics—around a single exception type instead of branching on payload shape.

### AC-109 · Scattered environment-variable resolution for core-repo configuration
Strength: Worth exploring
Files: app.py, internal_api_client.py

Problem:
`app.py` resolves `CORE_REPO_ROOT` by reading `AGENT_MANAGER_CORE_REPO_ROOT` from `os.environ`, while `internal_api_client.py` independently resolves `CORE_DASHBOARD_URL` by reading `AGENT_MANAGER_CORE_DASHBOARD_URL` from `os.environ`. Each file performs its own lookup, defaulting, and (potentially) validation at import time. There is no single place where the set of `AGENT_MANAGER_CORE_*` variables is documented, validated, or given consistent defaulting behaviour. Adding a new cross-cutting concern—such as a startup warning when a variable is unset, a shared default-derivation rule (e.g., dashboard URL derived from repo root), or a test-override mechanism—requires touching every file that reads the environment, and it is easy to miss one.

Solution:
Introduce a small configuration module (e.g., `config.py` or a `settings` dataclass) that reads all `AGENT_MANAGER_CORE_*` variables once at import time, applies defaults and validation in one place, and exposes the resolved values as module-level constants or a frozen dataclass. Both `app.py` and `internal_api_client.py` would then import the resolved values from that single module rather than calling `os.environ` themselves.

Benefits:
Configuration resolution, validation, and defaulting live in one auditable location, making it trivial to add logging, a `--dry-run` check, or a test fixture that monkey-patches a single object. New environment variables can be added in one file without hunting for every `os.environ` call site. It also makes the set of required/optional variables self-documenting, reducing onboarding friction and the risk of a typo in a variable name going unnoticed until runtime.

### AC-110 · Duplicated token resolution and header construction in agent_client.py
Strength: Strong
Files: agent_client.py

Problem:
The logic to resolve the correct Bearer token for a given host and build the `Authorization` header is duplicated across both `list_all()` and `open_events()`. Each function independently calls `agent_hosts.read_token(host)` and conditionally assembles the `headers` dictionary, meaning any change to the authentication mechanism—such as adding a new header, switching token formats, or introducing a fallback—must be replicated in every call site. This duplication is a maintenance hazard: a fix applied to one function can easily be missed in the other, leading to subtle auth mismatches between endpoints.

Solution:
Introduce a private helper function in `agent_client.py` (e.g., `_auth_headers(host)`) that encapsulates the token retrieval via `agent_hosts.read_token(host)` and the conditional construction of the `headers` dictionary. Both `list_all()` and `open_events()` (and any future endpoint functions) call this single helper, eliminating the repeated logic.

Benefits:
Reduces code duplication and ensures that authentication changes are made in exactly one place. Improves readability by abstracting low-level HTTP setup details away from the endpoint-specific logic, and makes it straightforward to add new authenticated endpoints without re-implementing the header boilerplate.

### AC-111 · Duplicated provider-labeling logic across view modules
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both `static/sessions-view.js` and `static/swap-view.js` export a function named `providerLabel` that formats provider/model display strings, but each implementation diverges: the sessions view produces `"Claude · <model>"` or `"Local · <model>"`, while the swap view appends an account segment (`"Claude · <model> · <account>"`). There is no shared abstraction, so any change to provider naming conventions—adding a new provider, altering the separator character, or updating how accounts are surfaced—requires synchronized edits in two separate files with no compile-time or test-time guarantee they stay consistent.

Solution:
Extract a single `formatProviderLabel(provider, model, account)` utility into a common module (e.g., `static/provider-label.js`) that both views import. The utility accepts an optional `account` parameter; when omitted it renders the two-segment form, when present it renders the three-segment form. Both `sessions-view.js` and `swap-view.js` delegate to this one function, eliminating the duplicated and divergent logic.

Benefits:
Provider display rules live in exactly one place, so adding a new provider, changing the separator, or adjusting account-display logic is a single-file edit. The risk of the two views silently drifting apart (e.g., one showing a new provider name and the other not) is eliminated, and a unit test against the shared utility covers both call-sites.

### AC-112 · Implicit host-discriminator contract in SSE row matching
Strength: Worth exploring
Files: static/sessions-view.js

Problem:
In `static/sessions-view.js`, the `applyTerminalEvent` path uses `matchesRow` to decide whether an incoming SSE event upserts or removes a row. The matching key switches between a composite `rowKey` (host + id) and a bare `id` depending on whether the event payload carries a `host` field. This makes `host` the implicit discriminator for row identity, but the contract is undocumented and unenforced: if a `session.updated` event arrives with a `host` that differs from the row's stored host (a rename, a misconfigured relay), the row is treated as a new session and a duplicate appears; conversely, if `host` is simply omitted from an update for a host-aware session, matching falls back to `id` alone and can update the wrong row when the same `id` exists on multiple hosts.

Solution:
Make the identity contract explicit and uniform. Decide whether terminal-session rows are always keyed by `host/id` or always by `id` alone, and enforce that choice in `matchesRow` regardless of whether the event payload happens to include `host`. If host-aware matching is required, add a validation step in the reducer that rejects (or logs and drops) events whose `host`/`id` pair does not correspond to an existing row, and document the expectation that upstream emitters must always include `host` for host-scoped sessions.

Benefits:
The row-identity rule becomes a single, auditable decision rather than an emergent behavior of payload shape. Duplicate-row and wrong-row-update failure modes are either structurally impossible or explicitly logged, reducing debugging time for operators who see inconsistent terminal-session lists after a host rename or a relay misconfiguration.

### AC-113 · Provider-specific token budgeting in a generic handoff module
Strength: Worth exploring
Files: handoff.py

Problem:
The `window_tokens` function in `handoff.py` hardcodes a mapping of provider names ("claude", "local") to specific environment variables (e.g., `AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS`, `AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS`) and their default token counts. The code documents this as an intentional, scoped design: the module is a pure helper with no I/O, and the mapping is treated as configuration data rather than logic. That is a reasonable choice for the current two-provider scope. However, the practical consequence remains that adding a third provider requires editing `handoff.py` to insert a new key, environment-variable name, and default into the map. For a team that anticipates growing the provider set, this creates a small but recurring touch-point in a module that is otherwise provider-agnostic in its logic (it accepts a `provider` string and applies a uniform resolution path).

Solution:
If the provider set is expected to grow, the mapping can be externalised to a registry or a per-provider configuration layer so that `handoff.py` resolves the token budget by looking up the active provider in a shared structure rather than naming each one. This is an optional extensibility improvement, not a correction of a defect; the current inline map is perfectly adequate for two providers.

Benefits:
When a new provider is added, its window-token configuration lives alongside the rest of that provider's settings, and `handoff.py` needs no modification. The change is low-risk and can be deferred until a third provider is actually planned.

### AC-114 · Token estimation heuristic is not centralised
Strength: Worth exploring
Files: handoff.py

Problem:
`handoff.py` defines `estimate_tokens` as a simple `math.ceil(len(text or "") / 4)` heuristic. The function is defined locally in the handoff module rather than in a shared utility. From the provided inputs, `handoff.py` is the only module that contains this heuristic; no other file in the set was shown to duplicate it. The concern is therefore not a confirmed duplication but a *fragility*: if any future module (a context-window guard, a summary trigger, a budget enforcer) needs the same estimate, it will either re-implement the divisor-of-4 heuristic independently or import a private helper from `handoff.py`, both of which make the estimation strategy harder to change in one place.

Solution:
Extract the token-estimation logic into a small shared utility module (e.g., `token_utils.py`) that all callers import. The utility can accept an optional model-family parameter so that a caller can supply a more accurate divisor or delegate to a real tokenizer, while `handoff.py` and any future consumer simply call the same function.

Benefits:
A single, importable source of truth for token estimation means that adjusting the strategy (e.g., switching to a model-specific tokenizer) is a one-line change in one file rather than a search across the codebase for every place the heuristic was re-implemented.

NOTE: Flag 1's core factual point (the design is documented and intentional, not an accidental OCP violation) was applied; the "violation" language was removed and the item reframed as an optional extensibility improvement. Flag 2's point that no duplication is evidenced in the provided inputs was applied; the speculative "likely duplicated in chat_sessions.py / ollama_client.py" claim was removed and the problem restated as a centralisation/fragility concern rather than an asserted duplication.

### AC-115 · Route layer duplicates label validation via private method
Strength: Strong
Files: routes/accounts.py, accounts.py

Problem:
In `api_chat_accounts_make_live`, the route handler calls `accounts._valid_label(label)` — a private method on the accounts module — to decide whether to return a 400 (invalid label) or let `accounts.make_live` raise a `KeyError` for a 404 (unknown label). The `make_live` method itself performs no label-format validation; it only checks membership. This means the route layer must reach into a private helper to replicate business-logic validation, creating a tight coupling: if the label-regex or validation rules in `accounts.py` are updated, the route's pre-check can silently drift out of sync, or the private method can be renamed/removed without the route layer failing at import time.

Solution:
Move the label-format validation into the public API of `accounts.py`. Either have `make_live` raise a `ValueError` for malformed labels (in addition to the existing `KeyError` for unknown ones), or expose a dedicated `accounts.validate_label(label)` public function. The route handler then simply catches `ValueError` → 400 and `KeyError` → 404, with no direct call to any private helper. Unit tests for `accounts.py` can exercise both error paths without spinning up an HTTP layer, and tests in `routes/` can mock `make_live` to raise either exception and assert the correct status code.

Benefits:
The route layer becomes a thin exception-to-HTTP-code mapper with no knowledge of label grammar. The public surface of `accounts.py` is self-contained and independently testable with plain `unittest` (no Flask test client required for the validation logic). Renaming or tightening the label regex in one place is sufficient; there is no second copy of the check to keep in sync.

### AC-116 · Lazy import of CHAT_STORAGE_DIR from app.py to dodge circular dependency
Strength: Worth exploring
Files: routes/accounts.py, app.py

Problem:
The module docstring and inline comments in `routes/accounts.py` explicitly note that `from app import CHAT_STORAGE_DIR` is deferred into the body of each view function because `app.py` imports the blueprint from `routes/accounts.py`, which would otherwise create a circular import at module-load time. This forces every view that needs the storage path to repeat the lazy import, makes the blueprint unimportable in isolation (e.g., in a unit test that does `import routes.accounts` without first importing `app`), and ties the route module's runtime behaviour to the specific entry-point module rather than to a stable configuration surface.

Solution:
Extract `CHAT_STORAGE_DIR` (and any other shared constants) into a standalone `config.py` module that has no imports from `app.py` or `routes/`. Both `app.py` and `routes/accounts.py` import from `config.py` at the top of the module, eliminating the circular dependency entirely. If the value must be set at runtime (e.g., from an environment variable or CLI flag), `config.py` can expose a `configure(...)` function that `app.py` calls once at startup; the route module simply reads `config.CHAT_STORAGE_DIR` at call time without an import-time cycle.

Benefits:
The lazy-import workaround and its explanatory comments disappear, making `routes/accounts.py` importable and unit-testable with plain `unittest` without first importing the full application. The blueprint can be registered in a test app or a different entry point without special-casing the import order. Adding a new route that needs the storage path no longer requires remembering to place the import inside the function body.

### AC-117 · Circular import workaround couples routing layer to application entry point
Strength: Strong
Files: routes/accounts.py, app.py

Problem:
`routes/accounts.py` performs a lazy import of `CHAT_STORAGE_DIR` from `app` inside both `_accounts_body` and the `api_chat_accounts_make_live` handler. The docstring in the file explicitly documents this as a workaround for the circular import that arises because `app.py` imports the module to register its blueprint. As a result, the routing layer is forced to reach into the application entry point to resolve a single configuration path, violating dependency inversion. Any consumer that wants to test or reuse the accounts blueprint in isolation must either stub out `app` or replicate the lazy-import dance, and the import graph acquires a back-edge from a leaf module to the composition root that is invisible to static analysis tools.

Solution:
Extract the storage-directory resolution logic (or the `CHAT_STORAGE_DIR` constant) into a dedicated configuration module such as `config.py`, or inject it through the blueprint's factory function so the value is passed in at registration time. `routes/accounts.py` then imports the configuration directly from `config` (or receives it as a closure parameter) and no longer references `app` at all. The circular import disappears because the dependency now flows one way: `app → config` and `routes/accounts → config`, with no back-edge to the entry point.

Benefits:
The routing module becomes provider-agnostic and independently importable, which simplifies unit testing (no need to mock `app`) and makes the blueprint reusable in alternate entry points or test harnesses. The import graph loses its cycle, so tools like `pylint`, `import-linter`, or IDE navigation no longer flag or mis-order the relationship. Future changes to where the storage directory lives require touching only `config.py` rather than hunting for every lazy-import site in the routing layer.

### AC-118 · "Exactly one live account" invariant enforced in multiple places
Strength: Worth exploring
Files: accounts.py

Problem:
The invariant that exactly one account carries the `live` flag is enforced in at least three separate code paths within `accounts.py`: `_enforce_one_live` (invoked during `load`), the `make_live` function (which manually flips the flag), and the `add` function (which sets `live` on the first account). Each site re-implements the "turn off the others, turn on this one" logic independently. If the invariant ever evolves—say, to allow zero live accounts during a migration window, or to add an audit log entry on transition—every site must be found and updated in lockstep, and a missed site silently produces an inconsistent state.

Solution:
Introduce a single internal helper, e.g. `_ensure_single_live(storage_path, target_id)`, in `accounts.py` that encapsulates the full transition: read all accounts, clear the `live` flag on every record, set it on the target, and write back. `load`, `add`, and `make_live` all delegate to this helper rather than manipulating the flag themselves. The helper becomes the sole mutation point for the `live` field, and any future policy change (logging, validation, atomicity) is made in one place.

Benefits:
The invariant is auditable in a single function, reducing the risk of a future edit breaking the "exactly one" guarantee in one path while leaving others correct. New contributors see one canonical implementation instead of three subtly different ones. The helper is trivially unit-testable in isolation (feed it a temp directory, assert the post-condition), whereas testing the invariant today requires exercising each of the three entry points separately.

### AC-119 · Duplicate provider-labeling logic with divergent semantics
Strength: Strong
Files: static/sessions-view.js, static/swap-view.js, static/chat.js

Problem:
Both `static/sessions-view.js` and `static/swap-view.js` export a function named `providerLabel` that formats provider and model information for display, but their implementations and expected data shapes differ significantly. `sessions-view.js` expects a row object with `provider` and `model` fields and returns a string like `'Claude · <model>'` or `'Local'`. `swap-view.js` expects a session object with `provider`, `model`, and `account` fields and returns a string like `'Claude · <model> · <account>'` or `'Local · <model>'`. This tight coupling means the two modules must be kept in sync manually whenever the provider or model display format changes, and it forces the caller (`chat.js`) to know which version to use in which context, violating the principle of locality and increasing the risk of inconsistent UI rendering.

Solution:
Extract the common provider/model formatting logic into a shared utility module (e.g., `static/provider-label.js`) that accepts a provider, model, and optional account as arguments and returns the formatted string. Both `sessions-view.js` and `swap-view.js` should import and use this shared function, passing the relevant fields from their respective row or session objects. The shared module becomes the single source of truth for how provider and model information is displayed, eliminating the need for callers to track which variant applies where.

Benefits:
A single formatting function guarantees consistent provider/model display across all views, removes the manual synchronization burden between two modules, and frees `chat.js` from having to select between two subtly different implementations. Future changes to the display format (e.g., adding a new provider or changing the separator) require editing exactly one file rather than hunting for every copy of the logic.

### AC-120 · Routing layer hard-coupled to a specific provider module
Strength: Strong
Files: test_chat_routes_swap.py, ollama_client.py, internal_api_client.py

Problem:
`test_chat_routes_swap.py` imports `ollama_client` directly and passes `ollama_client.MODEL` into `internal_api_client.reserve()`. This forces the generic chat-swap routing and its tests to know about a specific provider implementation (Ollama) and its environment variable `LOCAL_MODEL`. The routing layer is no longer provider-agnostic: any future swap of the local-model backend requires editing route handlers and their tests to point at a different client module and attribute.

Solution:
Move the model-name resolution into `internal_api_client.reserve()` itself. The function can read `os.environ.get("LOCAL_MODEL")` internally (or accept `model=None` and resolve it there), so callers no longer need to import a provider module just to supply a model string. Remove the `import ollama_client` and the explicit `ollama_client.MODEL` argument from the route handlers and from `test_chat_routes_swap.py`.

Benefits:
The routing and test layers become provider-agnostic. Adding or swapping the local-model provider later requires no changes to route files or their tests. The environment-variable read is co-located with the HTTP call that actually consumes the model name, making the data flow easier to follow.

### AC-121 · Context-window size contradicts between handoff logic and the Ollama call
Strength: Strong
Files: handoff.py, ollama_client.py

Problem:
`handoff.py` defines `window_tokens()` with a default of **16 384** tokens for the "local" provider (read from `AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS`), and `estimate_tokens()` uses that value to decide whether a summary is needed and to size the handoff text. Meanwhile, `ollama_client.py` hardcodes `"num_ctx": 8192` in its generate options. These are not merely independent assumptions that *might* drift apart — they **actively disagree right now**: the handoff logic sizes text for a 16 384-token window while the actual Ollama call is constrained to 8 192 tokens. Any handoff text that fits within the 16 384 budget but exceeds 8 192 tokens will be silently truncated or rejected by the model, producing a correctness bug in production. There is no shared source of truth tying the two values together.

Solution:
Extract the context-window constant and the token-estimation helpers into a small shared module (e.g. `model_context.py`) that both `handoff.py` and `ollama_client.py` import. `ollama_client.generate()` should read the window size from this shared helper instead of hardcoding `8192`. `handoff.py`'s `estimate_tokens()` and `window_tokens()` should likewise delegate to the shared helper so the value lives in one place. This resolves the active 16 384-vs-8 192 contradiction and prevents future drift.

Benefits:
Fixes an active correctness bug: the handoff logic and the provider call now agree on the context window, so handoff text is no longer sized for a window the model will not honour. A single source of truth means changing the local model's window size or the estimation heuristic requires editing one file rather than hunting for hardcoded values across modules, eliminating the entire class of silent-mismatch bugs where the two modules disagree about available tokens.

NOTE: Flag 3 (AC-001) was not applied because the critique's own re-evaluation confirmed that `test_chat_routes_swap.py` does import `ollama_client` and that the draft's description and solution are accurate; no correction was warranted.

### AC-122 · Fragile exception aliasing obscures the true error hierarchy
Strength: Strong
Files: local_tool_client.py, internal_api_client.py, app.py

Problem:
`local_tool_client.py` is a thin compatibility shim that delegates to `internal_api_client.stream_local_turn()`. To preserve its public interface it aliases `InternalApiError` as `LocalToolClientError` (`from internal_api_client import InternalApiError as LocalToolClientError`). Downstream, `app.py`'s `_call_chat` catches `LocalToolClientError` and `ClaudeClientError` as if they were distinct error families. In reality `LocalToolClientError` *is* `InternalApiError`; the alias creates a false abstraction. If `internal_api_client` later introduces sub-types (e.g., `AuthError`, `TimeoutError`) or restructures its hierarchy, the single-class alias in `local_tool_client` silently breaks the contract, and `app.py` will either miss new error types or catch them under the wrong name. The alias also forces `app.py` to import from two modules to handle what is fundamentally one error type, obscuring the true dependency graph.

Solution:
Remove the alias in `local_tool_client.py`. Either (a) have `local_tool_client.stream_plan_with_tools` catch `InternalApiError` and re-raise it as a genuinely distinct `LocalToolClientError` subclass that carries shim-specific context, or (b) let `InternalApiError` propagate unchanged and update `app.py` to catch `InternalApiError` directly, eliminating `LocalToolClientError` from the public surface. Option (b) is simpler if the shim adds no extra semantics; option (a) is preferable if the shim needs to attach its own diagnostic information. Either way the error hierarchy becomes explicit and the alias is gone.

Benefits:
The error-handling path in `app.py` reflects the actual type relationships, making it straightforward to reason about which exceptions can be raised from which call site. Adding new error sub-types in `internal_api_client` no longer risks silently breaking the shim's contract, because the shim either re-raises the real type or wraps it in a properly derived class. The dependency graph becomes honest: `app.py` depends on `internal_api_client`'s error types directly rather than through an opaque alias, simplifying refactors and onboarding.

### AC-123 · Centralize host token resolution to eliminate duplicated per-call logic
Strength: Strong
Files: agent_client.py, agent_hosts.py

Problem:
The logic to determine which token to send to a specific host is duplicated across `list_all` and `open_events` in `agent_client.py`. Both functions manually check whether a token exists for the resolved host and then construct the `Authorization: Bearer …` header by hand. This violates the DRY principle and creates a tight coupling where `agent_client.py` is simultaneously responsible for HTTP communication and token resolution. Any future change to the header format, token source, or authentication scheme must be applied in every client function independently, increasing the risk of drift.

Solution:
Refactor `agent_hosts.py` to expose a single function, such as `get_headers(host)`, that returns the fully constructed `headers` dictionary (including the `Authorization` header when a token is present, and omitting it otherwise). Update `agent_client.py` so that `list_all`, `open_events`, and any future client functions call this single helper instead of reading the token and building the header inline.

Benefits:
Token resolution logic is localized to the module that already owns host configuration. This reduces the risk of inconsistent header construction across different client functions, simplifies `agent_client.py` by removing its direct dependency on token-resolution details, and gives a single place to audit or extend authentication behavior (e.g., adding a new header or switching to a different scheme).

### AC-124 · Unify host resolution logic between agent_client.py and agent_hosts.py
Strength: Strong
Files: agent_client.py, agent_hosts.py, agent_errors.py (new)

Problem:
`agent_client.py` implements its own `_resolve_host` function to find a host by name or fall back to a single configured host, while `agent_hosts.py` already provides a `get_host` function for name-based lookup. The "default to single host" and "unknown host" error-handling paths are therefore split across two modules. This duplication makes it harder to keep error messages and fallback behavior consistent, and means a change to resolution semantics (e.g., supporting wildcards or a different default strategy) must be made in two places.

Solution:
1. **Extract the shared exception.** Create a small `agent_errors.py` module containing `AgentError` (moved out of `agent_client.py`). Both `agent_client.py` and `agent_hosts.py` import from this new module, eliminating any risk of a circular import.

2. **Centralize resolution in `agent_hosts.py`.** Add a `resolve_host(name=None)` function that encapsulates the full resolution contract: look up by name, fall back to the single configured host when `name` is `None`, and raise `AgentError` (imported from `agent_errors.py`) when no host can be determined.

3. **Simplify `agent_client.py`.** Remove the local `_resolve_host` implementation. All client functions (`list_all`, `open_events`, etc.) call `agent_hosts.resolve_host(name)` instead.

Benefits:
Host resolution logic is centralized in the configuration module, ensuring consistent error handling and eliminating code duplication. Because `AgentError` now lives in a shared leaf module, `agent_hosts.py` can raise it without importing from `agent_client.py`, preserving the existing one-way dependency (`agent_client` → `agent_hosts`) and avoiding a circular import. `agent_client.py` becomes a thinner HTTP layer that delegates all host and token concerns to `agent_hosts.py`, improving separation of concerns.

### AC-125 · Duplicated "plain row" filter logic between handoff and chat_sessions
Strength: Strong
Files: handoff.py, chat_sessions.py

Problem:
`handoff.py` defines `_is_plain_row` whose docstring explicitly states it mirrors the two skip rules in `chat_sessions._build_local_messages` (skip rows with a truthy `error` field, skip rows with `marker: True`). The same predicate is therefore implemented in two modules, and the only link between them is a prose note in a docstring. If the definition of a "plain row" ever changes—a new row type is introduced, the `marker` key is renamed, or an additional skip condition is added—both implementations must be updated in lockstep, and nothing in the type system or test suite enforces that they stay in agreement.

Solution:
Extract the predicate into a single shared helper (e.g. a `is_plain_row(row)` function in a small utility module or in `chat_sessions` itself, imported by `handoff`). Both `handoff.build_handoff` and `chat_sessions._build_local_messages` call the same function, eliminating the duplicated rule and the docstring cross-reference.

Benefits:
A single source of truth for the "plain row" rule removes the risk of the two implementations drifting apart. Future changes to the row schema require editing exactly one function. The docstring cross-reference becomes unnecessary, and a unit test on the shared helper covers both call sites.

### AC-126 · Inconsistent backend-location strategy between model_stats_client and ollama_client
Strength: Worth exploring
Files: model_stats_client.py, ollama_client.py

Problem:
`model_stats_client.py` resolves its backend path (`model-stats-db.js`) at import time by concatenating a `CORE_REPO_ROOT` constant with a relative sub-path, a choice its own docstring flags as a workaround for the repo's directory depth. `ollama_client.py`, in the same community, locates its backend (the Ollama server URL and model name) via environment variables read at import time. The two clients therefore use different mechanisms for the same concern—"where is my backend?"—and the `model_stats_client` approach is brittle to any reorganisation of the repository layout.

Solution:
Adopt a single convention for backend location across the community. The simplest path is to let `model_stats_client.py` read its script path from an environment variable (e.g. `MODEL_STATS_DB_JS`) with a sensible default that still uses `CORE_REPO_ROOT`, mirroring the env-var pattern already used by `ollama_client.py`. Alternatively, introduce a tiny shared configuration module that both clients import, so the "where is my backend" question is answered in one place.

Benefits:
Both clients follow the same configuration-locality convention, making the community easier to reason about and to deploy in different directory layouts. The fragile hard-coded relative path in `model_stats_client.py` becomes overridable without code changes, and the two modules no longer disagree on how to locate their backends.

### AC-127 · Account-registry shape duplicated across two test files
Strength: Worth exploring
Files: test_chat_routes_swap.py, test_chat_sessions_account.py

Problem:
Both `test_chat_routes_swap.py` and `test_chat_sessions_account.py` construct a fake account registry with the same set of keys (`label`, `host`, `tokenFile`, `live`). The swap test does so through an `_add_account` helper that calls `accounts.add(self.storage, label, "example.invalid", FAKE_TOKEN_FILE)`, while the account test builds the registry dict directly via a `_registry` helper. The registry schema is therefore encoded in two test files, and a change to the registry shape (a new required key, a renamed field) would require updating both files independently.

Solution:
Factor the fake-registry construction into a single shared test helper (e.g. a `make_fake_registry(label, host, token_file, live=True)` function in a small `test_helpers.py` or in a shared `conftest`-style module within the test directory). Both test files import and call that helper, so the registry shape lives in exactly one place.

Benefits:
A registry-schema change requires editing one helper instead of two test files. New tests that need a fake registry can reuse the same helper, reducing the chance of a subtly wrong shape being introduced. The test suite becomes easier to maintain as the account model evolves.

### AC-128 · Centralize stream-abort signaling in the SSE client
Strength: Strong
Files: internal_api_client.py, local_tool_client.py, routes/chat.py

Problem:
`stream_local_turn` in `internal_api_client.py` reports every failure—whether a definitive server rejection (401/403) or a mid-stream connection drop—by yielding a plain dictionary `{"type": "error", "error": ...}` into the same iterator that carries valid data chunks. The `local_tool_client.py` shim passes this mixed stream through with `yield from`, so the ultimate consumer in `routes/chat.py` must inspect every yielded item for a `type` field to distinguish data from error. This makes the failure mode implicit in the data stream rather than explicit in the control flow: a caller that forgets to check for the error dict will silently treat it as a content chunk, and there is no single `except` clause where the abort can be handled uniformly. The contract is fragile because it depends on every downstream consumer remembering to filter, and it conflates non-retryable rejections with transient network failures under one undifferentiated signal.

Solution:
Introduce a dedicated `StreamAbortedError` exception in `internal_api_client.py` that carries the same error payload (status code, message, retryability flag). When `r.raise_for_status()` fires or a `ConnectionError` is caught inside the `iter_lines()` loop, raise `StreamAbortedError` instead of yielding an error dict. Update `local_tool_client.py` to let the exception propagate (or re-raise it under its own name if a shim-level alias is desired), and update `routes/chat.py` to wrap the `for` loop in a `try/except StreamAbortedError` block. This converts the implicit data-stream contract into an explicit control-flow signal that the Python type system and exception-handling idioms already support.

Benefits:
Callers get a single, catchable point of failure instead of having to inspect every yielded item; the distinction between a definitive rejection and a transient drop becomes a field on the exception rather than something the caller must infer from the error message string; and the `yield from` pass-through in `local_tool_client.py` no longer needs to be "error-aware," reducing the surface area of the implicit contract.

### AC-129 · Make the error-type dependency in app.py explicit
Strength: Worth exploring
Files: local_tool_client.py, internal_api_client.py, app.py, claude_client.py

Problem:
`local_tool_client.py` re-exports `InternalApiError` under the alias `LocalToolClientError`. `app.py`'s `_call_chat` catches `LocalToolClientError` (imported from `local_tool_client`) alongside `ClaudeClientError` (imported from `claude_client`). Because the alias is just a name binding, the true origin of the exception type is `internal_api_client.py`, but the import graph visible in `app.py` points to `local_tool_client`. If `local_tool_client` is ever refactored to drop the re-export, or if `internal_api_client` introduces a base `CoreApiError` with `InternalApiError` as a subclass, `app.py` will either break or silently catch a narrower set of exceptions than intended—without any signal in its own import list that the dependency actually lives one module deeper.

Solution:
Replace the alias in `local_tool_client.py` with a direct re-export that documents the origin (`from internal_api_client import InternalApiError`), and in `app.py` import `InternalApiError` directly from `internal_api_client` for the catch clause. If a domain-specific name is still desired in `app.py`, define a thin `LocalToolClientError(InternalApiError)` subclass in `local_tool_client.py` so the hierarchy is real rather than a name swap, and catch the base `InternalApiError` in `app.py` to future-proof against hierarchy changes.

Benefits:
The dependency graph in `app.py` matches the actual module that defines the exception, making the coupling visible in the import list. A future refactor of `internal_api_client`'s error hierarchy will surface as a type-checker or import error at the point of use rather than silently changing the semantics of a name that appears to belong to a different module.

### AC-130 · Resolve the local model name inside the client that uses it
Strength: Worth exploring
Files: internal_api_client.py, ollama_client.py, routes/chat.py

Problem:
The `reserve` function in `internal_api_client.py` accepts a `model` parameter that callers populate by reading `os.environ.get("LOCAL_MODEL")` (exposed as `ollama_client.MODEL`). The module already reads `AGENT_MANAGER_CORE_DASHBOARD_URL` from the environment for its own base URL, so it is self-contained for one configuration value but delegates the other to every caller. The knowledge of which environment variable holds the model name is therefore scattered across `ollama_client.py` and each call site in `routes/chat.py`, rather than being co-located with the client that actually sends it to the dashboard. Adding a new caller requires remembering to read the same env var and pass it positionally or by keyword, and a typo in the variable name will only surface at runtime.

Solution:
Give `reserve` a default of `model=None` and, when `None`, resolve it internally via `os.environ.get("LOCAL_MODEL")` (or a module-level constant read once at import time, mirroring how the dashboard URL is handled). Callers that need to override for testing or multi-model scenarios can still pass an explicit value. Remove the now-unnecessary `MODEL` read from `routes/chat.py` call sites.

Benefits:
The client is self-contained with respect to the local model's identity, matching its existing pattern for the dashboard URL. New callers no longer need to know the env-var name, and a single change to the variable name (or a move to a config file) is localized to `internal_api_client.py` rather than every call site.

### AC-131 · `providerLabel` name collision between sessions-view and swap-view
Strength: Worth exploring
Files: static/sessions-view.js, static/swap-view.js

Problem:
Both modules export a function named `providerLabel` that is consumed by the same caller (`chat.js`), but they have different signatures and produce different output shapes. `sessions-view.js` takes a row and returns a two-segment string like `"Claude · model"` or `"Local"`, with no account token and no `"Unknown"` fallback. `swap-view.js` takes a session and returns up to three segments (`"Claude · model · live account"`, `"Local · model"`, or `"Unknown"`). Because the names are identical, the import site in `chat.js` must alias one of them, and any developer adding a new call must remember which alias maps to which formatting contract. A silent mix-up would produce a subtly wrong label in the UI—e.g., the swap view showing a two-segment string where the user expects the account, or the session list showing `"Unknown"` where it should show `"Local"`—and the error would be easy to miss in a quick visual pass.

Solution:
Rename the export in `static/swap-view.js` from `providerLabel` to `swapProviderLabel` (or `sessionProviderLabel`), updating the single import line in `chat.js` and any internal references within `swap-view.js` itself. The rename is a one-line change at the import site and a one-line change at the export site; no logic changes. Alternatively, rename the `sessions-view.js` export to `rowProviderLabel` to emphasise its row-level scope. Either direction removes the collision without altering behaviour.

Benefits:
Callers in `chat.js` can import both functions by their natural names without an alias, eliminating a class of copy-paste or mis-remembering bugs. New contributors reading the import block see two distinct, self-describing names rather than a name plus an `as` clause. The rename is zero-risk (no logic change, no API surface change beyond the identifier) and costs a single search-and-replace across two files.
