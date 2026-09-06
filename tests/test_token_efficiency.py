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
        implementer_usage: tuple[int, int, int, int, int, int | None, float],
        reviewer_usage: tuple[int, int, int, int, int, int | None, float],
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
                    "UPDATE roles SET provider_calls=?,input_tokens=?,output_tokens=?,"
                    "cache_read_tokens=?,cache_write_tokens=?,reasoning_tokens=?,cost_total=? "
                    "WHERE role=?",
                    (*usage, role),
                )
        return coord

    def test_analysis_aggregates_only_bounded_public_usage_metadata(self) -> None:
        self.create_run(
            "private-session-canary",
            "run-1",
            workflow="ready",
            implementer_usage=(2, 100, 20, 500, 0, 10, 1.25),
            reviewer_usage=(1, 50, 10, 200, 0, None, 0.5),
        )
        legacy = self.create_run(
            "private-session-canary",
            "run-2",
            workflow="active",
            implementer_usage=(2, 200, 30, 800, 0, 15, 2.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        with connect_broker_database(legacy) as database:
            database.execute("UPDATE meta SET value='2' WHERE key='schema_version'")

        result = analyze_retained_usage(self.root)

        self.assertEqual(result["runs_analyzed"], 2)
        self.assertEqual(result["runs_with_usage"], 2)
        self.assertEqual(result["sessions_analyzed"], 1)
        self.assertEqual(result["workflow_states"], {"active": 1, "ready": 1})
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["run_provider_calls"]["sum"], 3)
        self.assertEqual(result["runs_provider_calls_unavailable"], 1)
        self.assertEqual(result["total"]["provider_calls"], 3)
        self.assertEqual(result["total"]["provider_calls_unavailable_runs"], 2)
        self.assertEqual(result["total"]["input_tokens"], 350)
        self.assertEqual(result["total"]["output_tokens"], 60)
        self.assertEqual(result["total"]["cache_read_tokens"], 1_500)
        self.assertEqual(result["total"]["operational_tokens"], 1_910)
        self.assertEqual(result["total"]["reasoning_tokens"], 25)
        self.assertEqual(result["total"]["reasoning_unavailable_runs"], 2)
        self.assertEqual(result["total"]["provider_cost"], 3.75)
        activations = result["workflow_shape"]["specialist_activations"]
        self.assertEqual(activations["runs_available"], 1)
        self.assertEqual(activations["runs_unavailable"], 1)
        self.assertFalse(result["semantics"]["payload_bodies_read"])
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn("private-session-canary", rendered)
        self.assertNotIn(str(self.project), rendered)
        self.assertNotIn("auth_token", rendered)

    def test_analysis_exposes_workflow_shape_without_assignment_bodies(self) -> None:
        coord = self.create_run(
            "shape-session",
            "run-1",
            workflow="active",
            implementer_usage=(12, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(4, 0, 0, 0, 0, None, 0.0),
        )
        with connect_broker_database(coord) as database:
            database.execute(
                "UPDATE meta SET value='phased' WHERE key='implementation_flow'"
            )
            database.execute("UPDATE meta SET value='2' WHERE key='round'")
            database.execute(
                "INSERT INTO roles(role,auth_token,state,provider_calls,updated_at) "
                "VALUES ('probe',?,'idle',6,?)",
                ("d" * 32, "2026-08-01T00:00:00+00:00"),
            )
            assignments = [
                ("1" * 32, "implementer", 1, "implementation", "completed", "5" * 32),
                ("2" * 32, "reviewer", 1, "review", "completed", "6" * 32),
                ("3" * 32, "implementer", 2, "implementation", "accepted", "7" * 32),
                ("4" * 32, "probe", 2, "probe", "completed", "8" * 32),
            ]
            database.executemany(
                "INSERT INTO assignments"
                "(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        *assignment,
                        "2026-08-01T00:00:00+00:00",
                        "2026-08-01T00:00:00+00:00",
                    )
                    for assignment in assignments
                ],
            )
            reports = [
                ("a" * 32, "1" * 32, "implementer", 1, "implementation", 10, 100),
                ("b" * 32, "2" * 32, "reviewer", 1, "review", 4, 60),
                ("c" * 32, "4" * 32, "probe", 2, "probe", 6, 80),
            ]
            database.executemany(
                "INSERT INTO reports"
                "(id,assignment_id,role,round,kind,summary_chars,changed_path_count,"
                "check_count,finding_count,risk_count,limitation_count,provider_calls,"
                "input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_total,"
                "peak_context_tokens,created_at) "
                "VALUES (?,?,?,?,?,0,0,0,0,0,0,?,0,0,0,0,0,?,?)",
                [
                    (*report[:5], report[5], report[6], "2026-08-01T00:00:00+00:00")
                    for report in reports
                ],
            )
            database.execute(
                "INSERT INTO specialist_activations"
                "(role,round,decision,rule_id,forced,created_at) VALUES (?,?,?,?,?,?)",
                (
                    "probe",
                    2,
                    "run",
                    "probe-synthetic-test-v1",
                    1,
                    "2026-08-01T00:00:00+00:00",
                ),
            )

        result = analyze_retained_usage(self.root)

        self.assertEqual(result["implementation_flows"], {"phased": 1})
        self.assertEqual(result["run_rounds"], {"2": 1})
        self.assertEqual(result["run_provider_calls"]["sum"], 22)
        self.assertEqual(result["workflow_shape"]["run_assignment_count"]["maximum"], 4)
        assignments = result["workflow_shape"]["assignments"]
        self.assertEqual(assignments["count"], 4)
        self.assertEqual(assignments["repair_count"], 2)
        self.assertEqual(assignments["specialist_count"], 1)
        self.assertEqual(assignments["states"], {"accepted": 1, "completed": 3})
        activations = result["workflow_shape"]["specialist_activations"]
        self.assertEqual(activations["decisions"], {"run": 1})
        self.assertEqual(activations["roles"], {"probe": 1})
        self.assertEqual(activations["forced_count"], 1)
        usage = result["assignment_usage"]
        self.assertEqual(usage["assignment_count"], 3)
        self.assertEqual(usage["provider_calls"]["sum"], 20)
        self.assertEqual(usage["provider_calls"]["p95"], 10)
        self.assertEqual(usage["peak_context_tokens"]["p95"], 100)
        self.assertEqual(
            {item["stage"]: item["assignment_count"] for item in usage["by_stage"]},
            {"initial": 2, "repair": 1},
        )
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn("shape-session", rendered)
        self.assertNotIn("probe-synthetic-test-v1", rendered)
        self.assertNotIn("assignment_id", rendered)

    def test_specialist_activation_aggregates_are_complete_beyond_page_limit(
        self,
    ) -> None:
        coord = self.create_run(
            "activation-session",
            "run-1",
            workflow="ready",
            implementer_usage=(0, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        with connect_broker_database(coord) as database:
            database.execute(
                "INSERT INTO roles(role,auth_token,state,updated_at) "
                "VALUES ('probe',?,'idle',?)",
                ("d" * 32, "2026-08-01T00:00:00+00:00"),
            )
            database.executemany(
                "INSERT INTO specialist_activations"
                "(role,round,decision,rule_id,forced,created_at) VALUES (?,?,?,?,?,?)",
                [
                    (
                        "probe",
                        round_number,
                        "run" if round_number % 2 else "skipped",
                        "probe-synthetic-test-v1",
                        round_number % 2,
                        "2026-08-01T00:00:00+00:00",
                    )
                    for round_number in range(1, 102)
                ],
            )

        result = analyze_retained_usage(self.root)

        activations = result["workflow_shape"]["specialist_activations"]
        self.assertEqual(activations["count"], 101)
        self.assertEqual(activations["forced_count"], 51)
        self.assertEqual(activations["decisions"], {"run": 51, "skipped": 50})
        self.assertEqual(activations["roles"], {"probe": 101})
        self.assertEqual(result["runs_analyzed"], 1)
        self.assertEqual(result["issue_count"], 0)

        with connect_broker_database(coord) as database:
            database.execute(
                "UPDATE specialist_activations SET decision=? WHERE round=1",
                ("private-activation-canary",),
            )
        malformed = analyze_retained_usage(self.root)
        self.assertEqual(malformed["runs_analyzed"], 0)
        self.assertEqual(malformed["issue_count"], 1)
        self.assertNotIn(
            "private-activation-canary", json.dumps(malformed, sort_keys=True)
        )

    def test_assignment_usage_truncation_is_explicit_without_losing_shape(self) -> None:
        coord = self.create_run(
            "assignment-page-session",
            "run-1",
            workflow="ready",
            implementer_usage=(101, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        now = "2026-08-01T00:00:00+00:00"
        with connect_broker_database(coord) as database:
            database.executemany(
                "INSERT INTO assignments"
                "(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,'implementer',1,'implementation','completed',?,?,?)",
                [
                    (f"{index:032x}", f"{index + 1000:032x}", now, now)
                    for index in range(1, 102)
                ],
            )
            database.executemany(
                "INSERT INTO reports"
                "(id,assignment_id,role,round,kind,summary_chars,changed_path_count,"
                "check_count,finding_count,risk_count,limitation_count,provider_calls,"
                "input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost_total,"
                "peak_context_tokens,created_at) "
                "VALUES (?,?,'implementer',1,'implementation',0,0,0,0,0,0,?,0,0,0,0,0,?,?)",
                [
                    (f"{index + 2000:032x}", f"{index:032x}", 1, index, now)
                    for index in range(1, 102)
                ],
            )

        result = analyze_retained_usage(self.root)

        self.assertEqual(result["workflow_shape"]["assignments"]["count"], 101)
        self.assertEqual(result["assignment_usage"]["assignment_count"], 100)
        self.assertEqual(result["assignment_usage"]["usage_available"], 100)
        self.assertEqual(result["assignment_usage"]["truncated_runs"], 1)

    def test_malformed_retained_databases_are_counted_without_leaking_values(
        self,
    ) -> None:
        invalid_schema = self.create_run(
            "malformed-session",
            "run-1",
            workflow="ready",
            implementer_usage=(0, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        with connect_broker_database(invalid_schema) as database:
            database.execute(
                "UPDATE meta SET value='invalid' WHERE key='schema_version'"
            )
        invalid_assignment = self.create_run(
            "malformed-session",
            "run-2",
            workflow="ready",
            implementer_usage=(0, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        with connect_broker_database(invalid_assignment) as database:
            database.execute(
                "INSERT INTO assignments"
                "(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "1" * 32,
                    "implementer",
                    1,
                    "private-assignment-canary",
                    "accepted",
                    "2" * 32,
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T00:00:00+00:00",
                ),
            )
        self.create_run(
            "malformed-session",
            "run-3",
            workflow="private-workflow-canary",
            implementer_usage=(0, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        invalid_report = self.create_run(
            "malformed-session",
            "run-4",
            workflow="ready",
            implementer_usage=(0, 0, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        with connect_broker_database(invalid_report) as database:
            database.execute(
                "INSERT INTO assignments"
                "(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "3" * 32,
                    "implementer",
                    1,
                    "implementation",
                    "completed",
                    "4" * 32,
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T00:00:00+00:00",
                ),
            )
            database.execute(
                "INSERT INTO reports"
                "(id,assignment_id,role,round,kind,summary_chars,changed_path_count,"
                "check_count,finding_count,risk_count,limitation_count,created_at) "
                "VALUES (?,?,?,?,?,0,0,0,0,0,0,?)",
                (
                    "5" * 32,
                    "3" * 32,
                    "implementer",
                    1,
                    "private-report-canary",
                    "2026-08-01T00:00:00+00:00",
                ),
            )

        result = analyze_retained_usage(self.root)

        self.assertEqual(result["runs_analyzed"], 0)
        self.assertEqual(result["issue_count"], 4)
        rendered = json.dumps(result, sort_keys=True)
        for canary in (
            "private-assignment-canary",
            "private-workflow-canary",
            "private-report-canary",
        ):
            self.assertNotIn(canary, rendered)

    def test_analysis_limit_is_strict_and_reports_truncation(self) -> None:
        self.create_run(
            "bounded-session",
            "run-1",
            workflow="ready",
            implementer_usage=(1, 1, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
        )
        self.create_run(
            "bounded-session",
            "run-2",
            workflow="ready",
            implementer_usage=(1, 2, 0, 0, 0, None, 0.0),
            reviewer_usage=(0, 0, 0, 0, 0, None, 0.0),
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
