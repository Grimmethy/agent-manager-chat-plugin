# agent-manager-chat-plugin

Standalone extraction of agent-manager's in-tree dashboard "Chat" feature (system-wide
sidebar, Claude + local-model providers, real Read/Grep/Glob/Edit/Write/Bash tool access,
local-provider worker preemption) into its own toggleable plugin, following the
`plugins.json` "process + proxy" pattern agent-manager's hardware-tab plugins already use
(see `agent-manager-hardware-plugin` for the reference shape), extended with a new
`slotKind: "persistent-sidebar"` since Chat needs to stay visible across every tab, not
own a single tab region.

Full design/rollout plan: `/home/wok/.claude/plans/immutable-noodling-axolotl.md` in the
agent-manager checkout that authored this repo.

## Status: Phase 1 (in progress)

A standalone Flask app that boots and serves the real Chat routes independently of
agent-manager's dashboard process -- but Phase 1 deliberately keeps the SAME direct
filesystem/subprocess coupling to the core agent-manager repo the in-tree feature always
had (reads its `agent-manager.env`/`plugins.json`/`projects.json`, `instances/`,
`queue/`, shells out to its `scripts/gpu-arbiter-cli.js`), via `AGENT_MANAGER_CORE_REPO_ROOT`.
Phase 2/3 of the plan replaces this with a small internal API agent-manager exposes
instead, so this repo no longer needs read/write access to the core repo's runtime state
at all. The in-tree Chat feature in `agent-manager` is untouched and fully live until
cutover (Phase 4).

**What's vendored into this repo** (self-contained, verified): `chat_sessions.py`,
`chat_preempt.py` (extracted from agent-manager's `app.py` in PR #270 first, then copied
here verbatim -- see that PR for why: safety-critical SIGKILL/lock logic, extracted as a
real, tested module rather than hand-transcribed), `single_flight_lock.py`,
`ollama_client.py`.

**What stays pointed at the core repo, NOT vendored** (confirmed live, not just by
reading): `src/claude-client.js` and `src/local-tool-client.js`. Both were originally
assessed as "comparatively self-contained" in the plan's design pass -- that assessment
was wrong for `claude-client.js`, caught by actually running it: it requires
`src/local-client.js` (10+ further pipeline-internal modules) plus `sandbox.js`,
`side-finding.js`, `incident-amplification.js`, `concepts.js` -- genuine pipeline
features Chat's `allow_amplification=True` path really exercises (a Claude chat turn can
raise real side-findings into the brain dump and record concept-build tallies), not
incidental bloat safe to strip. `model_stats_client.py` similarly stays pointed at the
core repo's `src/model-stats-db.js` (writes into `model-stats.db`, which lives there).

## Running

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; source /media/model-cache/github/agent-manager/agent-manager.env; set +a
AGENT_MANAGER_CORE_REPO_ROOT=/media/model-cache/github/agent-manager \
AGENT_MANAGER_CHAT_PLUGIN_PORT=7442 \
.venv/bin/python app.py

curl localhost:7442/healthz
curl localhost:7442/api/chat/active
```

Verified live 2026-09-15: `/healthz`, `/api/chat/active` (correctly resolves all 11
`_chat_roots()` entries from the core repo's real `plugins.json`/`projects.json`), a
Claude-provider turn (correctly reaches the real `claude-client.js`, correctly refuses
without `CLAUDE_CODE_OAUTH_TOKEN` set), and a local-provider turn (the chat-preempt chain
fires against live `instances/`/`queue/` state, `local-tool-client.js` spawns and runs a
real turn against the live Ollama backend).

Session storage lives at `~/.local/state/agent-manager-chat-plugin/sessions/` by
default (override with `AGENT_MANAGER_CHAT_STORAGE_DIR`) -- independent of the core
repo's own root, unlike the in-tree feature's hardcoded `CHAT_STORAGE_DIR`.

## Session agent (`session_agent.py`)

Standalone, stdlib-only (Python 3.12) helper that runs on the developer's machine, discovers
local Claude Code terminal sessions and serves them over HTTP for the chat panel to consume
later. It does not touch the Flask app.

```
python3 session_agent.py [--bind 127.0.0.1] [--port 7450] [--token T | AM_AGENT_TOKEN=T] \
    [--windows windows.json] [--claude-home ~/.claude] [--interval 1.0]
```

- `GET /healthz`, `GET /sessions`, `GET /sessions/<id>/transcript?since=<cursor>&limit=200`,
  `GET /events` (SSE: `session.updated`, `session.removed`, `transcript.append`).
- Sources: `claude agents --json --all` (documented), the undocumented
  `~/.claude/sessions/<pid>.json` registry (all keys optional; also the fallback if the CLI is
  unavailable) and the transcripts, tailed by byte offset.
- Context = input + cache-read + cache-creation tokens of the latest non-sidechain assistant line.
- `contextWindow` is `null` unless `--windows` maps a model-id substring to a size (the agent
  ships no numbers). `deploy/windows.example.json` lists 1,000,000 for claude-fable-5-1,
  claude-opus-5-5, claude-sonnet-5-5 and claude-haiku-5-5 per the API models page; only the
  sonnet and opus entries are confirmed inside Claude Code (contexts above 900k seen), fable and
  haiku are not. The status line's `context_window.context_window_size` is the better source
  (200000 default, 1000000 extended) is read from optional `<sessionId>.json` files
  (`{"contextWindowSize": int, "model": str, ...}`) in `--statusline-dir` (default
  `~/.local/state/agent-manager/statusline`). The file never expires (a window does not change
  within a session) but is ignored if its `model` differs from the transcript's latest model, and
  then `--windows` applies. `statusline_writer.py` is an opt-in status line command that writes them
  (fast, atomic, mode 0600, only when the window or model changed, prints e.g. `Model 14%`); it never
  touches settings. To use it, add this yourself to `~/.claude/settings.json`:
  `"statusLine": {"type": "command", "command": "python3 /path/to/statusline_writer.py"}`.
  The file's `usedPercentage`/`observedAt` are a snapshot as of the last write and are not used.
- `claude agents` is polled at most every 10 s (it costs ~0.2 s CPU per call); registry files fill
  the gaps. `GET /events?types=session` omits `transcript.append`, and the plugin's passthrough
  additionally drops everything except `session.updated`/`session.removed`, so transcript text never
  reaches the browser.
- Status mapping: `busy|idle|waiting`; CLI state `blocked` maps to `waiting`; unknown values,
  including registry status `shell` (seen once, unexplained), map to `idle`. Registry kind `bg` is reported as `background`.
  `step` is `null` while idle. Sub-agent lines live in separate `<sessionId>/subagents/` files, and
  `isSidechain` lines are excluded anyway.
- `--claude-home` redirects only the registry/transcript reads; `claude agents` still reads the
  invoking user's own config.
- Only Host/Origin names `localhost`, `127.0.0.1`, `::1` (plus the bind address and any
  `--allow-host NAME`) are served, against DNS rebinding; no CORS headers are sent. If the
  plugin reaches the agent by another hostname, pass `--allow-host` for it.
- `--token auto` generates a random token into `--token-file` (default
  `~/.local/state/agent-manager/session-agent.token`, mode 0600, a new token on every start). The
  plugin reads it per request from `AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE`, so restarts need no
  reconfiguration. (Loopback with no token lets any local OS user read transcripts; prefer `auto`.)
- Rows carry `lastActivityAt` (last transcript timestamp). The switcher shows a busy session with no
  activity for an hour or more as `busy, no activity 29h` instead of a busy timer.
- Transcripts are private: loopback bind by default; a non-loopback `--bind` refuses to start
  without a token, and with a token every request needs `Authorization: Bearer <token>`.
  Message text is never logged.
- `--probe` prints a shapes-only report of the local data (no values) for format checks; see `VERIFY.md`.
- systemd template: `deploy/session-agent.service`.
- Experimental: `--allow-send` enables `POST /sessions/<id>/peer-message {"text": ...}`, which
  writes one line to the session's inbox Unix socket. The socket's wire format is
  undocumented, so the payload is an unverified placeholder (see the `TODO(peer-message)`
  block in `session_agent.py`). Peer messages are "not from the user" and cannot approve
  permission prompts.

### Showing terminal sessions in the chat panel

Set `AGENT_MANAGER_SESSION_AGENT_URL` (e.g. `http://127.0.0.1:7450`) and, if the agent uses a
token, `AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE` (or `AGENT_MANAGER_SESSION_AGENT_TOKEN`) in the plugin's
environment. The session switcher then
gets a read-only "Terminal sessions" group (status, timer, current step, context size) fed by
`GET api/chat/terminal-sessions` and the SSE passthrough `GET api/chat/terminal-sessions/events`
(the token stays server-side). Unset URL = feature off; agent down = "Session agent unreachable".

### Several session agents (multi-host)

One chat panel can show terminal sessions from several machines, each running its own `session_agent.py`.

| Variable | Meaning |
|---|---|
| `AGENT_MANAGER_SESSION_AGENT_URLS` | JSON list of `{host, url, tokenFile}` objects. `host` (the name shown in the badge and used in `?host=`) and `url` are required; `tokenFile` is optional. An entry missing `host` or `url` is skipped with a log warning, a duplicate `host` keeps the first, a trailing `/` on `url` is dropped. When this variable is set and non-empty it wins over the singular variables below. If its value is not a JSON list, the feature is off (it does not fall back to the singular variables). |
| `AGENT_MANAGER_SESSION_AGENT_URL`, `AGENT_MANAGER_SESSION_AGENT_TOKEN_FILE`, `AGENT_MANAGER_SESSION_AGENT_TOKEN` | The single-host form, unchanged: when the plural variable is unset they still work and give exactly one host named `default`. A token file wins over the static token. |

- **Tokens** are read on every call (a rotated token file needs no restart), stay on the server, and each host's token is sent only to that host; a host with no token gets no `Authorization` header. No token, token path or `tokenFile` value appears in any response body or in the event stream.
- **A host that is down is skipped, not fatal.** `GET api/chat/terminal-sessions` answers `{enabled, reachable, sessions, hosts, skipped}`: `hosts` are the configured names in config order, every row in `sessions` carries a `host`, `skipped` has one `{host, reason}` per host that failed (the reason is the exception class name, or `unexpected shape`), and `reachable` is true while at least one host answered. Hosts that answered are listed as usual.
- **Events.** `GET api/chat/terminal-sessions/events?host=<name>` streams one host's events, tagged with that host. The `host` parameter is optional with a single configured host; with several it is required (400 when missing, 404 for an unknown name, 502 when that host cannot be reached).

Worked example, wok (this machine) plus grimm. Token files live outside the repository, mode 0600. The grimm address below is the one in use when this was written: check `tailscale status` for the current one.

```
AGENT_MANAGER_SESSION_AGENT_URLS='[
  {"host": "wok",   "url": "http://127.0.0.1:7450",      "tokenFile": "/home/wok/.local/state/agent-manager/session-agent.token"},
  {"host": "grimm", "url": "http://100.96.112.78:7450",  "tokenFile": "/home/wok/.config/agent-manager-chat-plugin/grimm-session-agent.token"}
]'
```

The first token file is the default `--token-file` of the local agent (`--token auto`). The second holds a copy of the token the grimm agent printed or wrote; copy it by hand, keep it 0600, and never commit it. On grimm the agent must listen beyond loopback and name the address it is reached by: `session_agent.py --bind 0.0.0.0 --token auto --allow-host 100.96.112.78` (a non-loopback bind refuses to start without a token).

## Model swap (multi-chat)

A live chat can change the model behind it without losing the conversation. Nothing here is checked against a real swap yet; see the `Model swap (manual)` section of `VERIFY.md`.

**Session fields and `schemaVersion` 2.** Each chat records its `provider`, `model` and `account`, plus `swaps` (one `{at, from, to}` entry per swap) and `summary`. `SCHEMA_VERSION` is 2. An older chat without those fields is upgraded automatically the next time it is written (`_migrate_session`): the four new fields get defaults and nothing else changes. The migration is lossless and the transcript is never touched.

**`POST /api/chat/<id>/swap`.** The body is `{provider: 'local' | 'claude', model?, account?}`. For `claude`, `account` must be a label from the accounts registry; leave it out to follow the live account. For `local` any `account` is ignored. An unknown or inactive chat answers 404, a bad provider or an unknown account answers 400. A swap keeps every transcript row, appends one marker row and one `swaps` entry, and always resets `claudeSessionId` to `None`: a Claude CLI session is not assumed resumable after a swap, so the new side gets its history from the transcript. If the chat holds the local reservation and the target is not local, the reservation is released first, the same way `/reserve` releases it. The response is the updated session and never a token or `tokenFile`.

**Hand-off.** On the first Claude turn after a swap, `handoff.build_handoff` prepends a hand-off: a header, the stored local summary if there is one, the last `CHAT_RECENT_TURNS_KEPT` plain rows verbatim (oldest first), and a line saying how many earlier rows are not included. The original rows are never removed or edited. When the transcript would overflow the target provider's window (`handoff.needs_summary`), the swap also asks the local model for a summary of the older rows (`summarize_older`); a summarizer error is logged and the swap carries on with the verbatim hand-off.

**New environment variables.** `AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS` (default 200000) and `AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS` (default 16384) set the context window `handoff.window_tokens` uses per provider. A value that is not a positive integer falls back to the default. Token counts are a coarse estimate (characters / 4), only good enough to decide whether a summary is needed.

**A swap to local never takes the GPU.** Only the existing `/reserve` flow acquires the local reservation; `POST /api/chat/<id>/swap` with `provider: 'local'` never does.

**Account tokens.** The `token_file` argument of `claude_client` names a 0600 file holding an account's OAuth token. It is read at call time and passed only in the environment of the child process as `CLAUDE_CODE_OAUTH_TOKEN`; `os.environ`, the request JSON and log lines never see it. An unreadable or empty file raises `ClaudeClientError` with a fixed message that names neither the path nor any content.

**Limit-error classifier (phrases are ASSUMED).** `limit_errors.classify` recognizes a usage limit by the phrases `usage limit` or `limit reached`, and a rate limit by `429`, `rate limit` or `rate_limit`. No real limit-error text has been recorded, so these phrases are assumptions. A failed Claude turn that matches gets a `swapOffer` on its error event, and `record_sample` appends the error text to `limit-error-samples.jsonl` in the storage directory (mode 0600). **Owner step:** after the first real limit, open `limit-error-samples.jsonl` and send the shapes, so the phrases can be replaced with real ones.

## Multi-chat polish (phase 4)

Small features that make several chats easy to watch. All of them are browser-side and none changes what a chat does. The decisions live in pure modules (`static/attention-view.js`, `static/sessions-view.js`, `static/multi-chat.js` top section) with `node --test` tests; `static/chat.js` and the `multi-chat.js` browser adapter only apply them to the page. Nothing here has been checked in real use yet: see `VERIFY.md` and `docs/phase-4-exit-checklist.md`.

- **Needs-you flash and count.** A terminal session that newly becomes `waiting` flashes amber in the switcher for about 10 seconds (CSS class `waiting-flash`), then stays amber while it waits. The browser tab title gets a `(N) ` prefix with the number of waiting sessions, and every change of that number is posted to the parent page as `window.parent.postMessage({ type: 'chat-attention', count: N }, origin)`. The badge on the dashboard's own tab that would use that message is a separate change in the dashboard repo, not part of this one.
- **Optional chime.** The switcher menu ends with a "Chime when another chat needs me" checkbox, off by default and remembered in `localStorage` under `chat-chime` (`1` or `0`). When it is on and a session newly turns `waiting` and is not on screen, one short beep (880 Hz, about 0.2 s, no audio file) plays. Terminal sessions are never on screen as panes, so every newly waiting terminal session chimes while the box is ticked.
- **Phone width.** Below 760 px the multi-pane page shows one pane (the active chat, or the first open one) and hides the preset buttons. The saved layout in `chat-workspace-layout` is not touched, so widening the window restores it. Resizing is debounced by 150 ms and re-renders only when 760 px is crossed.
- **Layout templates.** The toolbar's Templates menu offers `driver + 2 workers` and `monitor grid` plus any you save with "Save current as...". A template is a preset and a pane count; choosing one keeps the chats already open, fills from the open ones, and pads with empty "New chat" slots. Your own templates live in `localStorage` under `chat-workspace-templates`, per browser.
- **Filter and jump.** The switcher menu starts with a "Filter chats" box that narrows the terminal sessions by name, folder or host as you type. With the menu open, Ctrl+1 to Ctrl+9 highlights the nth terminal session as listed; terminal sessions are read-only, so this is a highlight only. A browser may reserve Ctrl+number for its own tabs.
- **Popout panes are not available yet.** `static/vendor/dockview.js` is vendored, but nothing defines `window.DockviewGrid`, the adapter `multi-chat.js` looks for, so the plain flexbox panes are always used. Popout windows need that adapter first.
