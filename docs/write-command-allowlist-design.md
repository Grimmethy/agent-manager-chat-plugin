# Design decision: owner-approved write-command execution path for Chat

> **Status: APPROVED by the owner, 2026-10-25.** Decisions: **(A)** execution is
> option 2 of section 4, a restricted host-side runner in core; **(B)** the
> allowlist is ONE central config in the chat plugin's storage dir; **(C)** scope
> is the first proposal as written: exact argv, single repo, persisted confirm,
> allowlist-only, never a write-tool unlock (`queue_reviewed_task` stays the only
> code-change hand-off). Implementation proceeds in the pieces of section 6.
> Nothing here is implemented yet until those pieces merge.
>
> Date: 2026-10-25
> Grounding note: every symbol cited below from **this** (chat plugin) repo was
> verified against its real file in this checkout. Mechanisms described as living
> in the **core** agent-manager repo are cited **as stated in the task brief —
> the core repo is not readable from this plugin checkout, so nothing about it
> here is independently verified in this repo.**

## 0. Background: Chat is read-only by design — and the asymmetry

Chat's sole hand-off to the core pipeline is the `queue_reviewed_task` tool (see
its many references in `chat_sessions.py`'s system prompt, e.g. the lines
describing "only a real queue_reviewed_task text description of the fix is not
the hand-off" and the "queue_reviewed_task refuses them" rule for git
operations). The sandbox behind Chat is enforced at the filesystem level:
`run_bash` executes inside a **bwrap sandbox** with its own isolated PID
namespace — this is stated in `chat_sessions.py`'s system prompt ("your run_bash
tool executes inside a bwrap sandbox … by design, for containment"), and the
brief locates the wrapper and the read-only mount flags in the core repo's
`sandbox.js` (brief-cited core repo; the tool description and the roots split in
`runPlanWithTools`, `src/local-tool-client.js` ~line 253 and ~line 989, are also
brief-cited core repo, not verifiable here). The consequence, stated in the
prompt in `chat_sessions.py` ("and run_bash cannot write anything (every repo is
mounted read-only for you …)"), is: **Chat cannot execute a command that writes,
even one the owner would happily run manually.**

The concrete motivating example (from the task): `python3 -m inventory sales
email` in `/media/model-cache/github/inventory-plugin`, which ingests sales into
`inventory.db` — an owner-approved *operational* command that mutates a data
file, not a code change.

The asymmetry to keep visible:

- `chat_sessions.py`'s `_stream_local` already passes `allow_write=True` to the
  local turn (visible at the header of `chat_sessions.py` and in the call in
  `local_tool_client.py`, where `allow_write: bool = False` is a real parameter
  of the plugin-side client).
- The core honors a separate `.chat-write-tools-disabled` kill-switch for chat —
  `chat_sessions.py` references the "tools-disabled kill-switch path" in the
  `_stream_local` footer logic (the flag file itself is core-side; it does not
  appear in this repo's files).
- **Yet** the tools a chat source can actually use are still read-only by
  construction (the core picks a chat-scoped tool set over the write-capable one
  for chat sources — brief-cited core repo mechanism), and `run_bash`'s mounts
  are read-only. So today, **nothing writable is reachable from Chat**, and
  `allow_write=True` buys no writable surface. That is the gap this proposal
  addresses — for exactly one narrow class of commands.

## 1. Allowlist of exact commands per repo

- **Location (decided, B):** ONE central file in the chat plugin's storage dir,
  `~/.local/state/agent-manager-chat-plugin/command-allowlist.json` (override with
  the env var `AGENT_MANAGER_CHAT_COMMAND_ALLOWLIST_PATH`), mode 0600, keyed by
  repo root. One place to audit; per-repo `.chat-allowlist` files were the
  alternative and are not used.
- **Format:** JSON array of argv arrays. Example:

  ```json
  {
    "commands": [
      {
        "repo": "/media/model-cache/github/inventory-plugin",
        "argv": ["python3", "-m", "inventory", "sales", "email"],
        "env": {"PYTHONPATH": "src:/media/model-cache/github/crosslister/src"},
        "timeoutSeconds": 300
      }
    ]
  }
  ```

  `env` (optional) is the ONLY environment added to the run, and `timeoutSeconds`
  (optional) overrides the default, within the runner's maximum (section 4).

- **Matching rule:** **exact argv equality** against the entry, run in that
  repo's own working tree. No shell involved: no shell expansion, no wildcards,
  no `&&`/`|`/`;`, no environment interpolation, no `bash -c` strings. Anything
  that is not byte-for-byte equal to an allowlisted argv entry, in the allowlist's
  repo root, is **refused**. There is no fuzzy match, prefix match, or "close
  enough" — a typo'd command is a refusal, not a permission.
- **Editing:** the owner edits the file with their normal tools (it is a plain
  file, read via the existing read tools). No daemon, no hot-reload required:
  the file is read at the moment a command is proposed. Refusal is the default;
  an unreadable or malformed allowlist behaves the same as an empty one.

## 2. Owner confirmation gate in the chat UI before each run

Verified state first: searching this repo, `confirm` appears only as English
prose (e.g. "confirmed live", checklist verbs in the two existing docs) —
**there is no persisted confirmation gate today.** This section is new.

- When the chat proposes a command that matches the allowlist (section 1), the
  stream emits a new SSE/transcript event carrying the proposed argv and the
  target repo, e.g. `{"type": "command-proposal", "argv": [...], "repo": "..."}`.
- The UI then presents a **mandatory, persisted confirm/deny control** rendered
  by the existing transcript renderer `grillRenderTranscript` (present in
  `static/chat.js`, `templates/chat.html`, and referenced from `chat_sessions.py`)
  — a real button pair wired to a persisted transcript entry of the shape
  `{"type": "confirm", "proposalId": "...", "decision": "confirmed"|"denied", "at": "<iso>"}`.
  This is **not a toast** and not a transient modal: the decision is written into
  the session's transcript (next to where `chat_sessions.py`'s
  `_stream_message_turn` already stores `result_box["tools"]` on the assistant
  entry), so it survives reload, multi-pane display, and replay.
- **The command runs ONLY after the confirmation is recorded.** The execution
  call carries the decision's timestamp and `proposalId`; without a recorded
  `confirm` entry for that `proposalId`, the core-side runner (section 4)
  refuses.
- **If the user does not confirm:** the turn ends, the command never runs, and
  the transcript shows the pending proposal — it can be confirmed later from the
  same pane, or simply ignored forever. Denial is final for that turn and is
  itself persisted.
- Where the confirmation lives: **on the transcript entry** (not hidden session
  state), so the audit trail is exactly what the owner sees in the pane.

## 3. Output logging in the transcript

The transcript stays auditable by storing command executions in a shape parallel
to the existing `tools` list that `chat_sessions.py`'s `_stream_message_turn`
already stores via `tool_log.summarize_tool_calls` on the assistant entry:

```json
{
  "role": "assistant",
  "text": "…",
  "tools": [ …existing shape from tool_log.summarize_tool_calls… ],
  "command_executions": [
    {
      "argv": ["python3", "-m", "inventory", "sales", "email"],
      "repo": "/media/model-cache/github/inventory-plugin",
      "confirmedAt": "<iso timestamp of the persisted confirm>",
      "exitCode": 0,
      "stdout": "…clipped, scrubbed…",
      "stderr": "…clipped, scrubbed…",
      "ok": true
    }
  ]
}
```

- **stdout/stderr are clipped and scrubbed with the same logic as
  `tool_log.py`'s `_scrub` / `_clip`** (secret-key names, inline
  `token=…`/`password: …`, and `Bearer …` values are hidden; text is clipped to
  the module's length cap) — reused, not reinvented.
- `grillRenderTranscript` renders the block under the assistant message: command,
  confirmation time, exit code, output. A failed run (non-zero exit or runner
  error) shows the clipped stderr, mirroring how `tool_log.py`'s `_outcome`
  keeps a failure's error text and a success's size.

## 4. How it stays off the read-only mounts (core-side execution)

The mounts are set by the core's bwrap wrapper (brief-cited: `sandbox.js`; the
`runPlanWithTools` roots split — read-only roots separate from a writable roots
parameter — at `src/local-tool-client.js` ~line 989, also brief-cited core
repo). **The plugin cannot bwrap on its own** — `run_bash` from this process
still lands inside the read-only sandbox, so any writable execution must be
owned by a core-side path. Two options, both core-side:

1. **New core internal API route with a per-run sandbox** (NOT chosen). Cleanest
   containment, but needs a per-run sandbox where exactly one repo is writable.
2. **Restricted host-side runner (CHOSEN, decision A).** Core executes approved
   argv with `cwd` pinned to the target repo, with no per-run sandbox. The doc's
   own tradeoff stands (a trusted writer on the host, a larger standing surface
   than option 1), so the runner is kept deliberately small:
   - it is a function invoked per request by a core internal route
     (`POST /api/internal/chat/run-command`), **not** a daemon with its own state;
   - `execFile`-style execution of the argv list: **no shell**, no expansion;
   - `cwd` is the realpath of the allowlisted repo; the environment is a minimal
     base (`PATH`, `HOME`, `LANG`) plus **only** the allowlist entry's `env`;
   - timeout default 120 s, entry override up to a maximum of 900 s;
   - stdout and stderr each capped at 20000 characters;
   - one run at a time (a lock), so two confirmed commands never overlap;
   - one append-only audit line per attempt (accepted or refused) in
     `~/.local/state/agent-manager/chat-command-audit.jsonl`;
   - it refuses everything while the `.chat-write-tools-disabled` kill-switch file
     is present, and it re-validates the allowlist and the persisted confirmation
     itself rather than trusting the plugin.

Both options keep `queue_reviewed_task` **as the only chat→core hand-off for
code changes**: the new channel executes operational commands only, and this
mechanism is **strictly for owner-approved operational commands** (like ingesting
sales into `inventory.db`) — **not for writing source**. No new public endpoint
is introduced either way; option 1 adds a core *internal* method behind the
existing internal API boundary. **The 2026-09-15 read-only mount state is a
deliberate post-incident safeguard; any mechanism that makes a repo writable
must live in the core, never be a plugin-side bypass of the mounts.**

## 5. What this does NOT change (invariants) and risks

**Invariants:**

- `queue_reviewed_task` remains the **sole** path by which Chat asks for code
  changes; this proposal adds an execution channel for operational commands and
  nothing else.
- The `.chat-write-tools-disabled` kill-switch (core-side; referenced in
  `chat_sessions.py` as the "tools-disabled kill-switch path") still applies and
  must also disable this feature when set.
- The read-only mounts for every non-allowlisted command, and every repo not the
  allowlisted target, are unchanged.
- No source files change as a result of this doc; no public endpoints.

**Risks (the owner is deciding on these):**

1. **Irreversible writes / accidental data loss.** `inventory.db` mutations have
   no undo; the confirm gate is the *only* control. A wrong allowlist entry or a
   mis-read proposal is permanent.
2. **Allowlist drift.** The owner approves `python3 -m inventory sales email`;
   the `inventory` package later changes what that argv does under them. The
   allowlist matches argv, not behavior.
3. **Secret leakage into the transcript.** Mitigated by reusing `tool_log.py`'s
   `_scrub`/`_clip`, but a command's output can still carry sensitive data that
   the scrub patterns don't catch.
4. **Scope creep toward a general "chat can write" hole.** This must stay
   **command-allowlist-only — explicitly NOT a write-tool unlock**. Nothing in
   this proposal widens the tool set a chat source may use, or the mounts
   `run_bash` gets.
5. **Bypassing the pipeline, by design.** An executed allowlisted command skips
   the implement/critique/review pipeline. That is acceptable only because the
   surface is an exact-argv allowlist plus a persisted owner confirmation — it
   is precisely the trade the owner is making by approving this.

## Decisions (2026-10-25)

1. **Section 4, execution:** option 2, the restricted host-side runner.
2. **Section 1, allowlist location:** one central config in the chat plugin's
   storage dir.
3. **Scope:** exact argv, single repo, persisted confirm, allowlist-only, never a
   write-tool unlock: the first proposal as written.

## 6. Implementation contract and pieces

**Contract** (every piece uses exactly these names):

- **Allowlist file:** `AGENT_MANAGER_CHAT_COMMAND_ALLOWLIST_PATH` or
  `~/.local/state/agent-manager-chat-plugin/command-allowlist.json`, mode 0600,
  JSON `{"commands": [{"repo": "/abs/repo", "argv": ["python3", "-m", "inventory",
  "sales", "email"], "env": {"PYTHONPATH": "src:/media/model-cache/github/crosslister/src"},
  "timeoutSeconds": 300}]}`. A match needs the repo realpath equal and the argv
  exactly equal. A missing, unreadable or malformed file is an empty allowlist.
- **Proposal tool:** chat tool `propose_command({repo, argv, why})`, defined in
  core `src/local-tool-client.js` `CHAT_TOOLS`. It validates against the allowlist
  file and returns `{ proposalId, status: 'awaiting-confirmation' }` or a refusal
  naming why. Accepted proposals ride on the turn's final event as
  `commandProposals: [{ proposalId, repo, argv, why }]`.
- **Transcript fields** (on the assistant entry): `commandProposals: [{ proposalId,
  repo, argv, why, status: 'pending' | 'confirmed' | 'denied' | 'ran', decidedAt }]`
  and `commandExecutions: [{ proposalId, argv, repo, confirmedAt, exitCode, ok,
  stdout, stderr, durationMs }]` (stdout and stderr scrubbed and clipped with
  `tool_log.py`'s rules).
- **Plugin route:** `POST api/chat/<session id>/command/<proposalId>/<confirm|deny>`
  persists the decision (`decidedAt`) and, for confirm, calls core
  `POST /api/internal/chat/run-command` with `{ proposalId, sessionId, repo, argv,
  confirmedAt }`. Nothing runs before a persisted confirm.
- **Core runner reply:** `{ ok, exitCode, stdout, stderr, durationMs }` or
  `{ ok: false, refused: '<reason>' }`. Limits are those of section 4.
- **Model context:** a one-line summary of each execution is added to the model's
  context on the next turn.

**Pieces, each its own task, in order:**

1. Core: the runner module and the internal route.
2. Core: the `propose_command` chat tool.
3. Plugin: storage of proposals and executions, the confirm/deny route, the call
   to the core runner.
4. Plugin UI: the proposal card with Confirm and Deny, and the executions block.
5. The owner enables the first allowlist entry (`python3 -m inventory sales
   email`) after the feature is live.
