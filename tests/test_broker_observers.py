from __future__ import annotations

import asyncio
import json
import secrets
import unittest
from unittest import mock

from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import (
    Broker,
    Client,
    initialize_broker_run,
)
from pi_tmux_orchestrator.budgeting import packaged_budget_policy
from pi_tmux_orchestrator.constants import (
    BROKER_PROTOCOL_VERSION,
)
from pi_tmux_orchestrator.protocol import (
    encode_frame,
    validate_report,
)


from broker_test_support import BrokerFixture, assignment_usage_snapshot


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
