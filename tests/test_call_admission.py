from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from broker_test_support import BrokerFixture
from pi_tmux_orchestrator import broker_store
from pi_tmux_orchestrator.broker import Broker, Client, initialize_broker_run
from pi_tmux_orchestrator.call_admission import (
    admission_status,
    call_policy,
    extend_call_allowance,
    parse_call_limit,
    retained_call_policy,
    require_admission_runtime,
)
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.protocol import validate_client_message


class CallPolicyTests(unittest.TestCase):
    def test_strict_explicit_policy_and_parser(self):
        self.assertEqual(call_policy(), {"version": 1, "limits": {}})
        self.assertEqual(parse_call_limit("run=12"), ("run", 12))
        for value in [
            "run=0",
            "run=-1",
            "run=1.0",
            "run=true",
            "other=1",
            "run=1000001",
            "role=１",
        ]:
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                parse_call_limit(value)
        for value in [
            {},
            {"version": True, "limits": {}},
            {"version": 1, "limits": {"run": True}},
            {"version": 1, "limits": {"run": 0}},
            {"version": 1, "limits": {"cost": 1}},
            {"version": 1, "limits": {}, "extra": 1},
        ]:
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                call_policy(value)

    def test_provider_turn_frame_is_assignment_bound_and_body_free(self):
        frame = {
            "version": 1,
            "type": "provider_turn",
            "id": "a" * 32,
            "role": "implementer",
            "token": "b" * 32,
            "assignment_id": "c" * 32,
            "resume_id": None,
        }
        self.assertEqual(validate_client_message(frame), frame)
        for updates in [
            {"assignment_id": "bad"},
            {"resume_id": "bad"},
            {"prompt": "private"},
        ]:
            with self.assertRaises(OrchestrationError):
                validate_client_message({**frame, **updates})


class CallStartTests(unittest.TestCase):
    def test_threshold_confirmation_and_static_only_checks_precede_launch(self):
        from pi_tmux_orchestrator.cli import build_parser
        from pi_tmux_orchestrator.start_commands import start_command

        for flags, message in [
            (["--enforce-provider-calls", "run=2"], "confirm-call-enforcement"),
            (["--confirm-call-enforcement"], "requires explicit thresholds"),
            (
                ["--dry-run", "--enforce-provider-calls", "run=2", "--dynamic-plan"],
                "explicit static start",
            ),
            (
                [
                    "--dry-run",
                    "--enforce-provider-calls",
                    "run=2",
                    "--enforce-provider-calls",
                    "run=3",
                ],
                "must not be repeated",
            ),
        ]:
            args = build_parser().parse_args(["start", "--task", "Synthetic", *flags])
            with (
                self.subTest(flags=flags),
                self.assertRaisesRegex(OrchestrationError, message),
            ):
                start_command(args)

    def test_unreviewed_runtime_cannot_enable_enforcement(self):
        for output in ["0.87.1", "1.4.1", "1.4.0\\nprivate"]:
            with mock.patch(
                "pi_tmux_orchestrator.call_admission.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=output),
            ):
                with self.assertRaises(OrchestrationError):
                    require_admission_runtime()
        with mock.patch(
            "pi_tmux_orchestrator.call_admission.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="1.4.0\n"),
        ):
            require_admission_runtime()


class ProviderAdmissionTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        initialize_broker_run(
            self.coord,
            self.manifest,
            "synthetic",
            {},
            call_admission_policy={"version": 1, "limits": {"run": 1}},
        )
        self.broker = Broker(self.coord, self.manifest)
        self.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in ["implementer", "reviewer"]
        }
        self.broker.clients = self.clients
        for name in ["reply", "send_raw", "broadcast", "broadcast_workflow"]:
            setattr(self.broker, name, mock.AsyncMock())
        self.broker.refresh_dashboard = mock.Mock()
        with broker_store.connect_broker_database(self.coord) as database:
            now = broker_store.utc_now()
            for index, role in enumerate(self.clients, 1):
                identity = str(index) * 32
                database.execute(
                    "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) VALUES (?,?,1,?,'accepted',?,?,?)",
                    (
                        identity,
                        role,
                        "implementation" if role == "implementer" else "review",
                        str(index + 2) * 32,
                        now,
                        now,
                    ),
                )
                database.execute(
                    "UPDATE roles SET active_assignment_id=?,state='active' WHERE role=?",
                    (identity, role),
                )
            broker_store.set_meta(database, "workflow_state", "active")

    def message(self, number, role="implementer", **updates):
        return {
            "type": "provider_turn",
            "id": f"{number:032x}",
            "assignment_id": ("1" if role == "implementer" else "2") * 32,
            "resume_id": None,
            **updates,
        }

    def status(self):
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            return admission_status(database)

    async def turn(self, number, role="implementer", **updates):
        await self.broker.handle_message(
            self.clients[role], self.message(number, role, **updates)
        )

    async def test_concurrent_roles_commit_one_shared_reservation_before_reply(self):
        async def assert_committed(*_args, **_kwargs):
            self.assertEqual(self.status()["admitted_turns"], 1)

        self.broker.reply.side_effect = assert_committed
        await asyncio.gather(self.turn(10), self.turn(11, "reviewer"))
        replies = self.broker.reply.call_args_list
        self.assertEqual(sum(reply.args[2] for reply in replies), 1)
        self.assertTrue(self.status()["paused"])
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "ready")
            self.assertEqual(
                database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()[0],
                "needs_attention",
            )
        self.assertEqual(self.status()["cost_source"], "unavailable")

    async def test_replayed_grant_cannot_authorize_second_dispatch_or_reset_on_recovery(
        self,
    ):
        await self.turn(10)
        await self.turn(10)
        self.assertFalse(self.broker.reply.call_args.args[2])
        self.assertEqual(self.status()["admitted_turns"], 1)
        broker_store.prepare_broker_database(self.coord)
        replacement = Broker(self.coord, self.manifest)
        replacement.reply = mock.AsyncMock()
        replacement.broadcast = mock.AsyncMock()
        replacement.broadcast_workflow = mock.AsyncMock()
        replacement.refresh_dashboard = mock.Mock()
        await replacement.handle_message(
            self.clients["reviewer"], self.message(11, "reviewer")
        )
        self.assertFalse(replacement.reply.call_args.args[2])
        self.assertEqual(self.status()["admitted_turns"], 1)

    async def test_role_and_assignment_limits_are_independent_of_other_roles(self):
        for scope in ["role", "assignment"]:
            with self.subTest(scope=scope):
                with broker_store.connect_broker_database(self.coord) as database:
                    database.execute("DELETE FROM provider_admissions")
                    database.execute(
                        "UPDATE meta SET value=? WHERE key='call_admission_policy'",
                        (json.dumps({"version": 1, "limits": {scope: 1}}),),
                    )
                    database.execute(
                        "UPDATE meta SET value='0' WHERE key='call_admission_paused'"
                    )
                    database.execute("UPDATE assignments SET report_attempt='none'")
                await self.turn(10)
                await self.turn(11, "reviewer")
                self.assertEqual(self.status()["admitted_turns"], 2)
                await self.turn(12)
                self.assertTrue(self.status()["paused"])
                self.assertFalse(self.broker.reply.call_args.args[2])

    async def test_stale_generation_assignment_and_resume_cannot_reserve(self):
        for updates in [{"assignment_id": "f" * 32}, {"resume_id": "a" * 32}]:
            with self.assertRaises(OrchestrationError):
                await self.turn(10, **updates)
        self.clients["implementer"].generation = 2
        with self.assertRaises(OrchestrationError):
            await self.turn(11)
        self.assertEqual(self.status()["admitted_turns"], 0)

    async def test_operator_extension_is_bounded_exact_and_idempotent_without_auto_resume(
        self,
    ):
        await self.turn(10)
        await self.turn(11, "reviewer")
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            token = database.execute(
                "SELECT value FROM meta WHERE key='control_token'"
            ).fetchone()[0]
        command = {
            "version": 1,
            "type": "control",
            "id": "e" * 32,
            "token": token,
            "role": "implementer",
            "action": "extend_calls",
            "delivery": "calls:+2",
            "message": None,
        }
        await self.broker.handle_control(mock.Mock(), mock.Mock(), command)
        await self.broker.handle_control(mock.Mock(), mock.Mock(), command)
        self.assertEqual(self.status()["limits"], {"run": 3})
        self.assertFalse(self.status()["paused"])
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()[0],
                "needs_attention",
            )
        with self.assertRaises(OrchestrationError):
            await self.broker.handle_control(
                mock.Mock(), mock.Mock(), {**command, "delivery": "calls:+3"}
            )
        # Neither generic operator guidance nor an old approval clears the exact
        # waiting assignment; explicit assignment-bound resume remains required.
        await self.turn(12, "reviewer")
        self.assertFalse(self.broker.reply.call_args.args[2])
        self.assertEqual(self.status()["admitted_turns"], 1)

    async def test_legacy_migration_does_not_enable_enforcement_and_missing_policy_fails_closed(
        self,
    ):
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
            database.execute(
                "DELETE FROM meta WHERE key IN ('call_admission_policy','call_admission_paused')"
            )
            database.execute("DROP TABLE provider_admissions")
        broker_store.prepare_broker_database(self.coord)
        self.assertEqual(self.status()["mode"], "disabled")
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("DELETE FROM meta WHERE key='call_admission_policy'")
            with self.assertRaises(OrchestrationError):
                retained_call_policy(database)

    async def test_invalid_or_missing_retained_policy_and_pause_fail_closed(self):
        with broker_store.connect_broker_database(self.coord) as database:
            for raw in [
                "null",
                '{"version":1,"limits":{"run":1,"run":2}}',
                '{"version":true,"limits":{}}',
            ]:
                database.execute(
                    "UPDATE meta SET value=? WHERE key='call_admission_policy'", (raw,)
                )
                with self.assertRaises(OrchestrationError):
                    retained_call_policy(database)
            database.execute(
                "UPDATE meta SET value=? WHERE key='call_admission_policy'",
                (json.dumps({"version": 1, "limits": {"run": 1}}),),
            )
            database.execute("DELETE FROM meta WHERE key='call_admission_paused'")
            with self.assertRaises(OrchestrationError):
                admission_status(database)

    async def test_ceiling_extension_cannot_be_inferred_or_overflow(self):
        with broker_store.connect_broker_database(self.coord) as database:
            with self.assertRaises(OrchestrationError):
                extend_call_allowance(database, 2)
        await self.turn(10)
        await self.turn(11)
        with broker_store.connect_broker_database(self.coord) as database:
            for value in [True, 0, 1_000_000, "2"]:
                with self.assertRaises(OrchestrationError):
                    extend_call_allowance(database, value)
        self.assertEqual(self.status()["limits"], {"run": 1})
