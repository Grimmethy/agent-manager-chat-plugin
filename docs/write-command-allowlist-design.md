# Design decision: owner-approved write-command execution path for Chat

> **Status: PROPOSAL — a design decision, pending owner approval. This is NOT an
> approved design.** Nothing in this document is implemented, no code is changed,
> no routes or endpoints exist or are added by this doc. The owner reviews and
> decides; the decision questions at the end are what need answering.
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

- **Location (default):** a per-repo file named `.chat-allowlist` inside the
  repo itself (e.g. `inventory-plugin/.chat-allowlist`), so the allowlist lives
  next to the code and data it may touch. Alternative: a single JSON/YAML config
  under the chat plugin's storage dir keyed by repo root. Tradeoff: per-repo
  files keep ownership local and survive a plugin reinstall; the central config
  is easier to audit in one place. **Decision question below.**
- **Format:** JSON array of argv arrays. Example:

  ```json
  {
    "repo": "/media/model-cache/github/inventory-plugin",
    "commands": [
      ["python3", "-m", "inventory", "sales", "email"]
    ]
  }
  ```

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

1. **New core internal API route** that the chat plugin calls through the
   existing client pattern in `internal_api_client.py` (which already carries
   the plugin→core internal calls, e.g. the `internal_reserve` release pattern
   referenced in `routes/chat.py`). The route verifies: (a) the exact argv is in
   that repo's allowlist, (b) a persisted `confirm` for the `proposalId` exists,
   then runs the argv in the repo's own working tree in a **fresh sandbox where
   that ONE repo is writable** and every other accessible repo stays read-only.
   - *Tradeoff:* cleanest containment (per-command, per-repo writable scope,
     core owns the sandbox, plugin holds no privileged capability); requires a
     core-side code change and a new internal (not public) method.
2. **Restricted host-side runner** — a long-lived core-side process that
   pre-loads the allowlists and confirmation store and executes approved argv
   with `cwd` pinned to the target repo.
   - *Tradeoff:* one always-writable process to keep trusted (larger standing
     attack surface, easier to misconfigure); no per-run sandbox isolation.

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

## Decision questions for the owner

1. **Section 4: which execution option?** (a) new core internal API route with
   per-run sandbox where exactly one repo is writable, or (b) a restricted
   host-side runner?
2. **Section 1: where do allowlists live?** Per-repo `.chat-allowlist` files
   (default proposed) or a single config in the chat plugin's storage dir?
3. **Scope confirmation:** is "exact argv, single repo, persisted confirm,
   allowlist-only, never a write-tool unlock" the boundary the owner wants — or
   should this design decision be declined and Chat stay read-only?

*This document is a design decision awaiting owner approval. Until the owner
answers the questions above, nothing described in it is approved or
implemented.*
