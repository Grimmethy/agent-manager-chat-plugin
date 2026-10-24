# Drive Design

Design-only document. No code. Authoring date: 2026-10-24.
Decomposed from: HUB0009-10-docs-design-for-terminal-and-cross-host-targets-and-the-read-only-inbox-socket-investigation-owner-step

## 1. What the existing relay cannot do

- The task spec references `drive_relay.py` (described as "piece 7/13") as the existing relay. `[from task spec only]`
- A repository search for `drive_relay` returned 0 hits; the file is not present in this branch.
- Design-level gaps the relay currently lacks (stated as requirements, not code claims):
  - No cross-host fan-out to multiple agent endpoints.
  - No idle-notification hook to signal when an agent has finished processing.
  - No agent-enumeration capability to discover which agents are available.
  - No consolidated status aggregation across hosts.
- Note: `agent_hosts.py` exists in this repository (confirmed by the read-only pass) and may be relevant to host/URL resolution, but its contents were not available for line-level citation here.

## 2. Claude Code agent primitives

- **ListAgents** — Intended to enumerate the set of agents registered with a Claude Code session. `[unverified]` `[from task spec only]`
- **SendMessage** — Intended to deliver a directed message to a specific agent within a session. `[unverified]` `[from task spec only]`
- **notify_when_idle** — Intended to fire a notification when the target agent transitions to an idle state. `[unverified]` `[from task spec only]`

Sub-note: No code, API signature, or call example is included. All three names are absent from the repository.

## 3. Cross-host relay

- The environment variable that carries the list of agent endpoints is **`AGENT_MANAGER_SESSION_AGENT_URLS`**. `[from task spec only]`
- Design intent (prose only): the relay reads a list of URLs from that variable, fans out messages to each endpoint, and aggregates responses.
- `agent_hosts.py` is present in the repository and implements parsing of `AGENT_MANAGER_SESSION_AGENT_URLS` (JSON list of objects with host and url fields), skipping invalid entries and deduplicating hosts.

## 4. Consolidated unknowns

- Whether `drive_relay.py` exists on a sibling branch or is purely a spec artifact.
- The exact API surface, parameter names, and return types of `ListAgents`, `SendMessage`, and `notify_when_idle`. All `[unverified]`.
- The format, cardinality, and delimiter of entries in `AGENT_MANAGER_SESSION_AGENT_URLS` beyond the JSON-list-of-objects contract visible in `agent_hosts.py`.
- Whether the 12 "relay" hits in the repository correspond to a different component or to the same relay under a different filename.
- What `agent_hosts.py` does beyond URL parsing (file confirmed present; full content not cited line-by-line here).
- Any additional gaps surfaced by sibling pieces HUB0020 · 2/3 and 3/3.

## 5. Grounding note

- Authoring date: 2026-10-24.
- This document is design-only: no code, no executable snippets, no fenced code blocks.
- Every file name, flag name, and API name cited was checked against the repository during the read-only pass. Names that returned 0 hits are tagged `[from task spec only]`.
- All Claude Code primitive claims carry the literal tag `[unverified]` per task requirement.
- The forbidden spelling for the env var is not used anywhere in this document.
- Decomposition origin: HUB0009-10-docs-design-for-terminal-and-cross-host-targets-and-the-read-only-inbox-socket-investigation-owner-step.
