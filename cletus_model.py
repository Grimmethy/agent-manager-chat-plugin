"""Cletus gateway, part 2b: the model stream.

generate_stream(payload) is the generator the route streams from: it asks the resident Ollama model for an answer to the messages
cletus_prompt builds, yields the answer text as it arrives, and RETURNS the validated sources.

 * The request uses ollama_client's pinned values (num_ctx from pinned_num_ctx(), keep_alive from KEEP_ALIVE, the model from MODEL()),
   read when the request is built and never hard coded here: the owner rule is never to unload or swap a model, and a request with a
   different num_ctx would make Ollama reload it. Temperature 0.3, at most 500 tokens, no thinking, streaming.
 * The model ends an answer with one line starting SOURCES:. That line is WITHHELD from the streamed text (SourcesFilter), and its ids
   are checked against the payload: a lesson id must be one of the kb sections supplied, a field name one of the property fields. An
   id the gateway was not given is dropped, so a model cannot cite something invented.
 * No model configured, a connection error, a timeout or a broken stream raise gw.Napping, which the route turns into a napping error.
   Nothing here logs a question or an answer.
"""
import json
import re
import urllib.error
import urllib.request

import cletus_gateway as gw
import cletus_prompt
import ollama_client

temperature = 0.3
max_tokens = 500
request_timeout_s = 300
sources_marker = "SOURCES:"


class SourcesFilter:
    """Passes answer text through but holds back a line that starts with SOURCES: and everything after it."""

    def __init__(self):
        self.sources_line = None
        self._held = ""
        self._plain = False  # the current line cannot be the sources line any more

    def feed(self, text):
        out = []
        for ch in text:
            if self.sources_line is not None:
                self.sources_line += ch
                continue
            if self._plain:
                out.append(ch)
                if ch == "\n":
                    self._plain = False
                continue
            self._held += ch
            if self._held.startswith(sources_marker):
                self.sources_line = self._held
                self._held = ""
            elif sources_marker.startswith(self._held):
                continue  # it may still turn into the marker: hold it
            else:
                out.append(self._held)
                self._held = ""
                self._plain = ch != "\n"
        return "".join(out)

    def finish(self):
        """The text still held when the answer ended (a line that began like the marker but never became it)."""
        tail = self._held
        self._held = ""
        return tail


class IdFilter:
    """Removes internal id tokens (lesson:<id>, field:<name>, property:<id>) from the streamed answer text.

    The prompt tells the model never to write ids, but it sometimes does, for example "(lesson:ba-u02-s01)". A token inside
    parentheses is removed with the parentheses and the space before them; a bare token is removed with the space before it.
    Text that could still turn into a token (a chunk that ends in "lesson:ba-u0", "(", "(les") is held until it resolves, the same
    way SourcesFilter holds a partial SOURCES: marker, so a token split across chunks is removed too. finish() flushes what is held.
    """

    _names = "lesson|field|property"
    _token = re.compile(r"[ \t]?\(\s*(?:%s):[\w.\-]+\s*\)|[ \t]?(?:%s):[\w.\-]+" % (_names, _names), re.IGNORECASE)
    _open_tail = re.compile(r"(?:[ \t]?\(\s*(?:%s):[\w.\-]*\s*|[ \t]?(?:%s):[\w.\-]*)$" % (_names, _names), re.IGNORECASE)
    _prefixes = {lead + (name + ":")[:j] for lead in ("", " ", "(", " (", "( ", " ( ", "\t") for name in ("lesson", "field", "property")
                 for j in range(0, len(name) + 2)} - {""}

    def __init__(self):
        self._buf = ""

    def _clean(self, text):
        return self._token.sub("", text)

    def _hold_at(self, buf):
        m = self._open_tail.search(buf)
        at = m.start() if m else len(buf)
        for k in range(min(len(buf), 12), 0, -1):
            if buf[-k:].lower() in self._prefixes and len(buf) - k < at:
                at = len(buf) - k
        return at

    def feed(self, text):
        self._buf += text
        at = self._hold_at(self._buf)
        out, self._buf = self._buf[:at], self._buf[at:]
        return self._clean(out)

    def finish(self):
        out, self._buf = self._buf, ""
        return self._clean(out)


class PhraseFilter:
    """Rewrites the internal phrase "data block" (with a leading your, the or my) to "the information I have" in the streamed text.

    The prompt forbids the words, but the model still says "none of the lessons in your data block". A chunk that ends in a possible
    start of the phrase ("your da", "data") is held until it resolves, as IdFilter and SourcesFilter hold theirs; finish() flushes.
    """

    _phrase = re.compile(r"\b(?:(?:your|the|my)\s+)?data\s+block\b", re.IGNORECASE)
    _prefixes = {full[:j] for full in ("your data block", "the data block", "my data block", "data block") for j in range(1, len(full) + 1)}
    replacement = "the information I have"

    def __init__(self):
        self._buf = ""

    def _hold_at(self, buf):
        at = len(buf)
        for k in range(min(len(buf), 16), 0, -1):
            tail = buf[-k:].lower()  # a possible start of the phrase, at a word boundary
            if tail in self._prefixes and len(buf) - k < at and (k == len(buf) or not buf[-k - 1].isalnum()):
                at = len(buf) - k
        return at

    def feed(self, text):
        self._buf += text
        at = self._hold_at(self._buf)
        out, self._buf = self._buf[:at], self._buf[at:]
        return self._rewrite(out)

    def _rewrite(self, text):
        def swap(m):
            r = self.replacement
            return r[0].upper() + r[1:] if m.group(0)[0].isupper() else r
        return self._phrase.sub(swap, text)

    def finish(self):
        out, self._buf = self._buf, ""
        return self._rewrite(out)


def parse_sources(line, payload):
    """The ids on a SOURCES: line that match what the payload supplied, in order, without repeats."""
    context = payload.get("context") or {}
    lessons = {item["sectionId"]: item["lessonId"] for item in (context.get("kb") or [])}
    fields = set(((context.get("property") or {}).get("fields") or {}).keys())
    out = []
    seen = set()
    for token in re.split(r"[,\s]+", line[len(sources_marker):].strip()):
        if not token or token in seen:
            continue
        kind, _, ident = token.partition(":")
        if kind == "lesson" and ident in lessons:
            out.append({"kind": "lesson", "lessonId": lessons[ident], "sectionId": ident})
        elif kind == "field" and ident in fields:
            out.append({"kind": "field", "name": ident})
        else:
            continue
        seen.add(token)
    return out


def request_body(payload, model):
    return {
        "model": model,
        "messages": cletus_prompt.build_messages(payload),
        "stream": True,
        "think": False,
        "keep_alive": ollama_client.KEEP_ALIVE,
        "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": ollama_client.pinned_num_ctx()},
    }


def _default_post(body):
    """POST to Ollama's /api/chat and yield each streamed JSON line."""
    req = urllib.request.Request(
        f"{ollama_client.OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=request_timeout_s) as resp:
        for raw in resp:
            line = raw.strip()
            if line:
                yield json.loads(line)


def generate_stream(payload, post=None):
    model = ollama_client.MODEL()
    if not model:
        raise gw.Napping()
    body = request_body(payload, model)
    send = post or _default_post
    flt = SourcesFilter()
    ids = IdFilter()
    phrases = PhraseFilter()
    try:
        for part in send(body):
            if not isinstance(part, dict):
                continue
            if part.get("error"):
                raise gw.Napping()
            text = (part.get("message") or {}).get("content") or ""
            if text:
                out = phrases.feed(ids.feed(flt.feed(text)))
                if out:
                    yield out
            if part.get("done"):
                break
    except gw.Napping:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise gw.Napping() from e
    tail = phrases.feed(ids.feed(flt.finish()) + ids.finish()) + phrases.finish()
    if tail:
        yield tail
    return parse_sources(flt.sources_line, payload) if flt.sources_line else []
