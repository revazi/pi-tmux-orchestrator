from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PACKAGE_ROOT / "bin" / "pi-tmux-agents"
STATE_ROOT = Path(
    os.environ.get(
        "PI_TMUX_AGENTS_HOME",
        str(Path.home() / ".pi" / "agent" / "orchestrations"),
    )
).expanduser()
JSON_MODE = False
