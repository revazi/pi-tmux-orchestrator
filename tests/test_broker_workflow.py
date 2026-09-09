"""Direct workflow-boundary tests with real metadata storage and fake transport."""

from __future__ import annotations

import copy
import secrets
import unittest
from types import SimpleNamespace
from unittest import mock

from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import Broker, initialize_broker_run
from pi_tmux_orchestrator.broker_observers import MAX_OBSERVER_REPORTS
from pi_tmux_orchestrator.broker_workflow import BrokerWorkflowSupport
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.evidence_reuse import EvidenceReuse
from pathlib import Path
from pi_tmux_orchestrator.protocol import validate_report
from test_broker import BrokerFixture, assignment_usage_snapshot


class WorkflowHarness(BrokerWorkflowSupport):
    """No sockets, worker runtime, or inherited Broker implementation."""

    def __init__(self, coord, manifest):
        self.coord = coord
        self.manifest = manifest
        self.clients = {role: SimpleNamespace(role=role) for role in manifest["roles"]}
        self.recent_reports = []
        self.latest_reports = {}
        self.evidence_reuse = EvidenceReuse(Path(manifest["project"]), set())
        self.role_run_state = {}
        self.pending_run_state = {}
        self.reply = mock.AsyncMock()
        self.broadcast = mock.AsyncMock()
        self.broadcast_workflow = mock.AsyncMock()
        self.deliver = mock.AsyncMock()
        self.assign = mock.AsyncMock(side_effect=self.create_assignment)
        self._assignment = mock.Mock(return_value="synthetic assignment")

    def create_assignment(self, role, kind, round_number=1, content=""):
        assignment_id = secrets.token_hex(16)
        now = broker_store.utc_now()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    role,
                    round_number,
                    kind,
                    "accepted",
                    secrets.token_hex(16),
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' WHERE role=?",
                (assignment_id, role),
            )
        return assignment_id

    async def report(self, role, kind, **fields):
        report = {"kind": kind, "summary": "synthetic report", **fields}
        if kind == "plan":
            report.update(
                {
                    key: []
                    for key in (
                        "relevant_paths",
                        "relevant_symbols",
                        "intended_changes",
                        "required_checks",
                        "risks",
                        "open_questions",
                    )
                }
            )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            assignment_id = database.execute(
                "SELECT active_assignment_id FROM roles WHERE role=?", (role,)
            ).fetchone()[0]
        if assignment_id is None:
            assignment_id = self.create_assignment(role, kind)
        await self.handle_report(
            self.clients[role],
            {
                "id": secrets.token_hex(16),
                "assignment_id": assignment_id,
                "report": report,
            },
        )


class WorkflowSupportTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    def workflow(self, **options):
        initialize_broker_run(
            self.coord, self.manifest, "synthetic task", {}, **options
        )
        return WorkflowHarness(self.coord, self.manifest)

    def test_broker_inherits_the_workflow_surface_without_wrappers(self):
        methods = [
            name
            for name, value in vars(BrokerWorkflowSupport).items()
            if callable(value)
        ]
        self.assertEqual(len(methods), 11)
        for name in methods:
            with self.subTest(method=name):
                self.assertNotIn(name, vars(Broker))
                self.assertIs(
                    getattr(Broker, name), getattr(BrokerWorkflowSupport, name)
                )

    def test_usage_validation_preserves_strict_and_legacy_shapes(self):
        workflow = self.workflow()
        usage = assignment_usage_snapshot()
        self.assertTrue(workflow._valid_report_usage(usage))
        legacy = {
            key: usage["cumulative"][key]
            for key in ("input", "output", "cacheRead", "cacheWrite", "cost")
        }
        self.assertTrue(workflow._valid_usage(legacy))
        self.assertFalse(workflow._valid_usage(legacy, require_provider_calls=True))
        for section, key, value in (
            ("cumulative", "providerCalls", True),
            ("cumulative", "input", -1),
            ("cumulative", "reasoning", False),
            ("cumulative", "cost", {"total": float("inf")}),
            ("cumulative", "cost", {"total": 0, "body": "not metadata"}),
            ("cumulative", "contextPercent", float("nan")),
            ("cumulative", "peakContextTokens", 100),
            ("assignment", "peakContextTokens", -1),
            ("assignment", "body", "not metadata"),
        ):
            with self.subTest(section=section, key=key, value=value):
                invalid = copy.deepcopy(usage)
                invalid[section][key] = value
                self.assertFalse(workflow._valid_report_usage(invalid))

    async def test_report_commits_before_observation_and_routing_and_deduplicates(self):
        workflow = self.workflow()
        assignment_id = workflow.create_assignment("implementer", "implementation")
        message = {
            "id": "c" * 32,
            "assignment_id": assignment_id,
            "report": {"kind": "implementation", "summary": "PRIVATE_REPORT_CANARY"},
            "usage": assignment_usage_snapshot(),
        }
        calls = []

        async def observe(*_args):
            with broker_store.connect_broker_database(
                self.coord, readonly=True
            ) as database:
                report = database.execute("SELECT * FROM reports").fetchone()
                role = database.execute(
                    "SELECT * FROM roles WHERE role='implementer'"
                ).fetchone()
                self.assertEqual(report["input_tokens"], 40)
                self.assertEqual(report["provider_calls"], 1)
                self.assertEqual(role["provider_calls"], 3)
                self.assertIsNone(role["active_assignment_id"])
                self.assertEqual(role["state"], "idle")
                self.assertNotIn(
                    "PRIVATE_REPORT_CANARY", "\n".join(database.iterdump())
                )
            calls.append("observe")

        workflow.broadcast.side_effect = observe
        with mock.patch.object(
            workflow, "route_report", new=mock.AsyncMock(side_effect=observe)
        ) as route:
            await workflow.handle_report(workflow.clients["implementer"], message)
            changed = copy.deepcopy(message)
            changed["usage"]["assignment"]["input"] = 999
            changed["report"]["summary"] = "PRIVATE_DUPLICATE_CANARY"
            await workflow.handle_report(workflow.clients["implementer"], changed)
            route.assert_awaited_once()
        self.assertEqual(calls, ["observe", "observe"])
        self.assertEqual(len(workflow.recent_reports), 1)
        self.assertEqual(
            workflow.latest_reports["implementer"]["report"]["summary"],
            "PRIVATE_REPORT_CANARY",
        )
        self.assertEqual(
            [call.kwargs["status"] for call in workflow.reply.await_args_list],
            ["accepted", "duplicate"],
        )
        # Lost in-memory bodies do not authorize re-routing an already accepted report.
        restored = WorkflowHarness(self.coord, self.manifest)
        await restored.handle_report(restored.clients["implementer"], changed)
        restored.broadcast.assert_not_awaited()
        restored.assign.assert_not_awaited()
        self.assertEqual(restored.recent_reports, [])
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT input_tokens FROM reports").fetchone()[0], 40
            )
            self.assertNotIn("PRIVATE_DUPLICATE_CANARY", "\n".join(database.iterdump()))

    async def test_invalid_report_cannot_write_or_route(self):
        workflow = self.workflow()
        assignment_id = workflow.create_assignment("implementer", "implementation")
        for changes in (
            {"assignment_id": workflow.create_assignment("reviewer", "review")},
            {"assignment_id": workflow.create_assignment("implementer", "plan")},
            {"assignment_id": "not-an-id"},
            {"assignment_id": "f" * 32},
            {
                "report": {
                    "kind": "review",
                    "verdict": "approved",
                    "summary": "invalid role",
                }
            },
            {"report": {"kind": "plan", "summary": "wrong assignment kind"}},
            {"usage": {"body": "not usage"}},
        ):
            with self.subTest(changes=changes):
                message = {
                    "id": "a" * 32,
                    "assignment_id": assignment_id,
                    "report": {"kind": "implementation", "summary": "synthetic report"},
                    **changes,
                }
                with self.assertRaises(OrchestrationError):
                    await workflow.handle_report(
                        workflow.clients["implementer"], message
                    )
        workflow.broadcast.assert_not_awaited()
        workflow.assign.assert_not_awaited()
        self.assertEqual(workflow.latest_reports, {})
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0
            )
            self.assertEqual(
                database.execute("SELECT state FROM assignments").fetchone()[0],
                "accepted",
            )

    async def test_failed_repair_delivery_retains_report_and_pauses_without_replay(
        self,
    ):
        workflow = self.workflow()
        assignment_id = workflow.create_assignment("reviewer", "review")
        message = {
            "id": "a" * 32,
            "assignment_id": assignment_id,
            "report": {
                "kind": "review",
                "summary": "repair needed",
                "verdict": "changes_requested",
            },
        }
        workflow.assign.side_effect = RuntimeError("synthetic delivery failure")
        with self.assertRaisesRegex(OrchestrationError, "routing became uncertain"):
            await workflow.handle_report(workflow.clients["reviewer"], message)
        workflow.reply.assert_not_awaited()
        workflow.broadcast_workflow.assert_has_awaits(
            [mock.call("active", 2), mock.call("uncertain", 1)]
        )
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "uncertain")
        await workflow.handle_report(workflow.clients["reviewer"], message)
        workflow.assign.assert_awaited_once()
        self.assertEqual(workflow.reply.await_args.kwargs["status"], "duplicate")

    async def test_bounded_latest_state_is_coalesced_until_assignment_boundary(self):
        workflow = self.workflow()
        workflow.create_assignment("implementer", "implementation")
        latest_round = MAX_OBSERVER_REPORTS + 2
        for round_number in range(1, latest_round + 1):
            workflow._remember_report(
                {
                    "role": "reviewer",
                    "round": round_number,
                    "report": validate_report(
                        {
                            "kind": "review",
                            "summary": f"LATEST_{round_number}_CANARY",
                            "verdict": "changes_requested",
                        },
                        "reviewer",
                    ),
                }
            )
            await workflow._deliver_run_state(
                ("implementer", "implementer", "offline"), round_number
            )
        workflow.deliver.assert_not_awaited()
        self.assertEqual(len(workflow.recent_reports), MAX_OBSERVER_REPORTS)
        self.assertEqual(workflow.pending_run_state, {"implementer": latest_round})
        workflow._remember_report(
            {"role": "reviewer", "round": 1, "report": {"summary": "STALE_CANARY"}}
        )
        self.assertEqual(workflow.latest_reports["reviewer"]["round"], latest_round)
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE roles SET active_assignment_id=NULL WHERE role='implementer'"
            )
        await workflow._flush_pending_run_state("implementer", latest_round)
        await workflow._flush_pending_run_state("implementer", latest_round)
        workflow.deliver.assert_awaited_once_with(
            "implementer", "run_state", latest_round, mock.ANY, trigger=False
        )
        content = workflow.deliver.await_args.args[3]
        self.assertIn(f"LATEST_{latest_round}_CANARY", content)
        self.assertNotIn("STALE_CANARY", content)
        self.assertEqual(workflow.pending_run_state, {})
        self.assertEqual(workflow.role_run_state["implementer"], content)

    async def test_phased_plan_routes_same_round_implementation_not_review(self):
        workflow = self.workflow(implementation_flow="phased")
        await workflow.report("implementer", "plan")
        workflow.assign.assert_awaited_once_with(
            "implementer", "implementation", 1, "synthetic assignment"
        )
        workflow.deliver.assert_awaited_once_with(
            "implementer", "run_state", 1, mock.ANY, trigger=False
        )

    async def test_single_plan_does_not_start_implementation_or_review(self):
        workflow = self.workflow()
        await workflow.report("implementer", "plan")
        workflow.assign.assert_not_awaited()
        workflow.deliver.assert_awaited_once()

    async def test_forced_specialist_and_skipped_specialist_gate_mandatory_review(self):
        self.enable_specialists("playwright", "django")
        workflow = self.workflow(forced_specialists=("django",))
        await workflow.report(
            "implementer", "implementation", changed_paths=["README.md"]
        )
        workflow.assign.assert_awaited_once_with(
            "django", "django", 1, "synthetic assignment"
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            activations = broker_store.public_specialist_activations(
                database, round_number=1
            )
        self.assertEqual(
            {item["role"]: item["decision"] for item in activations},
            {"django": "run", "playwright": "skipped"},
        )
        await workflow.report("django", "django", verdict="advisory_approved")
        self.assertEqual(workflow.assign.await_args.args[:3], ("reviewer", "review", 1))
        await workflow.maybe_assign_reviewer(1)
        self.assertEqual(workflow.assign.await_count, 2)
        workflow.broadcast_workflow.assert_not_awaited()
        await workflow.report("reviewer", "review", verdict="approved")
        workflow.broadcast_workflow.assert_awaited_once_with("ready", 1)
        self.assertEqual(
            broker_store.public_broker_snapshot(self.coord)["workflow"]["state"],
            "ready",
        )

    async def test_missing_activation_facts_block_review_even_after_implementation(
        self,
    ):
        self.enable_specialists("probe")
        workflow = self.workflow()
        with mock.patch.object(workflow, "route_report", new=mock.AsyncMock()):
            await workflow.report("implementer", "implementation")
        await workflow.maybe_assign_reviewer(1)
        workflow.assign.assert_not_awaited()
        workflow.broadcast_workflow.assert_not_awaited()
