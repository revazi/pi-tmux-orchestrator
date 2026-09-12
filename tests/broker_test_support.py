from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pi_tmux_orchestrator import runtime
from pi_tmux_orchestrator.constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    READ_ONLY_TOOLS,
    WINDOW,
)
from pi_tmux_orchestrator.storage import ensure_private_directory, save_manifest
from pi_tmux_orchestrator.workspace_capsules import construct_workspace_capsule


class BrokerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.old_root = runtime.STATE_ROOT
        self.addCleanup(setattr, runtime, "STATE_ROOT", self.old_root)
        runtime.STATE_ROOT = Path(self.temporary.name) / "state"
        root = ensure_private_directory(runtime.STATE_ROOT, parents=True)
        session = "pi-broker-test"
        self.coord = ensure_private_directory(root / session / "run-1", parents=True)
        project = ensure_private_directory(Path(self.temporary.name) / "project")
        roles = {}
        for index, role in enumerate(("implementer", "reviewer"), start=1):
            session_dir = ensure_private_directory(
                self.coord / "sessions" / role, parents=True
            )
            roles[role] = {
                "provider": "test",
                "model": "model",
                "thinking": "off",
                "tools": None if role == "implementer" else READ_ONLY_TOOLS,
                "pane_id": f"%{index}",
                "session_dir": str(session_dir),
                "session_id": f"run-1-{role}",
            }
        self.manifest = {
            "version": 3,
            "created_at": "2026-08-01T00:00:00+00:00",
            "session": session,
            "window": WINDOW,
            "project": str(project),
            "coord": str(self.coord),
            "approve_project": False,
            "transport": "tui",
            "coordination": BROKER_COORDINATION,
            "protocol_version": BROKER_PROTOCOL_VERSION,
            "monitor_pane_id": "%3",
            "roles": roles,
        }
        save_manifest(self.coord, self.manifest)

    def initialize_workspace_git(self) -> tuple[Path, dict[str, Any]]:
        project = Path(self.manifest["project"])
        subprocess.run(
            ["git", "-C", str(project), "init", "-q"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "config",
                "user.email",
                "fixture@example.invalid",
            ],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(project), "config", "user.name", "Fixture"],
            check=True,
        )
        (project / "AGENTS.md").write_text(
            "# Synthetic instructions\n", encoding="utf-8"
        )
        source = project / "src" / "service.py"
        source.parent.mkdir()
        source.write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(project), "add", "."],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            }
        )
        subprocess.run(
            ["git", "-C", str(project), "commit", "-q", "-m", "fixture"],
            check=True,
            env=environment,
        )
        return source, construct_workspace_capsule(project, ["src/service.py"])

    def enable_specialists(self, *roles: str) -> None:
        for index, role in enumerate(roles, start=4):
            session_dir = ensure_private_directory(
                self.coord / "sessions" / role, parents=True
            )
            self.manifest["roles"][role] = {
                "provider": "test",
                "model": "model",
                "thinking": "off",
                "tools": READ_ONLY_TOOLS,
                "pane_id": f"%{index}",
                "session_dir": str(session_dir),
                "session_id": f"run-1-{role}",
            }


def assignment_usage_snapshot(*, assignment_input: int = 40) -> dict[str, object]:
    return {
        "cumulative": {
            "providerCalls": 3,
            "input": 140,
            "output": 35,
            "cacheRead": 150,
            "cacheWrite": 15,
            "reasoning": 10,
            "cost": {"total": 0.35},
            "contextTokens": 175,
            "contextWindow": 1_000,
            "contextPercent": 17.5,
        },
        "assignment": {
            "providerCalls": 1,
            "input": assignment_input,
            "output": 15,
            "cacheRead": 120,
            "cacheWrite": 5,
            "reasoning": 6,
            "cost": {"total": 0.15},
            "contextTokens": 175,
            "contextWindow": 1_000,
            "contextPercent": 17.5,
            "peakContextTokens": 180,
        },
    }
