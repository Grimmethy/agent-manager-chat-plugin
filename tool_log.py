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


def _outcome(result):
    """(ok, one-line outcome) for a tool result; only an error's text is kept."""
    if isinstance(result, dict) and (result.get("error") or result.get("ok") is False):
        return False, _clip(_scrub(str(result.get("error") or "failed")))
    size = len(result) if isinstance(result, str) else len(json.dumps(result, default=str, ensure_ascii=False))
    return True, f"{size} chars returned"


def summarize_tool_calls(tool_call_log) -> list:
    """[{name, args, ok, summary}] for the first MAX_TOOLS entries of core's toolCallLog; anything that is not a list gives [].

    For `record_branch_verdict` calls, three extra scrubbed fields are preserved
    BEFORE args are clipped: `branch`, `verdict`, and `project`. These come from
    the call's `args` (branch, verdict, project) and `result` (project) so the
    final-text audit can build a deterministic ledger even when the args payload
    would normally exceed MAX_TEXT and get truncated.
    """
    if not isinstance(tool_call_log, list):
        return []
    out = []
    for call in tool_call_log:
        if not isinstance(call, dict):
            continue
        ok, summary = _outcome(call.get("result"))
        entry = {"name": str(call.get("tool") or "tool")[:60], "args": _args_text(call.get("args")), "ok": ok, "summary": summary}
        # 2026-10-29 (HUB0032 redo): preserve verdict fields for record_branch_verdict
        # calls BEFORE args are clipped, so the ledger has data even for long args.
        if str(call.get("tool")) == "record_branch_verdict":
            args = call.get("args") or {}
            if isinstance(args, dict):
                entry["branch"] = str(args.get("branch") or "")[:80]
                entry["verdict"] = str(args.get("verdict") or "")[:40]
                entry["project"] = str(args.get("project") or "")[:80]
            # Fall back to result if project wasn't in args
            result = call.get("result") or {}
            if not entry.get("project") and isinstance(result, dict):
                entry["project"] = str(result.get("project") or "")[:80]
        out.append(entry)
        if len(out) >= MAX_TOOLS:
            break
    return out


class VerdictLedger:
    """Tracks the LAST recorded verdict per (project, branch) and how many times
    each branch's verdict was changed during a turn.

    The model in the walkthrough rewrote its own summary from memory; this ledger
    is the single source of truth for what was actually recorded. The final-text
    audit compares the model's stated counts against this ledger and prepends a
    'Correction:' line when they disagree.
    """

    def __init__(self):
        self._last_verdicts: dict[tuple[str, str], str] = {}
        self._change_counts: dict[tuple[str, str], int] = {}

    def record(self, project: str, branch: str, verdict: str) -> None:
        """Record a verdict for (project, branch). Last call wins; change count increments on overwrite."""
        key = (project, branch)
        if key in self._last_verdicts:
            self._change_counts[key] = self._change_counts.get(key, 0) + 1
        self._last_verdicts[key] = verdict

    def build_from_tool_entries(self, tool_entries: list) -> None:
        """Populate from summarize_tool_calls output (the real code path)."""
        for entry in tool_entries:
            if entry.get("name") == "record_branch_verdict":
                project = entry.get("project") or ""
                branch = entry.get("branch") or ""
                verdict = entry.get("verdict") or ""
                if project and branch and verdict:
                    self.record(project, branch, verdict)

    def format(self) -> str:
        """Return the deterministic ledger block, or empty string if no verdicts."""
        if not self._last_verdicts:
            return ""
        counts = {"merge": 0, "needs-work": 0, "discard": 0}
        for v in self._last_verdicts.values():
            if v in counts:
                counts[v] += 1
        lines = ["Verdict Ledger:", f"  merge: {counts['merge']}", f"  needs-work: {counts['needs-work']}", f"  discard: {counts['discard']}"]
        for (project, branch) in sorted(self._last_verdicts.keys()):
            v = self._last_verdicts[(project, branch)]
            n = self._change_counts.get((project, branch), 0)
            if n:
                lines.append(f"  {project}/{branch}: {v} (changed {n} time{'s' if n != 1 else ''})")
            else:
                lines.append(f"  {project}/{branch}: {v}")
        return "\n".join(lines)

    def detect_disagreement(self, model_text: str) -> str | None:
        """Check if model text states counts that disagree with the ledger.

        Returns a 'Correction: ...' string if counts differ, else None.
        Only checks 'merge' and 'needs-work' counts since discard is implicit.
        Matches patterns like:
        - "2 branches approved for merge"
        - "3 branches merge"
        - "1 needs-work"
        - "0 discard"
        
        Only flags disagreement if the model text actually states counts
        (i.e., has at least one match). If the model text has no count patterns,
        we assume the model is summarizing without stating explicit counts, so
        no correction is needed.
        """
        model_counts = {"merge": 0, "needs-work": 0, "discard": 0}
        found_any = False
        
        # Pattern 1: "N branches (approved for) merge/needs-work/discard"
        pattern1 = r"(\d+)\s+branches?\s+(?:approved\s+for\s+)?(merge|needs-work|discard)"
        for match in re.finditer(pattern1, model_text, re.IGNORECASE):
            found_any = True
            n = int(match.group(1))
            verdict = match.group(2).lower()
            if verdict in model_counts:
                model_counts[verdict] = n
        
        # Pattern 2: "N needs-work" or "N discard" (without "branches" prefix)
        pattern2 = r"(\d+)\s+(needs-work|discard)"
        for match in re.finditer(pattern2, model_text, re.IGNORECASE):
            found_any = True
            n = int(match.group(1))
            verdict = match.group(2).lower()
            if verdict in model_counts:
                model_counts[verdict] = n

        # If model text has no count patterns, no disagreement to flag
        if not found_any:
            return None

        actual = {"merge": 0, "needs-work": 0, "discard": 0}
        for v in self._last_verdicts.values():
            if v in actual:
                actual[v] += 1

        if model_counts["merge"] != actual["merge"] or model_counts["needs-work"] != actual["needs-work"]:
            return f"Correction: the cards show {actual['merge']} merge, {actual['needs-work']} needs-work, {actual['discard']} discard"
        return None


OUTPUT_MAX = 4000


def scrub_output(text, limit: int = OUTPUT_MAX) -> str:
    """A command's stdout or stderr as stored on the transcript: secrets hidden with the same rules as the tool arguments, then cut to `limit` characters."""
    text = _scrub("" if text is None else str(text))
    return text if len(text) <= limit else text[:limit - 1] + "…"
