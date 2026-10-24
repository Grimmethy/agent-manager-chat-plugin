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

HUB0005 7/7, replacing the shorter HUB0017 list. Two machines, wok and grimm, each running `session_agent.py`, with the example `AGENT_MANAGER_SESSION_AGENT_URLS` from the README set in the plugin's environment (plugin on its default port 7442). Do not paste token text, token file paths or session ids anywhere: report pass or fail per item. Each item is marked (never checked).

1. **Both hosts listed.** `curl -s localhost:7442/api/chat/terminal-sessions` answers `enabled` true, `reachable` true, `hosts` `["wok","grimm"]` in that order, `skipped` `[]`, and every row in `sessions` has `host` set to `wok` or `grimm` (never checked).
2. **One agent down, the other stays live.** Stop the agent on grimm. Within one poll the same request answers `reachable` true, `skipped` with one entry `{"host":"grimm","reason":...}`, and still lists wok's sessions; the chat panel shows wok's rows and a line saying grimm was skipped that names grimm (and its reason), not a bare object. Start it again: grimm's rows return and `skipped` is `[]` (never checked).
3. **Both agents down.** Stop both: `reachable` false, `sessions` `[]`, `skipped` has both hosts, and the panel says "Session agent unreachable" (never checked).
4. **A session ending on one host disappears only there.** End a terminal session on grimm: its row disappears from the panel within a few seconds and no wok row changes, including a wok session that happens to share the same session id (never checked).
5. **The events route.** With two hosts configured, `curl -N 'localhost:7442/api/chat/terminal-sessions/events'` answers 400; `?host=wok` streams events tagged `wok`; `?host=nobody` answers 404; `?host=grimm` with grimm stopped answers 502 (never checked).
6. **Host badge and filter.** Each terminal row shows its host in a small badge (`.terminal-row-host-badge`); the filter lists `grimm` and `wok` sorted plus All; choosing one shows only that host's rows and All resets it (never checked).
7. **Tokens stay server-side.** Search every response body and the event stream from items 1 to 6 for the token file paths and for the first characters of each token: neither appears (never checked).

## Attention: amber flash + count badge in the chat switcher (piece 2/10)

Manual DOM checks (no automated test for these). None have ever been checked.

1. **Amber flash on newly waiting.** A terminal session that transitions to `waiting` shows
   its switcher row flash amber for ~10 seconds, then settle to a steady amber highlight
   while it stays `waiting`. (never checked)
2. **Steady amber while waiting.** A session already `waiting` stays steady-amber on
   subsequent updates without re-flashing. (never checked)
3. **Title prefix.** While at least one session is `waiting`, the browser tab title reads
   `(N) <original title>` where N is the waiting count; it restores to `<original title>`
   when the count returns to 0. (never checked)
4. **Parent postMessage.** Every time the waiting count changes, the switcher posts
   `{ type: 'chat-attention', count: N }` to `window.parent` (the dashboard's tab badge
   itself is a separate change in the dashboard repo). (never checked)

## Attention: optional chime (piece 3/10)

Manual browser checks (the decision itself, `shouldChime`, is unit-tested; the sound and the stored setting are not). None have ever been checked.

1. **Off by default.** On a fresh profile the switcher menu ends with an unchecked "Chime when another chat needs me" box, and a session turning `waiting` makes no sound. (never checked)
2. **Saved setting.** Ticking the box stores `chat-chime` = `1` in localStorage; after a reload it is still ticked, and unticking stores `0`. With storage blocked the page still works and the box just resets on reload. (never checked)
3. **One short beep.** With the box ticked, a session newly turning `waiting` that is not on screen plays one short beep (about 0.2 s); a session already waiting does not repeat it. (never checked)
4. **Silent when on screen.** In the multi-pane layout a newly waiting chat that is mounted in a pane does not chime. Terminal sessions never match chat panes, so every newly waiting terminal session chimes while the box is ticked. (never checked)

## Phone width: one pane, the switcher is the navigation (piece 5/10)

Manual browser checks (layoutForWidth itself is unit-tested). None have ever been checked.

1. **Narrow window shows one pane.** With a 2-column or 2x2 layout saved, shrinking the window below 760 px shows only the active chat and the preset buttons (Single / Two columns / Grid 2x2) disappear. (never checked)
2. **Widening restores the layout.** Widening back to 760 px or more brings the saved wide layout and the preset buttons back, without a reload, and `localStorage` key `chat-workspace-layout` was not changed by the narrow period. (never checked)
3. **Load at phone width.** Loading the page already narrow shows the single active pane and no preset buttons. (never checked)
4. **Resize is debounced.** Dragging the window edge across 760 px renders once after the drag settles (about 150 ms), and a plain resize that does not cross 760 px does not reload any pane. (never checked)
5. **Navigation on a phone.** The session switcher inside the pane can open another chat; the old dropdown above the pane is gone. (never checked)

## Layout templates menu (piece 7/10)

Manual browser checks (the template functions are unit-tested; the menu and storage are not). None have ever been checked.

1. **Built-ins.** The toolbar's Templates menu lists 'driver + 2 workers' and 'monitor grid'; choosing one switches to the 2x2 grid with the open chats first and empty 'New chat' slots after them, and saves that layout. (never checked)
2. **Save current as.** 'Save current as…' opens an inline name box (no browser pop-up); typing a name and pressing Enter or Save adds it to the menu, Escape closes the box without saving; an empty name saves nothing. (never checked)
3. **Persists per browser.** After a reload the saved template is still in the menu (localStorage key 'chat-workspace-templates'); in another browser profile it is not. (never checked)
4. **Storage blocked.** With site storage blocked the menu still works and the built-ins show; a saved template lasts until reload. (never checked)
