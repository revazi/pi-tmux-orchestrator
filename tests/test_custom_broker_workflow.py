"""Gated custom workflow/ACL tests: real SQLite, synthetic transport, no Pi lifecycle."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
import sqlite3
import socket
import unittest
from types import MethodType
from unittest import mock

from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import Broker
from pi_tmux_orchestrator.constants import MAX_RUN_STATE_BYTES
from pi_tmux_orchestrator.context_capsules import render_run_state_capsule
from pi_tmux_orchestrator.custom_role_resources import retained_custom_contracts
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.evidence_reuse import EvidenceReuse
from pi_tmux_orchestrator.protocol import encode_frame
from pi_tmux_orchestrator.role_contracts import validate_assignment_kind
from test_broker import assignment_usage_snapshot
from test_broker_workflow import WorkflowHarness
from test_custom_role_resources import CustomRoleResourceFixture


class CustomBrokerWorkflowTests(
    CustomRoleResourceFixture, unittest.IsolatedAsyncioTestCase
):
    def workflow(self, *, flow="single", count=3):
        original = self.manifest["roles"].pop(self.name)
        for index in range(count):
            name = f"custom-specialist-{index}"
            role = copy.deepcopy(original)
            role.update(
                pane_id=f"%{index + 4}",
                session_id=f"run-1-{name}",
                session_dir=str(self.coord / "sessions" / name),
            )
            role["custom_role"].update(
                id=name, contract=("probe", "playwright", "django")[index % 3]
            )
            self.manifest["roles"][name] = role
        # Exercise only the lower workflow boundary; public initialization and
        # Broker construction intentionally still reject this worker set.
        retained_custom_contracts(self.manifest, self.coord)
        broker_store.initialize_broker_database(
            self.coord,
            self.manifest,
            {role: "a" * 32 for role in self.manifest["roles"]},
            "b" * 32,
            soft_role_tokens=0,
            soft_total_tokens=0,
            implementation_flow=flow,
        )
        workflow = WorkflowHarness(self.coord, self.manifest)
        workflow.refresh_dashboard = mock.Mock()
        workflow.send = mock.AsyncMock()
        for client in workflow.clients.values():
            client.generation = 1
        return workflow

    async def finish_specialists(self, workflow):
        for role, contract in workflow.custom_contracts.items():
            fields = {
                "probe": {},
                "playwright": {"verdict": "fail"},
                "django": {"verdict": "issues_found"},
            }[contract]
            await workflow.report(role, contract, **fields)

    async def test_all_selected_contracts_gate_independent_review_each_round(self):
        workflow = self.workflow(count=8)
        await workflow.report("implementer", "implementation")
        self.assertEqual(workflow.assign.await_count, 8)
        for call, (role, contract) in zip(
            workflow.assign.await_args_list, workflow.custom_contracts.items()
        ):
            self.assertEqual(call.args[:3], (role, contract, 1))
        await self.finish_specialists(workflow)
        self.assertEqual(workflow.assign.await_args.args[:3], ("reviewer", "review", 1))
        self.assertEqual(workflow.assign.await_count, 9)
        workflow.broadcast_workflow.assert_not_awaited()
        await workflow.maybe_assign_reviewer(1)
        self.assertEqual(workflow.assign.await_count, 9)
        await workflow.report("reviewer", "review", verdict="changes_requested")
        self.assertEqual(
            workflow.assign.await_args.args[:3], ("implementer", "implementation", 2)
        )
        workflow.assign.reset_mock()
        await workflow.report("implementer", "implementation")
        self.assertEqual(workflow.assign.await_count, 8)
        self.assertTrue(
            all(call.args[2] == 2 for call in workflow.assign.await_args_list)
        )
        # Historical custom reports cannot satisfy this round's review gate.
        await workflow.maybe_assign_reviewer(2)
        self.assertEqual(workflow.assign.await_count, 8)
        await self.finish_specialists(workflow)
        self.assertEqual(workflow.assign.await_args.args[:3], ("reviewer", "review", 2))
        await workflow.report("reviewer", "review", verdict="approved")
        workflow.broadcast_workflow.assert_awaited_with("ready", 2)

    async def test_retained_report_kind_mismatch_does_not_satisfy_review(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        await workflow.report("implementer", "implementation")
        with mock.patch.object(workflow, "maybe_assign_reviewer"):
            await workflow.report(role, "probe")
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("UPDATE reports SET kind='review' WHERE role=?", (role,))
        await workflow.maybe_assign_reviewer(1)
        self.assertEqual(workflow.assign.await_count, 1)
        workflow.broadcast_workflow.assert_not_awaited()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("UPDATE reports SET kind='probe' WHERE role=?", (role,))
        await workflow.maybe_assign_reviewer(1)
        self.assertEqual(workflow.assign.await_args.args[:3], ("reviewer", "review", 1))

    def test_custom_reuse_receipts_require_explicit_bounded_bindings(self):
        workflow = self.workflow(count=8)
        roles = set(workflow.custom_contracts)
        with self.assertRaises(ValueError):
            EvidenceReuse(self.project, roles)
        reuse = EvidenceReuse(
            self.project, roles, custom_contracts=workflow.custom_contracts
        )
        with mock.patch(
            "pi_tmux_orchestrator.evidence_reuse.worktree_stamp", return_value=None
        ):
            for role in roles:
                reuse.remember(role)
        self.assertEqual(reuse.snapshot(), dict.fromkeys(roles, "unavailable"))
        with self.assertRaises(ValueError):
            reuse.remember("custom-unselected")
        self.assertEqual(len(reuse.receipts), 8)
        reuse.invalidate_guidance()
        self.assertEqual(reuse.snapshot(), dict.fromkeys(roles, "guidance_changed"))

    async def test_phased_plan_never_activates_custom_specialists(self):
        workflow = self.workflow(flow="phased")
        await workflow.report("implementer", "plan")
        workflow.assign.assert_awaited_once()
        self.assertEqual(
            workflow.assign.await_args.args[:3], ("implementer", "implementation", 1)
        )
        workflow.assign.reset_mock()
        await workflow.report("implementer", "implementation")
        self.assertEqual(workflow.assign.await_count, 3)
        self.assertEqual(
            {call.args[0] for call in workflow.assign.await_args_list},
            set(workflow.custom_contracts),
        )

    async def test_report_usage_is_atomic_idempotent_and_body_free(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        assignment_id = workflow.create_assignment(role, "probe")
        message = {
            "id": secrets.token_hex(16),
            "assignment_id": assignment_id,
            "report": {"kind": "probe", "summary": "PRIVATE_CUSTOM_REPORT_CANARY"},
            "usage": assignment_usage_snapshot(),
        }
        # Fail after report insertion but before cumulative accounting completes.
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "CREATE TRIGGER fail_accounting BEFORE UPDATE OF input_tokens ON roles "
                "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            await workflow.handle_report(workflow.clients[role], message)
        workflow.broadcast.assert_not_awaited()
        with broker_store.connect_broker_database(self.coord) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0
            )
            self.assertEqual(
                database.execute("SELECT state FROM assignments").fetchone()[0],
                "accepted",
            )
            database.execute("DROP TRIGGER fail_accounting")
        await workflow.handle_report(workflow.clients[role], message)
        duplicate = copy.deepcopy(message)
        duplicate["usage"]["cumulative"]["input"] = 999
        duplicate["usage"]["assignment"]["input"] = 999
        await workflow.handle_report(workflow.clients[role], duplicate)
        workflow.broadcast.assert_awaited_once()
        workflow.broadcast_workflow.assert_not_awaited()
        workflow.assign.assert_not_awaited()
        snapshot = broker_store.public_broker_snapshot(self.coord)
        selected = next(item for item in snapshot["roles"] if item["role"] == role)
        self.assertEqual(selected["provider_calls"], 3)
        self.assertEqual(
            selected["latest_assignment_usage"]["usage"]["input_tokens"], 40
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1
            )
            self.assertNotIn("PRIVATE_", "\n".join(database.iterdump()))
        self.assertNotIn("PRIVATE_", json.dumps(snapshot))

    async def test_custom_frames_cannot_select_identity_contract_or_authority(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        hello = {
            "version": 1,
            "type": "hello",
            "role": role,
            "id": "c" * 32,
            "token": "a" * 32,
            "generation": 1,
        }
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(hello))
        self.assertEqual(await Broker.read_frame(workflow, reader), hello)
        for fields in (
            {"contract": "reviewer"},
            {"role": "custom-unselected"},
            {"custom_contracts": {role: "reviewer"}},
            {"type": "control"},
        ):
            reader = asyncio.StreamReader()
            reader.feed_data(encode_frame({**hello, **fields}))
            with self.subTest(fields=fields), self.assertRaises(OrchestrationError):
                await Broker.read_frame(workflow, reader)
        assignment_id = workflow.create_assignment(role, "probe")
        for report in (
            {"kind": "review", "verdict": "approved"},
            {"kind": "implementation"},
            {"kind": "plan"},
            {"kind": "django", "verdict": "advisory_approved"},
            {"kind": "probe", "changed_paths": ["source.py"]},
            {"kind": "probe", "verdict": "approved"},
        ):
            with self.subTest(report=report), self.assertRaises(OrchestrationError):
                await workflow.handle_report(
                    workflow.clients[role],
                    {
                        "id": "c" * 32,
                        "assignment_id": assignment_id,
                        "report": {"summary": "untrusted", **report},
                        "usage": assignment_usage_snapshot(),
                    },
                )
        workflow.broadcast.assert_not_awaited()
        workflow.assign.assert_not_awaited()
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0
            )

    async def test_authenticated_handler_binds_role_token_and_generation(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        assignment_id = workflow.create_assignment(role, "probe")
        workflow.clients.clear()
        workflow.maybe_start_workflow = mock.AsyncMock()
        workflow.recover_role = mock.AsyncMock()
        for name in (
            "read_raw_frame",
            "read_frame",
            "reply",
            "send",
            "handle_message",
            "_verify_peer",
            "handle_control",
        ):
            setattr(workflow, name, MethodType(getattr(Broker, name), workflow))
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "UPDATE roles SET auth_token=? WHERE role=?", ("d" * 32, role)
            )
        hello = {
            "version": 1,
            "type": "hello",
            "role": role,
            "token": "d" * 32,
            "id": "c" * 32,
            "generation": 1,
        }
        report = {
            "version": 1,
            "type": "report",
            "role": role,
            "token": "d" * 32,
            "id": "e" * 32,
            "assignment_id": assignment_id,
            "report": {"kind": "probe", "summary": "PRIVATE_WIRE_CANARY"},
            "usage": assignment_usage_snapshot(),
        }
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        async def exchange(frames):
            reader = asyncio.StreamReader()
            for frame in frames:
                reader.feed_data(encode_frame(frame))
            reader.feed_eof()
            writer = mock.Mock()
            writer.get_extra_info.return_value = left
            writer.drain = mock.AsyncMock()
            writer.wait_closed = mock.AsyncMock()
            await Broker.handle_client(workflow, reader, writer)
            return [
                json.loads(call.args[0][4:]) for call in writer.write.call_args_list
            ]

        for invalid in (
            {**hello, "token": "a" * 32},
            {**hello, "generation": 2},
            {**hello, "role": "custom-unselected"},
        ):
            self.assertEqual(await exchange([invalid, report]), [])
        for invalid in (
            {**report, "role": "reviewer"},
            {**report, "token": "a" * 32},
            {
                **report,
                "report": {
                    "kind": "review",
                    "summary": "forged",
                    "verdict": "approved",
                },
            },
        ):
            responses = await exchange([hello, invalid])
            self.assertTrue(responses[0]["success"])
            self.assertFalse(responses[-1]["success"])
        control = {
            "version": 1,
            "type": "control",
            "token": "d" * 32,
            "id": "f" * 32,
            "action": "restart",
            "role": "implementer",
            "delivery": None,
            "message": None,
        }
        self.assertEqual(await exchange([control]), [])
        workflow.broadcast.assert_not_awaited()
        responses = await exchange([hello, report, report])
        self.assertEqual(
            [response["status"] for response in responses],
            ["connected", "accepted", "duplicate"],
        )
        workflow.broadcast.assert_awaited_once()
        workflow.broadcast_workflow.assert_not_awaited()
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1
            )
            self.assertEqual(
                database.execute(
                    "SELECT state FROM roles WHERE role=?", (role,)
                ).fetchone()[0],
                "disconnected",
            )
            self.assertNotIn("PRIVATE_", "\n".join(database.iterdump()))

    async def test_stale_generation_cannot_commit_custom_report_usage(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        assignment_id = workflow.create_assignment(role, "probe")
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("UPDATE roles SET generation=2 WHERE role=?", (role,))
        with self.assertRaisesRegex(OrchestrationError, "generation is stale"):
            await Broker.handle_message(
                workflow,
                workflow.clients[role],
                {
                    "type": "report",
                    "id": "c" * 32,
                    "assignment_id": assignment_id,
                    "report": {"kind": "probe", "summary": "stale"},
                    "usage": assignment_usage_snapshot(),
                },
            )
        workflow.broadcast.assert_not_awaited()
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 0
            )

    async def test_outgoing_and_restored_assignment_cannot_escalate_custom_role(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        for kind in ("implementation", "plan", "review", "django"):
            with self.subTest(kind=kind), self.assertRaises(OrchestrationError):
                await Broker.assign(workflow, role, kind, 1, "unauthorized")
        workflow.send.assert_not_awaited()
        validate_assignment_kind(
            role, "probe", custom_contracts=workflow.custom_contracts
        )
        instructions = Broker._assignment(workflow, role, 1)
        self.assertIn("Do not execute shell or browser checks", instructions)
        self.assertIn("never replaces independent built-in review", instructions)
        workflow.create_assignment(role, "review")
        workflow.current_round = mock.Mock(return_value=1)
        workflow.deliver.reset_mock()
        with self.assertRaisesRegex(OrchestrationError, "Assignment kind"):
            await Broker.recover_role(workflow, workflow.clients[role], handover=True)
        workflow.send.assert_not_awaited()
        workflow.deliver.assert_not_awaited()
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT state FROM assignments").fetchone()[0],
                "accepted",
            )

    async def test_routing_failure_is_uncertain_never_ready_or_replayed(self):
        workflow = self.workflow(count=1)
        role = next(iter(workflow.custom_contracts))
        assignment_id = workflow.create_assignment(role, "probe")
        message = {
            "id": "c" * 32,
            "assignment_id": assignment_id,
            "report": {"kind": "probe", "summary": "PRIVATE_FAILURE_CANARY"},
        }
        with mock.patch.object(
            workflow, "route_report", side_effect=OSError("synthetic")
        ):
            with self.assertRaisesRegex(OrchestrationError, "routing became uncertain"):
                await workflow.handle_report(workflow.clients[role], message)
        workflow.broadcast_workflow.assert_awaited_once_with("uncertain", 1)
        restored = WorkflowHarness(self.coord, self.manifest)
        await restored.handle_report(restored.clients[role], message)
        restored.assign.assert_not_awaited()
        restored.broadcast.assert_not_awaited()
        self.assertEqual(
            broker_store.public_broker_snapshot(self.coord)["workflow"]["state"],
            "uncertain",
        )

    def test_maximum_fanout_capsule_preserves_all_identities_and_reviewer(self):
        workflow = self.workflow(count=8)
        roles = [
            "implementer",
            "probe",
            "playwright",
            "django",
            "reviewer",
            *workflow.custom_contracts,
        ]
        events = [
            {
                "role": role,
                "round": 1,
                "report": {
                    "kind": workflow.custom_contracts.get(role, role),
                    "summary": "大" * 4000,
                    "findings": [{"severity": "high", "summary": "risk"}] * 20,
                },
            }
            for role in roles
        ]
        events.append(
            {"role": "custom-unselected", "round": 1, "report": {"summary": "INTRUDER"}}
        )
        capsule = render_run_state_capsule(
            events, 1, custom_contracts=workflow.custom_contracts
        )
        self.assertLessEqual(len(capsule.encode()), MAX_RUN_STATE_BYTES)
        for role in roles:
            self.assertIn(f"## {role} · round 1", capsule)
        self.assertNotIn("INTRUDER", capsule)
        self.assertNotIn("custom-unselected", capsule)
