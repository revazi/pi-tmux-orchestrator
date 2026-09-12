from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import (
    Broker,
    Client,
    Observer,
    initialize_broker_run,
)
from pi_tmux_orchestrator.broker_control import BrokerControlSupport
from pi_tmux_orchestrator.broker_observers import BrokerObserverSupport
from pi_tmux_orchestrator.constants import (
    BROKER_PROTOCOL_VERSION,
)
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.protocol import (
    validate_report,
)


from broker_test_support import BrokerFixture


class BrokerRecoveryTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    """Connected handover, restart, uncertainty, and attention regressions."""

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
        self.assertIsInstance(broker, BrokerControlSupport)
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
        self.assertIsInstance(broker, BrokerObserverSupport)
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

    async def test_attention_send_targets_only_the_waiting_assignment_owner(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ("implementer", "reviewer")
        }
        assignment_id = "3" * 32
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
                    "4" * 32,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='waiting' "
                "WHERE role='implementer'",
                (assignment_id,),
            )
            broker_store.set_meta(database, "workflow_state", "needs_attention")
        token = (self.coord / "control.token").read_text(encoding="ascii").strip()

        def send_message(role: str, command_id: str) -> dict[str, Any]:
            return {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "control",
                "token": token,
                "id": command_id,
                "action": "send",
                "role": role,
                "delivery": "steer",
                "message": "Submit the active assignment report.",
            }

        with (
            mock.patch.object(broker, "deliver", new=mock.AsyncMock()) as deliver,
            mock.patch.object(broker, "send_raw", new=mock.AsyncMock()) as send_raw,
            mock.patch.object(
                broker, "broadcast_workflow", new=mock.AsyncMock()
            ) as broadcast_workflow,
        ):
            await broker.handle_control(
                mock.Mock(), mock.Mock(), send_message("reviewer", "5" * 32)
            )
            rejected = send_raw.await_args_list[-1].args[1]
            self.assertFalse(rejected["success"])
            self.assertEqual(rejected["status"], "conflict")
            deliver.assert_not_awaited()
            self.assertEqual(
                broker_store.public_broker_snapshot(self.coord)["workflow"]["state"],
                "needs_attention",
            )

            with broker_store.connect_broker_database(self.coord) as database:
                database.execute(
                    "UPDATE assignments SET state='completed' WHERE id=?",
                    (assignment_id,),
                )
            await broker.handle_control(
                mock.Mock(), mock.Mock(), send_message("implementer", "6" * 32)
            )
            stale = send_raw.await_args_list[-1].args[1]
            self.assertFalse(stale["success"])
            self.assertEqual(stale["status"], "conflict")
            deliver.assert_not_awaited()

            with broker_store.connect_broker_database(self.coord) as database:
                database.execute(
                    "UPDATE assignments SET state='accepted' WHERE id=?",
                    (assignment_id,),
                )
            await broker.handle_control(
                mock.Mock(), mock.Mock(), send_message("implementer", "7" * 32)
            )
            accepted = send_raw.await_args_list[-1].args[1]
            self.assertTrue(accepted["success"])
            self.assertEqual(accepted["status"], "accepted")
            deliver.assert_awaited_once()
            self.assertEqual(
                broker_store.public_broker_snapshot(self.coord)["workflow"]["state"],
                "needs_attention",
            )
            broadcast_workflow.assert_not_awaited()

            with mock.patch.object(broker, "reply", new=mock.AsyncMock()):
                await broker.handle_lifecycle(
                    broker.clients["implementer"],
                    {
                        "state": "active",
                        "usage": None,
                        "id": "8" * 32,
                    },
                )
            self.assertEqual(
                broker_store.public_broker_snapshot(self.coord)["workflow"]["state"],
                "active",
            )
            broadcast_workflow.assert_awaited_once_with("active", 1)
