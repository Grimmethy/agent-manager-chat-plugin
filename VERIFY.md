# Verifying the session agent against a real Claude Code install

For a Claude Code session running on the developer's machine (with at least one other terminal
session open). Do **not** paste transcript text, session ids, names, paths or hostnames anywhere:
report only pass/fail per item plus `--probe` output (shapes only).

Already checked once, in a cloud container on Claude Code 2.1.304 with ONE interactive session:
items 1-3 and 7 (shapes matched; `/sessions` and `/events` returned sane values for the busy
session). Please re-check them on your machine; items 4-6 and 8 have never been checked.

1. **Listing shape.** `claude agents --json --all` is a JSON list; entries have `pid`, `cwd`,
   `kind`, `startedAt` (ms), `sessionId`, `name`, `status`. Run
   `python3 session_agent.py --probe` and confirm `cli.keys` and `registry.keys` (extra keys are fine;
   `statusUpdatedAt` must be present and an int).
2. **Transcript shape.** In `--probe` -> `transcripts[*]`: `lineTypes` has `assistant`;
   `usageKeys` has `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`;
   `blockKeys.tool_use` has `name` and `input`; `corrupt` and `oversized` are small or 0.
3. **Sidechains.** Run a task that spawns a sub-agent. `sidechainAssistantLines` should be > 0
   and `contextTokens` in `/sessions` should not jump to the sub-agent's figure.
4. **Context vs the terminal.** For a session, compare `contextTokens` from
   `curl localhost:7450/sessions` with what `/context` reports in that terminal. Record
   "matches / off by N%" and whether the transcript lags (how long after a reply it catches up).
5. **Status transitions.** Watch `curl -N localhost:7450/events` while one session goes
   idle -> busy -> idle, and while one hits a permission prompt. Record the exact `status` values
   seen, whether `waitingFor` is ever non-null (and its shape: string/object), and whether
   `statusSince` changes only on status changes.
6. **Step.** While busy, `step` should show the running tool (e.g. `Bash: ...`); right after you
   send a prompt and before any tool, it should read `Thinking`; at a permission prompt it should
   read `Waiting: ...` (or fall back to the last tool if `waitingFor` is empty).
7. **Liveness.** Kill a session; its row must disappear within a few seconds
   (`session.removed`). A leftover registry file for a dead pid must not appear.
8. **Inbox socket (READ-ONLY investigation, do not send anything).** The registry exposes
   `messagingSocketPath`, `peerProtocol` and `peerFeatures` (observed: protocol `1`, features such as
   `notify_idle`, `reply_across_default_dirs`, `artifact_yield`). Find where Claude Code
   documents the peer protocol and the message envelope. Report: envelope fields, whether an
   auth line is required for a sender that is not a child process, any reply/ack, and how the
   receiver labels peer messages. Until this is answered, `--allow-send` stays off.

Also report: the `claude --version`, how many sessions were open, and any `status` or `kind`
value that is not `busy|idle|waiting` / `interactive`.
