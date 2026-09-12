from __future__ import annotations

import json
import os
import signal
import unittest
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import (
    Broker,
    Client,
    _register_broker_signal_handlers,
    initialize_broker_run,
)
from pi_tmux_orchestrator.constants import (
    BROKER_PROTOCOL_VERSION,
    MAX_RUN_STATE_BYTES,
    MAX_WORKER_DELIVERY_CHARS,
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


from broker_test_support import BrokerFixture, assignment_usage_snapshot


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
            "playwright: run; evidence=reported; rule=playwright-forced-v1; source=per-run-force",
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
        self.assertEqual(schema_version, "10")
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
                "10",
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
        bridge = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((root / "extensions").glob("orchestrator-worker*.js"))
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
