"""Tests for internal_api_client.py -- the HTTP client for agent-manager's internal
Chat API (Phase 3 of the extraction, see
/home/wok/.claude/plans/immutable-noodling-axolotl.md).

Run: .venv/bin/python -m unittest test_internal_api_client -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).parent))

import internal_api_client as iac  # noqa: E402


def _fake_response(json_body=None, status=200):
    r = mock.MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status} error")
    return r


class TokenTest(unittest.TestCase):
    def test_missing_token_raises_before_any_request(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("requests.get") as m_get:
            with self.assertRaises(iac.InternalApiError):
                iac.get_roots()
            m_get.assert_not_called()


class GetRootsTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_returns_roots_on_success(self):
        with mock.patch("requests.get", return_value=_fake_response({"roots": ["/a", "/b"]})) as m:
            self.assertEqual(iac.get_roots(), ["/a", "/b"])
            headers = m.call_args.kwargs["headers"]
            self.assertEqual(headers["X-Internal-Token"], "tok")

    def test_connection_failure_raises_internal_api_error(self):
        with mock.patch("requests.get", side_effect=requests.ConnectionError("refused")):
            with self.assertRaises(iac.InternalApiError):
                iac.get_roots()

    def test_http_error_raises_internal_api_error(self):
        with mock.patch("requests.get", return_value=_fake_response(status=403)):
            with self.assertRaises(iac.InternalApiError):
                iac.get_roots()


class PreemptTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_returns_preempted_list(self):
        summary = [{"lane": "worker-1", "action": "killed"}]
        with mock.patch("requests.post", return_value=_fake_response({"preempted": summary})):
            self.assertEqual(iac.preempt(), summary)

    def test_failure_raises(self):
        with mock.patch("requests.post", side_effect=requests.Timeout("slow")):
            with self.assertRaises(iac.InternalApiError):
                iac.preempt()


class ReserveTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_claim_sends_model_and_on_true(self):
        with mock.patch("requests.post", return_value=_fake_response({"reservationId": "s1", "reserved": True})) as m:
            result = iac.reserve("s1", "qwen3.8", True)
            self.assertEqual(result, {"reservationId": "s1", "reserved": True})
            self.assertEqual(m.call_args.kwargs["json"], {"reservationId": "s1", "on": True, "model": "qwen3.8"})

    def test_release_includes_model_even_when_empty(self):
        # Contract: the body ALWAYS carries a "model" key (the server expects it
        # present); only a genuinely absent model (None, and LOCAL_MODEL unset)
        # is a client-side error.
        with mock.patch("requests.post", return_value=_fake_response({"reservationId": "s1", "reserved": False})) as m:
            iac.reserve("s1", "", False)
            self.assertEqual(m.call_args.kwargs["json"], {"reservationId": "s1", "on": False, "model": ""})

    def test_claiming_without_a_model_raises_internalapierror_before_any_request(self):
        # model=None and LOCAL_MODEL unset, on=True -> InternalApiError (the type the routes handle), and no HTTP request is sent.
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("requests.post") as m_post:
                with self.assertRaises(iac.InternalApiError) as ctx:
                    iac.reserve("s1", None, True)
                self.assertIn("model", str(ctx.exception).lower())
                self.assertIn("LOCAL_MODEL", str(ctx.exception))
                m_post.assert_not_called()

    def test_releasing_without_a_model_still_works_and_sends_no_model_key(self):
        # The server only requires a model when claiming; a release (api_chat_close's best-effort call) must not start raising.
        with mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"}, clear=True):
            with mock.patch("requests.post", return_value=_fake_response({"reservationId": "s1", "reserved": False})) as m:
                self.assertEqual(iac.reserve("s1", None, False), {"reservationId": "s1", "reserved": False})
                self.assertEqual(m.call_args.kwargs["json"], {"reservationId": "s1", "on": False})

    def test_local_model_env_var_fills_in_when_model_not_passed(self):
        with mock.patch.dict("os.environ", {
            "AGENT_MANAGER_INTERNAL_TOKEN": "tok",
            "LOCAL_MODEL": "local-x",
        }, clear=True):
            with mock.patch("requests.post", return_value=_fake_response({"reservationId": "s1", "reserved": True})) as m:
                iac.reserve("s1", on=True)
                self.assertEqual(m.call_args.kwargs["json"], {"reservationId": "s1", "on": True, "model": "local-x"})


class StreamLocalTurnTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict("os.environ", {"AGENT_MANAGER_INTERNAL_TOKEN": "tok"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _fake_stream_response(self, lines):
        r = mock.MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.iter_lines.return_value = iter(lines)
        r.__enter__ = lambda self: r
        r.__exit__ = lambda self, *a: None
        return r

    def test_yields_parsed_chunks_and_final(self):
        lines = ['data: {"type": "chunk", "text": "a"}', 'data: {"type": "final", "response": "a"}']
        with mock.patch("requests.post", return_value=self._fake_stream_response(lines)):
            events = list(iac.stream_local_turn(prompt="hi", max_turns=3))
            self.assertEqual(events, [{"type": "chunk", "text": "a"}, {"type": "final", "response": "a"}])

    def test_skips_non_data_and_blank_lines(self):
        lines = ["", ": comment", 'data: {"type": "final", "response": "ok"}']
        with mock.patch("requests.post", return_value=self._fake_stream_response(lines)):
            events = list(iac.stream_local_turn(prompt="hi"))
            self.assertEqual(events, [{"type": "final", "response": "ok"}])

    def test_request_failure_yields_an_error_event_instead_of_raising(self):
        with mock.patch("requests.post", side_effect=requests.ConnectionError("refused")):
            events = list(iac.stream_local_turn(prompt="hi"))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["type"], "error")

    def test_messages_takes_precedence_over_prompt_in_the_request_body(self):
        captured = {}

        def fake_post(url, headers=None, json=None, **kwargs):
            captured.update(json)
            return self._fake_stream_response(['data: {"type": "final", "response": "ok"}'])

        with mock.patch("requests.post", side_effect=fake_post):
            list(iac.stream_local_turn(messages=[{"role": "user", "content": "hi"}], prompt="ignored"))
            self.assertIn("messages", captured)
            self.assertNotIn("prompt", captured)

    def test_context_log_session_id_included_when_set(self):
        captured = {}

        def fake_post(url, headers=None, json=None, **kwargs):
            captured.update(json)
            return self._fake_stream_response(['data: {"type": "final", "response": "ok"}'])

        with mock.patch("requests.post", side_effect=fake_post):
            list(iac.stream_local_turn(prompt="hi", context_log_session_id="chat-1-abc"))
        self.assertEqual(captured.get("contextLogSessionId"), "chat-1-abc")

    def test_context_log_session_id_omitted_when_not_set(self):
        captured = {}

        def fake_post(url, headers=None, json=None, **kwargs):
            captured.update(json)
            return self._fake_stream_response(['data: {"type": "final", "response": "ok"}'])

        with mock.patch("requests.post", side_effect=fake_post):
            list(iac.stream_local_turn(prompt="hi"))
        self.assertNotIn("contextLogSessionId", captured)


if __name__ == "__main__":
    unittest.main()
