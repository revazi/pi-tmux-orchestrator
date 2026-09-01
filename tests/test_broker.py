from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from pi_tmux_orchestrator import broker_store, runtime
from pi_tmux_orchestrator.broker import (
    Broker,
    Client,
    Observer,
    _register_broker_signal_handlers,
    initialize_broker_run,
)
from pi_tmux_orchestrator.budgeting import packaged_budget_policy
from pi_tmux_orchestrator.constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    MAX_RUN_STATE_BYTES,
    MAX_WORKER_DELIVERY_CHARS,
    READ_ONLY_TOOLS,
    WINDOW,
)
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.context_capsules import (
    render_run_state_capsule,
    render_worker_baseline,
)
from pi_tmux_orchestrator.protocol import (
    decode_frame,
    encode_frame,
    validate_client_message,
    validate_report,
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


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip_is_strict_and_bounded(self) -> None:
        value = {"version": 1, "type": "response", "success": True}
        encoded = encode_frame(value)
        self.assertEqual(int.from_bytes(encoded[:4], "big"), len(encoded) - 4)
        self.assertEqual(decode_frame(encoded[4:]), value)

    def test_worker_hello_requires_a_positive_generation(self) -> None:
        hello = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "hello",
            "role": "implementer",
            "token": "a" * 32,
            "id": "b" * 32,
            "generation": 2,
        }
        self.assertEqual(validate_client_message(hello), hello)
        for generation in (0, True, "2"):
            invalid = {**hello, "generation": generation}
            with self.assertRaisesRegex(Exception, "generation is invalid"):
                validate_client_message(invalid)

    def test_report_message_accepts_bounded_usage_and_legacy_shape(self) -> None:
        message = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "report",
            "role": "reviewer",
            "token": "a" * 32,
            "id": "b" * 32,
            "assignment_id": "c" * 32,
            "report": {"kind": "review", "summary": "Ready.", "verdict": "approved"},
        }
        self.assertEqual(validate_client_message(message), message)
        current = {**message, "usage": assignment_usage_snapshot()}
        self.assertEqual(validate_client_message(current), current)
        with self.assertRaisesRegex(Exception, "missing or unknown fields"):
            validate_client_message({**current, "unknown": 1})

    def test_guardrail_message_is_bounded_numeric_metadata_only(self) -> None:
        message = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "guardrail",
            "role": "implementer",
            "token": "a" * 32,
            "id": "b" * 32,
            "assignment_id": "c" * 32,
            "level": "hard",
            "metric": "provider_calls",
            "observed": 6,
            "threshold": 6,
        }
        self.assertEqual(validate_client_message(message), message)
        for changes in (
            {"metric": "cache_read_tokens"},
            {"observed": 5},
            {"threshold": 0},
            {"observed": "6"},
            {"report": "PRIVATE_REPORT_CANARY"},
        ):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                validate_client_message({**message, **changes})

    def test_worker_progress_is_assignment_bound_metadata_only(self) -> None:
        message = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "progress",
            "role": "implementer",
            "token": "a" * 32,
            "id": "b" * 32,
            "assignment_id": "c" * 32,
            "phase": "streaming",
            "usage": None,
        }
        self.assertEqual(validate_client_message(message), message)
        for changes in (
            {"phase": "PRIVATE_MESSAGE_BODY"},
            {"assignment_id": "invalid"},
            {"content": "PRIVATE_MESSAGE_BODY"},
        ):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                validate_client_message({**message, **changes})

    def test_plan_report_is_bounded_read_only_and_implementer_only(self) -> None:
        contract = json.loads(
            (Path(__file__).parent / "fixtures" / "phased-plan-wire.json").read_text(
                encoding="utf-8"
            )
        )
        value = contract["tool_input"]
        report = validate_report(value, "implementer")
        self.assertEqual(report, contract["wire_report"])
        self.assertEqual(report["kind"], "plan")
        self.assertEqual(report["changed_paths"], [])
        self.assertEqual(report["checks"], [])
        self.assertEqual(report["findings"], [])
        self.assertIsNone(report["verdict"])
        self.assertEqual(report["relevant_symbols"], value["relevant_symbols"])
        self.assertEqual(validate_report(report, "implementer"), report)

        invalid_values = (
            ({**value, "changed_paths": ["src/service.py"]}, "implementer"),
            ({**report, "changed_paths": ["src/service.py"]}, "implementer"),
            (
                {**report, "checks": [{"name": "unit", "status": "passed"}]},
                "implementer",
            ),
            (
                {
                    **report,
                    "findings": [{"severity": "info", "summary": "A claim."}],
                },
                "implementer",
            ),
            ({**report, "limitations": ["A claimed limitation."]}, "implementer"),
            ({**report, "verdict": "approved"}, "implementer"),
            ({**value, "verdict": "approved"}, "implementer"),
            (
                {**value, "checks": [{"name": "unit", "status": "passed"}]},
                "implementer",
            ),
            ({**value, "summary": "x" * 1001}, "implementer"),
            ({**value, "relevant_paths": ["../secret"]}, "implementer"),
            ({**value, "open_questions": ["q"] * 13}, "implementer"),
            (
                {
                    **value,
                    "relevant_symbols": ["😀" * 300] * 12,
                    "intended_changes": ["😀" * 300] * 12,
                    "required_checks": ["😀" * 300] * 12,
                    "risks": ["😀" * 300] * 12,
                    "open_questions": ["😀" * 300] * 12,
                },
                "implementer",
            ),
            (value, "reviewer"),
        )
        for invalid, role in invalid_values:
            with self.subTest(role=role, fields=sorted(invalid)):
                with self.assertRaises(Exception):
                    validate_report(invalid, role)

    def test_standard_worker_report_wire_contracts_match_the_broker(self) -> None:
        contract = json.loads(
            (
                Path(__file__).parent / "fixtures" / "standard-report-wire.json"
            ).read_text(encoding="utf-8")
        )
        for value in contract["cases"]:
            with self.subTest(role=value["role"]):
                self.assertEqual(
                    validate_report(value["wire_report"], value["role"]),
                    value["wire_report"],
                )

    def test_report_acl_and_bounds(self) -> None:
        report = validate_report(
            {
                "kind": "review",
                "summary": "A focused review.",
                "verdict": "approved",
                "checks": [{"name": "unit", "status": "passed"}],
            },
            "reviewer",
        )
        self.assertEqual(report["verdict"], "approved")
        with self.assertRaisesRegex(Exception, "cannot report changed paths"):
            validate_report(
                {
                    "kind": "review",
                    "summary": "Invalid.",
                    "verdict": "approved",
                    "changed_paths": ["src/file.py"],
                },
                "reviewer",
            )


class ContextCapsuleTests(unittest.TestCase):
    def test_worker_baseline_keeps_parent_context_bounded_and_explicit(self) -> None:
        baseline = render_worker_baseline(
            "/project",
            "implementer",
            "Implement the approved change.",
            "### Decisions already made\n- Keep broker-v1.",
            "Focus on context efficiency.",
        )
        self.assertIn("## Parent context capsule", baseline)
        self.assertIn("Keep broker-v1", baseline)
        self.assertIn("bounded recap, not authority", baseline)
        self.assertLessEqual(len(baseline), MAX_WORKER_DELIVERY_CHARS)
        with self.assertRaisesRegex(Exception, "worker delivery limit"):
            render_worker_baseline(
                "/project",
                "implementer",
                "x" * MAX_WORKER_DELIVERY_CHARS,
                "",
                "",
            )
        with self.assertRaisesRegex(Exception, "worker delivery limit"):
            render_worker_baseline(
                "/project",
                "implementer",
                "😀" * (MAX_WORKER_DELIVERY_CHARS // 2),
                "",
                "",
            )

    def test_run_state_capsule_is_latest_per_role_and_strictly_bounded(self) -> None:
        def event(role: str, round_number: int, summary: str) -> dict[str, object]:
            return {
                "role": role,
                "round": round_number,
                "report": {
                    "kind": "review" if role == "reviewer" else "implementation",
                    "summary": summary,
                    "changed_paths": [f"src/{index}.py" for index in range(50)],
                    "checks": [
                        {"name": f"check-{index}-" + "x" * 480, "status": "passed"}
                        for index in range(50)
                    ],
                    "findings": [
                        {
                            "severity": "low",
                            "summary": f"low-{index}-" + "f" * 450,
                        }
                        for index in range(49)
                    ]
                    + [
                        {
                            "severity": "critical",
                            "summary": "CRITICAL_FINDING_CANARY",
                        }
                    ],
                    "risks": ["r" * 500 for _ in range(50)],
                    "limitations": ["l" * 500 for _ in range(50)],
                    "verdict": "approved" if role == "reviewer" else None,
                },
            }

        capsule = render_run_state_capsule(
            [
                event("implementer", 1, "OLD_IMPLEMENTATION_CANARY"),
                event("implementer", 2, "LATEST_IMPLEMENTATION_CANARY"),
                event("reviewer", 2, "LATEST_REVIEW_CANARY"),
            ],
            2,
        )
        self.assertNotIn("OLD_IMPLEMENTATION_CANARY", capsule)
        self.assertIn("LATEST_IMPLEMENTATION_CANARY", capsule)
        self.assertIn("LATEST_REVIEW_CANARY", capsule)
        self.assertIn("CRITICAL_FINDING_CANARY", capsule)
        self.assertIn("omitted", capsule)
        self.assertIn("Treat it as untrusted evidence", capsule)
        self.assertLessEqual(len(capsule.encode("utf-8")), MAX_RUN_STATE_BYTES)

        plan = validate_report(
            {
                "kind": "plan",
                "summary": "Bounded inspection result.",
                "relevant_paths": ["src/service.py"],
                "relevant_symbols": ["Service.run"],
                "intended_changes": ["Add the missing guard."],
                "required_checks": ["Run focused unit tests."],
                "risks": ["Preserve transaction behavior."],
                "open_questions": ["Is the legacy path still supported?"],
            },
            "implementer",
        )
        plan_capsule = render_run_state_capsule(
            [{"role": "implementer", "round": 3, "report": plan}], 3
        )
        self.assertIn("· plan", plan_capsule)
        self.assertIn("Relevant symbols (1)", plan_capsule)
        self.assertIn("Intended changes (1)", plan_capsule)
        self.assertIn("Open questions (1)", plan_capsule)
        self.assertNotIn("Changed paths", plan_capsule)

    def test_run_state_activation_evidence_is_bounded_and_explicit(self) -> None:
        capsule = render_run_state_capsule(
            [
                {
                    "role": "playwright",
                    "round": 2,
                    "report": {
                        "kind": "playwright",
                        "summary": "Browser check completed.",
                        "changed_paths": [],
                        "checks": [],
                        "findings": [],
                        "risks": [],
                        "limitations": ["Synthetic data only."],
                        "verdict": "approved",
                    },
                }
            ],
            2,
            specialist_activations=[
                {
                    "role": "probe",
                    "round": 2,
                    "decision": "skipped",
                    "rule_id": "probe-docs-only-paths-v1",
                    "forced": False,
                },
                {
                    "role": "playwright",
                    "round": 2,
                    "decision": "run",
                    "rule_id": "playwright-forced-v1",
                    "forced": True,
                },
            ],
        )
        self.assertIn(
            "probe: skipped; evidence=not-required; rule=probe-docs-only-paths-v1",
            capsule,
        )
        self.assertIn(
            "playwright: run; evidence=reported; rule=playwright-forced-v1; source=forced",
            capsule,
        )
        self.assertIn("Synthetic data only.", capsule)
        self.assertLessEqual(len(capsule.encode("utf-8")), MAX_RUN_STATE_BYTES)

    def test_run_state_byte_limit_preserves_every_latest_role_section(self) -> None:
        events = []
        for role in ("implementer", "probe", "playwright", "django", "reviewer"):
            events.append(
                {
                    "role": role,
                    "round": 3,
                    "report": {
                        "summary": "😀" * 1_000,
                        "changed_paths": [],
                        "checks": [],
                        "findings": [
                            {
                                "severity": "critical",
                                "summary": f"{role.upper()}_CRITICAL_CANARY",
                            }
                        ],
                        "risks": [],
                        "limitations": [],
                        "verdict": "approved" if role == "reviewer" else None,
                    },
                }
            )

        capsule = render_run_state_capsule(events, 3)

        self.assertLessEqual(len(capsule.encode("utf-8")), MAX_RUN_STATE_BYTES)
        for role in ("implementer", "probe", "playwright", "django", "reviewer"):
            self.assertIn(f"## {role} · round 3", capsule)
            self.assertIn(f"{role.upper()}_CRITICAL_CANARY", capsule)


class BrokerStoreTests(BrokerFixture):
    def test_schema_one_migrates_boundary_and_assignment_usage_metadata(self) -> None:
        with broker_store.connect_broker_database(self.coord) as database:
            database.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO meta(key,value) VALUES ('schema_version','1');
                CREATE TABLE roles (
                    role TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE assignments (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    delivery_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE reports (
                    id TEXT PRIMARY KEY,
                    assignment_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """)
        broker_store.prepare_broker_database(self.coord)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            schema_version = database.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            assignment_columns = {
                row["name"]
                for row in database.execute("PRAGMA table_info(assignments)")
            }
            role_columns = {
                row["name"] for row in database.execute("PRAGMA table_info(roles)")
            }
            report_columns = {
                row["name"] for row in database.execute("PRAGMA table_info(reports)")
            }
        self.assertEqual(schema_version, "8")
        self.assertIn("boundary_effective", assignment_columns)
        self.assertIn("provider_calls", role_columns)
        self.assertIn("activity_sequence", role_columns)
        self.assertIn("peak_context_tokens", report_columns)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            budget_tables = {
                row["name"]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("assignment_guardrails", budget_tables)

    def test_schema_four_exhaustion_state_migrates_to_observational_guardrails(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("DROP TABLE assignment_guardrails")
            database.execute("""
                CREATE TABLE budget_exhaustions (
                    fingerprint TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    role TEXT NOT NULL REFERENCES roles(role),
                    assignment_id TEXT REFERENCES assignments(id),
                    metric TEXT NOT NULL,
                    observed REAL NOT NULL,
                    threshold REAL NOT NULL,
                    status TEXT NOT NULL,
                    override_command_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            database.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
        broker_store.prepare_broker_database(self.coord)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()["value"],
                "8",
            )
            tables = {
                row["name"]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("budget_exhaustions", tables)
        self.assertIn("assignment_guardrails", tables)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertNotIn("budget", snapshot)
        self.assertEqual(snapshot["guardrails"]["mode"], "observational")

    def test_schema_five_migrates_to_single_implementation_flow(self) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            implementation_flow="phased",
        )
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("DELETE FROM meta WHERE key='implementation_flow'")
            database.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
        broker_store.prepare_broker_database(self.coord)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["implementation_flow"], "single")
        self.assertEqual(snapshot["workflow"]["forced_specialists"], [])
        self.assertEqual(snapshot["specialist_activations"], [])

    def test_new_run_has_metadata_only_sqlite_and_no_coordination_payload_files(
        self,
    ) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_TASK_CANARY",
            {"reviewer": "PRIVATE_ROLE_CANARY"},
            context_capsule="PRIVATE_CONTEXT_CAPSULE_CANARY",
        )
        names = {path.name for path in self.coord.iterdir()}
        self.assertIn("broker.sqlite3", names)
        self.assertIn("startup.json", names)
        self.assertIn("control.token", names)
        self.assertFalse(
            any(
                name.startswith(
                    ("handoff-", "review-", "playwright-", "django-review-")
                )
                or name.endswith(".ready")
                or name == "task.md"
                for name in names
            )
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            for row in database.iterdump():
                self.assertNotIn("PRIVATE_TASK_CANARY", row)
                self.assertNotIn("PRIVATE_ROLE_CANARY", row)
                self.assertNotIn("PRIVATE_CONTEXT_CAPSULE_CANARY", row)
            retained_policy = json.loads(
                database.execute(
                    "SELECT value FROM meta WHERE key='budget_policy'"
                ).fetchone()["value"]
            )
            self.assertEqual(retained_policy["enforcement"], "warn-only")
            self.assertEqual(
                retained_policy["warning"]["role"]["operational_tokens"],
                200_000,
            )
            self.assertEqual(retained_policy["hard"]["assignment"], {})
        mode = os.stat(self.coord / "broker.sqlite3").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_implementation_flow_is_strict_and_corruption_fails_closed(self) -> None:
        with self.assertRaisesRegex(OrchestrationError, "Implementation flow"):
            initialize_broker_run(
                self.coord,
                self.manifest,
                "task",
                {},
                implementation_flow="automatic",
            )

        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            implementation_flow="phased",
        )
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE meta SET value='tampered' WHERE key='implementation_flow'"
            )
        with self.assertRaisesRegex(OrchestrationError, "Implementation flow"):
            broker_store.public_broker_snapshot(self.coord)

    def test_forced_specialist_and_activation_metadata_fail_closed(self) -> None:
        self.enable_specialists("django")
        with self.assertRaisesRegex(OrchestrationError, "Forced specialists"):
            initialize_broker_run(
                self.coord,
                self.manifest,
                "task",
                {},
                forced_specialists=("playwright",),
            )
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            forced_specialists=("django",),
        )
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.record_specialist_activation(
                database,
                role="django",
                round_number=1,
                decision="run",
                rule_id="django-forced-v1",
                forced=True,
            )
            database.execute(
                "UPDATE specialist_activations SET rule_id='PRIVATE_BODY INVALID'"
            )
        with self.assertRaisesRegex(OrchestrationError, "activation"):
            broker_store.public_broker_snapshot(self.coord)

    def test_snapshot_reports_actual_usage_fields_and_workflow_state(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        socket_path = broker_store.broker_paths(self.coord)["socket"]
        self.assertLess(len(os.fsencode(socket_path)), 100)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "starting")
        self.assertEqual(snapshot["workflow"]["implementation_flow"], "single")
        self.assertEqual(snapshot["workflow"]["forced_specialists"], [])
        self.assertEqual(snapshot["specialist_activations"], [])
        self.assertEqual(snapshot["usage"]["provider_calls"], 0)
        self.assertEqual(snapshot["usage"]["total_tokens"], 0)
        self.assertTrue(snapshot["usage"]["actual_provider_usage_only"])
        self.assertFalse(snapshot["usage"]["soft_total_budget_exceeded"])
        self.assertEqual({role["total_tokens"] for role in snapshot["roles"]}, {0})
        self.assertEqual(
            {role["state"] for role in snapshot["roles"]}, {"disconnected"}
        )
        self.assertEqual({role["assignment"] for role in snapshot["roles"]}, {None})
        self.assertEqual(
            {role["latest_assignment_usage"] for role in snapshot["roles"]}, {None}
        )
        self.assertFalse(
            any("active_assignment_id" in role for role in snapshot["roles"])
        )

    def test_protocol_v1_retained_reports_keep_assignment_usage_unavailable(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "4" * 32,
                    "reviewer",
                    1,
                    "review",
                    "completed",
                    "5" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "INSERT INTO reports(id,assignment_id,role,round,kind,verdict,summary_chars,"
                "changed_path_count,check_count,finding_count,risk_count,limitation_count,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "6" * 32,
                    "4" * 32,
                    "reviewer",
                    1,
                    "review",
                    "approved",
                    10,
                    0,
                    0,
                    0,
                    0,
                    0,
                    now,
                ),
            )
            database.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        snapshot = broker_store.public_broker_snapshot(self.coord)
        reviewer = next(
            role for role in snapshot["roles"] if role["role"] == "reviewer"
        )
        self.assertIsNone(snapshot["usage"]["provider_calls"])
        self.assertEqual(reviewer["latest_assignment_usage"]["assignment_id"], "4" * 32)
        self.assertIsNone(reviewer["latest_assignment_usage"]["usage"])

    def test_assignment_usage_page_is_latest_bounded_and_body_free(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        with broker_store.connect_broker_database(self.coord) as database:
            for index, role in enumerate(("implementer", "reviewer"), start=1):
                assignment_id = f"{index:032x}"
                database.execute(
                    "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        assignment_id,
                        role,
                        index,
                        "implementation" if role == "implementer" else "review",
                        "completed",
                        f"{index + 10:032x}",
                        f"2026-08-01T00:00:0{index}+00:00",
                        f"2026-08-01T00:00:0{index}+00:00",
                    ),
                )
                database.execute(
                    "INSERT INTO reports(id,assignment_id,role,round,kind,verdict,summary_chars,"
                    "changed_path_count,check_count,finding_count,risk_count,limitation_count,"
                    "provider_calls,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,"
                    "cost_total,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{index + 20:032x}",
                        assignment_id,
                        role,
                        index,
                        "implementation" if role == "implementer" else "review",
                        None if role == "implementer" else "approved",
                        20,
                        0,
                        0,
                        0,
                        0,
                        0,
                        index,
                        10 * index,
                        2 * index,
                        30 * index,
                        index,
                        0.1 * index,
                        f"2026-08-01T00:00:0{index}+00:00",
                    ),
                )
        page = broker_store.public_assignment_usage(self.coord, limit=1)
        self.assertTrue(page["truncated"])
        self.assertEqual(page["assignments"][0]["role"], "reviewer")
        self.assertEqual(page["assignments"][0]["usage"]["operational_tokens"], 86)
        self.assertNotIn("summary", json.dumps(page))
        with self.assertRaisesRegex(Exception, "between 1 and"):
            broker_store.public_assignment_usage(self.coord, limit=0)

    def test_supervisor_command_status_reads_broker_metadata(self) -> None:
        from pi_tmux_orchestrator.supervisor_api import supervisor_command_status

        initialize_broker_run(self.coord, self.manifest, "task", {})
        command_id = "a" * 32
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO control_commands(id,action,role,delivery,status,received_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    command_id,
                    "send",
                    "reviewer",
                    "follow-up",
                    "accepted",
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T00:00:01+00:00",
                ),
            )
        result = supervisor_command_status(
            self.manifest["session"],
            self.coord.name,
            role="reviewer",
            command_id=command_id,
        )
        self.assertEqual(result["command"]["action"], "send")
        self.assertEqual(result["command"]["status"], "accepted")
        self.assertNotIn("message", result["command"])


class BrokerObserverTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_workspace_capsule_is_transient_and_revalidated_before_delivery(
        self,
    ) -> None:
        source, capsule = self.initialize_workspace_git()
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_TASK_CANARY",
            {},
            workspace_capsule=capsule,
        )
        startup = (self.coord / "startup.json").read_text(encoding="utf-8")
        self.assertIn('"workspace_capsule"', startup)
        self.assertIn("src/service.py", startup)
        self.assertNotIn("Synthetic instructions", startup)
        self.assertNotIn("VALUE = 1", startup)
        manifest_body = (self.coord / "manifest.json").read_text(encoding="utf-8")
        database_body = (self.coord / "broker.sqlite3").read_bytes()
        self.assertNotIn("src/service.py", manifest_body)
        self.assertNotIn(b"src/service.py", database_body)

        broker = Broker(self.coord, self.manifest)
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "connecting")
        broker.clients = {role: mock.Mock() for role in self.manifest["roles"]}
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()),
            mock.patch.object(broker, "assign", new=mock.AsyncMock()),
            mock.patch.object(broker, "broadcast_workflow", new=mock.AsyncMock()),
        ):
            await broker.maybe_start_workflow()
        self.assertFalse((self.coord / "startup.json").exists())
        self.assertIn("src/service.py", broker.worker_baselines["implementer"])
        self.assertIn(
            "reading governing AGENTS.md/CLAUDE.md", broker.worker_baselines["reviewer"]
        )
        recovered_broker = Broker(self.coord, self.manifest)
        self.assertIsNone(recovered_broker.workspace_capsule)
        self.assertEqual(recovered_broker.worker_baselines, {})

        source.write_text("VALUE = 2\n", encoding="utf-8")
        replayed = broker._baseline("implementer")
        self.assertIn("Initial Git state: clean", replayed)
        self.assertIn("src/service.py", replayed)

    async def test_workspace_capsule_staleness_fails_restart_replay_closed(
        self,
    ) -> None:
        source, capsule = self.initialize_workspace_git()
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            workspace_capsule=capsule,
        )
        broker = Broker(self.coord, self.manifest)
        broker.worker_baselines["implementer"] = "validated baseline"
        source.write_text("VALUE = 2\n", encoding="utf-8")
        client = Client("implementer", mock.Mock(), mock.Mock())
        with (
            mock.patch.object(
                broker, "mark_handover_uncertain", new=mock.AsyncMock()
            ) as uncertain,
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
        ):
            await broker.recover_role(client, handover=True)
        uncertain.assert_not_awaited()
        deliver.assert_awaited_once()

        (source.parents[1] / "AGENTS.md").write_text(
            "changed instructions\n", encoding="utf-8"
        )
        with (
            mock.patch.object(
                broker, "mark_handover_uncertain", new=mock.AsyncMock()
            ) as uncertain,
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
        ):
            await broker.recover_role(client, handover=True)
        uncertain.assert_awaited_once_with(client)
        deliver.assert_not_awaited()

    async def _read_frame(self, reader: asyncio.StreamReader) -> dict[str, object]:
        size = int.from_bytes(await reader.readexactly(4), "big")
        return json.loads(await reader.readexactly(size))

    async def test_authenticated_observer_receives_ephemeral_reports(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "PRIVATE_TASK", {})
        broker = Broker(self.coord, self.manifest)
        run_task = asyncio.create_task(broker.run())
        try:
            for _ in range(100):
                if broker.server is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(broker.server)
            reader, writer = await asyncio.open_unix_connection(
                broker_store.broker_paths(self.coord)["socket"]
            )
            token = (self.coord / "control.token").read_text(encoding="ascii").strip()
            request_id = secrets.token_hex(16)
            writer.write(
                encode_frame(
                    {
                        "version": BROKER_PROTOCOL_VERSION,
                        "type": "observe",
                        "token": token,
                        "id": request_id,
                    }
                )
            )
            await writer.drain()
            response = await self._read_frame(reader)
            snapshot = await self._read_frame(reader)
            self.assertEqual(response["status"], "observing")
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["state"], "connecting")
            self.assertEqual(snapshot["report_count"], 0)
            self.assertTrue(snapshot["report_replay_complete"])

            assignment_id = secrets.token_hex(16)
            now = broker_store.utc_now()
            with broker_store.connect_broker_database(self.coord) as database:
                database.execute(
                    "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        assignment_id,
                        "reviewer",
                        1,
                        "review",
                        "accepted",
                        secrets.token_hex(16),
                        now,
                        now,
                    ),
                )
                database.execute(
                    "UPDATE roles SET active_assignment_id=?,state='active' WHERE role='reviewer'",
                    (assignment_id,),
                )
            worker_writer = mock.Mock()
            worker_writer.drain = mock.AsyncMock()
            client = Client("reviewer", mock.Mock(), worker_writer)
            report = {
                "kind": "review",
                "summary": "PRIVATE_EPHEMERAL_REPORT_CANARY",
                "changed_paths": [],
                "checks": [],
                "findings": [],
                "risks": [],
                "limitations": [],
                "verdict": "approved",
            }
            with mock.patch.object(
                broker, "route_report", new=mock.AsyncMock()
            ) as route_report:
                await broker.handle_report(
                    client,
                    {
                        "id": secrets.token_hex(16),
                        "assignment_id": assignment_id,
                        "report": report,
                    },
                )
            report_event = await self._read_frame(reader)
            self.assertEqual(report_event["type"], "report")
            self.assertEqual(report_event["assignment_id"], assignment_id)
            self.assertEqual(report_event["report"], report)
            route_report.assert_awaited_once_with("reviewer", 1, report)
            with broker_store.connect_broker_database(
                self.coord, readonly=True
            ) as database:
                dump = "\n".join(database.iterdump())
            self.assertNotIn("PRIVATE_EPHEMERAL_REPORT_CANARY", dump)
            writer.close()
            await writer.wait_closed()
        finally:
            broker.stopping.set()
            await run_task

    async def test_plan_report_requires_plan_assignment_and_stores_only_metadata(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        assignment_id = "9" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "implementer",
                    1,
                    "plan",
                    "accepted",
                    "8" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' WHERE role='implementer'",
                (assignment_id,),
            )
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()
        client = Client("implementer", mock.Mock(), writer)
        with self.assertRaisesRegex(Exception, "does not match"):
            await broker.handle_report(
                client,
                {
                    "id": "7" * 32,
                    "assignment_id": assignment_id,
                    "report": {
                        "kind": "implementation",
                        "summary": "Wrong assignment kind.",
                    },
                },
            )

        plan = {
            "kind": "plan",
            "summary": "PRIVATE_PLAN_BODY_CANARY",
            "relevant_paths": ["src/service.py"],
            "relevant_symbols": ["Service.run"],
            "intended_changes": ["Add a state guard."],
            "required_checks": ["Run focused service tests."],
            "risks": [],
            "open_questions": [],
        }
        with mock.patch.object(
            broker, "route_report", new=mock.AsyncMock()
        ) as route_report:
            await broker.handle_report(
                client,
                {
                    "id": "6" * 32,
                    "assignment_id": assignment_id,
                    "report": validate_report(plan, "implementer"),
                },
            )
            await broker.handle_report(
                client,
                {
                    "id": "5" * 32,
                    "assignment_id": assignment_id,
                    "report": validate_report(plan, "implementer"),
                },
            )
        normalized = validate_report(plan, "implementer")
        route_report.assert_awaited_once_with("implementer", 1, normalized)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            row = database.execute(
                "SELECT kind,changed_path_count,check_count,finding_count,verdict "
                "FROM reports WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            dump = "\n".join(database.iterdump())
        self.assertEqual(
            dict(row),
            {
                "kind": "plan",
                "changed_path_count": 0,
                "check_count": 0,
                "finding_count": 0,
                "verdict": None,
            },
        )
        self.assertNotIn("PRIVATE_PLAN_BODY_CANARY", dump)

    async def test_report_usage_is_atomic_immutable_and_rejects_malformed_input(
        self,
    ) -> None:
        policy = packaged_budget_policy()
        policy["enforcement"] = "hard"
        policy["hard"]["assignment"]["provider_calls"] = 1
        initialize_broker_run(
            self.coord, self.manifest, "task", {}, budget_policy=policy
        )
        broker = Broker(self.coord, self.manifest)
        assignment_id = "d" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "reviewer",
                    1,
                    "review",
                    "accepted",
                    "e" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' WHERE role='reviewer'",
                (assignment_id,),
            )
        client = Client("reviewer", mock.Mock(), mock.Mock())
        report = {
            "kind": "review",
            "summary": "Atomic metadata only.",
            "verdict": "approved",
        }
        malformed = assignment_usage_snapshot()
        malformed["assignment"]["input"] = -1  # type: ignore[index]
        with self.assertRaisesRegex(Exception, "provider usage is invalid"):
            await broker.handle_report(
                client,
                {
                    "id": "1" * 32,
                    "assignment_id": assignment_id,
                    "report": report,
                    "usage": malformed,
                },
            )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0
            )

        async def assert_usage_precedes_routing(*_args: object) -> None:
            snapshot = broker_store.public_broker_snapshot(self.coord)
            reviewer = next(
                role for role in snapshot["roles"] if role["role"] == "reviewer"
            )
            self.assertEqual(reviewer["provider_calls"], 3)
            self.assertEqual(
                reviewer["latest_assignment_usage"]["usage"]["input_tokens"], 40
            )

        with (
            mock.patch.object(
                broker, "route_report", side_effect=assert_usage_precedes_routing
            ) as route_report,
            mock.patch.object(broker, "broadcast", new=mock.AsyncMock()) as broadcast,
            mock.patch.object(broker, "reply", new=mock.AsyncMock()) as reply,
        ):
            await broker.handle_report(
                client,
                {
                    "id": "2" * 32,
                    "assignment_id": assignment_id,
                    "report": report,
                    "usage": assignment_usage_snapshot(),
                },
            )
            changed = assignment_usage_snapshot(assignment_input=99)
            await broker.handle_report(
                client,
                {
                    "id": "3" * 32,
                    "assignment_id": assignment_id,
                    "report": report,
                    "usage": changed,
                },
            )

        route_report.assert_awaited_once_with("reviewer", 1, mock.ANY)
        self.assertEqual(broadcast.await_args.args[0]["usage"]["input"], 40)
        self.assertEqual(
            [call.kwargs["status"] for call in reply.await_args_list],
            ["accepted", "duplicate"],
        )
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "starting")
        self.assertNotIn("budget", snapshot)
        reviewer = next(
            role for role in snapshot["roles"] if role["role"] == "reviewer"
        )
        self.assertEqual(
            reviewer["latest_assignment_usage"]["usage"]["input_tokens"], 40
        )
        self.assertEqual(snapshot["usage"]["provider_calls"], 3)
        from pi_tmux_orchestrator.supervisor_api import supervisor_snapshot

        supervisor = supervisor_snapshot(self.manifest["session"], self.coord.name)
        supervised_reviewer = next(
            role for role in supervisor["roles"] if role["name"] == "reviewer"
        )
        self.assertEqual(
            supervised_reviewer["runtime"]["state"]["latest_assignment_usage"][
                "assignment_id"
            ],
            assignment_id,
        )
        from pi_tmux_orchestrator.supervisor_api import supervisor_usage

        analytics = supervisor_usage(
            self.manifest["session"], self.coord.name, limit=10
        )
        reviewer_analytics = next(
            role for role in analytics["roles"] if role["role"] == "reviewer"
        )
        self.assertEqual(analytics["cumulative"]["provider_calls"], 3)
        self.assertEqual(analytics["assignment_count"], 1)
        self.assertEqual(
            reviewer_analytics["assignments"][0]["usage"]["cache_read_tokens"],
            120,
        )
        self.assertEqual(
            reviewer_analytics["assignments"][0]["usage"]["operational_tokens"],
            180,
        )
        self.assertFalse(analytics["semantics"]["payload_bodies_included"])
        from pi_tmux_orchestrator.commands import _status_assignment_usage

        status_delta = _status_assignment_usage(reviewer)
        self.assertIn("latest round=1 kind=review", status_delta)
        self.assertIn("input=40 cache-read=120 cache-write=5 output=15", status_delta)
        self.assertNotIn("Atomic metadata only", status_delta)

    async def test_assignment_guardrail_metadata_is_authenticated_immutable_and_public(
        self,
    ) -> None:
        policy = packaged_budget_policy()
        policy["warning"]["assignment"]["provider_calls"] = 4
        policy["hard"]["assignment"]["provider_calls"] = 6
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_TASK_CANARY",
            {},
            budget_policy=policy,
        )
        broker = Broker(self.coord, self.manifest)
        assignment_id = "6" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "implementer",
                    1,
                    "implementation",
                    "accepted",
                    "7" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' "
                "WHERE role='implementer'",
                (assignment_id,),
            )
        client = Client("implementer", mock.Mock(), mock.Mock())
        warning = {
            "id": "8" * 32,
            "assignment_id": assignment_id,
            "level": "warning",
            "metric": "provider_calls",
            "observed": 4,
            "threshold": 4,
        }
        hard = {
            "id": "9" * 32,
            "assignment_id": assignment_id,
            "level": "hard",
            "metric": "provider_calls",
            "observed": 6,
            "threshold": 6,
        }
        with mock.patch.object(broker, "reply", new=mock.AsyncMock()) as reply:
            await broker.handle_guardrail(client, warning)
            await broker.handle_guardrail(
                client, {**warning, "id": "a" * 32, "observed": 5}
            )
            await broker.handle_guardrail(client, hard)
        self.assertEqual(
            [call.kwargs["status"] for call in reply.await_args_list],
            ["recorded", "duplicate", "recorded"],
        )
        with self.assertRaisesRegex(Exception, "not owned"):
            await broker.handle_guardrail(
                Client("reviewer", mock.Mock(), mock.Mock()),
                {**hard, "id": "b" * 32},
            )
        with self.assertRaisesRegex(Exception, "not active"):
            await broker.handle_guardrail(
                client,
                {**hard, "id": "c" * 32, "threshold": 7},
            )
        snapshot = broker_store.public_broker_snapshot(self.coord)
        implementer = next(
            role for role in snapshot["roles"] if role["role"] == "implementer"
        )
        self.assertEqual(
            implementer["assignment_guardrails"],
            [
                {
                    "assignment_id": assignment_id,
                    "level": "warning",
                    "metric": "provider_calls",
                    "observed": 4,
                    "threshold": 4,
                },
                {
                    "assignment_id": assignment_id,
                    "level": "hard",
                    "metric": "provider_calls",
                    "observed": 6,
                    "threshold": 6,
                },
            ],
        )
        self.assertEqual(snapshot["guardrails"]["mode"], "observational")
        self.assertFalse(snapshot["guardrails"]["payload_bodies_included"])
        from pi_tmux_orchestrator.supervisor_api import supervisor_snapshot

        supervised = supervisor_snapshot(self.manifest["session"], self.coord.name)
        supervised_implementer = next(
            role for role in supervised["roles"] if role["name"] == "implementer"
        )
        self.assertEqual(
            supervised_implementer["runtime"]["state"]["assignment_guardrails"],
            implementer["assignment_guardrails"],
        )
        self.assertEqual(supervised["guardrails"]["mode"], "observational")
        self.assertFalse(supervised["guardrails"]["payload_bodies_included"])
        encoded = json.dumps(snapshot)
        self.assertNotIn("PRIVATE_TASK_CANARY", encoded)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            rows = list(
                database.execute(
                    "SELECT level,metric,observed,threshold FROM assignment_guardrails "
                    "ORDER BY level"
                )
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                database.execute(
                    "SELECT COUNT(*) FROM events WHERE event='assignment_guardrail'"
                ).fetchone()[0],
                2,
            )
            for row in database.iterdump():
                self.assertNotIn("PRIVATE_TASK_CANARY", row)
                self.assertNotIn("PRIVATE_REPORT_CANARY", row)

    def test_rolling_state_keeps_latest_role_beyond_observer_replay_window(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        probe_event = {
            "role": "probe",
            "round": 1,
            "report": {
                "summary": "PROBE_LATEST_CANARY",
                "changed_paths": [],
                "checks": [],
                "findings": [],
                "risks": [],
                "limitations": [],
                "verdict": None,
            },
        }
        broker._remember_report(probe_event)
        for round_number in range(1, 102):
            broker._remember_report(
                {
                    "role": "implementer",
                    "round": round_number,
                    "report": {
                        "summary": f"implementation {round_number}",
                        "changed_paths": [],
                        "checks": [],
                        "findings": [],
                        "risks": [],
                        "limitations": [],
                        "verdict": None,
                    },
                }
            )

        self.assertEqual(len(broker.recent_reports), 100)
        self.assertNotIn(probe_event, broker.recent_reports)
        capsule = broker._run_state_capsule(101)
        self.assertIn("PROBE_LATEST_CANARY", capsule)
        self.assertIn("implementation 101", capsule)

    async def test_report_routing_replaces_individual_evidence_with_run_state(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ("implementer", "reviewer")
        }
        report = {
            "kind": "implementation",
            "summary": "A bounded implementation summary.",
            "changed_paths": ["src/feature.py"],
            "checks": [],
            "findings": [],
            "risks": [],
            "limitations": [],
            "verdict": None,
        }
        broker._remember_report({"role": "implementer", "round": 1, "report": report})
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
            mock.patch.object(
                broker, "maybe_assign_reviewer", new=mock.AsyncMock()
            ) as maybe_assign_reviewer,
        ):
            await broker.route_report("implementer", 1, report)
        self.assertEqual(
            [call.args[:3] for call in deliver.await_args_list],
            [("reviewer", "run_state", 1)],
        )
        self.assertTrue(
            all(
                "A bounded implementation summary." in call.args[3]
                and call.kwargs == {"trigger": False}
                for call in deliver.await_args_list
            )
        )
        maybe_assign_reviewer.assert_awaited_once_with(1)

    async def test_specialist_activation_skips_only_with_bounded_rule_evidence(
        self,
    ) -> None:
        self.enable_specialists("probe", "playwright", "django")
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in self.manifest["roles"]
        }
        report = {
            "kind": "implementation",
            "summary": "Documentation-only implementation.",
            "changed_paths": ["README.md"],
            "checks": [],
            "findings": [],
            "risks": [],
            "limitations": [],
            "verdict": None,
        }
        broker._remember_report({"role": "implementer", "round": 1, "report": report})
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
            mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign,
            mock.patch.object(
                broker, "maybe_assign_reviewer", new=mock.AsyncMock()
            ) as reviewer,
        ):
            await broker.route_report("implementer", 1, report)
        assign.assert_not_awaited()
        reviewer.assert_awaited_once_with(1)
        deliver.assert_awaited_once()
        capsule = deliver.await_args.args[3]
        self.assertIn("Specialist activation · round 1", capsule)
        self.assertIn("probe: skipped", capsule)
        self.assertIn("playwright: skipped", capsule)
        self.assertIn("django: skipped", capsule)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            decisions = broker_store.public_specialist_activations(
                database, round_number=1
            )
            dump = "\n".join(database.iterdump())
        self.assertEqual({value["decision"] for value in decisions}, {"skipped"})
        self.assertTrue(all(value["rule_id"].endswith("-v1") for value in decisions))
        self.assertNotIn("Documentation-only implementation", dump)
        self.assertNotIn("README.md", dump)

    async def test_forced_specialist_cannot_be_satisfied_by_skip_predicate(
        self,
    ) -> None:
        self.enable_specialists("playwright")
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            forced_specialists=("playwright",),
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in self.manifest["roles"]
        }
        report = {
            "kind": "implementation",
            "summary": "Documentation-only implementation.",
            "changed_paths": ["README.md"],
            "checks": [],
            "findings": [],
            "risks": [],
            "limitations": [],
            "verdict": None,
        }
        broker._remember_report({"role": "implementer", "round": 1, "report": report})
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()),
            mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign,
            mock.patch.object(broker, "maybe_assign_reviewer", new=mock.AsyncMock()),
        ):
            await broker.route_report("implementer", 1, report)
        assign.assert_awaited_once()
        self.assertEqual(assign.await_args.args[:3], ("playwright", "playwright", 1))
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["forced_specialists"], ["playwright"])
        self.assertEqual(
            snapshot["specialist_activations"],
            [
                {
                    "role": "playwright",
                    "round": 1,
                    "decision": "run",
                    "rule_id": "playwright-forced-v1",
                    "forced": True,
                }
            ],
        )

    async def test_reviewer_waits_for_run_decisions_but_accepts_exact_skips(
        self,
    ) -> None:
        self.enable_specialists("probe", "playwright", "django")
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in self.manifest["roles"]
        }
        now = broker_store.utc_now()

        def insert_report(
            database: Any, role: str, round_number: int, kind: str
        ) -> None:
            assignment_id = (
                f"{round_number:x}{role[0]}".encode().hex().ljust(32, "0")[:32]
            )
            report_id = f"r{round_number}{role[0]}".encode().hex().ljust(32, "0")[:32]
            delivery_id = f"d{round_number}{role[0]}".encode().hex().ljust(32, "0")[:32]
            database.execute(
                "INSERT INTO assignments"
                "(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    role,
                    round_number,
                    kind,
                    "completed",
                    delivery_id,
                    now,
                    now,
                ),
            )
            database.execute(
                "INSERT INTO reports"
                "(id,assignment_id,role,round,kind,verdict,summary_chars,"
                "changed_path_count,check_count,finding_count,risk_count,"
                "limitation_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report_id,
                    assignment_id,
                    role,
                    round_number,
                    kind,
                    None,
                    1,
                    0,
                    0,
                    0,
                    0,
                    0,
                    now,
                ),
            )

        with broker_store.connect_broker_database(self.coord) as database:
            insert_report(database, "implementer", 1, "implementation")
            for role in ("probe", "playwright", "django"):
                broker_store.record_specialist_activation(
                    database,
                    role=role,
                    round_number=1,
                    decision="skipped",
                    rule_id=f"{role}-docs-only-test-v1",
                    forced=False,
                )
        with mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign:
            await broker.maybe_assign_reviewer(1)
        assign.assert_awaited_once()
        self.assertEqual(assign.await_args.args[:3], ("reviewer", "review", 1))

        with broker_store.connect_broker_database(self.coord) as database:
            insert_report(database, "implementer", 2, "implementation")
            for role in ("probe", "django"):
                broker_store.record_specialist_activation(
                    database,
                    role=role,
                    round_number=2,
                    decision="skipped",
                    rule_id=f"{role}-docs-only-test-v1",
                    forced=False,
                )
            broker_store.record_specialist_activation(
                database,
                role="playwright",
                round_number=2,
                decision="run",
                rule_id="playwright-forced-v1",
                forced=True,
            )
        with mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign:
            await broker.maybe_assign_reviewer(2)
        assign.assert_not_awaited()

        with broker_store.connect_broker_database(self.coord) as database:
            insert_report(database, "playwright", 2, "playwright")
        with mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign:
            await broker.maybe_assign_reviewer(2)
        assign.assert_awaited_once()
        self.assertEqual(assign.await_args.args[:3], ("reviewer", "review", 2))

    async def test_initial_probe_gate_skips_docs_without_waking_worker(self) -> None:
        self.enable_specialists("probe")
        initialize_broker_run(
            self.coord,
            self.manifest,
            "Update README documentation for a typo.",
            {},
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in self.manifest["roles"]
        }
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "connecting")
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()),
            mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign,
            mock.patch.object(broker, "broadcast_workflow", new=mock.AsyncMock()),
        ):
            await broker.maybe_start_workflow()
        assign.assert_awaited_once()
        self.assertEqual(
            assign.await_args.args[:3], ("implementer", "implementation", 1)
        )
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(
            snapshot["specialist_activations"][0]["rule_id"],
            "probe-docs-only-task-v1",
        )
        self.assertEqual(snapshot["specialist_activations"][0]["decision"], "skipped")

    async def test_phased_workflow_starts_with_plan_before_implementation(self) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            implementation_flow="phased",
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ("implementer", "reviewer")
        }
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "connecting")
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
            mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign,
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as workflow,
        ):
            await broker.maybe_start_workflow()
        self.assertEqual(deliver.await_count, 2)
        workflow.assert_awaited_once_with("active", 1)
        assign.assert_awaited_once()
        self.assertEqual(assign.await_args.args[:3], ("implementer", "plan", 1))
        self.assertIn(
            "Inspect the task and worktree read-only", assign.await_args.args[3]
        )
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["implementation_flow"], "phased")

    async def test_plan_report_projects_run_state_without_claiming_phase_routing(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            "implementer": Client("implementer", mock.Mock(), mock.Mock())
        }
        plan_assignment = broker._assignment("implementer", 1, "plan")
        self.assertIn("read-only", plan_assignment)
        self.assertIn("Do not modify files", plan_assignment)
        self.assertIn("relevant paths/symbols", plan_assignment)
        plan = validate_report(
            {
                "kind": "plan",
                "summary": "Inspection found the focused change surface.",
                "relevant_paths": ["src/feature.py"],
                "relevant_symbols": ["Feature.apply"],
                "intended_changes": ["Guard the state transition."],
                "required_checks": ["Run focused feature tests."],
                "risks": [],
                "open_questions": [],
            },
            "implementer",
        )
        broker._remember_report({"role": "implementer", "round": 1, "report": plan})
        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
            mock.patch.object(
                broker, "maybe_assign_reviewer", new=mock.AsyncMock()
            ) as reviewer,
            mock.patch.object(broker, "assign", new=mock.AsyncMock()) as assign,
        ):
            await broker.route_report("implementer", 1, plan)
        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.args[:3], ("implementer", "run_state", 1))
        self.assertIn("Relevant symbols (1)", deliver.await_args.args[3])
        self.assertEqual(deliver.await_args.kwargs, {"trigger": False})
        reviewer.assert_not_awaited()
        assign.assert_not_awaited()

    async def test_phased_plan_creates_same_round_implementation_boundary(self) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            implementation_flow="phased",
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            "implementer": Client("implementer", mock.Mock(), mock.Mock())
        }
        plan = validate_report(
            {
                "kind": "plan",
                "summary": "Inspection found the focused change surface.",
                "relevant_paths": ["src/feature.py"],
                "relevant_symbols": ["Feature.apply"],
                "intended_changes": ["Guard the state transition."],
                "required_checks": ["Run focused feature tests."],
                "risks": [],
                "open_questions": [],
            },
            "implementer",
        )
        broker._remember_report({"role": "implementer", "round": 1, "report": plan})
        transitions: list[str] = []

        async def deliver(
            role: str,
            kind: str,
            round_number: int,
            content: str,
            *,
            trigger: bool,
        ) -> None:
            self.assertIn("Inspection found the focused change surface.", content)
            self.assertFalse(trigger)
            transitions.append(f"deliver:{role}:{kind}:{round_number}")

        async def assign(role: str, kind: str, round_number: int, content: str) -> None:
            self.assertIn("Implement and verify", content)
            transitions.append(f"assign:{role}:{kind}:{round_number}")

        with (
            mock.patch.object(broker, "deliver", new=deliver),
            mock.patch.object(broker, "assign", new=assign),
        ):
            await broker.route_report("implementer", 1, plan)
        self.assertEqual(
            transitions,
            [
                "deliver:implementer:run_state:1",
                "assign:implementer:implementation:1",
            ],
        )

    async def test_changes_requested_delivers_rolling_state_before_round_two(
        self,
    ) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "task",
            {},
            implementation_flow="phased",
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ("implementer", "reviewer")
        }
        implementation = {
            "kind": "implementation",
            "summary": "Round one implementation.",
            "changed_paths": ["src/feature.py"],
            "checks": [],
            "findings": [],
            "risks": [],
            "limitations": [],
            "verdict": None,
        }
        review = {
            "kind": "review",
            "summary": "One focused correction remains.",
            "changed_paths": [],
            "checks": [],
            "findings": [
                {
                    "severity": "high",
                    "summary": "REVIEW_FINDING_CANARY",
                }
            ],
            "risks": [],
            "limitations": [],
            "verdict": "changes_requested",
        }
        broker._remember_report(
            {"role": "implementer", "round": 1, "report": implementation}
        )
        broker._remember_report({"role": "reviewer", "round": 1, "report": review})
        transitions: list[str] = []

        async def deliver(
            role: str,
            kind: str,
            round_number: int,
            content: str,
            *,
            trigger: bool,
        ) -> None:
            self.assertIn("Round one implementation.", content)
            self.assertIn("REVIEW_FINDING_CANARY", content)
            self.assertFalse(trigger)
            transitions.append(f"deliver:{role}:{kind}:{round_number}")

        async def broadcast(state: str, round_number: int) -> None:
            transitions.append(f"workflow:{state}:{round_number}")

        async def assign(
            role: str, kind: str, round_number: int, _content: str
        ) -> None:
            transitions.append(f"assign:{role}:{kind}:{round_number}")

        with (
            mock.patch.object(broker, "deliver", new=deliver),
            mock.patch.object(broker, "broadcast_workflow", new=broadcast),
            mock.patch.object(broker, "assign", new=assign),
        ):
            await broker.route_report("reviewer", 1, review)
        self.assertEqual(
            transitions,
            [
                "deliver:implementer:run_state:1",
                "workflow:active:2",
                "assign:implementer:implementation:2",
            ],
        )

    async def test_active_role_run_state_is_coalesced_until_next_assignment(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ("implementer", "reviewer")
        }
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE roles SET active_assignment_id=? WHERE role='implementer'",
                ("a" * 32,),
            )
        deliver = mock.AsyncMock()
        with mock.patch.object(broker, "deliver", new=deliver):
            for summary in ("STALE_RUN_STATE_CANARY", "LATEST_RUN_STATE_CANARY"):
                broker._remember_report(
                    {
                        "role": "probe",
                        "round": 1,
                        "report": {
                            "summary": summary,
                            "changed_paths": [],
                            "checks": [],
                            "findings": [],
                            "risks": [],
                            "limitations": [],
                            "verdict": None,
                        },
                    }
                )
                await broker._deliver_run_state(("implementer",), 1)
        deliver.assert_not_awaited()
        self.assertEqual(broker.pending_run_state, {"implementer": 1})

        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE roles SET active_assignment_id=NULL WHERE role='implementer'"
            )
        transitions: list[tuple[str, str]] = []

        async def capture_deliver(
            _role: str,
            kind: str,
            _round_number: int,
            content: str,
            *,
            trigger: bool,
        ) -> None:
            self.assertFalse(trigger)
            transitions.append((kind, content))

        async def capture_send(_client: Client, value: dict[str, object]) -> None:
            transitions.append((str(value["type"]), str(value.get("content", ""))))

        with (
            mock.patch.object(broker, "deliver", new=capture_deliver),
            mock.patch.object(broker, "send", new=capture_send),
        ):
            await broker.assign(
                "implementer",
                "implementation",
                2,
                broker._assignment("implementer", 2),
            )
        self.assertEqual([item[0] for item in transitions], ["run_state", "assignment"])
        self.assertNotIn("STALE_RUN_STATE_CANARY", transitions[0][1])
        self.assertIn("LATEST_RUN_STATE_CANARY", transitions[0][1])
        self.assertEqual(broker.pending_run_state, {})

    async def test_assignment_ack_emits_one_metadata_only_context_boundary(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        assignment_id = "1" * 32
        delivery_id = "2" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "implementer",
                    1,
                    "implementation",
                    "delivering",
                    delivery_id,
                    now,
                    now,
                ),
            )
        client = Client("implementer", mock.Mock(), mock.Mock())
        message = {
            "id": "3" * 32,
            "delivery_id": delivery_id,
            "status": "accepted",
        }
        with mock.patch.object(broker, "reply", new=mock.AsyncMock()):
            await broker.handle_delivery_ack(client, message)
            message["id"] = "4" * 32
            message["status"] = "duplicate"
            await broker.handle_delivery_ack(client, message)
        events = broker_store.public_broker_events(
            self.coord, after=0, limit=100, role="implementer"
        )["events"]
        boundaries = [event for event in events if event["event"] == "context_boundary"]
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(
            set(boundaries[0]),
            {
                "sequence",
                "timestamp",
                "event",
                "role",
                "round",
                "assignment_id",
                "delivery_id",
                "status",
            },
        )
        self.assertEqual(boundaries[0]["status"], "effective")

    async def test_confirmed_handover_replays_deferred_state_before_active_assignment(
        self,
    ) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_STARTUP_CANARY",
            {},
            implementation_flow="phased",
        )
        broker = Broker(self.coord, self.manifest)
        broker.worker_baselines["implementer"] = "PRIVATE_BASELINE_REPLAY_CANARY"
        broker.role_run_state["implementer"] = "PRIVATE_STALE_RUN_STATE_CANARY"
        client = Client("implementer", mock.Mock(), mock.Mock(), generation=2)
        broker.clients = {"implementer": client}
        assignment_id = "5" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE roles SET generation=2,state='recovering',active_assignment_id=? "
                "WHERE role='implementer'",
                (assignment_id,),
            )
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "implementer",
                    2,
                    "implementation",
                    "accepted",
                    "6" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE assignments SET boundary_effective=1 WHERE id=?",
                (assignment_id,),
            )
            broker_store.record_event(
                database,
                "context_boundary",
                role="implementer",
                round_number=2,
                assignment_id=assignment_id,
                delivery_id="6" * 32,
                status="effective",
            )
        broker._remember_report(
            {
                "role": "implementer",
                "round": 1,
                "report": {
                    "kind": "plan",
                    "summary": "PRIVATE_ACCEPTED_PLAN_CANARY",
                    "relevant_paths": ["src/feature.py"],
                    "relevant_symbols": [],
                    "intended_changes": [],
                    "required_checks": [],
                    "risks": [],
                    "open_questions": [],
                    "changed_paths": [],
                    "checks": [],
                    "findings": [],
                    "limitations": [],
                    "verdict": None,
                },
            }
        )
        broker._remember_report(
            {
                "role": "probe",
                "round": 2,
                "report": {
                    "summary": "PRIVATE_DEFERRED_RUN_STATE_CANARY",
                    "changed_paths": [],
                    "checks": [],
                    "findings": [],
                    "risks": [],
                    "limitations": [],
                    "verdict": None,
                },
            }
        )
        await broker._deliver_run_state(("implementer",), 2)
        self.assertEqual(broker.pending_run_state, {"implementer": 2})
        frames: list[dict[str, object]] = []

        async def capture_send(_client: Client, value: dict[str, object]) -> None:
            frames.append(value)

        with mock.patch.object(broker, "send", new=capture_send):
            await broker.recover_role(client, handover=True)
        self.assertEqual(
            [(frame["type"], frame.get("kind")) for frame in frames],
            [
                ("context", "baseline"),
                ("context", "run_state"),
                ("assignment", "implementation"),
            ],
        )
        self.assertEqual(frames[0]["content"], "PRIVATE_BASELINE_REPLAY_CANARY")
        self.assertIn("PRIVATE_ACCEPTED_PLAN_CANARY", frames[1]["content"])
        self.assertIn("PRIVATE_DEFERRED_RUN_STATE_CANARY", frames[1]["content"])
        self.assertNotIn("PRIVATE_STALE_RUN_STATE_CANARY", frames[1]["content"])
        self.assertEqual(broker.pending_run_state, {})
        recovered_delivery = str(frames[2]["id"])
        self.assertNotEqual(recovered_delivery, "6" * 32)
        with mock.patch.object(broker, "reply", new=mock.AsyncMock()):
            await broker.handle_delivery_ack(
                client,
                {
                    "id": "7" * 32,
                    "delivery_id": recovered_delivery,
                    "status": "accepted",
                },
            )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            dump = "\n".join(database.iterdump())
            role_state = database.execute(
                "SELECT state FROM roles WHERE role='implementer'"
            ).fetchone()["state"]
            boundary_count = database.execute(
                "SELECT COUNT(*) AS count FROM events "
                "WHERE event='context_boundary' AND assignment_id=?",
                (assignment_id,),
            ).fetchone()["count"]
        self.assertEqual(role_state, "active")
        self.assertEqual(boundary_count, 1)
        self.assertNotIn("PRIVATE_BASELINE_REPLAY_CANARY", dump)
        self.assertNotIn("PRIVATE_STALE_RUN_STATE_CANARY", dump)
        self.assertNotIn("PRIVATE_DEFERRED_RUN_STATE_CANARY", dump)

    async def test_worker_rejection_uses_the_request_id_before_disconnect(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        with broker_store.connect_broker_database(self.coord) as database:
            token = database.execute(
                "SELECT auth_token FROM roles WHERE role='implementer'"
            ).fetchone()["auth_token"]
        hello = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "hello",
            "role": "implementer",
            "token": token,
            "id": "a" * 32,
            "generation": 1,
        }
        request = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "lifecycle",
            "role": "implementer",
            "token": token,
            "id": "b" * 32,
            "state": "active",
            "usage": None,
        }
        writer = mock.Mock()
        writer.wait_closed = mock.AsyncMock()
        with (
            mock.patch.object(broker, "_verify_peer"),
            mock.patch.object(
                broker, "read_raw_frame", new=mock.AsyncMock(return_value=hello)
            ),
            mock.patch.object(
                broker, "read_frame", new=mock.AsyncMock(return_value=request)
            ),
            mock.patch.object(broker, "maybe_start_workflow", new=mock.AsyncMock()),
            mock.patch.object(
                broker,
                "handle_message",
                new=mock.AsyncMock(
                    side_effect=OrchestrationError(
                        "Synthetic request rejection", "invalid_protocol"
                    )
                ),
            ),
            mock.patch.object(broker, "reply", new=mock.AsyncMock()) as reply,
        ):
            await broker.handle_client(mock.Mock(), writer)
        self.assertEqual(reply.await_count, 2)
        self.assertEqual(reply.await_args_list[0].args[1], hello["id"])
        self.assertEqual(reply.await_args_list[1].args[1], request["id"])
        self.assertFalse(reply.await_args_list[1].args[2])
        self.assertEqual(
            reply.await_args_list[1].kwargs["error"], "Synthetic request rejection"
        )

    async def test_replacement_disconnect_during_recovery_fails_closed(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        with broker_store.connect_broker_database(self.coord) as database:
            token = database.execute(
                "SELECT auth_token FROM roles WHERE role='implementer'"
            ).fetchone()["auth_token"]
            database.execute(
                "UPDATE roles SET generation=2,state='restarting',active_assignment_id=? "
                "WHERE role='implementer'",
                ("9" * 32,),
            )
            broker_store.set_meta(database, "workflow_state", "active")
        hello = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "hello",
            "role": "implementer",
            "token": token,
            "id": "a" * 32,
            "generation": 2,
        }
        writer = mock.Mock()
        writer.wait_closed = mock.AsyncMock()
        with (
            mock.patch.object(broker, "_verify_peer"),
            mock.patch.object(
                broker, "read_raw_frame", new=mock.AsyncMock(return_value=hello)
            ),
            mock.patch.object(broker, "reply", new=mock.AsyncMock()),
            mock.patch.object(broker, "maybe_start_workflow", new=mock.AsyncMock()),
            mock.patch.object(broker, "recover_role", new=mock.AsyncMock()),
            mock.patch.object(
                broker,
                "read_frame",
                new=mock.AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 4)),
            ),
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
        ):
            await broker.handle_client(mock.Mock(), writer)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        implementer = next(
            role for role in snapshot["roles"] if role["role"] == "implementer"
        )
        self.assertEqual(snapshot["workflow"]["state"], "uncertain")
        self.assertEqual(implementer["state"], "uncertain")
        self.assertFalse(implementer["connected"])
        broadcast_workflow.assert_awaited_once_with("uncertain", 1)
        events = broker_store.public_broker_events(
            self.coord, after=0, limit=100, role="implementer"
        )["events"]
        handover_events = [
            event for event in events if event["event"] == "worker_handover_uncertain"
        ]
        self.assertEqual(len(handover_events), 1)
        self.assertEqual(handover_events[0]["status"], "uncertain")
        self.assertIsNone(handover_events[0]["delivery_id"])

    async def test_old_generation_disconnect_preserves_restarting_state(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        with broker_store.connect_broker_database(self.coord) as database:
            token = database.execute(
                "SELECT auth_token FROM roles WHERE role='implementer'"
            ).fetchone()["auth_token"]
            broker_store.set_meta(database, "workflow_state", "active")
        hello = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "hello",
            "role": "implementer",
            "token": token,
            "id": "b" * 32,
            "generation": 1,
        }

        async def prepare_restart(_client: Client, *, handover: bool = False) -> None:
            self.assertFalse(handover)
            with broker_store.connect_broker_database(self.coord) as database:
                database.execute(
                    "UPDATE roles SET generation=2,state='restarting' "
                    "WHERE role='implementer'"
                )

        writer = mock.Mock()
        writer.wait_closed = mock.AsyncMock()
        with (
            mock.patch.object(broker, "_verify_peer"),
            mock.patch.object(
                broker, "read_raw_frame", new=mock.AsyncMock(return_value=hello)
            ),
            mock.patch.object(broker, "reply", new=mock.AsyncMock()),
            mock.patch.object(broker, "maybe_start_workflow", new=mock.AsyncMock()),
            mock.patch.object(broker, "recover_role", new=prepare_restart),
            mock.patch.object(
                broker,
                "read_frame",
                new=mock.AsyncMock(side_effect=asyncio.IncompleteReadError(b"", 4)),
            ),
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
        ):
            await broker.handle_client(mock.Mock(), writer)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        implementer = next(
            role for role in snapshot["roles"] if role["role"] == "implementer"
        )
        self.assertEqual(snapshot["workflow"]["state"], "active")
        self.assertEqual(implementer["state"], "restarting")
        self.assertFalse(implementer["connected"])
        broadcast_workflow.assert_not_awaited()
        events = broker_store.public_broker_events(
            self.coord, after=0, limit=100, role="implementer"
        )["events"]
        self.assertFalse(
            any(event["event"] == "worker_handover_uncertain" for event in events)
        )

    async def test_post_ready_implementer_send_opens_one_reviewed_repair_round(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ("implementer", "reviewer")
        }
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "ready")
            broker_store.set_meta(database, "round", "1")
        token = (self.coord / "control.token").read_text(encoding="ascii").strip()
        command_id = "7" * 32
        message = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "control",
            "token": token,
            "id": command_id,
            "action": "send",
            "role": "implementer",
            "delivery": "follow-up",
            "message": "PRIVATE_POST_READY_REPAIR_CANARY",
        }
        worker_messages: list[dict[str, Any]] = []

        async def capture_worker_send(_client: Client, value: dict[str, Any]) -> None:
            worker_messages.append(value)

        with (
            mock.patch.object(broker, "send", new=capture_worker_send),
            mock.patch.object(broker, "send_raw", new=mock.AsyncMock()) as send_raw,
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
        ):
            await broker.handle_control(mock.Mock(), mock.Mock(), message)
            self.assertEqual(
                [
                    (value["type"], value.get("kind"), value.get("trigger"))
                    for value in worker_messages
                ],
                [
                    ("context", "run_state", False),
                    ("context", "operator_message", False),
                    ("assignment", "implementation", True),
                ],
            )
            self.assertEqual(worker_messages[-1]["round"], 2)
            self.assertEqual(worker_messages[-2]["round"], 2)
            self.assertIn(
                "PRIVATE_POST_READY_REPAIR_CANARY", worker_messages[-2]["content"]
            )
            broadcast_workflow.assert_awaited_once_with("active", 2)
            response = send_raw.await_args_list[0].args[1]
            self.assertTrue(response["success"])

            worker_message_count = len(worker_messages)
            await broker.handle_control(mock.Mock(), mock.Mock(), message)
            self.assertEqual(len(worker_messages), worker_message_count)
            duplicate_response = send_raw.await_args_list[-1].args[1]
            self.assertTrue(duplicate_response["duplicate"])

            with broker_store.connect_broker_database(
                self.coord, readonly=True
            ) as database:
                repair = database.execute(
                    "SELECT id,round,kind,state FROM assignments "
                    "WHERE role='implementer' AND round=2"
                ).fetchone()
                self.assertIsNotNone(repair)
                self.assertEqual(
                    dict(repair),
                    {
                        "id": repair["id"],
                        "round": 2,
                        "kind": "implementation",
                        "state": "delivering",
                    },
                )
                self.assertEqual(
                    database.execute(
                        "SELECT COUNT(*) AS count FROM assignments "
                        "WHERE role='implementer' AND round=2"
                    ).fetchone()["count"],
                    1,
                )

            report = validate_report(
                {
                    "kind": "implementation",
                    "summary": "The bounded repair is complete.",
                    "changed_paths": ["src/scan.ts"],
                    "checks": [{"name": "focused tests", "status": "passed"}],
                },
                "implementer",
            )
            with mock.patch.object(broker, "reply", new=mock.AsyncMock()):
                await broker.handle_report(
                    broker.clients["implementer"],
                    {
                        "id": "8" * 32,
                        "assignment_id": repair["id"],
                        "report": report,
                    },
                )

        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "active")
        self.assertEqual(snapshot["workflow"]["round"], 2)
        reviewer = next(
            role for role in snapshot["roles"] if role["role"] == "reviewer"
        )
        self.assertEqual(reviewer["assignment"]["kind"], "review")
        self.assertEqual(reviewer["assignment"]["round"], 2)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            dump = "\n".join(database.iterdump())
        self.assertNotIn("PRIVATE_POST_READY_REPAIR_CANARY", dump)

    async def test_restart_control_advances_broker_generation(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        worker_writer = mock.Mock()
        broker.clients = {
            "implementer": Client("implementer", mock.Mock(), worker_writer)
        }
        broker.worker_baselines["implementer"] = "bounded baseline"
        token = (self.coord / "control.token").read_text(encoding="ascii").strip()
        message = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "control",
            "token": token,
            "id": "8" * 32,
            "action": "restart",
            "role": "implementer",
            "delivery": None,
            "message": None,
        }
        control_writer = mock.Mock()
        with mock.patch.object(broker, "send_raw", new=mock.AsyncMock()) as send_raw:
            await broker.handle_control(mock.Mock(), control_writer, message)
        response = send_raw.await_args.args[1]
        self.assertTrue(response["success"])
        worker_writer.close.assert_called_once_with()
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            role = database.execute(
                "SELECT generation,state FROM roles WHERE role='implementer'"
            ).fetchone()
        self.assertEqual(dict(role), {"generation": 2, "state": "restarting"})

    async def test_restart_failure_control_marks_handover_uncertain(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE roles SET generation=2,state='restarting' "
                "WHERE role='implementer'"
            )
            broker_store.set_meta(database, "workflow_state", "active")
        token = (self.coord / "control.token").read_text(encoding="ascii").strip()
        command_id = "c" * 32
        message = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "control",
            "token": token,
            "id": command_id,
            "action": "restart_failed",
            "role": "implementer",
            "delivery": None,
            "message": None,
        }
        with (
            mock.patch.object(broker, "send_raw", new=mock.AsyncMock()) as send_raw,
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
        ):
            await broker.handle_control(mock.Mock(), mock.Mock(), message)
        response = send_raw.await_args.args[1]
        self.assertTrue(response["success"])
        self.assertEqual(response["status"], "accepted")
        snapshot = broker_store.public_broker_snapshot(self.coord)
        implementer = next(
            role for role in snapshot["roles"] if role["role"] == "implementer"
        )
        self.assertEqual(snapshot["workflow"]["state"], "uncertain")
        self.assertEqual(implementer["state"], "uncertain")
        broadcast_workflow.assert_awaited_once_with("uncertain", 1)
        events = broker_store.public_broker_events(
            self.coord, after=0, limit=100, role="implementer"
        )["events"]
        handover = [
            event for event in events if event["event"] == "worker_handover_uncertain"
        ]
        self.assertEqual(len(handover), 1)
        self.assertEqual(handover[0]["delivery_id"], command_id)

    async def test_broken_observer_cannot_block_workflow_broadcast(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        writer = mock.Mock()
        observer = Observer(writer)
        broker.observers.add(observer)
        with mock.patch.object(
            broker,
            "send_observer",
            new=mock.AsyncMock(side_effect=RuntimeError("closed transport")),
        ):
            await broker.broadcast(
                {
                    "version": BROKER_PROTOCOL_VERSION,
                    "type": "workflow",
                    "session": self.manifest["session"],
                    "state": "active",
                    "round": 1,
                }
            )
        self.assertNotIn(observer, broker.observers)
        writer.close.assert_called_once_with()

    async def test_observer_snapshot_fails_closed_when_report_replay_was_lost(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "7" * 32,
                    "reviewer",
                    1,
                    "review",
                    "completed",
                    "8" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "INSERT INTO reports(id,assignment_id,role,round,kind,verdict,summary_chars,"
                "changed_path_count,check_count,finding_count,risk_count,limitation_count,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "9" * 32,
                    "7" * 32,
                    "reviewer",
                    1,
                    "review",
                    "approved",
                    10,
                    0,
                    0,
                    0,
                    0,
                    0,
                    now,
                ),
            )
        snapshot = Broker(self.coord, self.manifest).observer_snapshot()
        self.assertEqual(snapshot["report_count"], 1)
        self.assertFalse(snapshot["report_replay_complete"])

    async def test_report_routing_failure_notifies_parent_as_uncertain(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        assignment_id = "a" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "reviewer",
                    1,
                    "review",
                    "accepted",
                    "b" * 32,
                    now,
                    now,
                ),
            )
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()
        client = Client("reviewer", mock.Mock(), writer)
        report = {
            "kind": "review",
            "summary": "A bounded report.",
            "verdict": "approved",
        }
        with (
            mock.patch.object(broker, "broadcast", new=mock.AsyncMock()) as broadcast,
            mock.patch.object(
                broker,
                "route_report",
                new=mock.AsyncMock(
                    side_effect=RuntimeError("synthetic routing failure")
                ),
            ),
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
            mock.patch.object(broker, "reply", new=mock.AsyncMock()) as reply,
        ):
            with self.assertRaisesRegex(Exception, "routing became uncertain"):
                await broker.handle_report(
                    client,
                    {
                        "id": "c" * 32,
                        "assignment_id": assignment_id,
                        "report": report,
                    },
                )
        broadcast.assert_awaited_once()
        broadcast_workflow.assert_awaited_once_with("uncertain", 1)
        reply.assert_not_awaited()
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "uncertain")

    async def test_uncertain_assignment_is_not_blindly_replayed_on_reconnect(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    "3" * 32,
                    "implementer",
                    1,
                    "implementation",
                    "uncertain",
                    "4" * 32,
                    now,
                    now,
                ),
            )
            broker_store.set_meta(database, "workflow_state", "active")
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()
        client = Client("implementer", mock.Mock(), writer)
        with (
            mock.patch.object(broker, "send", new=mock.AsyncMock()) as send,
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
        ):
            await broker.recover_role(client)
        send.assert_not_awaited()
        broadcast_workflow.assert_awaited_once_with("uncertain", 1)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "uncertain")

    async def test_worker_progress_updates_live_activity_without_a_handoff(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        assignment_id = "c" * 32
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "implementer",
                    1,
                    "implementation",
                    "accepted",
                    "d" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' "
                "WHERE role='implementer'",
                (assignment_id,),
            )
        client = Client("implementer", mock.Mock(), mock.Mock())
        usage = {
            "providerCalls": 2,
            "input": 100,
            "output": 20,
            "cacheRead": 30,
            "cacheWrite": 4,
            "reasoning": 5,
            "cost": {"total": 0.1},
            "contextTokens": 120,
            "contextWindow": 1000,
            "contextPercent": 12.0,
        }
        with mock.patch.object(broker, "reply", new=mock.AsyncMock()) as reply:
            await broker.handle_progress(
                client,
                {
                    "id": "e" * 32,
                    "assignment_id": assignment_id,
                    "phase": "streaming",
                    "usage": usage,
                },
            )
        reply.assert_awaited_once_with(client, "e" * 32, True, status="recorded")
        snapshot = broker_store.public_broker_snapshot(self.coord)
        implementer = next(
            role for role in snapshot["roles"] if role["role"] == "implementer"
        )
        self.assertEqual(implementer["activity"], "streaming")
        self.assertEqual(implementer["activity_sequence"], 1)
        self.assertEqual(implementer["provider_calls"], 2)
        self.assertEqual(implementer["total_tokens"], 154)

    async def test_settled_assignment_without_report_requires_parent_attention(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        assignment_id = "d" * 32
        with broker_store.connect_broker_database(self.coord) as database:
            now = broker_store.utc_now()
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    "implementer",
                    1,
                    "implementation",
                    "accepted",
                    "e" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' WHERE role='implementer'",
                (assignment_id,),
            )
            broker_store.set_meta(database, "workflow_state", "active")
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()
        client = Client("implementer", mock.Mock(), writer)
        with mock.patch.object(
            broker, "broadcast_workflow", new=mock.AsyncMock()
        ) as broadcast_workflow:
            await broker.handle_lifecycle(
                client,
                {
                    "state": "waiting",
                    "usage": None,
                    "id": "f" * 32,
                },
            )
            snapshot = broker_store.public_broker_snapshot(self.coord)
            self.assertEqual(snapshot["workflow"]["state"], "needs_attention")
            await broker.handle_lifecycle(
                client,
                {
                    "state": "active",
                    "usage": None,
                    "id": "1" * 32,
                },
            )
            snapshot = broker_store.public_broker_snapshot(self.coord)
            self.assertEqual(snapshot["workflow"]["state"], "active")
            await broker.handle_lifecycle(
                client,
                {
                    "state": "uncertain",
                    "usage": None,
                    "id": "2" * 32,
                },
            )
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "uncertain")
        self.assertEqual(
            broadcast_workflow.await_args_list,
            [
                mock.call("needs_attention", 1),
                mock.call("active", 1),
                mock.call("uncertain", 1),
            ],
        )


class BrokerDashboardHookTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    def test_signal_handlers_add_event_driven_resize_refresh_portably(self) -> None:
        broker = mock.Mock()
        loop = mock.Mock()

        _register_broker_signal_handlers(loop, broker)

        self.assertIn(
            mock.call(signal.SIGINT, broker.stopping.set),
            loop.add_signal_handler.call_args_list,
        )
        self.assertIn(
            mock.call(signal.SIGTERM, broker.stopping.set),
            loop.add_signal_handler.call_args_list,
        )
        hangup_signal = getattr(signal, "SIGHUP", None)
        if hangup_signal is not None:
            self.assertIn(
                mock.call(hangup_signal, broker.stopping.set),
                loop.add_signal_handler.call_args_list,
            )
        resize_signal = getattr(signal, "SIGWINCH", None)
        if resize_signal is not None:
            self.assertIn(
                mock.call(resize_signal, broker.refresh_dashboard),
                loop.add_signal_handler.call_args_list,
            )

        unsupported_loop = mock.Mock()
        unsupported_loop.add_signal_handler.side_effect = NotImplementedError
        _register_broker_signal_handlers(unsupported_loop, broker)

    async def test_worker_messages_refresh_dashboard_after_success_or_error(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.dashboard = mock.Mock()
        broker.dashboard_active = True
        writer = mock.Mock()
        writer.drain = mock.AsyncMock()
        client = Client("implementer", mock.Mock(), writer)

        await broker.handle_message(
            client,
            {
                "type": "lifecycle",
                "state": "idle",
                "usage": None,
                "id": "1" * 32,
            },
        )
        broker.dashboard.refresh_from_store.assert_called_once_with(self.coord)

        broker.dashboard.reset_mock()
        with self.assertRaisesRegex(Exception, "Unsupported worker message"):
            await broker.handle_message(client, {"type": "unsupported"})
        broker.dashboard.refresh_from_store.assert_called_once_with(self.coord)

    def test_dashboard_failure_cannot_change_broker_workflow(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.dashboard = mock.Mock()
        broker.dashboard.refresh_from_store.side_effect = RuntimeError(
            "PRIVATE_RAW_ERROR_CANARY"
        )
        broker.dashboard_active = True

        broker.refresh_dashboard()

        broker.dashboard.render_unavailable.assert_called_once_with()
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "starting")


class PromptAndExtensionContractTests(unittest.TestCase):
    def test_production_code_has_no_lifecycle_polling_sleep(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production = [
            path
            for path in (root / "pi_tmux_orchestrator").glob("*.py")
            if path.name not in {"relay.py", "rpc_protocol.py"}
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in production)
        self.assertNotIn("time.sleep(", combined)
        bridge = (root / "extensions" / "orchestrator-worker.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("orchestrator_report", bridge)
        self.assertIn("terminate: true", bridge)
        self.assertNotIn("handoff-N.md", bridge)
        self.assertNotIn(".ready", bridge)

    def test_worker_prompts_end_turn_instead_of_waiting(self) -> None:
        from pi_tmux_orchestrator.prompts import role_system_prompt

        for role in ("implementer", "reviewer", "probe", "playwright", "django"):
            prompt = role_system_prompt(Path("/tmp/project"), role)
            self.assertIn("end the turn", prompt)
            self.assertIn("never sleep or poll", prompt)
            self.assertIn("orchestrator_report", prompt)
            self.assertIn("Prefer targeted reads", prompt)
            self.assertIn("avoid rereading unchanged files", prompt)
            self.assertNotIn("handoff-N", prompt)
