"""POST /api/cletus/ask: the Cletus gateway route (part 1: auth, queue, preempt). See cletus_gateway.py for the rules.

Settings from the environment: CLETUS_GATEWAY_SECRET (the shared secret; unset answers 503) and CLETUS_GATEWAY=off (the kill switch;
answers 503 {"error":"off"}). Status codes: 401 bad, missing or stale signature; 400 a body that breaks the contract; 429 busy (two
requests already waiting); 503 off, or not configured. A 200 is a text/event-stream of frames: {"type":"queued","ahead":n} while
waiting for a turn, {"type":"chunk","text":...}, {"type":"final","sources":[...]}, or {"type":"error","code":"napping"} when the wait
ran out or the model is away.

This route imports nothing from app.py (app.py imports this module to register the blueprint), and the preempt call is imported
when it is needed, so the route can be tested without the core dashboard.
"""
import json
import os
import sys
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

import cletus_gateway as gw

cletus_bp = Blueprint("cletus-bp", __name__)

queue = gw.CallQueue()
limiter = gw.PreemptLimiter()
now = time.time  # tests replace this with a fixed clock
wait_timeout_s = gw.wait_timeout_s
generate_stream = gw.default_generate_stream  # the model part replaces this


def _log(line):
    print(line, file=sys.stderr, flush=True)


def _preempt():
    from internal_api_client import preempt  # imported here: it needs the core dashboard settings
    return preempt()


def _sse(frame):
    return f"data: {json.dumps(frame)}\n\n"


def _stream(ticket, data):
    try:
        if ticket.ahead > 0:
            yield _sse({"type": "queued", "ahead": ticket.ahead})
        try:
            ticket.wait(wait_timeout_s)
        except gw.Napping:
            yield _sse({"type": "error", "code": "napping"})
            return
        limiter.run(_preempt, _log)
        try:
            for frame in gw.stream_answer(data, generate_stream):
                yield _sse(frame)
        except gw.Napping:
            yield _sse({"type": "error", "code": "napping"})
        except Exception as e:  # noqa: BLE001 -- the client sees napping, the log sees the kind of failure
            _log(f"[cletus] model stream failed ({type(e).__name__})")
            yield _sse({"type": "error", "code": "napping"})
    finally:
        ticket.release()


@cletus_bp.route("/api/cletus/ask", methods=["POST"])
def ask():
    if os.environ.get("CLETUS_GATEWAY", "").strip().lower() == "off":
        return jsonify({"error": "off"}), 503
    secret = os.environ.get("CLETUS_GATEWAY_SECRET", "")
    if not secret:
        return jsonify({"error": "not_configured"}), 503
    raw = request.get_data()
    if not gw.verify_signature(secret, request.headers.get("X-Cletus-Timestamp"), raw, request.headers.get("X-Cletus-Signature"), now()):
        return jsonify({"error": "unauthorized"}), 401
    try:
        body = json.loads(raw)
    except ValueError:  # includes UnicodeDecodeError
        return jsonify({"error": "bad_request"}), 400
    data, why = gw.validate_payload(body)
    if data is None:
        return jsonify({"error": "bad_request", "detail": why}), 400
    try:
        ticket = queue.enter()
    except gw.Busy:
        return jsonify({"error": "busy"}), 429
    return Response(stream_with_context(_stream(ticket, data)), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
