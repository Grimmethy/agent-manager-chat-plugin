"""Tests for the Cletus gateway part 1: signed requests, the one-at-a-time queue, the once-a-minute preempt and the route.

Run: .venv/bin/python -m unittest test_cletus_gateway -v

The route is tested on a tiny Flask app that registers only the Cletus blueprint, so no core dashboard, no model and no network are
involved. The model part is a fake generator; the preempt is a fake that counts. What is proved: the signature rules (the exact message,
the 60 second window, constant-time compare, missing pieces), the body limits, that only one call runs at a time and a 4th request is
refused at once, FIFO turns, that a waiting request gives up and frees its place, that a failed or repeated preempt never fails or
repeats within a minute, the frames a client gets, and that a turn is always handed on (finished, failed or abandoned).
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask  # noqa: E402

import cletus_gateway as gw  # noqa: E402
from routes import cletus as route  # noqa: E402

KEY = "unit-test-shared-value"
NOW = 1_700_000_000


def sign(secret, ts, body):
    return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def good_body(**over):
    body = {"question": "what is cash flow?", "history": [], "context": {"page": {"view": "learn", "lessonTitle": None, "unitId": None, "sectionId": None}, "property": None, "kb": []}}
    body.update(over)
    return body


class SignatureTest(unittest.TestCase):
    def test_a_correct_signature_passes_as_str_and_as_bytes(self):
        body = b'{"a":1}'
        sig = sign(KEY, NOW, body)
        self.assertTrue(gw.verify_signature(KEY, str(NOW), body, sig, NOW))
        self.assertTrue(gw.verify_signature(KEY, str(NOW), body.decode(), sig, NOW))

    def test_the_signed_message_is_the_timestamp_a_dot_and_the_raw_body(self):
        body = b'{"a":1}'
        self.assertFalse(gw.verify_signature(KEY, str(NOW), body, hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest(), NOW))
        self.assertFalse(gw.verify_signature(KEY, str(NOW), body, hmac.new(KEY.encode(), f"{NOW}{body.decode()}".encode(), hashlib.sha256).hexdigest(), NOW))

    def test_a_wrong_secret_a_changed_body_and_a_changed_timestamp_fail(self):
        body = b'{"a":1}'
        sig = sign(KEY, NOW, body)
        self.assertFalse(gw.verify_signature("other", str(NOW), body, sig, NOW))
        self.assertFalse(gw.verify_signature(KEY, str(NOW), b'{"a":2}', sig, NOW))
        self.assertFalse(gw.verify_signature(KEY, str(NOW + 1), body, sig, NOW))

    def test_the_window_is_60_seconds_either_way(self):
        body = b"x"
        for offset, ok in [(0, True), (59, True), (60, True), (61, False), (-60, True), (-61, False), (3600, False)]:
            ts = NOW + offset
            self.assertEqual(gw.verify_signature(KEY, str(ts), body, sign(KEY, ts, body), NOW), ok, offset)

    def test_missing_pieces_and_a_bad_timestamp_fail(self):
        body = b"x"
        sig = sign(KEY, NOW, body)
        for args in [("", str(NOW), body, sig), (KEY, None, body, sig), (KEY, "", body, sig), (KEY, str(NOW), body, None), (KEY, str(NOW), body, ""),
                     (KEY, "abc", body, sig), (KEY, "12.5", body, sig)]:
            self.assertFalse(gw.verify_signature(*args, NOW), args)

    def test_the_signature_must_be_lowercase_hex(self):
        body = b"x"
        self.assertFalse(gw.verify_signature(KEY, str(NOW), body, sign(KEY, NOW, body).upper(), NOW))

    def test_the_comparison_is_constant_time(self):
        with mock.patch.object(gw.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            gw.verify_signature(KEY, str(NOW), b"x", sign(KEY, NOW, b"x"), NOW)
        self.assertEqual(spy.call_count, 1)

    def test_the_clock_defaults_to_now(self):
        ts = int(time.time())
        self.assertTrue(gw.verify_signature(KEY, str(ts), b"x", sign(KEY, ts, b"x")))
        self.assertFalse(gw.verify_signature(KEY, str(ts - 3600), b"x", sign(KEY, ts - 3600, b"x")))


class PayloadTest(unittest.TestCase):
    def check(self, body, ok=True):
        cleaned, why = gw.validate_payload(body)
        self.assertEqual(cleaned is not None, ok, why)
        return cleaned

    def test_a_good_body_passes_and_is_cleaned(self):
        cleaned = self.check(good_body(question="  what is a comp?  "))
        self.assertEqual(cleaned["question"], "what is a comp?")
        self.assertEqual(sorted(cleaned), ["context", "history", "question"])

    def test_the_question_is_1_to_1000_characters_of_text(self):
        for q in [None, "", "   ", 5, ["x"], "x" * 1001]:
            self.check(good_body(question=q), ok=False)
        self.check(good_body(question="x" * 1000))

    def test_history_is_at_most_10_messages_each_with_a_role_and_2000_characters(self):
        msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": "hi"} for i in range(10)]
        self.check(good_body(history=msgs))
        self.check(good_body(history=msgs + [{"role": "user", "content": "x"}]), ok=False)
        self.check(good_body(history=[{"role": "system", "content": "x"}]), ok=False)
        self.check(good_body(history=[{"role": "user", "content": "x" * 2001}]), ok=False)
        self.check(good_body(history=[{"role": "user", "content": "x" * 2000}]))
        self.check(good_body(history=[{"role": "user"}]), ok=False)
        self.check(good_body(history="no"), ok=False)

    def test_history_may_be_left_out(self):
        body = good_body()
        del body["history"]
        self.assertEqual(self.check(body)["history"], [])

    def test_context_page_property_and_kb_are_checked(self):
        ctx = lambda **o: {"page": {"view": "learn"}, "property": None, "kb": [], **o}  # noqa: E731
        self.check(good_body(context="no"), ok=False)
        self.check(good_body(context={"property": None, "kb": []}), ok=False)
        self.check(good_body(context=ctx(page={"view": 5})), ok=False)
        self.check(good_body(context=ctx(page={"view": "learn", "unitId": 7})), ok=False)
        self.check(good_body(context=ctx(property={"id": "p", "fields": {"noi": 1, "type": "x", "flag": True, "gone": None}})))
        self.check(good_body(context=ctx(property={"id": "p"})), ok=False)
        self.check(good_body(context=ctx(property={"id": "p", "fields": {"a": {"nested": 1}}})), ok=False)
        self.check(good_body(context=ctx(property={"id": "p", "fields": {"a": "x" * 1001}})), ok=False)
        item = {"lessonId": "l", "sectionId": "s", "title": "t", "text": "x"}
        self.check(good_body(context=ctx(kb=[item] * 4)))
        self.check(good_body(context=ctx(kb=[item] * 5)), ok=False)
        self.check(good_body(context=ctx(kb=[{"lessonId": "l"}])), ok=False)
        self.check(good_body(context=ctx(kb=[{**item, "text": "x" * 5001}])), ok=False)

    def test_a_property_field_name_over_100_characters_is_refused(self):
        ctx = {"page": {"view": "learn"}, "property": {"id": "p", "fields": {"n" * 101: 1}}, "kb": []}
        self.check(good_body(context=ctx), ok=False)
        ctx["property"]["fields"] = {"n" * 100: 1}
        self.check(good_body(context=ctx))

    def test_kb_ids_and_titles_are_limited_to_300_characters_but_the_text_is_not(self):
        item = {"lessonId": "l", "sectionId": "s", "title": "t", "text": "x" * 4000}
        ctx = lambda it: {"page": {"view": "learn"}, "property": None, "kb": [it]}  # noqa: E731
        self.check(good_body(context=ctx(item)))
        for key in ("lessonId", "sectionId", "title"):
            self.check(good_body(context=ctx({**item, key: "x" * 301})), ok=False)
            self.check(good_body(context=ctx({**item, key: "x" * 300})))

    def test_only_the_page_property_and_kb_of_the_context_are_kept(self):
        cleaned = self.check(good_body(context={"page": {"view": "learn"}, "property": None, "kb": [], "email": "a@b.com", "extra": 1}))
        self.assertEqual(sorted(cleaned["context"]), ["kb", "page", "property"])

    def test_a_body_that_is_not_an_object_is_refused(self):
        for body in [None, [], "x", 3]:
            self.check(body, ok=False)


class QueueTest(unittest.TestCase):
    def test_the_first_request_runs_at_once_and_the_next_two_wait_in_line(self):
        q = gw.CallQueue()
        a, b, c = q.enter(), q.enter(), q.enter()
        self.assertEqual([a.ahead, b.ahead, c.ahead], [0, 1, 2])
        self.assertEqual([a.running, b.running, c.running], [True, False, False])

    def test_the_fourth_request_is_busy_at_once(self):
        q = gw.CallQueue()
        for _ in range(3):
            q.enter()
        with self.assertRaises(gw.Busy):
            q.enter()

    def test_turns_go_in_order_and_a_released_place_can_be_taken(self):
        q = gw.CallQueue()
        a, b, c = q.enter(), q.enter(), q.enter()
        a.release()
        b.wait(1)
        self.assertTrue(b.running)
        self.assertFalse(c.running)
        b.release()
        c.wait(1)
        self.assertTrue(c.running)
        d = q.enter()
        self.assertEqual(d.ahead, 1)
        c.release()
        d.wait(1)
        self.assertTrue(d.running)

    def test_the_waiter_at_the_back_cannot_jump_the_one_in_front(self):
        q = gw.CallQueue()
        a, b, c = q.enter(), q.enter(), q.enter()
        a.release()
        with self.assertRaises(gw.Napping):
            c.wait(0.05)
        b.wait(1)
        self.assertTrue(b.running)

    def test_a_waiting_ticket_that_leaves_does_not_stop_the_running_one_from_blocking_new_requests(self):
        q = gw.CallQueue()
        a, b = q.enter(), q.enter()
        b.release()
        d = q.enter()
        self.assertEqual(d.ahead, 1)
        self.assertFalse(d.running)
        self.assertTrue(a.running)
        a.release()
        d.wait(1)
        self.assertTrue(d.running)

    def test_a_waiter_does_not_get_a_turn_while_another_request_runs(self):
        q = gw.CallQueue()
        a, b = q.enter(), q.enter()
        with self.assertRaises(gw.Napping):
            b.wait(0.05)
        self.assertTrue(a.running)

    def test_a_waiter_that_times_out_leaves_the_line_and_frees_its_place(self):
        q = gw.CallQueue()
        a, b, c = q.enter(), q.enter(), q.enter()
        with self.assertRaises(gw.Napping):
            b.wait(0.05)
        d = q.enter()
        self.assertEqual(d.ahead, 2)
        a.release()
        c.wait(1)
        self.assertTrue(c.running)

    def test_a_blocked_waiter_is_woken_when_the_turn_is_handed_on(self):
        q = gw.CallQueue()
        a, b = q.enter(), q.enter()
        got = []
        t = threading.Thread(target=lambda: (b.wait(5), got.append(b.running)))
        t.start()
        time.sleep(0.05)
        self.assertEqual(got, [])
        a.release()
        t.join(2)
        self.assertEqual(got, [True])

    def test_releasing_a_waiting_ticket_never_frees_the_running_one(self):
        q = gw.CallQueue()
        a, b = q.enter(), q.enter()
        b.release()
        self.assertTrue(a.running)
        c = q.enter()
        self.assertEqual(c.ahead, 1)

    def test_releasing_twice_is_harmless(self):
        q = gw.CallQueue()
        a, b = q.enter(), q.enter()
        a.release()
        a.release()
        b.wait(1)
        b.release()
        b.release()
        self.assertEqual(q.enter().ahead, 0)

    def test_the_waiting_limit_is_a_setting(self):
        q = gw.CallQueue(waiting_limit=1)
        q.enter()
        q.enter()
        with self.assertRaises(gw.Busy):
            q.enter()

    def test_a_ticket_that_already_has_its_turn_does_not_wait(self):
        q = gw.CallQueue()
        a = q.enter()
        a.wait(0)
        self.assertTrue(a.running)


class PreemptTest(unittest.TestCase):
    def test_it_runs_once_for_three_calls_within_a_minute_and_again_after(self):
        t = [100.0]
        limiter = gw.PreemptLimiter(clock=lambda: t[0])
        calls = []
        ran = [limiter.run(lambda: calls.append(1))]
        t[0] = 130
        ran.append(limiter.run(lambda: calls.append(1)))
        t[0] = 159.9
        ran.append(limiter.run(lambda: calls.append(1)))
        self.assertEqual([ran, len(calls)], [[True, False, False], 1])
        t[0] = 160.0
        ran.append(limiter.run(lambda: calls.append(1)))
        self.assertEqual([ran[-1], len(calls)], [True, 2])

    def test_a_failing_preempt_is_logged_without_details_and_counts_as_a_run(self):
        t = [0.0]
        limiter = gw.PreemptLimiter(clock=lambda: t[0])
        logs = []

        def boom():
            raise RuntimeError("core said: secret address 12 Main St")

        self.assertTrue(limiter.run(boom, logs.append))
        self.assertEqual(logs, ["[cletus] preempt failed (RuntimeError)"])
        self.assertFalse(limiter.run(boom, logs.append))
        self.assertEqual(len(logs), 1)

    def test_a_failing_preempt_without_a_log_is_still_swallowed(self):
        limiter = gw.PreemptLimiter(clock=lambda: 0.0)
        self.assertTrue(limiter.run(lambda: 1 / 0))

    def test_the_interval_is_a_setting(self):
        t = [0.0]
        limiter = gw.PreemptLimiter(every_s=5, clock=lambda: t[0])
        n = []
        limiter.run(lambda: n.append(1))
        t[0] = 4
        limiter.run(lambda: n.append(1))
        t[0] = 5
        limiter.run(lambda: n.append(1))
        self.assertEqual(len(n), 2)


class StreamAnswerTest(unittest.TestCase):
    def test_chunks_then_a_final_frame_with_the_sources_the_generator_returned(self):
        def gen(payload):
            yield "Cash "
            yield "flow."
            return [{"kind": "lesson", "lessonId": "l", "sectionId": "s"}]

        frames = list(gw.stream_answer({"question": "q"}, gen))
        self.assertEqual(frames, [{"type": "chunk", "text": "Cash "}, {"type": "chunk", "text": "flow."}, {"type": "final", "sources": [{"kind": "lesson", "lessonId": "l", "sectionId": "s"}]}])

    def test_empty_pieces_are_skipped_and_no_sources_means_an_empty_list(self):
        def gen(payload):
            yield ""
            yield "Hi"
            yield None

        self.assertEqual(list(gw.stream_answer({}, gen)), [{"type": "chunk", "text": "Hi"}, {"type": "final", "sources": []}])

    def test_a_return_value_that_is_not_a_list_is_ignored(self):
        def gen(payload):
            yield "x"
            return "nonsense"

        self.assertEqual(list(gw.stream_answer({}, gen))[-1], {"type": "final", "sources": []})

    def test_the_generator_gets_the_payload(self):
        seen = []

        def gen(payload):
            seen.append(payload)
            yield "x"

        list(gw.stream_answer({"question": "q"}, gen))
        self.assertEqual(seen, [{"question": "q"}])

    def test_an_error_from_the_generator_reaches_the_caller(self):
        def gen(payload):
            yield "x"
            raise gw.Napping()

        with self.assertRaises(gw.Napping):
            list(gw.stream_answer({}, gen))

    def test_the_default_generator_says_the_model_is_not_available(self):
        with self.assertRaises(NotImplementedError):
            list(gw.stream_answer({}, gw.default_generate_stream))


class RouteDefaultsTest(unittest.TestCase):
    """The module's own defaults, read without the patches RouteTest installs."""

    def test_the_defaults(self):
        self.assertIs(route.now, time.time)
        self.assertEqual(route.wait_timeout_s, 120)
        self.assertIs(route.generate_stream, gw.default_generate_stream)
        self.assertIsInstance(route.queue, gw.CallQueue)
        self.assertIsInstance(route.limiter, gw.PreemptLimiter)

    def test_the_log_goes_to_stderr(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            route._log("[cletus] hello")
        self.assertEqual(buf.getvalue(), "[cletus] hello\n")

    def test_the_real_preempt_calls_the_chat_plugins_own_internal_api_client(self):
        fake = mock.Mock()
        fake.preempt.return_value = ["worker-3090"]
        with mock.patch.dict(sys.modules, {"internal_api_client": fake}):
            self.assertEqual(route._preempt(), ["worker-3090"])
        fake.preempt.assert_called_once_with()


class RouteTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(route.cletus_bp)
        self.client = app.test_client()
        self.env = mock.patch.dict(os.environ, {"CLETUS_GATEWAY_SECRET": KEY}, clear=False)
        self.env.start()
        os.environ.pop("CLETUS_GATEWAY", None)
        self.patches = [
            mock.patch.object(route, "queue", gw.CallQueue()),
            mock.patch.object(route, "limiter", gw.PreemptLimiter(clock=lambda: self.clock[0])),
            mock.patch.object(route, "now", lambda: NOW),
            mock.patch.object(route, "wait_timeout_s", 0.05),
            mock.patch.object(route, "_preempt", self.fake_preempt),
            mock.patch.object(route, "_log", self.logs_append),
            mock.patch.object(route, "generate_stream", self.fake_generate),
        ]
        self.clock = [1000.0]
        self.preempts = []
        self.logs = []
        self.generated = []
        for p in self.patches:
            p.start()
        self.addCleanup(self.stop)

    def stop(self):
        for p in self.patches:
            p.stop()
        self.env.stop()

    def fake_preempt(self):
        self.preempts.append(1)
        if getattr(self, "preempt_error", None):
            raise self.preempt_error

    def logs_append(self, line):
        self.logs.append(line)

    def fake_generate(self, payload):
        self.generated.append(payload)
        yield "Cash flow is "
        yield "what is left."
        return [{"kind": "field", "name": "noi"}]

    def post(self, body=None, ts=NOW, secret=KEY, raw=None, headers=None, **kw):
        data = raw if raw is not None else json.dumps(body if body is not None else good_body()).encode()
        h = {"Content-Type": "application/json", "X-Cletus-Timestamp": str(ts), "X-Cletus-Signature": sign(secret, ts, data)}
        if headers is not None:
            h = headers
        return self.client.post("/api/cletus/ask", data=data, headers=h, **kw)

    def frames(self, resp):
        text = resp.get_data(as_text=True)
        return [json.loads(b[6:]) for b in text.split("\n\n") if b.startswith("data: ")]

    def test_a_signed_request_streams_chunks_then_final(self):
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/event-stream"))
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        self.assertEqual(self.frames(r), [{"type": "chunk", "text": "Cash flow is "}, {"type": "chunk", "text": "what is left."}, {"type": "final", "sources": [{"kind": "field", "name": "noi"}]}])

    def test_the_model_gets_the_cleaned_payload(self):
        self.post(good_body(question="  hi  "))
        self.assertEqual(self.generated[0]["question"], "hi")

    def test_no_signature_a_wrong_one_and_a_stale_timestamp_are_401(self):
        self.assertEqual(self.post(headers={"Content-Type": "application/json"}).status_code, 401)
        self.assertEqual(self.post(secret="other").status_code, 401)
        self.assertEqual(self.post(ts=NOW - 61).status_code, 401)
        self.assertEqual(self.post(ts=NOW + 61).status_code, 401)
        r = self.post(headers={"Content-Type": "application/json", "X-Cletus-Timestamp": str(NOW), "X-Cletus-Signature": "0" * 64})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json(), {"error": "unauthorized"})
        self.assertEqual(self.generated, [])

    def test_a_body_changed_after_signing_is_401(self):
        data = json.dumps(good_body()).encode()
        sig = sign(KEY, NOW, data)
        r = self.client.post("/api/cletus/ask", data=data.replace(b"cash", b"CASH"), headers={"X-Cletus-Timestamp": str(NOW), "X-Cletus-Signature": sig})
        self.assertEqual(r.status_code, 401)

    def test_an_unset_secret_is_503(self):
        os.environ.pop("CLETUS_GATEWAY_SECRET")
        r = self.post()
        self.assertEqual([r.status_code, r.get_json()["error"]], [503, "not_configured"])
        os.environ["CLETUS_GATEWAY_SECRET"] = ""
        self.assertEqual(self.post().status_code, 503)

    def test_the_kill_switch_is_503_off_in_any_case_and_with_spaces(self):
        for value in ["off", "OFF", " Off "]:
            os.environ["CLETUS_GATEWAY"] = value
            r = self.post()
            self.assertEqual([r.status_code, r.get_json()], [503, {"error": "off"}], value)
        os.environ["CLETUS_GATEWAY"] = "on"
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.generated.__len__(), 1)

    def test_the_kill_switch_wins_over_a_bad_signature(self):
        os.environ["CLETUS_GATEWAY"] = "off"
        self.assertEqual(self.post(secret="other").status_code, 503)

    def test_bad_json_and_a_body_that_breaks_the_contract_are_400(self):
        r = self.post(raw=b"{not json")
        self.assertEqual([r.status_code, r.get_json()["error"]], [400, "bad_request"])
        self.assertEqual(self.post(raw=b"\xff\xfe").status_code, 400)
        for body in [good_body(question=""), good_body(question="x" * 1001), good_body(history=[{"role": "user", "content": "x"}] * 11), {"question": "q"}]:
            r = self.post(body)
            self.assertEqual([r.status_code, r.get_json()["error"]], [400, "bad_request"])
        self.assertEqual(self.generated, [])

    def test_the_fourth_concurrent_request_is_429_busy_and_the_model_is_not_asked(self):
        tickets = [route.queue.enter() for _ in range(3)]
        r = self.post()
        self.assertEqual([r.status_code, r.get_json()], [429, {"error": "busy"}])
        self.assertEqual(self.generated, [])
        for t in tickets:
            t.release()

    def test_a_request_behind_a_running_one_is_told_it_is_queued_and_gives_up_as_napping(self):
        holder = route.queue.enter()
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.frames(r), [{"type": "queued", "ahead": 1}, {"type": "error", "code": "napping"}])
        self.assertEqual(self.generated, [])
        holder.release()
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.generated), 1)

    def test_a_queued_request_runs_when_its_turn_comes(self):
        holder = route.queue.enter()
        longer = mock.patch.object(route, "wait_timeout_s", 5)
        longer.start()
        self.addCleanup(longer.stop)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("frames", self.frames(self.post())))
        t.start()
        time.sleep(0.1)
        self.assertNotIn("frames", out)
        holder.release()
        t.join(5)
        self.assertEqual([f["type"] for f in out["frames"]], ["queued", "chunk", "chunk", "final"])

    def test_preempt_runs_once_for_three_requests_within_a_minute_and_again_after(self):
        for _ in range(3):
            self.post()
        self.assertEqual(len(self.preempts), 1)
        self.clock[0] += 61
        self.post()
        self.assertEqual(len(self.preempts), 2)

    def test_a_failing_preempt_does_not_fail_the_request(self):
        self.preempt_error = RuntimeError("core is slow")
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.frames(r)[-1]["type"], "final")
        self.assertEqual(self.logs, ["[cletus] preempt failed (RuntimeError)"])

    def test_a_model_that_is_away_is_an_error_frame_and_the_turn_is_handed_on(self):
        def napping(payload):
            yield "Cash"
            raise gw.Napping()

        with mock.patch.object(route, "generate_stream", napping):
            frames = self.frames(self.post())
        self.assertEqual([f["type"] for f in frames], ["chunk", "error"])
        self.assertEqual(frames[-1]["code"], "napping")
        self.assertEqual(self.logs, [])
        self.assertEqual(route.queue.enter().ahead, 0)

    def test_any_other_model_failure_is_napping_with_the_kind_logged_and_never_the_text(self):
        def broken(payload):
            raise ValueError("secret address 12 Main St")
            yield

        with mock.patch.object(route, "generate_stream", broken):
            frames = self.frames(self.post())
        self.assertEqual(frames, [{"type": "error", "code": "napping"}])
        self.assertEqual(self.logs, ["[cletus] model stream failed (ValueError)"])
        self.assertEqual(route.queue.enter().ahead, 0)

    def test_the_default_model_stream_answers_napping(self):
        with mock.patch.object(route, "generate_stream", gw.default_generate_stream):
            frames = self.frames(self.post())
        self.assertEqual(frames, [{"type": "error", "code": "napping"}])
        self.assertEqual(self.logs, ["[cletus] model stream failed (NotImplementedError)"])

    def test_a_client_who_leaves_mid_answer_hands_the_turn_on(self):
        def long(payload):
            for i in range(100):
                yield f"w{i} "

        with mock.patch.object(route, "generate_stream", long):
            r = self.post(buffered=False)
            it = iter(r.response)
            next(it)
            r.close()
        self.assertEqual(route.queue.enter().ahead, 0)

    def test_a_finished_answer_hands_the_turn_on(self):
        self.post()
        self.assertEqual(route.queue.enter().ahead, 0)

    def test_the_default_wait_is_120_seconds_and_the_limit_two_waiting(self):
        self.assertEqual([gw.wait_timeout_s, gw.max_waiting, gw.preempt_every_s, gw.signature_window_s], [120, 2, 60, 60])

    def test_app_py_registers_the_blueprint_next_to_the_others(self):
        text = (Path(__file__).parent / "app.py").read_text()
        self.assertIn("from routes.cletus import cletus_bp", text)
        self.assertIn("app.register_blueprint(cletus_bp)", text)
        self.assertLess(text.index("app.register_blueprint(accounts_bp)"), text.index("app.register_blueprint(cletus_bp)"))


if __name__ == "__main__":
    unittest.main()
