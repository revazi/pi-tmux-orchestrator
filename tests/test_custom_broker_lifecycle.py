"""Real Unix-socket custom broker tests, NOT tmux/Pi worker acceptance.

Only the constructor's temporary role-catalog gate is bypassed in test setup.
Public initialization remains gated; framing, authentication, workflow, control,
SQLite transactions and recovery run unmocked with synthetic worker peers.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import unittest
from unittest import mock

from pi_tmux_orchestrator import broker_store, constants
from pi_tmux_orchestrator.broker import Broker, initialize_broker_run
from pi_tmux_orchestrator.custom_role_resources import retained_custom_contracts
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.protocol import encode_frame
from pi_tmux_orchestrator.storage import secure_write
from test_broker import assignment_usage_snapshot
from test_custom_role_resources import CustomRoleResourceFixture


class Peer:
    """Bounded synthetic wire peer; no worker behavior is emulated implicitly."""

    def __init__(self, reader, writer, role, token):
        self.reader = reader
        self.writer = writer
        self.role = role
        self.token = token
        self.pending = []

    async def send(self, kind, **fields):
        message = {
            "version": 1,
            "type": kind,
            "role": self.role,
            "token": self.token,
            "id": secrets.token_hex(16),
            **fields,
        }
        self.writer.write(encode_frame(message))
        await self.writer.drain()
        return message["id"]

    async def receive(self, kind, **fields):
        def matches(frame):
            return frame["type"] == kind and all(
                frame.get(key) == value for key, value in fields.items()
            )

        for index, frame in enumerate(self.pending):
            if matches(frame):
                return self.pending.pop(index)
        async with asyncio.timeout(3):
            for _ in range(100):
                size = int.from_bytes(await self.reader.readexactly(4), "big")
                if not 1 <= size <= constants.MAX_BROKER_FRAME_BYTES:
                    raise AssertionError("Invalid broker frame size")
                frame = json.loads(await self.reader.readexactly(size))
                if matches(frame):
                    return frame
                self.pending.append(frame)
        raise AssertionError("Expected frame was not received")

    async def accept(self, assignment):
        request = await self.send(
            "ack", delivery_id=assignment["id"], status="accepted"
        )
        return await self.receive("response", id=request)


class CustomBrokerLifecycleTests(
    CustomRoleResourceFixture, unittest.IsolatedAsyncioTestCase
):
    async def start_broker(self, contract="probe"):
        self.definition["contract"] = contract
        self.manifest["roles"][self.name]["custom_role"]["contract"] = contract
        self.save_registry()
        contracts = retained_custom_contracts(self.manifest, self.coord)
        self.tokens = {role: secrets.token_hex(16) for role in self.manifest["roles"]}
        self.control_token = secrets.token_hex(16)
        with self.assertRaisesRegex(OrchestrationError, "not enabled"):
            initialize_broker_run(self.coord, self.manifest, "synthetic", {})
        self.assertFalse(broker_store.broker_paths(self.coord)["database"].exists())
        broker_store.initialize_broker_database(
            self.coord,
            self.manifest,
            self.tokens,
            self.control_token,
            soft_role_tokens=0,
            soft_total_tokens=0,
        )
        secure_write(
            self.coord / "startup.json",
            json.dumps({"task": "PRIVATE_TASK_CANARY", "role_tasks": {}}),
        )
        # Prove the production gate still rejects this exact validated manifest.
        with self.assertRaisesRegex(OrchestrationError, "not enabled"):
            Broker(self.coord, self.manifest)
        # This imported catalog is read locally only by the constructor gate.
        # The protocol/manifest modules retain their original built-in catalogs.
        with mock.patch.object(
            constants, "KNOWN_ROLES", constants.KNOWN_ROLES | {self.name}
        ):
            self.broker = Broker(self.coord, self.manifest)
        self.assertEqual(self.broker.custom_contracts, contracts)
        self.peers = []
        self.handlers = set()
        self.handler_errors = []
        handle = self.broker.handle_client

        async def tracked_handle(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                await handle(reader, writer)
            except Exception as error:
                self.handler_errors.append(error)
                writer.close()
            finally:
                self.handlers.discard(task)

        self.broker.handle_client = tracked_handle
        self.run_task = asyncio.create_task(self.broker._run())
        self.addAsyncCleanup(self.stop_broker)
        await self.wait_for(lambda: self.broker.server is not None)

    async def stop_broker(self):
        for peer in self.peers:
            peer.writer.close()
        self.broker.stopping.set()
        await asyncio.wait_for(self.run_task, 3)
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*self.handlers), 3)
        for peer in self.peers:
            await peer.writer.wait_closed()
        self.assertFalse(self.broker.paths["socket"].exists())
        self.assertEqual(self.handler_errors, [])

    async def wait_for(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                if self.run_task.done():
                    self.run_task.result()
                    self.fail("Broker stopped unexpectedly")
                await asyncio.sleep(0.01)

    def row(self, role=None):
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            return dict(
                db.execute(
                    "SELECT * FROM roles WHERE role=?", (role or self.name,)
                ).fetchone()
            )

    def state(self):
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            return db.execute(
                "SELECT value FROM meta WHERE key='workflow_state'"
            ).fetchone()[0]

    async def connect(self, role, *, generation=1, token=None, accepted=True):
        reader, writer = await asyncio.open_unix_connection(self.broker.paths["socket"])
        peer = Peer(reader, writer, role, token or self.tokens[role])
        self.peers.append(peer)
        request = await peer.send("hello", generation=generation)
        if accepted:
            response = await peer.receive("response", id=request)
            self.assertTrue(response["success"])
        else:
            self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        return peer

    async def connect_all(self):
        self.workers = {
            role: await self.connect(role) for role in self.manifest["roles"]
        }
        assignment = await self.workers["implementer"].receive("assignment")
        self.assertEqual(assignment["kind"], "implementation")
        return assignment

    async def report(self, role, assignment, *, verdict=None, request=None):
        peer = self.workers[role]
        self.assertTrue((await peer.accept(assignment))["success"])
        report = {"kind": assignment["kind"], "summary": "PRIVATE_REPORT_CANARY"}
        if verdict is not None:
            report["verdict"] = verdict
        request = await peer.send(
            "report",
            id=request or secrets.token_hex(16),
            assignment_id=assignment["assignment_id"],
            report=report,
            usage=assignment_usage_snapshot(),
        )
        response = await peer.receive("response", id=request)
        self.assertTrue(response["success"], response)
        return request

    async def custom_assignment(self):
        initial = await self.connect_all()
        await self.report("implementer", initial)
        assignment = await self.workers[self.name].receive("assignment")
        self.assertEqual(assignment["kind"], self.broker.custom_contracts[self.name])
        return assignment

    async def control(self, action, *, request=None):
        reader, writer = await asyncio.open_unix_connection(self.broker.paths["socket"])
        peer = Peer(reader, writer, self.name, self.control_token)
        self.peers.append(peer)
        command = {
            "version": 1,
            "type": "control",
            "token": self.control_token,
            "id": request or secrets.token_hex(16),
            "action": action,
            "role": self.name,
            "delivery": None,
            "message": None,
        }
        writer.write(encode_frame(command))
        await writer.drain()
        return peer, command["id"]

    async def roundtrip(self, contract, verdict):
        await self.start_broker(contract)
        assignment = await self.custom_assignment()
        self.assertEqual(self.state(), "active")
        self.assertIsNone(self.row("reviewer")["active_assignment_id"])
        await self.report(self.name, assignment, verdict=verdict)
        review = await self.workers["reviewer"].receive("assignment")
        self.assertEqual(review["kind"], "review")
        self.assertNotEqual(self.state(), "ready")
        await self.report("reviewer", review, verdict="approved")
        await self.wait_for(lambda: self.state() == "ready")
        self.assertFalse((self.coord / "startup.json").exists())
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 3
            )
            retained = "\n".join(db.iterdump())
        for canary in (
            "PRIVATE_TASK_CANARY",
            "PRIVATE_REPORT_CANARY",
            "PRIVATE_GUIDANCE_CANARY",
            "PRIVATE_SKILL_CANARY",
        ):
            self.assertNotIn(canary, retained)

    async def test_probe_roundtrip_requires_independent_review(self):
        await self.roundtrip("probe", None)

    async def test_playwright_failure_is_evidence_not_final_authority(self):
        await self.roundtrip("playwright", "fail")

    async def test_django_findings_are_evidence_not_final_authority(self):
        await self.roundtrip("django", "issues_found")

    async def test_partial_connection_and_invalid_auth_do_not_start_workflow(self):
        await self.start_broker()
        await self.connect("implementer")
        await self.connect(self.name, token=self.tokens["reviewer"], accepted=False)
        await self.connect(self.name, generation=2, accepted=False)
        self.assertEqual(self.state(), "connecting")
        self.assertEqual(self.row()["connected"], 0)
        custom = await self.connect(self.name)
        await self.connect(self.name, accepted=False)
        self.assertEqual(self.row()["connected"], 1)
        self.assertEqual(self.state(), "connecting")
        custom.writer.close()
        await self.wait_for(lambda: self.row()["connected"] == 0)
        self.assertIsNone(self.row("implementer")["active_assignment_id"])

    async def test_accepted_assignment_reconnect_preserves_identity(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        await self.workers[self.name].accept(assignment)
        self.workers[self.name].writer.close()
        await self.wait_for(lambda: self.row()["connected"] == 0)
        peer = await self.connect(self.name)
        restored = await peer.receive("assignment")
        self.assertEqual(restored["id"], assignment["id"])
        self.assertEqual(restored["assignment_id"], assignment["assignment_id"])
        self.assertEqual(restored["kind"], "probe")
        self.workers[self.name] = peer
        await self.report(self.name, restored)
        await self.workers["reviewer"].receive("assignment")

    async def test_unacknowledged_disconnect_becomes_uncertain_not_replayed(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        self.workers[self.name].writer.close()
        await self.wait_for(lambda: self.row()["connected"] == 0)
        await self.connect(self.name)
        await self.wait_for(lambda: self.row()["state"] == "uncertain")
        self.assertEqual(self.state(), "uncertain")
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            state = db.execute(
                "SELECT state FROM assignments WHERE id=?",
                (assignment["assignment_id"],),
            ).fetchone()[0]
            self.assertEqual(state, "uncertain")
        self.assertIsNone(self.row("reviewer")["active_assignment_id"])

    async def test_restart_rejects_old_generation_and_restores_new_delivery(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        await self.workers[self.name].accept(assignment)
        control, request = await self.control("restart")
        self.assertTrue((await control.receive("response", id=request))["success"])
        await self.wait_for(lambda: self.row()["connected"] == 0)
        self.assertEqual(self.row()["generation"], 2)
        await self.connect(self.name, accepted=False)
        peer = await self.connect(self.name, generation=2)
        restored = await peer.receive("assignment")
        self.assertEqual(restored["assignment_id"], assignment["assignment_id"])
        self.assertNotEqual(restored["id"], assignment["id"])
        self.workers[self.name] = peer
        await self.report(self.name, restored)
        await self.workers["reviewer"].receive("assignment")
        retry, _ = await self.control("restart", request=request)
        response = await retry.receive("response", id=request)
        self.assertTrue(response["duplicate"])
        self.assertEqual(self.row()["generation"], 2)

    async def test_interrupted_restart_delivery_stays_uncertain(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        await self.workers[self.name].accept(assignment)
        control, request = await self.control("restart")
        self.assertTrue((await control.receive("response", id=request))["success"])
        await self.wait_for(lambda: self.row()["connected"] == 0)
        peer = await self.connect(self.name, generation=2)
        await peer.receive("assignment")
        self.assertEqual(self.row()["state"], "recovering")
        peer.writer.close()
        await self.wait_for(lambda: self.row()["connected"] == 0)
        self.assertEqual(self.row()["state"], "uncertain")
        self.assertEqual(self.state(), "uncertain")
        await self.connect(self.name, generation=2)
        await self.wait_for(lambda: self.row()["state"] == "uncertain")
        self.assertIsNone(self.row("reviewer")["active_assignment_id"])

    async def test_report_replay_after_reconnect_does_not_account_or_route_twice(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        request = await self.report(self.name, assignment)
        await self.workers["reviewer"].receive("assignment")
        self.assertEqual(self.row()["input_tokens"], 140)
        self.assertEqual(self.row()["provider_calls"], 3)
        self.workers[self.name].writer.close()
        await self.wait_for(lambda: self.row()["connected"] == 0)
        peer = await self.connect(self.name)
        # Lost in-memory evidence does not permit another durable acceptance.
        self.broker.latest_reports.clear()
        self.broker.recent_reports.clear()
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            before = "\n".join(db.iterdump())
        await peer.send(
            "report",
            id=request,
            assignment_id=assignment["assignment_id"],
            report={"kind": "probe", "summary": "PRIVATE_REPORT_CANARY"},
            usage=assignment_usage_snapshot(assignment_input=999),
        )
        response = await peer.receive("response", id=request)
        self.assertTrue(response["success"])
        self.assertEqual(response["status"], "duplicate")
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            self.assertEqual("\n".join(db.iterdump()), before)

    async def test_authenticated_custom_peer_cannot_impersonate_reviewer(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        peer = self.workers[self.name]
        request = await peer.send(
            "report",
            role="reviewer",
            token=self.tokens["reviewer"],
            assignment_id=assignment["assignment_id"],
            report={"kind": "review", "summary": "forged", "verdict": "approved"},
        )
        response = await peer.receive("response")
        self.assertFalse(response["success"])
        self.assertEqual(response["status"], "forbidden")
        # Authentication fails before the untrusted request ID is adopted.
        self.assertNotEqual(response["id"], request)
        await self.wait_for(lambda: self.row()["connected"] == 0)
        self.assertNotEqual(self.state(), "ready")
        self.assertIsNone(self.row("reviewer")["active_assignment_id"])
        with broker_store.connect_broker_database(self.coord, readonly=True) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM reports").fetchone()[0], 1
            )

    async def test_revoked_resource_restart_does_not_disconnect_or_mutate(self):
        await self.start_broker()
        assignment = await self.custom_assignment()
        await self.workers[self.name].accept(assignment)
        before = self.row()
        self.registry.unlink()
        control, _ = await self.control("restart")
        self.assertEqual(await asyncio.wait_for(control.reader.read(), 3), b"")
        self.assertEqual(self.row(), before)
        self.assertEqual(self.state(), "active")
        await self.report(self.name, assignment)
        await self.workers["reviewer"].receive("assignment")
