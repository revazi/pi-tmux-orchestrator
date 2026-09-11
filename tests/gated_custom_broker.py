#!/usr/bin/env python3
"""Test-only broker entry that bypasses only the temporary custom catalog gate."""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pi_tmux_orchestrator import constants  # noqa: E402
from pi_tmux_orchestrator.cli import main  # noqa: E402
from pi_tmux_orchestrator.role_registry import valid_custom_role_id  # noqa: E402

role = os.environ.get("CUSTOM_GATED_BROKER_ROLE", "")
if not valid_custom_role_id(role):
    raise SystemExit("test-only custom broker role is invalid")
constants.KNOWN_ROLES = constants.KNOWN_ROLES | {role}

raise SystemExit(main())
