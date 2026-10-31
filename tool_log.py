"""A compact, safe summary of the local model's tool calls, stored on the assistant transcript entry (2026-10-25).

In the sale-lookup test the transcript showed only narration, so the owner could not tell which commands ran. core's final event carries
`toolCallLog: [{tool, args, result}]`; this keeps the name, a clipped copy of the arguments with secrets hidden, whether the call worked, and a short
outcome. Results are NOT stored (a file read could contain a credential): a success keeps only its size, a failure its clipped error text."""
import json
import re

MAX_TOOLS = 50
MAX_TEXT = 200
_SECRET_KEY = re.compile(r"token|key|secret|password", re.I)
_SECRET_INLINE = re.compile(r"(?i)\b(token|secret|password|api[_-]?key)(\s*[=:]\s*)\S+")
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


def _scrub(text: str) -> str:
    return _BEARER.sub("Bearer [hidden]", _SECRET_INLINE.sub(lambda m: m.group(1) + m.group(2) + "[hidden]", text))


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def _args_text(args) -> str:
    if isinstance(args, dict):
        args = {k: "[hidden]" if _SECRET_KEY.search(str(k)) else v for k, v in args.items()}
    text = args if isinstance(args, str) else json.dumps(args, default=str, ensure_ascii=False)
    return _clip(_scrub(text))


_BACKGROUND_CMD = re.compile(r"(?:\bnohup\b|(?<!&)&\s*$)")


def is_background_command(command) -> bool:
    """True for a shell command that starts a background process: it contains nohup or ends with a single & (not &&)."""
    return isinstance(command, str) and bool(_BACKGROUND_CMD.search(command.strip()))


def _outcome(result):
    """(ok, one-line outcome) for a tool result; only an error's text is kept."""
    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
        return False, _clip(_scrub(str(result.get("error") or "failed")))
    size = len(result) if isinstance(result, str) else len(json.dumps(result, default=str, ensure_ascii=False))
    return True, f"{size} chars returned"


def summarize_tool_calls(tool_call_log) -> list:
    """[{name, args, ok, summary}] for the first MAX_TOOLS entries of core's toolCallLog; anything that is not a list gives []."""
    if not isinstance(tool_call_log, list):
        return []
    out = []
    for call in tool_call_log:
        if not isinstance(call, dict):
            continue
        ok, summary = _outcome(call.get("result"))
        entry = {"name": str(call.get("tool") or "tool")[:60], "args": _args_text(call.get("args")), "ok": ok, "summary": summary}
        args = call.get("args")
        # Decided here on the unclipped command: the stored args text is cut at MAX_TEXT, which can hide a trailing &. Only set when true.
        if entry["name"] == "run_bash" and isinstance(args, dict) and is_background_command(args.get("command")):
            entry["background"] = True
        out.append(entry)
        if len(out) >= MAX_TOOLS:
            break
    return out


OUTPUT_MAX = 4000


def scrub_output(text, limit: int = OUTPUT_MAX) -> str:
    """A command's stdout or stderr as stored on the transcript: secrets hidden with the same rules as the tool arguments, then cut to `limit` characters."""
    text = _scrub("" if text is None else str(text))
    return text if len(text) <= limit else text[:limit - 1] + "…"
