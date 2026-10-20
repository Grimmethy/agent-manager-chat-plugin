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
