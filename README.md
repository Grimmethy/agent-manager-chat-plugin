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
- `contextWindow` is `null` unless `--windows` maps a model-id substring to a size, e.g.
  `{"some-model-substring": 123456}` (the agent ships no numbers).
- Transcripts are private: loopback bind by default; a non-loopback `--bind` refuses to start
  without a token, and with a token every request needs `Authorization: Bearer <token>`.
  Message text is never logged.
- systemd template: `deploy/session-agent.service`.
