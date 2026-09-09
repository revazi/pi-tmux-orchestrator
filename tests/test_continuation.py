"""Model-free regressions for broker-enforced repair continuation."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from pi_tmux_orchestrator import broker_store, commands
from pi_tmux_orchestrator.broker import Broker, Client, initialize_broker_run
from pi_tmux_orchestrator.cli import build_parser
from pi_tmux_orchestrator.continuation import (
    MAX_REPAIR_ROUNDS,
    continuation_status,
    repair_policy,
    retained_repair_policy,
)
from pi_tmux_orchestrator.models import OrchestrationError
from test_broker import BrokerFixture


class RepairPolicyTests(BrokerFixture):
    def test_strict_opt_in_limits_and_cli_confirmation(self):
        self.enterContext(mock.patch("pi_tmux_orchestrator.runtime.JSON_MODE", True))
        for value in (None, 0, 2, MAX_REPAIR_ROUNDS):
            self.assertEqual(repair_policy(value)["max_repair_rounds"], value)
        for value in (True, False, -1, 1.5, "2", {}, MAX_REPAIR_ROUNDS + 1):
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                repair_policy(value)
        parser = build_parser()
        for value in ("-1", "1.5", "true", str(MAX_REPAIR_ROUNDS + 1)):
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                parser.parse_args(["start", "--max-repair-rounds", value])
        self.assertIsNone(parser.parse_args(["start"]).max_repair_rounds)
        self.assertEqual(
            parser.parse_args(["start", "--max-repair-rounds", "0"]).max_repair_rounds,
            0,
        )
        with self.assertRaises(OrchestrationError):
            parser.parse_args(["continue", "pi-test", "--yes"])
        args = parser.parse_args(["continue", "pi-test", "--command-id", "a" * 32])
        with mock.patch.object(commands, "control_target") as target:
            with self.assertRaisesRegex(OrchestrationError, "pass --yes"):
                commands.continue_command(args)
            target.assert_not_called()

    def test_legacy_disabled_and_forward_migration_does_not_reset_counts(self):
        initialize_broker_run(self.coord, self.manifest, "synthetic", {})
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("DELETE FROM meta WHERE key='continuation_policy'")
            broker_store.set_meta(database, "schema_version", "8")
            self.assertIsNone(retained_repair_policy(database)["max_repair_rounds"])
        broker_store.prepare_broker_database(self.coord)
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                "9",
            )
            self.assertEqual(retained_repair_policy(database), repair_policy(None))
        for policy in (
            "invalid",
            '{"version":2,"max_repair_rounds":1}',
            '{"version":true,"max_repair_rounds":1}',
            '{"version":1,"max_repair_rounds":false}',
            '{"version":1,"max_repair_rounds":1,"body":"private"}',
            '{"version":1,"max_repair_rounds":1,"max_repair_rounds":null}',
            " " * 257,
        ):
            with self.subTest(policy=policy):
                with broker_store.connect_broker_database(self.coord) as database:
                    broker_store.set_meta(database, "continuation_policy", policy)
                with self.assertRaises(OrchestrationError):
                    broker_store.prepare_broker_database(self.coord)

    def test_continue_cli_uses_fixed_authenticated_action_and_exact_key(self):
        args = build_parser().parse_args(
            ["continue", "pi-broker-test", "--yes", "--command-id", "a" * 32]
        )
        response = {"id": "a" * 32, "status": "accepted", "duplicate": True}
        with (
            mock.patch.object(
                commands,
                "control_target",
                return_value=(self.manifest["session"], self.coord, self.manifest),
            ),
            mock.patch.object(
                commands, "broker_control_request", return_value=response
            ) as request,
        ):
            result = commands.continue_command(args)
        request.assert_called_once_with(
            self.coord, "implementer", "continue", command_id="a" * 32
        )
        self.assertTrue(result.data["duplicate"])
        self.assertNotIn("ready", result.data)


class RepairAdmissionTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    async def start_broker(self, limit=0, *, flow="single"):
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_CONTINUATION_TASK",
            {},
            max_repair_rounds=limit,
            implementation_flow=flow,
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in self.manifest["roles"]
        }
        broker.send = mock.AsyncMock()
        broker.send_raw = mock.AsyncMock()
        broker.reply = mock.AsyncMock()
        broker.broadcast = mock.AsyncMock()
        broker.broadcast_workflow = mock.AsyncMock()
        broker.worker_baselines["implementer"] = "PRIVATE_BASELINE"
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "active")
        kind = "plan" if flow == "phased" else "implementation"
        await broker.assign("implementer", kind, 1, "synthetic assignment")
        return broker

    async def finish(self, broker, role, verdict=None, *, plan=False):
        report = {
            "kind": "review" if role == "reviewer" else "implementation",
            "summary": "PRIVATE_REPORT_BODY",
        }
        if verdict is not None:
            report["verdict"] = verdict
        if plan:
            report["kind"] = "plan"
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
        message = {"id": "b" * 32, "assignment_id": assignment_id, "report": report}
        await broker.handle_report(broker.clients[role], message)
        return message

    def snapshot(self):
        return broker_store.public_broker_snapshot(self.coord)

    def control(self, action="continue", command_id="c" * 32, role="implementer"):
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            token = database.execute(
                "SELECT value FROM meta WHERE key='control_token'"
            ).fetchone()[0]
        return {
            "version": 1,
            "type": "control",
            "token": token,
            "id": command_id,
            "action": action,
            "role": role,
            "delivery": None,
            "message": None,
        }

    async def pause(self, broker):
        await self.finish(broker, "implementer")
        return await self.finish(broker, "reviewer", "changes_requested")

    async def test_zero_cap_allows_initial_work_and_review_but_no_repair(self):
        broker = await self.start_broker()
        report = await self.pause(broker)
        state = self.snapshot()
        self.assertEqual(state["workflow"]["state"], "needs_attention")
        self.assertEqual(
            state["workflow"]["continuation"],
            {
                "version": 1,
                "max_repair_rounds": 0,
                "repair_rounds_admitted": 0,
                "pending_repair_round": 2,
                "pause_reason": "repair_round_limit",
            },
        )
        self.assertTrue(all(role["assignment"] is None for role in state["roles"]))
        assignments = [
            call.args[1]
            for call in broker.send.await_args_list
            if call.args[1]["type"] == "assignment"
        ]
        self.assertEqual(
            [(item["kind"], item["round"]) for item in assignments],
            [("implementation", 1), ("review", 1)],
        )
        broker.broadcast_workflow.assert_any_await("needs_attention", 2)
        await broker.handle_report(broker.clients["reviewer"], report)
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 0
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            dump = "\n".join(database.iterdump())
        for body in (
            "PRIVATE_REPORT_BODY",
            "PRIVATE_BASELINE",
            "PRIVATE_CONTINUATION_TASK",
        ):
            self.assertNotIn(body, dump)

    async def test_one_round_approval_is_idempotent_and_review_is_still_required(self):
        broker = await self.start_broker()
        await self.pause(broker)
        control = self.control()
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        state = self.snapshot()["workflow"]
        self.assertEqual(state["state"], "active")
        self.assertEqual(state["continuation"]["max_repair_rounds"], 1)
        self.assertEqual(state["continuation"]["repair_rounds_admitted"], 1)
        self.assertIsNone(state["continuation"]["pending_repair_round"])
        self.assertTrue(broker.send_raw.await_args.args[1]["duplicate"])
        # A different ID cannot approve more work while that repair is active.
        await broker.handle_control(
            mock.Mock(), mock.Mock(), self.control(command_id="d" * 32)
        )
        self.assertEqual(broker.send_raw.await_args.args[1]["status"], "conflict")
        await self.finish(broker, "implementer")
        self.assertEqual(self.snapshot()["workflow"]["state"], "active")
        await self.finish(broker, "reviewer", "approved")
        self.assertEqual(self.snapshot()["workflow"]["state"], "ready")

    async def test_approval_does_not_reset_allowance_and_stale_id_cannot_resume_next_pause(
        self,
    ):
        broker = await self.start_broker()
        await self.pause(broker)
        control = self.control()
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        await self.pause(broker)
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        state = self.snapshot()["workflow"]
        self.assertEqual(state["state"], "needs_attention")
        self.assertEqual(state["continuation"]["pending_repair_round"], 3)
        self.assertEqual(state["continuation"]["max_repair_rounds"], 1)
        await broker.handle_control(
            mock.Mock(), mock.Mock(), self.control(command_id="e" * 32)
        )
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 2
        )

    async def test_disabled_limit_preserves_repair_routing(self):
        broker = await self.start_broker(None)
        await self.pause(broker)
        state = self.snapshot()["workflow"]
        self.assertEqual(state["state"], "active")
        self.assertEqual(state["continuation"]["repair_rounds_admitted"], 1)
        self.assertIsNone(state["continuation"]["max_repair_rounds"])

    async def test_phased_initial_implementation_does_not_consume_repair_allowance(
        self,
    ):
        broker = await self.start_broker(flow="phased")
        await self.finish(broker, "implementer", plan=True)
        await self.pause(broker)
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 0
        )

    async def test_post_ready_operator_followup_cannot_bypass_cap(self):
        broker = await self.start_broker()
        await self.finish(broker, "implementer")
        await self.finish(broker, "reviewer", "approved")
        control = {
            **self.control("send"),
            "message": "PRIVATE_FOLLOWUP",
            "delivery": "steer",
        }
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        self.assertEqual(self.snapshot()["workflow"]["state"], "needs_attention")
        await broker.handle_control(
            mock.Mock(), mock.Mock(), {**control, "id": "f" * 32}
        )
        self.assertEqual(broker.send_raw.await_args.args[1]["status"], "conflict")
        await broker.handle_lifecycle(
            broker.clients["implementer"],
            {"id": "b" * 32, "state": "active", "usage": None},
        )
        self.assertEqual(self.snapshot()["workflow"]["state"], "needs_attention")

    async def test_restored_broker_keeps_counts_and_requires_recoverable_evidence(self):
        broker = await self.start_broker(1)
        await self.pause(broker)  # first repair admitted
        await self.pause(broker)  # second repair paused
        restored = Broker(self.coord, self.manifest)
        restored.clients = broker.clients
        restored.send = mock.AsyncMock()
        restored.send_raw = mock.AsyncMock()
        restored.broadcast_workflow = mock.AsyncMock()
        await restored.handle_control(mock.Mock(), mock.Mock(), self.control())
        self.assertEqual(restored.send_raw.await_args.args[1]["status"], "uncertain")
        restored.send.assert_not_awaited()
        await restored.assign("implementer", "implementation", 3, "synthetic")
        restored.send.assert_not_awaited()
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 1
        )
        broker_store.prepare_broker_database(self.coord)
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["max_repair_rounds"], 1
        )

    async def test_failed_continuation_delivery_is_uncertain_not_retried(self):
        broker = await self.start_broker()
        await self.pause(broker)
        control = self.control()
        broker.send.side_effect = RuntimeError("synthetic delivery failure")
        with self.assertRaisesRegex(
            OrchestrationError, "continuation became uncertain"
        ):
            await broker.handle_control(mock.Mock(), mock.Mock(), control)
        self.assertEqual(self.snapshot()["workflow"]["state"], "uncertain")
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 1
        )
        broker.send.reset_mock()
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        broker.send.assert_not_awaited()
        self.assertTrue(broker.send_raw.await_args.args[1]["duplicate"])

    async def test_cancelled_continuation_records_uncertainty_and_consumes_no_extra_approval(
        self,
    ):
        broker = await self.start_broker()
        await self.pause(broker)
        control = self.control()
        with mock.patch.object(broker, "assign", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await broker.handle_control(mock.Mock(), mock.Mock(), control)
        self.assertEqual(self.snapshot()["workflow"]["state"], "uncertain")
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            status = continuation_status(database)
            self.assertEqual(status["max_repair_rounds"], 1)
            self.assertEqual(status["repair_rounds_admitted"], 0)
        await broker.handle_control(mock.Mock(), mock.Mock(), control)
        self.assertEqual(self.snapshot()["workflow"]["state"], "uncertain")

    async def test_concurrent_approvals_cannot_reserve_two_repairs(self):
        broker = await self.start_broker()
        await self.pause(broker)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_send(*_args):
            entered.set()
            await release.wait()

        broker.send.side_effect = blocked_send
        control = self.control()
        task = asyncio.create_task(
            broker.handle_control(mock.Mock(), mock.Mock(), control)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await broker.handle_control(
                mock.Mock(), mock.Mock(), self.control(command_id="d" * 32)
            )
            self.assertEqual(broker.send_raw.await_args.args[1]["status"], "conflict")
            await broker.handle_control(mock.Mock(), mock.Mock(), control)
            self.assertTrue(broker.send_raw.await_args.args[1]["duplicate"])
            self.assertEqual(
                self.snapshot()["workflow"]["continuation"]["max_repair_rounds"], 1
            )
            self.assertEqual(
                self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 1
            )
        finally:
            release.set()
            await task

    async def test_worker_handover_keeps_the_same_repair_count(self):
        broker = await self.start_broker(1)
        await self.pause(broker)
        with broker_store.connect_broker_database(self.coord) as database:
            assignment = database.execute(
                "SELECT * FROM assignments WHERE round=2"
            ).fetchone()
            database.execute(
                "UPDATE assignments SET state='accepted' WHERE id=?",
                (assignment["id"],),
            )
            database.execute(
                "UPDATE roles SET state='recovering' WHERE role='implementer'"
            )
        broker.send.reset_mock()
        await broker.recover_role(broker.clients["implementer"], handover=True)
        replay = broker.send.await_args.args[1]
        self.assertEqual(replay["assignment_id"], assignment["id"])
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["repair_rounds_admitted"], 1
        )
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["max_repair_rounds"], 1
        )

    async def test_unassigned_active_worker_cannot_be_continued(self):
        broker = await self.start_broker()
        await self.pause(broker)
        await broker.handle_lifecycle(
            broker.clients["implementer"],
            {"id": "b" * 32, "state": "active", "usage": None},
        )
        await broker.handle_control(mock.Mock(), mock.Mock(), self.control())
        self.assertEqual(broker.send_raw.await_args.args[1]["status"], "conflict")
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["max_repair_rounds"], 0
        )

    async def test_reports_during_delivery_do_not_get_overwritten_by_active(self):
        broker = await self.start_broker()
        await self.pause(broker)

        async def immediate_report(client, message):
            if message["type"] == "assignment":
                await self.finish(
                    broker,
                    client.role,
                    "approved" if client.role == "reviewer" else None,
                )

        broker.send.side_effect = immediate_report
        await broker.handle_control(mock.Mock(), mock.Mock(), self.control())
        self.assertEqual(self.snapshot()["workflow"]["state"], "ready")
        self.assertEqual(broker.broadcast_workflow.await_args.args[0], "ready")

    async def test_interrupted_approval_is_uncertain_on_broker_restart(self):
        broker = await self.start_broker()
        await self.pause(broker)
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "routing")
        self.assertEqual(broker.observer_snapshot()["state"], "active")
        restored = Broker(self.coord, self.manifest)
        restored.stopping.set()
        await restored._run()
        self.assertEqual(restored.observer_snapshot()["state"], "uncertain")
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["max_repair_rounds"], 0
        )
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("DELETE FROM meta WHERE key='continuation_policy'")
        with self.assertRaisesRegex(OrchestrationError, "policy is missing"):
            broker_store.prepare_broker_database(self.coord)

    async def test_new_approval_requires_control_auth_and_implementer_role(self):
        broker = await self.start_broker()
        await self.pause(broker)
        for control in (
            {**self.control(), "token": "0" * 32},
            self.control(role="reviewer"),
        ):
            with self.subTest(control=control), self.assertRaises(OrchestrationError):
                await broker.handle_control(mock.Mock(), mock.Mock(), control)
        self.assertEqual(
            self.snapshot()["workflow"]["continuation"]["max_repair_rounds"], 0
        )
