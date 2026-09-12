from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import (
    Broker,
    Client,
    initialize_broker_run,
)
from pi_tmux_orchestrator.protocol import (
    validate_report,
)


from broker_test_support import BrokerFixture


class BrokerWorkflowRuntimeTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    """Connected report routing and mandatory-review regressions."""

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
                    "source": "per-run-force",
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
