# Verification record — AC-5, AC-20, AC-21 (loopback-server coupling in test_agent_client.py)

Purpose: confirm, with exact line numbers and verbatim quotes, that AC-5, AC-20, and AC-21 in
`Docs/ARCH_REVIEW_CANDIDATES.md` all **propose removing** the live loopback-server coupling from
`test_agent_client.py` (prescriptive language: split / move / create / rewrite — not permissive).
Sibling tasks HUB0021 · 2/4, 3/4, 4/4 cite these lines.

Every line number below was reproduced with `sed -n '<N>p' Docs/ARCH_REVIEW_CANDIDATES.md`.

---

## AC-5 — CONFIRMED

- **Header (line 59):** `### AC-5 · test_agent_client.py is an app integration test wearing an agent_client label`
- **Problem statement (line 64), verbatim:**
  > Seven of the nine test methods in `test_agent_client.py` (...) exercise `app.app` HTTP routes and require a live `session_agent` threaded server. Only two methods (`test_filter_events_reassembles_split_blocks`, `test_list_sessions_rejects_wrong_shape`) actually call into `agent_client` in isolation.
- **Prescriptive removal language (line 67), verbatim:**
  > Split the file along the actual dependency boundary. Move the two pure `agent_client` unit tests into a dedicated file (e.g. `test_agent_client_unit.py` or simply rename the existing file to reflect its true scope) that imports only `agent_client` and `unittest`. Move the seven server-dependent integration tests into an `app`-level integration test file (e.g. `test_app_integration.py` or a new `tests/app/` subpackage) where they belong alongside other `app` route tests.
- **Verdict: CONFIRMED.** Uses "Split ... Move ... Move" — prescriptive removal of the live-server coupling.

## AC-20 — CONFIRMED

- **Header (line 287):** `### AC-20 · Create test_app_integration.py with the seven session-agent route tests`
- **Problem statement (line 293), verbatim (key sentence):**
  > Because the class-level setUp unconditionally starts a threaded HTTP server, even the two unit tests pay the cost of importing app, session_agent, and the fixture tree. The integration tests need a dedicated file that owns the server lifecycle and the app/session_agent imports, so the unit tests can later be isolated in a file that has zero dependency on the server stack.
- **Prescriptive removal language (line 296), verbatim (key sentences):**
  > Create a new file test_app_integration.py at the same directory level as test_agent_client.py. ... exactly the seven integration test methods listed above, copied verbatim. ... No unit-test methods are included.
- **Verdict: CONFIRMED.** Uses "Create ... a dedicated file that owns the server lifecycle" — prescriptive removal of the loopback-server coupling from the unit-test file.

## AC-21 — CONFIRMED

- **Header (line 301):** `### AC-21 · Trim test_agent_client.py to the two agent_client unit tests only`
- **Files: test_agent_client.py (line 304); Depends-On: AC-20 (line 305).**
- **Problem statement (line 308), verbatim (key sentence):**
  > ...the two unit tests (test_filter_events_reassembles_split_blocks, test_list_sessions_rejects_wrong_shape) are buried inside that class and inherit the server setUp/tearDown, meaning they cannot run in an environment where app or session_agent are absent.
- **Prescriptive removal language (line 311), verbatim (key sentences):**
  > Rewrite test_agent_client.py to contain only: an updated module docstring ('Unit tests for agent_client.py — no server, no app import'); ... Remove the app, session_agent, threading, tempfile, shutil, and json imports; remove FIXTURE_HOME, TOKEN, the env helper, _stop, and all seven integration methods. The file must be importable and runnable in an environment where app and session_agent are not installed.
- **Verdict: CONFIRMED.** Uses "Rewrite ... Remove ... Remove" — prescriptive removal of the loopback-server coupling.

---

## Cross-check summary

| AC | Header line | Body lines (Problem / Solution) | Coupling language | Prescriptive? | Verdict |
|----|-------------|---------------------------------|-------------------|---------------|---------|
| AC-5 | 59 | 64 / 67 | "require a live `session_agent` threaded server" → "Split ... Move ... Move" | Yes | CONFIRMED |
| AC-20 | 287 | 293 / 296 | "class-level setUp unconditionally starts a threaded HTTP server" → "Create a new file test_app_integration.py" | Yes | CONFIRMED |
| AC-21 | 301 | 308 / 311 | "inherit the server setUp/tearDown" → "Rewrite ... Remove ... all seven integration methods" | Yes | CONFIRMED |

No permissive/optional language ("may", "optionally", "could consider") appears in the Solution
paragraphs of any of the three ACs. All three propose removing the live loopback-server coupling
from `test_agent_client.py`, not merely authorizing it.
