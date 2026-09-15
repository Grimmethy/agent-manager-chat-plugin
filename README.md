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

Building a standalone-but-still-directly-coupled version first (same worker-preemption/
GPU-arbiter coupling as the in-tree feature, just relocated here) so it's independently
testable before Phase 2/3 swap that coupling for a small internal API agent-manager
exposes. The in-tree Chat feature in `agent-manager` is untouched and fully live until
cutover (Phase 4).

Files currently present are a **starting-point copy** from agent-manager's
`python/dashboard/` (`chat_sessions.py`, `claude_client.py`, `claude-client.js`) plus two
`_original` reference copies (`chat_routes_original.py`, `local_tool_client_original.py`)
kept only so the real adapted versions can be diffed against what they came from during
Phase 1 build-out -- not meant to ship as-is.

## Running (once Phase 1 lands a real app.py)

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
curl localhost:7442/healthz
```
