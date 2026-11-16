"""Central configuration. Secrets are read from the environment only and never logged or returned."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load a local .env if present (simple parser, no extra dependency).
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
USE_LLM = os.getenv("USE_LLM", "auto").lower()  # auto | off

DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_DEPTH = 1
HARD_MAX_PAGES = 25
HARD_MAX_DEPTH = 3
CLI_TIMEOUT_S = int(os.getenv("CLI_TIMEOUT_S", "90"))
MAX_STDOUT_BYTES = 8_000_000
PYTHON = sys.executable

def llm_enabled() -> bool:
    from . import llm
    return llm.provider() is not None


def llm_label() -> str | None:
    from . import llm
    p = llm.provider()
    return f"{p}:{llm.model_name()}" if p else None
