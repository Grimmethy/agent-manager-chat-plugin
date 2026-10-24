"""Pure hand-off helpers for swapping the model behind a live chat (multi-chat, HUB0008 5/11).

No I/O here: transcript rows are the same dicts `chat_sessions.py` stores --
`{"role": "user"|"assistant", "text": ...}` -- where a failure notice row carries a
truthy `"error"` key and `swap_session`'s audit row carries `"marker": True` (both
skips mirror `_build_local_messages` in chat_sessions.py). No environment access
except `window_tokens`, and no file/network access at all.

New environment variables (read per call, by `window_tokens` only):
  AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS -- context window for 'claude' (default 200000)
  AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS  -- context window for 'local'  (default 16384)
A value that is not a positive integer falls back to the default.

Token math is deliberately coarse (`estimate_tokens` = ceil(chars/4)) -- good enough
to decide "do we need a summary" and "does this hand-off fit", never a real tokenizer.
"""
import math
import os

HANDOFF_HEADER = "HANDOFF: this conversation is continuing under a new model; " \
                 "here is what happened before."

_DEFAULT_WINDOWS = {"claude": 200000, "local": 16384}
_ENV_VARS = {
    "claude": "AGENT_MANAGER_CHAT_CLAUDE_WINDOW_TOKENS",
    "local": "AGENT_MANAGER_CHAT_LOCAL_WINDOW_TOKENS",
}


def window_tokens(provider):
    """Context window in tokens for 'claude' or 'local', read from the environment
    on every call; a missing, non-integer or non-positive value falls back to the
    documented default for that provider."""
    if provider not in _DEFAULT_WINDOWS:
        raise ValueError("unknown provider %r" % (provider,))
    value = os.environ.get(_ENV_VARS[provider])
    if value is not None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = None
        if value is not None and value > 0:
            return value
    return _DEFAULT_WINDOWS[provider]


def estimate_tokens(text):
    """Rough token count: one token per four characters, rounded up."""
    return math.ceil(len(text or "") / 4)


def _is_plain_row(row):
    """True for a normal user/assistant turn -- not a failure notice and not a
    swap_session audit marker (the exact two skips chat_sessions._build_local_messages uses)."""
    if not isinstance(row, dict):
        return False
    if row.get("error"):
        return False
    if row.get("marker") is True:
        return False
    return True


def transcript_tokens(transcript):
    """Sum of estimate_tokens over every non-error, non-marker row."""
    total = 0
    for row in transcript or []:
        if _is_plain_row(row):
            total += estimate_tokens(row.get("text") or "")
    return total


def needs_summary(transcript, provider, reserve_fraction=0.5):
    """True when the transcript alone would eat more than `reserve_fraction` of the
    provider's window -- by default half the window must stay free for the reply and tools."""
    return transcript_tokens(transcript) > reserve_fraction * window_tokens(provider)


def build_handoff(transcript, summary_text, keep_turns, max_tokens):
    """Render the hand-off text for the new model: header, optional summary, the last
    `keep_turns` plain rows verbatim as 'User: ...' / 'Assistant: ...' (oldest first),
    dropping the oldest kept rows until the whole thing fits `max_tokens`, and one line
    stating how many earlier rows are not included. An empty transcript returns the
    header line only and never raises."""
    plain = [row for row in (transcript or []) if _is_plain_row(row)]
    if keep_turns and keep_turns > 0:
        kept = plain[-keep_turns:]
    else:
        kept = []

    def render(kept):
        lines = [HANDOFF_HEADER]
        if summary_text:
            lines.append(summary_text)
        for row in kept:
            prefix = "Assistant" if row.get("role") == "assistant" else "User"
            lines.append("%s: %s" % (prefix, row.get("text") or ""))
        omitted = len(plain) - len(kept)
        if omitted > 0:
            lines.append("%d earlier row(s) are not included." % omitted)
        return "\n".join(lines)

    result = render(kept)
    while kept and estimate_tokens(result) > max_tokens:
        kept = kept[1:]  # drop the OLDEST kept row first
        result = render(kept)
    return result
