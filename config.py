"""Shared configuration constants for the agent-manager chat plugin.

Imported by app.py, claude_client.py, and model_stats_client.py so that no
module has to pull in the full Flask application just to obtain a path constant.
"""
import os
from pathlib import Path

CORE_REPO_ROOT = Path(os.environ.get(
    "AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager",
)).resolve()
