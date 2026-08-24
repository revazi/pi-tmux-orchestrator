from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pi_tmux_orchestrator import runtime
from pi_tmux_orchestrator.broker_store import (
    connect_broker_database,
    initialize_broker_database,
)
from pi_tmux_orchestrator.constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    READ_ONLY_TOOLS,
    WINDOW,
)
from pi_tmux_orchestrator.storage import ensure_private_directory, save_manifest
from pi_tmux_orchestrator.token_efficiency import analyze_retained_usage


class RetainedUsageAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = ensure_private_directory(
            Path(self.temporary.name) / "orchestrations", parents=True
        )
        self.old_root = runtime.STATE_ROOT
        self.addCleanup(setattr, runtime, "STATE_ROOT", self.old_root)
        runtime.STATE_ROOT = self.root
        self.project = ensure_private_directory(Path(self.temporary.name) / "project")

    def create_run(
        self,
        session: str,
        run_id: str,
        *,
        workflow: str,
        implementer_usage: tuple[int, int, int, int, int | None, float],
        reviewer_usage: tuple[int, int, int, int, int | None, float],
    ) -> Path:
        coord = ensure_private_directory(
            self.root / session / run_id,
            parents=True,
        )
        roles = {}
        for index, role in enumerate(("implementer", "reviewer"), start=1):
            session_dir = ensure_private_directory(
                coord / "sessions" / role, parents=True
            )
            roles[role] = {
                "provider": "test",
                "model": "model",
                "thinking": "off",
                "tools": None if role == "implementer" else READ_ONLY_TOOLS,
                "pane_id": f"%{index}",
                "session_dir": str(session_dir),
                "session_id": f"{run_id}-{role}",
            }
        manifest = {
            "version": 3,
            "created_at": "2026-08-01T00:00:00+00:00",
            "session": session,
            "window": WINDOW,
            "project": str(self.project),
            "coord": str(coord),
            "approve_project": False,
            "transport": "tui",
            "coordination": BROKER_COORDINATION,
            "protocol_version": BROKER_PROTOCOL_VERSION,
            "monitor_pane_id": "%3",
            "roles": roles,
        }
        save_manifest(coord, manifest)
        initialize_broker_database(
            coord,
            manifest,
            {"implementer": "a" * 32, "reviewer": "b" * 32},
            "c" * 32,
            soft_role_tokens=200_000,
            soft_total_tokens=600_000,
        )
        with connect_broker_database(coord) as database:
            database.execute(
                "UPDATE meta SET value=? WHERE key='workflow_state'", (workflow,)
            )
            for role, usage in {
                "implementer": implementer_usage,
                "reviewer": reviewer_usage,
            }.items():
                database.execute(
                    "UPDATE roles SET input_tokens=?,output_tokens=?,cache_read_tokens=?,"
                    "cache_write_tokens=?,reasoning_tokens=?,cost_total=? WHERE role=?",
                    (*usage, role),
                )
        return coord

    def test_analysis_aggregates_only_bounded_public_usage_metadata(self) -> None:
        self.create_run(
            "private-session-canary",
            "run-1",
            workflow="ready",
            implementer_usage=(100, 20, 500, 0, 10, 1.25),
            reviewer_usage=(50, 10, 200, 0, None, 0.5),
        )
        self.create_run(
            "private-session-canary",
            "run-2",
            workflow="active",
            implementer_usage=(200, 30, 800, 0, 15, 2.0),
            reviewer_usage=(0, 0, 0, 0, None, 0.0),
        )

        result = analyze_retained_usage(self.root)

        self.assertEqual(result["runs_analyzed"], 2)
        self.assertEqual(result["runs_with_usage"], 2)
        self.assertEqual(result["sessions_analyzed"], 1)
        self.assertEqual(result["workflow_states"], {"active": 1, "ready": 1})
        self.assertEqual(result["total"]["input_tokens"], 350)
        self.assertEqual(result["total"]["output_tokens"], 60)
        self.assertEqual(result["total"]["cache_read_tokens"], 1_500)
        self.assertEqual(result["total"]["operational_tokens"], 1_910)
        self.assertEqual(result["total"]["reasoning_tokens"], 25)
        self.assertEqual(result["total"]["reasoning_unavailable_runs"], 2)
        self.assertEqual(result["total"]["provider_cost"], 3.75)
        self.assertFalse(result["semantics"]["payload_bodies_read"])
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn("private-session-canary", rendered)
        self.assertNotIn(str(self.project), rendered)
        self.assertNotIn("auth_token", rendered)

    def test_analysis_limit_is_strict_and_reports_truncation(self) -> None:
        self.create_run(
            "bounded-session",
            "run-1",
            workflow="ready",
            implementer_usage=(1, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, None, 0.0),
        )
        self.create_run(
            "bounded-session",
            "run-2",
            workflow="ready",
            implementer_usage=(2, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, None, 0.0),
        )

        result = analyze_retained_usage(self.root, max_runs=1)

        self.assertEqual(result["runs_analyzed"], 1)
        self.assertTrue(result["truncated"])
        with self.assertRaisesRegex(Exception, "between 1 and 100"):
            analyze_retained_usage(self.root, max_runs=0)
        with self.assertRaisesRegex(Exception, "must be absolute"):
            analyze_retained_usage(Path("relative"))


if __name__ == "__main__":
    unittest.main()
