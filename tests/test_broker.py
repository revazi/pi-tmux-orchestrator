from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import broker_store, runtime
from pi_tmux_orchestrator.broker import (
    Broker,
    Client,
    Observer,
    _register_broker_signal_handlers,
    initialize_broker_run,
)
from pi_tmux_orchestrator.constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    MAX_RUN_STATE_BYTES,
    MAX_WORKER_DELIVERY_CHARS,
    READ_ONLY_TOOLS,
    WINDOW,
)
from pi_tmux_orchestrator.context_capsules import (
    render_run_state_capsule,
    render_worker_baseline,
)
from pi_tmux_orchestrator.protocol import decode_frame, encode_frame, validate_report
from pi_tmux_orchestrator.storage import ensure_private_directory, save_manifest


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


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip_is_strict_and_bounded(self) -> None:
        value = {"version": 1, "type": "response", "success": True}
        encoded = encode_frame(value)
        self.assertEqual(int.from_bytes(encoded[:4], "big"), len(encoded) - 4)
        self.assertEqual(decode_frame(encoded[4:]), value)

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
        mode = os.stat(self.coord / "broker.sqlite3").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_snapshot_reports_actual_usage_fields_and_workflow_state(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        socket_path = broker_store.broker_paths(self.coord)["socket"]
        self.assertLess(len(os.fsencode(socket_path)), 100)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "starting")
        self.assertEqual(snapshot["usage"]["total_tokens"], 0)
        self.assertTrue(snapshot["usage"]["actual_provider_usage_only"])
        self.assertFalse(snapshot["usage"]["soft_total_budget_exceeded"])
        self.assertEqual({role["total_tokens"] for role in snapshot["roles"]}, {0})
        self.assertEqual(
            {role["state"] for role in snapshot["roles"]}, {"disconnected"}
        )
        self.assertEqual({role["assignment"] for role in snapshot["roles"]}, {None})
        self.assertFalse(
            any("active_assignment_id" in role for role in snapshot["roles"])
        )

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

    async def test_changes_requested_delivers_rolling_state_before_round_two(
        self,
    ) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
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
            self.assertIn("End your turn", prompt)
            self.assertIn("Never run sleep commands", prompt)
            self.assertIn("orchestrator_report", prompt)
            self.assertIn("Keep provider context efficient", prompt)
            self.assertIn("Avoid rereading unchanged files", prompt)
            self.assertNotIn("handoff-N", prompt)
