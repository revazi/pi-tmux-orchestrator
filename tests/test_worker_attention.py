from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest import mock

from broker_test_support import BrokerFixture
from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import Broker, Client, initialize_broker_run
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.dashboard import render_dashboard
from pi_tmux_orchestrator.supervisor_api import public_supervisor_run
from pi_tmux_orchestrator.protocol import encode_frame, validate_client_message
from pi_tmux_orchestrator.worker_attention import validate_attention


class AttentionProtocolTests(unittest.TestCase):
    def test_strict_bounds_and_no_raw_errors(self):
        for reason in ("clarification", "blocked", "tool_failure", "report_failure"):
            value = {"reason": reason, "summary": "😀" * 500, "question": "q"}
            self.assertEqual(validate_attention(value), value)
        for value in (
            None,
            [],
            {},
            {"reason": []},
            {"reason": "other"},
            {"reason": "blocked", "extra": "PRIVATE_CANARY"},
            *(
                {"reason": "blocked", "question": text}
                for text in (
                    "",
                    " ",
                    None,
                    1,
                    "x" * 501,
                    "\x00",
                    "\n",
                    "\x1b",
                    "\x7f",
                    "\x80",
                    "\ud800",
                )
            ),
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(OrchestrationError) as caught,
            ):
                validate_attention(value)
            self.assertNotIn("PRIVATE_CANARY", str(caught.exception))
        message = {
            "version": 1,
            "type": "attention",
            "id": "a" * 32,
            "role": "implementer",
            "token": "b" * 32,
            "assignment_id": "c" * 32,
            "attention": {"reason": "clarification", "question": "q"},
        }
        self.assertEqual(validate_client_message(message), message)
        for changes in (
            {"assignment_id": None},
            {"raw_args": "private"},
            {"attention": {"reason": False}},
        ):
            with self.assertRaises(OrchestrationError):
                validate_client_message({**message, **changes})


class WorkerAttentionTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        initialize_broker_run(self.coord, self.manifest, "synthetic task", {})
        self.broker = Broker(self.coord, self.manifest)
        self.client = Client("implementer", mock.Mock(), mock.Mock())
        self.broker.clients = {"implementer": self.client}
        for name in (
            "send",
            "reply",
            "broadcast",
            "broadcast_workflow",
            "route_report",
            "refresh_dashboard",
        ):
            setattr(
                self.broker,
                name,
                mock.AsyncMock() if name != "refresh_dashboard" else mock.Mock(),
            )
        self.assignment = "1" * 32
        with broker_store.connect_broker_database(self.coord) as database:
            now = broker_store.utc_now()
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) VALUES (?, 'implementer',1,'implementation','accepted',?,?,?)",
                (self.assignment, "2" * 32, now, now),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active' WHERE role='implementer'",
                (self.assignment,),
            )
            broker_store.set_meta(database, "workflow_state", "active")

    def message(self, kind, event="3", **fields):
        return {
            "version": 1,
            "type": kind,
            "id": event * 32,
            "assignment_id": self.assignment,
            **fields,
        }

    def metadata(self):
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            return dict(
                database.execute(
                    "SELECT * FROM assignments WHERE id=?", (self.assignment,)
                ).fetchone()
            )

    async def test_one_nudge_one_provider_grant_and_next_settlement_is_actionable(self):
        message = self.message("settlement")
        await self.broker.handle_message(self.client, message)
        await self.broker.handle_message(self.client, message)
        self.assertEqual(self.metadata()["settlement_count"], 1)
        self.assertEqual(self.metadata()["reminder_state"], "reserved")
        self.assertEqual(self.broker.send.await_count, 1)
        self.assertEqual(self.broker.send.call_args.args[1]["type"], "report_reminder")
        await self.broker.handle_message(
            self.client, self.message("recovery_turn", "4")
        )
        self.assertTrue(self.broker.reply.call_args.kwargs["status"] == "granted")
        await self.broker.handle_message(
            self.client, self.message("recovery_turn", "4")
        )
        self.assertFalse(self.broker.reply.call_args.args[2])
        await self.broker.handle_message(self.client, self.message("settlement", "5"))
        await self.broker.handle_message(
            self.client, message
        )  # old replay cannot nudge
        self.assertEqual(self.metadata()["settlement_count"], 2)
        self.assertEqual(self.broker.send.await_count, 1)
        self.assertEqual(self.broker.observer_snapshot()["state"], "needs_attention")
        self.assertEqual(
            self.broker.observer_snapshot()["roles"][0]["assignment"]["report_attempt"],
            "none",
        )

    async def test_attention_prose_is_live_only_duplicate_and_recovery_have_no_prose(
        self,
    ):
        prose = "PRIVATE_ATTENTION_CANARY"
        message = self.message(
            "attention",
            attention={"reason": "clarification", "summary": prose, "question": prose},
        )
        await self.broker.handle_message(self.client, message)
        await self.broker.handle_message(self.client, message)
        await self.broker.handle_message(self.client, self.message("settlement"))
        self.assertEqual(self.broker.send.await_count, 0)
        frames = [call.args[0] for call in self.broker.broadcast.call_args_list]
        self.assertEqual(sum(frame["type"] == "attention" for frame in frames), 1)
        self.assertNotIn(prose, json.dumps(self.broker.observer_snapshot()))
        self.assertNotIn(
            prose, json.dumps(broker_store.public_broker_snapshot(self.coord))
        )
        public = broker_store.public_broker_snapshot(self.coord)
        self.assertNotIn(
            prose,
            render_dashboard(
                self.manifest, public, [], width=120, height=40, color=False
            ),
        )
        self.assertNotIn(
            prose, json.dumps(public_supervisor_run(self.coord, self.manifest))
        )
        self.assertNotIn(prose, json.dumps(self.manifest))
        self.assertNotIn(
            prose, (self.coord / "broker.sqlite3").read_bytes().decode("latin1")
        )
        self.assertEqual(self.broker.recent_reports, [])
        recovered = Broker(self.coord, self.manifest)
        recovered.worker_baselines = {"implementer": "synthetic baseline"}
        recovered.deliver = mock.AsyncMock()
        with (
            mock.patch.object(recovered, "send", new=mock.AsyncMock()) as send,
            mock.patch.object(recovered, "broadcast", new=mock.AsyncMock()),
        ):
            await recovered.recover_role(self.client, handover=True)
        self.assertFalse(send.call_args.args[1]["trigger"])
        self.assertNotIn(prose, json.dumps(recovered.observer_snapshot()))

    async def test_rejected_schema_attempt_then_accepted_report_is_exact_once(self):
        with self.assertRaises(OrchestrationError):
            await self.broker.handle_message(
                self.client,
                self.message(
                    "report", report={"kind": "implementation", "summary": ""}
                ),
            )
        self.assertEqual(self.metadata()["report_attempt"], "rejected")
        await self.broker.handle_message(self.client, self.message("rejected_report"))
        await self.broker.handle_message(self.client, self.message("settlement"))
        self.assertEqual(self.broker.send.await_count, 1)
        await self.broker.handle_message(
            self.client,
            self.message(
                "report", report={"kind": "implementation", "summary": "done"}
            ),
        )
        await self.broker.handle_message(
            self.client,
            self.message(
                "report", report={"kind": "implementation", "summary": "done"}
            ),
        )
        self.assertEqual(self.metadata()["report_attempt"], "accepted")
        self.assertEqual(self.broker.route_report.await_count, 1)
        self.assertEqual(
            self.broker.observer_snapshot()["roles"][0]["assignment"]["report_attempt"],
            "accepted",
        )
        await self.broker.recover_role(self.client)
        closure = self.broker.send.call_args.args[1]
        self.assertEqual(closure["type"], "assignment_closed")
        self.assertEqual(closure["assignment_id"], self.assignment)
        with self.assertRaises(OrchestrationError):
            await self.broker.handle_message(
                self.client, self.message("settlement", "6")
            )
        with self.assertRaises(OrchestrationError):
            await self.broker.handle_message(
                self.client, self.message("attention", attention={"reason": "blocked"})
            )

    async def test_reconnect_restart_and_uncertain_scheduling_consume_allowance(self):
        await self.broker.handle_message(self.client, self.message("settlement"))
        recovered = Broker(self.coord, self.manifest)
        recovered.worker_baselines = {"implementer": "synthetic baseline"}
        recovered.deliver = mock.AsyncMock()
        for handover in (False, True):
            with (
                mock.patch.object(recovered, "send", new=mock.AsyncMock()) as send,
                mock.patch.object(recovered, "broadcast", new=mock.AsyncMock()),
            ):
                await recovered.recover_role(self.client, handover=handover)
            self.assertFalse(send.call_args.args[1]["trigger"])
        self.assertEqual(self.metadata()["reminder_state"], "used")
        # Handover sets delivering, not a new assignment or allowance.
        self.assertEqual(self.metadata()["settlement_count"], 1)
        await self.broker.handle_message(
            self.client, self.message("recovery_turn", "4")
        )
        self.assertFalse(self.broker.reply.call_args.args[2])
        await self.broker.handle_message(self.client, self.message("settlement", "5"))
        self.assertEqual(self.broker.send.await_count, 1)

    async def test_stale_assignment_generation_lifecycle_and_ack_cannot_reopen(self):
        for kind, extra in (
            ("attention", {"attention": {"reason": "blocked"}}),
            ("settlement", {}),
            ("rejected_report", {}),
            ("recovery_turn", {}),
        ):
            with self.subTest(kind=kind), self.assertRaises(OrchestrationError):
                await self.broker.handle_message(
                    self.client,
                    {**self.message(kind, **extra), "assignment_id": "f" * 32},
                )
        stale = Client("implementer", mock.Mock(), mock.Mock(), generation=2)
        with self.assertRaises(OrchestrationError):
            await self.broker.handle_message(stale, self.message("settlement"))
        await self.broker.handle_message(
            self.client, self.message("attention", attention={"reason": "tool_failure"})
        )
        await self.broker.handle_message(
            self.client, self.message("lifecycle", state="active", usage=None)
        )
        self.assertEqual(self.broker.observer_snapshot()["state"], "needs_attention")
        with self.assertRaises(OrchestrationError):
            await self.broker.handle_message(
                self.client,
                {
                    **self.message("lifecycle", state="active", usage=None),
                    "assignment_id": "f" * 32,
                },
            )
        self.assertEqual(self.metadata()["settlement_count"], 0)

    def test_schema_ten_migration_is_conservative_and_metadata_only(self):
        with broker_store.connect_broker_database(self.coord) as database:
            for column in (
                "settlement_count",
                "last_settlement_id",
                "reminder_state",
                "report_attempt",
                "attention_reason",
                "last_phase",
            ):
                database.execute(f"ALTER TABLE assignments DROP COLUMN {column}")
            broker_store.set_meta(database, "schema_version", "10")
        broker_store.prepare_broker_database(self.coord)
        broker_store.prepare_broker_database(self.coord)
        self.assertEqual(self.metadata()["report_attempt"], "none")
        self.assertEqual(self.metadata()["reminder_state"], "used")

    async def test_authenticated_live_wire_fanout_and_observer_reconnect_never_replays_prose(
        self,
    ):
        broker = Broker(self.coord, self.manifest)
        broker.refresh_dashboard = mock.Mock()
        socket_path = Path(self.temporary.name) / "attention.sock"
        server = await asyncio.start_unix_server(broker.handle_client, path=socket_path)
        writers = []

        async def read(reader):
            prefix = await asyncio.wait_for(reader.readexactly(4), 2)
            return json.loads(
                await asyncio.wait_for(
                    reader.readexactly(int.from_bytes(prefix, "big")), 2
                )
            )

        async def connect(message):
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writers.append(writer)
            writer.write(encode_frame(message))
            await writer.drain()
            response = await read(reader)
            self.assertTrue(response["success"])
            return reader, writer

        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            token = database.execute(
                "SELECT auth_token FROM roles WHERE role='implementer'"
            ).fetchone()[0]
            parent_token = database.execute(
                "SELECT value FROM meta WHERE key='control_token'"
            ).fetchone()[0]
        try:
            worker_reader, worker_writer = await connect(
                {
                    "version": 1,
                    "type": "hello",
                    "id": "8" * 32,
                    "role": "implementer",
                    "token": token,
                    "generation": 1,
                }
            )
            assignment = await read(worker_reader)
            self.assertEqual(assignment["assignment_id"], self.assignment)
            observer_reader, observer_writer = await connect(
                {"version": 1, "type": "observe", "id": "9" * 32, "token": parent_token}
            )
            self.assertEqual((await read(observer_reader))["type"], "snapshot")
            worker_writer.write(
                encode_frame(
                    {
                        **self.message(
                            "attention",
                            attention={
                                "reason": "blocked",
                                "question": "PRIVATE_WIRE_CANARY",
                            },
                        ),
                        "role": "implementer",
                        "token": token,
                    }
                )
            )
            await worker_writer.drain()
            self.assertTrue((await read(worker_reader))["success"])
            live_frames = [await read(observer_reader) for _ in range(3)]
            self.assertEqual(
                [frame["type"] for frame in live_frames],
                ["assignment_state", "attention", "workflow"],
            )
            self.assertEqual(
                live_frames[1]["attention"]["question"], "PRIVATE_WIRE_CANARY"
            )
            observer_writer.close()
            await observer_writer.wait_closed()
            replay_reader, _ = await connect(
                {"version": 1, "type": "observe", "id": "7" * 32, "token": parent_token}
            )
            snapshot = await read(replay_reader)
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertNotIn("PRIVATE_WIRE_CANARY", json.dumps(snapshot))
            self.assertNotIn(
                "PRIVATE_WIRE_CANARY",
                (self.coord / "broker.sqlite3").read_bytes().decode("latin1"),
            )
        finally:
            for writer in writers:
                writer.close()
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers), return_exceptions=True
            )
            server.close()
            await server.wait_closed()

    async def test_failed_nudge_transport_is_actionable_without_renewed_allowance(self):
        self.broker.send.side_effect = ConnectionError("PRIVATE_TRANSPORT_CANARY")
        with self.assertRaises(OrchestrationError) as caught:
            await self.broker.handle_message(self.client, self.message("settlement"))
        self.assertNotIn("PRIVATE_TRANSPORT_CANARY", str(caught.exception))
        self.assertEqual(self.metadata()["reminder_state"], "used")
        self.assertEqual(self.broker.observer_snapshot()["state"], "needs_attention")
        self.broker.send.side_effect = None
        await self.broker.handle_message(self.client, self.message("settlement", "4"))
        self.assertEqual(self.broker.send.await_count, 1)
        self.assertNotIn(
            "PRIVATE_TRANSPORT_CANARY",
            (self.coord / "broker.sqlite3").read_bytes().decode("latin1"),
        )
