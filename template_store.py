"""Saving reusable templates authored in Chat into the forge apps that consume them.

The local model's shell is read-only, so it cannot write a template file itself, and it
should not save on its own either. Instead it ends a reply with a SAVE-TEMPLATE block;
THIS module turns that into a PROPOSAL shown in the chat as an editable card with a Save
button. Only when the user clicks Save does the plugin validate it and save it through the
target app's own entry API (comment-preserving, the same route the app's own "add entry"
boxes use):

    SAVE-TEMPLATE: scene | high school classroom
    empty high school classroom, wide establishing view, rows of desks, ...
    END-TEMPLATE

Kinds are a closed registry (KINDS) mapping to an app + target; adding a kind is one
entry. Saving never overwrites: an existing key is reported back, not replaced. Every
outcome (saved / refused / failed) is returned as a result dict so the caller can put it in
the transcript -- nothing here is allowed to fail silently.
"""
import os
import re
import threading

import requests

PROMPTFORGE_URL = os.environ.get("PROMPTFORGE_URL", "http://localhost:7430").rstrip("/")

# kind -> where it is saved and how it shows up for the user.
KINDS = {
    "scene": {
        "app": "PromptForge",
        "base_url": lambda: PROMPTFORGE_URL,
        "target": "backgrounds",   # data/backgrounds.yaml == the Setting dropdown
        "where": "the Setting dropdown",
    },
}

MAX_BLOCKS_PER_TURN = 3
MAX_KEY_LEN = 60
MAX_TEXT_LEN = 1500
MIN_TEXT_LEN = 20

_BLOCK_RE = re.compile(
    r"^[ \t]*SAVE-TEMPLATE:[ \t]*(?P<kind>[A-Za-z_-]+)[ \t]*\|[ \t]*(?P<key>[^\n]*)\n"
    r"(?P<text>.*?)"
    r"^[ \t]*END-TEMPLATE[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def _clean_text(raw: str) -> str:
    text = raw.strip()
    # models like to wrap the body in a code fence or add a folded-scalar header
    text = re.sub(r"^```[a-z]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = re.sub(r"^[A-Za-z0-9 _-]+:\s*>\s*\n", "", text)  # "key: >" header line
    return " ".join(text.split())


def parse_blocks(response: str):
    """Returns (cleaned_response_without_blocks, [ {kind,key,text} ... ])."""
    blocks = [
        {"kind": m.group("kind").lower(), "key": m.group("key").strip(), "text": _clean_text(m.group("text"))}
        for m in _BLOCK_RE.finditer(response)
    ]
    return _BLOCK_RE.sub("", response).rstrip() + "\n", blocks


def _validate(block: dict):
    kind = KINDS.get(block["kind"])
    if not kind:
        return f"unknown template kind '{block['kind']}' (known: {', '.join(sorted(KINDS))})"
    key = block["key"]
    if not key or len(key) > MAX_KEY_LEN or re.search(r"[:\[\]{}#&*!|>'\"%@`]", key):
        return f"template name '{key}' is empty, too long, or contains YAML-special characters"
    if len(block["text"]) < MIN_TEXT_LEN or len(block["text"]) > MAX_TEXT_LEN:
        return f"template text must be {MIN_TEXT_LEN}-{MAX_TEXT_LEN} characters (got {len(block['text'])})"
    return None


# --- Per-(target, key) locks that serialise the get-then-add window -----------------------
_save_locks: dict = {}
_locks_guard = threading.Lock()


def _key_lock(target: str, key: str) -> threading.Lock:
    """Return (creating if needed) the per-(target,key) lock that serialises the
    get-then-add sequence so two threads in this process cannot interleave their writes."""
    pair = (target, key)
    with _locks_guard:
        if pair not in _save_locks:
            _save_locks[pair] = threading.Lock()
        return _save_locks[pair]


def save_template(block: dict) -> dict:
    """Saves one parsed block. Returns {ok, kind, key, message}.

    Concurrency: the get+add pair is serialised per (target, key) via an in-process
    threading.Lock, and a post-add verification read detects any cross-process overwrite.
    The "never overwrite" guarantee holds for same-process callers; cross-process races
    are detected and reported rather than silently applied."""
    out = {"ok": False, "kind": block["kind"], "key": block["key"], "message": ""}
    problem = _validate(block)
    if problem:
        out["message"] = problem
        return out
    kind = KINDS[block["kind"]]
    base = kind["base_url"]()
    with _key_lock(kind["target"], block["key"]):
        try:
            r = requests.post(f"{base}/api/entry", json={"action": "get", "target": kind["target"], "key": block["key"]}, timeout=10)
            r.raise_for_status()
            if (r.json().get("value") or "").strip():
                out["message"] = f"'{block['key']}' already exists in {kind['app']} -- not overwritten (pick another name or edit it there)"
                return out
            r = requests.post(
                f"{base}/api/entry",
                json={"action": "add", "target": kind["target"], "path": [], "key": block["key"], "value": block["text"]},
                timeout=15,
            )
            if r.status_code >= 400:
                out["message"] = f"{kind['app']} refused it: {(r.json() or {}).get('error', r.status_code)}"
                return out
            # Post-add verification: detect a cross-process writer that slipped past the
            # in-process lock and overwrote our value between our add and now.
            verify = requests.post(
                f"{base}/api/entry",
                json={"action": "get", "target": kind["target"], "key": block["key"]},
                timeout=10,
            )
            verify.raise_for_status()
            stored = (verify.json().get("value") or "").strip()
            if stored != block["text"].strip():
                out["message"] = (
                    f"'{block['key']}' was saved but then overwritten by a concurrent writer "
                    f"before we could confirm it -- your text was replaced (re-save or edit it in {kind['app']})"
                )
                return out
        except (requests.RequestException, ValueError) as e:
            out["message"] = f"could not reach {kind['app']} at {base} ({e})"
            return out
    out["ok"] = True
    out["message"] = f"Saved {block['kind']} template '{block['key']}' to {kind['app']} -- it now appears in {kind['where']}."
    return out


def extract_proposals(response: str):
    """Strips SAVE-TEMPLATE blocks out of the reply and returns (text, proposals).
    Nothing is saved here -- each proposal is {kind,key,text,status:'proposed',message:''}
    for the UI to render with a Save button. Extra blocks past MAX_BLOCKS_PER_TURN are
    dropped with a visible note rather than silently."""
    cleaned, blocks = parse_blocks(response)
    if not blocks:
        return response, []
    proposals = [{"kind": b["kind"], "key": b["key"], "text": b["text"], "status": "proposed", "message": ""}
                 for b in blocks[:MAX_BLOCKS_PER_TURN]]
    text = cleaned.rstrip() + "\n"
    if len(blocks) > MAX_BLOCKS_PER_TURN:
        text += f"\n*(Only the first {MAX_BLOCKS_PER_TURN} template proposals are shown; {len(blocks) - MAX_BLOCKS_PER_TURN} more were dropped.)*\n"
    return text, proposals


def save_proposal(session: dict, entry_index: int, tpl_index: int, key=None, text=None) -> dict:
    """Saves proposal `tpl_index` on transcript entry `entry_index` (optionally with the
    user's edited name/text), records the outcome on the proposal, and returns it. The
    caller persists the session. A saved proposal is never saved twice."""
    try:
        prop = session["transcript"][entry_index]["templates"][tpl_index]
    except (IndexError, KeyError, TypeError):
        raise LookupError("no such template proposal")
    if prop["status"] == "saved":
        return prop
    if key is not None:
        prop["key"] = str(key).strip()
    if text is not None:
        prop["text"] = _clean_text(str(text))
    result = save_template(prop)
    prop["status"] = "saved" if result["ok"] else "failed"
    prop["message"] = result["message"]
    return prop
