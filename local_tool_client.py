"""Thin compatibility shim over internal_api_client.py's stream_local_turn().

2026-09-15 (Phase 3 of the Chat-plugin extraction -- see
/home/wok/.claude/plans/immutable-noodling-axolotl.md): this module used to shell out
directly to the core repo's src/local-tool-client.js (Phase 1 -- see git history for
that version). That coupling (getConfig(), instances/, queue/, the GPU single-flight
lock coordinating with live workers) now lives entirely behind the internal API
instead -- this module keeps the SAME public name/signature chat_sessions.py already
calls (`local_tool_client.stream_plan_with_tools(...)`) so nothing else needed to
change, but every call now goes over HTTP to the core dashboard rather than spawning a
local Node subprocess.

run_plan_with_tools() (the non-streaming sibling) is NOT ported -- nothing in this repo
calls it (chat_sessions.py only ever uses the streaming path); add it here, backed by a
new core-side /api/internal/chat/local-turn-sync route, if a future caller needs it.
"""
from internal_api_client import InternalApiError as LocalToolClientError
from internal_api_client import stream_local_turn


def stream_plan_with_tools(messages: list = None, prompt: str = None, max_turns: int = 5,
                            source: str = None, allow_write: bool = False,
                            primary_root: str = None, extra_roots: list = None,
                            force_summary_on_cap: bool = False, allow_amplification: bool = False):
    """Same generator contract the in-tree/Phase-1 version had: yields {"type": "chunk",
    ...} as text arrives, then exactly one {"type": "final", ...}. Delegates entirely to
    internal_api_client.stream_local_turn() -- see that function's own header."""
    yield from stream_local_turn(
        messages=messages, prompt=prompt, max_turns=max_turns, source=source,
        allow_write=allow_write, primary_root=primary_root, extra_roots=extra_roots,
        force_summary_on_cap=force_summary_on_cap, allow_amplification=allow_amplification,
    )
