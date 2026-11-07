"""Minimal Ollama HTTP client for the dashboard's interactive Grill Me / Grill With
Docs sessions -- mirrors src/ollama-http.js's approach (same OLLAMA_URL/LOCAL_MODEL env
vars) but in Python since these are synchronous, per-click calls made directly from the
Flask dashboard process, not routed through the Node pipeline.
"""
import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# No hardcoded model tag fallback -- see src/local-client.js's matching comment
# (2026-08-22, Grimmethy: "models should be fully interchangeable and their names should
# not be hardcoded anywhere"). An unset LOCAL_MODEL surfaces as a real Ollama "model not
# found" error instead of a guessed name.
def MODEL():
    """Return the current LOCAL_MODEL value, read from the environment on every call
    (mirroring handoff.window_tokens' per-call env reads), so a LOCAL_MODEL set after
    this module was imported is seen immediately instead of being frozen at import."""
    return os.environ.get("LOCAL_MODEL")


# 2026-11-06 (owner: never unload, never swap a model). Ollama restarts its runner -- a full reload of the 21 GB model -- whenever a request's num_ctx
# differs from the loaded one, and a per-request keep_alive overrides the server's OLLAMA_KEEP_ALIVE. This client used to send num_ctx 8192 and no
# keep_alive while every pipeline caller pins 49152 (agent-manager src/gpu-capacity.js laneNumCtx / DEFAULT_PINNED_NUM_CTX), so each chat call could
# evict and reload the model. Mirror the pipeline: AGENT_MANAGER_LANE_NUM_CTX, else 49152; and the long-duration form of "never expire" (the string
# "-1" is rejected by Ollama with HTTP 400; same value as agent-manager src/keep-alive.js).
DEFAULT_PINNED_NUM_CTX = 49152
KEEP_ALIVE = "8760h"


def pinned_num_ctx() -> int:
    """The num_ctx every request must carry, read per call from the same env the pipeline lanes use."""
    try:
        n = int(os.environ.get("AGENT_MANAGER_LANE_NUM_CTX", ""))
    except ValueError:
        return DEFAULT_PINNED_NUM_CTX
    return n if n > 0 else DEFAULT_PINNED_NUM_CTX


def generate(prompt: str, think: bool = False, temperature: float = 0.4, num_predict: int = 900) -> dict:
    """POSTs to Ollama's /api/generate. Returns {"response": str, "thinking": str}."""
    body = {
        "model": MODEL(),
        "prompt": prompt,
        "think": think,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": pinned_num_ctx()},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {"response": data.get("response", ""), "thinking": data.get("thinking", "")}