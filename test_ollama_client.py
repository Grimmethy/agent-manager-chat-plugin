"""ollama_client.generate must never make Ollama reload the model: same num_ctx as the pipeline lanes, and a never-expire keep_alive."""
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ollama_client  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _call(env):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(json.dumps({"response": "ok"}).encode("utf-8"))

    base = {k: v for k, v in os.environ.items() if k != "AGENT_MANAGER_LANE_NUM_CTX"}
    with mock.patch.dict(os.environ, {**base, **env}, clear=True), mock.patch("urllib.request.urlopen", fake_urlopen):
        ollama_client.generate("hi")
    return seen["body"]


class OllamaClientPinning(unittest.TestCase):
    def test_default_matches_pipeline_pin_and_never_expires(self):
        body = _call({"LOCAL_MODEL": "m"})
        self.assertEqual(body["options"]["num_ctx"], 49152)
        self.assertEqual(body["keep_alive"], "8760h")

    def test_lane_env_overrides_num_ctx(self):
        self.assertEqual(_call({"LOCAL_MODEL": "m", "AGENT_MANAGER_LANE_NUM_CTX": "32768"})["options"]["num_ctx"], 32768)

    def test_bad_lane_env_falls_back_to_default(self):
        for bad in ("", "abc", "0", "-5"):
            self.assertEqual(_call({"LOCAL_MODEL": "m", "AGENT_MANAGER_LANE_NUM_CTX": bad})["options"]["num_ctx"], 49152, bad)


if __name__ == "__main__":
    unittest.main()
