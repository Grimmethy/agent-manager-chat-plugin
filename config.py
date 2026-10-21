"""Shared path constants for the Chat plugin."""
import os
from pathlib import Path

CORE_REPO_ROOT = Path(os.environ.get("AGENT_MANAGER_CORE_REPO_ROOT", "/media/model-cache/github/agent-manager")).resolve()
