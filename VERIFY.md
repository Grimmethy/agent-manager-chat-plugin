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
3. **Sidechains.** Run a task that spawns a sub-agent. Observed: sub-agent lines live in
   `<sessionId>/subagents/agent-*.jsonl`, not the main file (`sidechainAssistantLines` is 0), and
   the parent's `contextTokens` does not change. Confirm the same.
4. **Context vs the terminal.** For a session, compare `contextTokens` from
   `curl localhost:7450/sessions` with what `/context` reports in that terminal. Record
   "matches / off by N%" and whether the transcript lags (how long after a reply it catches up).
5. **Status transitions.** Watch `curl -N localhost:7450/events` while one session goes
   idle -> busy -> idle, and while one hits a permission prompt. Record the exact `status` values
   seen (observed so far: bg sessions report CLI state `blocked`, the registry can report status `shell`;
   both map to `waiting`), whether `waitingFor` is ever non-null (and its shape: string/object), and whether
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

## Model swap (manual)

HUB0008 10/11 (header provider label, Swap menu, one-click swap offer). Manual items only
-- there is no automated DOM test for chat.js. Each item is marked (never checked).

1. The chat header shows the loaded session's provider label (e.g. `Claude · opus · work`,
   `Claude · sonnet · live account`, or `Local · <model>`) (never checked).
2. The Swap button opens a menu listing the registered Claude accounts (minus the one the
   session already uses) plus `Local model`; a local session's menu lists the accounts only
   (never checked).
3. Choosing a Swap menu entry performs the swap and the transcript re-renders showing the
   swap marker row the server appended to the session (never checked).
4. A failed Claude turn whose error carries a swap offer shows the offer text with one
   button per offered option; errors without an offer show nothing extra (never checked).
5. Clicking a swap-offer button performs the same swap and the offer disappears afterwards
   (never checked).
6. A chat whose JSON has no `schemaVersion` loads and, after its next write, carries `schemaVersion` 2 with `account`, `swaps` and `summary` added and its transcript unchanged (never checked).
7. `POST /api/chat/<id>/swap` with `{"provider": "claude"}` or `{"provider": "local"}` answers with the updated session, appends one marker row and one `swaps` entry, keeps every earlier row, and leaves `claudeSessionId` empty (never checked).
8. A swap with an unknown `account` answers 400 and an unknown or closed chat answers 404 (never checked).
9. Swapping a chat that holds the local reservation to Claude releases the reservation; swapping to local never acquires one (never checked).
10. The first Claude turn after a swap shows the hand-off in the model's view (header, summary if any, the last `CHAT_RECENT_TURNS_KEPT` rows); a long transcript gets a summary and a failed summarizer does not block the swap (never checked).
11. `limit-error-samples.jsonl` appears in the storage directory after a real Claude limit error, and its text shows whether the assumed phrases in `limit_errors.py` match; send the shapes to the owner (never checked).

## Multi-host terminal sessions (manual)

HUB0017 3/3. Manual items only. Each item is marked (never checked).

1. With `AGENT_MANAGER_SESSION_AGENT_URLS` set to two or more agent endpoints, the `GET` terminal-sessions list response includes `hosts` (array of host names) and `skipped` (array of unreachable host names); every row carries a `host` field populated by `formatTerminalRow` (never checked).
2. The UI opens one SSE stream per host at `api/chat/terminal-sessions/events?host=<name>` (falling back to `api/chat/terminal-sessions/events` when only a single host is configured), and rows from different hosts interleave correctly without loss (never checked).
3. Each terminal row displays its host in a small badge using the `.terminal-row-host-badge` CSS class; rows without a host field show no badge (never checked).
4. The host filter dropdown is populated via `hostsOf(rows)` and filtering works via `filterByHost(rows, selectedHost)` in `static/sessions-view.js`: selecting a host shows only that host's rows, "All" resets the filter (never checked).
5. Hosts listed in the `skipped` array are visible in the UI (e.g. a muted line or badge) so the operator knows a host was unreachable (never checked).
