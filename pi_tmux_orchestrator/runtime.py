from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PACKAGE_ROOT / "bin" / "pi-tmux-agents"
WORKER_EXTENSION_PATH = PACKAGE_ROOT / "extensions" / "orchestrator-worker.js"
PI_HOME = Path(
    os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi" / "agent"))
).expanduser()
STATE_ROOT = Path(
    os.environ.get("PI_TMUX_AGENTS_HOME", str(PI_HOME / "orchestrations"))
).expanduser()
JSON_MODE = False
