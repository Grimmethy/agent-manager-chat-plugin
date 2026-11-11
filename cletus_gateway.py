"""Cletus gateway, part 1: signed requests, a one-at-a-time queue and a once-a-minute preempt.

The Cletus gateway is a narrow, stateless door in front of the resident local model. Only pf-backend calls it (never a browser):
it sends one question with the context pf-backend already looked up, and the gateway streams the answer back. This module holds
the parts that need no model, so they can be tested without one:

  verify_signature(secret, timestamp, raw_body, signature, now) -> bool
      X-Cletus-Signature must be the lowercase hex HMAC-SHA256 of "<timestamp>.<raw body>" with the shared secret, and
      X-Cletus-Timestamp (unix seconds) must be within 60 seconds of now. Constant-time comparison.
  validate_payload(data) -> (cleaned or None, error text or None)
      the request body shape and size limits of the contract.
  CallQueue
      ONE model call at a time; up to 2 requests may wait. enter() raises Busy for a request that would be the 4th, and returns a
      ticket whose .ahead says how many were in front. ticket.wait(timeout) blocks until it is this request's turn and raises
      Napping on timeout. ticket.release() hands the turn on (safe to call twice, and a ticket that never got a turn releases nothing).
  PreemptLimiter
      runs the chat plugin's own preempt at most once per 60 seconds across all requests; a failed preempt is logged and swallowed.
  stream_answer(payload, generate_stream) -> frames
      the response frames of the contract (chunk, then final). generate_stream(payload) is injected: a generator yielding text
      chunks whose return value is the list of sources (the model part supplies the real one).

Nothing here stores anything, and nothing here logs a question or an answer.
"""
import collections
import hashlib
import hmac
import threading
import time

signature_window_s = 60
max_waiting = 2
wait_timeout_s = 120
preempt_every_s = 60
question_max = 1000
history_max = 10
history_content_max = 2000
kb_max = 4
kb_text_max = 5000

history_roles = ("user", "assistant")
scalar_types = (str, int, float, bool, type(None))


class Busy(Exception):
    """Two requests are already waiting behind the running one."""


class Napping(Exception):
    """The model is away or the wait for a turn ran out."""


def verify_signature(secret, timestamp, raw_body, signature, now=None):
    if not secret:
        return False
    try:
        ts = int(str(timestamp))
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > signature_window_s:
        return False
    body = raw_body if isinstance(raw_body, bytes) else str(raw_body).encode("utf-8")
    message = str(timestamp).encode("utf-8") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature or ""))


def _is_text(v, limit):
    return isinstance(v, str) and len(v) <= limit


rankings_max = 3


def _is_number_or_none(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e12)


def _validate_rankings(rankings):
    """context.rankings: the top counties by median cap rate for a where-to-look question. Returns why it is refused, or None."""
    if not isinstance(rankings, dict) or rankings.get("kind") != "counties":
        return "context.rankings must be an object of kind counties"
    if not _is_number_or_none(rankings.get("minProperties")):
        return "context.rankings.minProperties must be a number or null"
    top = rankings.get("top")
    if not isinstance(top, list) or not 1 <= len(top) <= rankings_max:
        return "context.rankings.top must be a list of 1 to 3 counties"
    for item in top:
        if not isinstance(item, dict) or not _is_text(item.get("key"), 64) or not item.get("key") or not _is_text(item.get("county"), 80) or not item.get("county"):
            return "each ranked county needs a key and a county name"
        state = item.get("state")
        if state is not None and not _is_text(state, 2):
            return "a ranked county's state must be two letters or null"
        if not isinstance(item.get("capRate"), (int, float)) or isinstance(item.get("capRate"), bool) or not _is_number_or_none(item.get("capRate")):
            return "a ranked county needs a numeric capRate"
        if not all(_is_number_or_none(item.get(k)) for k in ("capRateN", "properties", "mailablePct")):
            return "capRateN, properties and mailablePct must be numbers or null"
    return None


def validate_payload(data):
    """Check the request body against the contract. Returns (cleaned payload, None) or (None, why it is refused)."""
    if not isinstance(data, dict):
        return None, "body must be an object"
    question = data.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > question_max:
        return None, "question must be 1 to 1000 characters"
    history = data.get("history", [])
    if not isinstance(history, list) or len(history) > history_max:
        return None, "history must be a list of at most 10 messages"
    for m in history:
        if not isinstance(m, dict) or m.get("role") not in history_roles or not _is_text(m.get("content"), history_content_max):
            return None, "each history message needs a role and content up to 2000 characters"
    context = data.get("context")
    if not isinstance(context, dict):
        return None, "context must be an object"
    page = context.get("page")
    if not isinstance(page, dict) or not _is_text(page.get("view"), 100):
        return None, "context.page needs a view"
    for key in ("lessonTitle", "unitId", "sectionId"):
        v = page.get(key)
        if v is not None and not _is_text(v, 300):
            return None, f"context.page.{key} must be text or null"
    prop = context.get("property")
    if prop is not None:
        if not isinstance(prop, dict) or not _is_text(prop.get("id"), 100) or not isinstance(prop.get("fields"), dict):
            return None, "context.property needs an id and fields"
        for name, value in prop["fields"].items():
            if not isinstance(name, str) or len(name) > 100 or not isinstance(value, scalar_types) or (isinstance(value, str) and len(value) > 1000):
                return None, "context.property.fields must be short scalar values"
    kb = context.get("kb", [])
    if not isinstance(kb, list) or len(kb) > kb_max:
        return None, "context.kb must be a list of at most 4 sections"
    for item in kb:
        if not isinstance(item, dict) or not all(_is_text(item.get(k), kb_text_max if k == "text" else 300) for k in ("lessonId", "sectionId", "title", "text")):
            return None, "each kb section needs lessonId, sectionId, title and text"
    cleaned = {"page": page, "property": prop, "kb": kb}
    if context.get("rankings") is not None:
        why = _validate_rankings(context["rankings"])
        if why:
            return None, why
        cleaned["rankings"] = context["rankings"]
    return {"question": question.strip(), "history": history, "context": cleaned}, None


class Ticket:
    """One request's place in the queue."""

    def __init__(self, queue, ahead, running):
        self._queue = queue
        self.ahead = ahead
        self.running = running

    def wait(self, timeout_s):
        """Block until it is this request's turn. Raises Napping when the turn does not come in time."""
        self._queue._wait_for_turn(self, timeout_s)

    def release(self):
        self._queue._release(self)


class CallQueue:
    def __init__(self, waiting_limit=max_waiting, clock=time.monotonic):
        self.waiting_limit = waiting_limit
        self._clock = clock
        self._cond = threading.Condition()
        self._running = False
        self._line = collections.deque()

    def enter(self):
        with self._cond:
            if len(self._line) >= self.waiting_limit:
                raise Busy()
            if not self._running and not self._line:
                self._running = True
                return Ticket(self, 0, True)
            ticket = Ticket(self, len(self._line) + (1 if self._running else 0), False)
            self._line.append(ticket)
            return ticket

    def _wait_for_turn(self, ticket, timeout_s):
        with self._cond:
            if ticket.running:
                return
            deadline = self._clock() + timeout_s
            while True:
                if not self._running and self._line and self._line[0] is ticket:
                    self._line.popleft()
                    self._running = True
                    ticket.running = True
                    return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    if ticket in self._line:
                        self._line.remove(ticket)
                    self._cond.notify_all()
                    raise Napping()
                self._cond.wait(remaining)

    def _release(self, ticket):
        with self._cond:
            if ticket.running:
                ticket.running = False
                self._running = False
            elif ticket in self._line:
                self._line.remove(ticket)
            self._cond.notify_all()


class PreemptLimiter:
    def __init__(self, every_s=preempt_every_s, clock=time.monotonic):
        self.every_s = every_s
        self._clock = clock
        self._lock = threading.Lock()
        self._last = None

    def run(self, preempt_fn, log=None):
        """Call preempt_fn unless it ran within the last minute. Returns True when it was called. A failure is logged, never raised."""
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self.every_s:
                return False
            self._last = now
        try:
            preempt_fn()
        except Exception as e:  # noqa: BLE001 -- a failed preempt must never fail the request
            if log:
                log(f"[cletus] preempt failed ({type(e).__name__})")
        return True


def default_generate_stream(payload):
    """The real model stream (cletus_model), imported when first used so this module stays free of the model client."""
    import cletus_model
    return (yield from cletus_model.generate_stream(payload))


def stream_answer(payload, generate_stream):
    """Yield the response frames: a chunk frame per piece of text, then a final frame with the sources."""
    gen = generate_stream(payload)
    sources = []
    while True:
        try:
            chunk = next(gen)
        except StopIteration as stop:
            sources = stop.value if isinstance(stop.value, list) else []
            break
        if isinstance(chunk, str) and chunk:
            yield {"type": "chunk", "text": chunk}
    yield {"type": "final", "sources": sources}
