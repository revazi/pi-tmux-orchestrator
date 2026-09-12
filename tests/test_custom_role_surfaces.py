"""Retained custom control/presentation checks, not custom Pi lifecycle acceptance."""

from __future__ import annotations

import copy
import json
import secrets
import unittest
from pathlib import Path
from types import MethodType
from unittest import mock

from pi_tmux_orchestrator import broker_store, commands, supervisor_api
from pi_tmux_orchestrator.broker import Broker
from pi_tmux_orchestrator.broker_control import BrokerControlSupport
from pi_tmux_orchestrator.broker_observers import BrokerObserverSupport
from pi_tmux_orchestrator.cli import build_parser, supervisor_cursor
from pi_tmux_orchestrator.custom_role_resources import retained_custom_contracts
from pi_tmux_orchestrator.dashboard import render_dashboard
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.storage import save_manifest
from test_broker_workflow import WorkflowHarness
from test_custom_role_resources import CustomRoleResourceFixture


class CustomRoleSurfaceTests(
    CustomRoleResourceFixture, unittest.IsolatedAsyncioTestCase
):
    def initialize(self):
        broker_store.initialize_broker_database(
            self.coord,
            self.manifest,
            {role: "a" * 32 for role in self.manifest["roles"]},
            "b" * 32,
            soft_role_tokens=0,
            soft_total_tokens=0,
        )

    def test_public_control_parser_accepts_syntax_without_enabling_roles(self):
        self.enterContext(mock.patch("pi_tmux_orchestrator.runtime.JSON_MODE", True))
        parser = build_parser()
        prefixes = [
            ["send", "pi-test"],
            ["abort", "pi-test"],
            ["restart", "pi-test"],
            ["events", "pi-test"],
            ["supervisor", "events", "pi-test"],
            ["supervisor", "command", "pi-test", "--command-id", "c" * 32],
        ]
        for prefix in prefixes:
            for role in (self.name, "custom-unselected", "reviewer"):
                parsed = parser.parse_args([*prefix, "--role", role])
                self.assertIn(
                    role,
                    parsed.role if isinstance(parsed.role, list) else [parsed.role],
                )
            for role in (
                "custom-reviewer",
                "custom-a:1",
                "custom-a.b",
                "custom-a--b",
                "custom-" + "x" * 26,
                "parent",
                "--all",
            ):
                with (
                    self.subTest(prefix=prefix, role=role),
                    self.assertRaises(OrchestrationError),
                ):
                    parser.parse_args([*prefix, "--role", role])
        self.assertEqual(supervisor_cursor(f"{self.name}=7"), (self.name, 7))
        self.assertEqual(
            supervisor_api.supervisor_cursor_arguments([(self.name, 7)]), {self.name: 7}
        )
        with self.assertRaises(OrchestrationError):
            supervisor_api.supervisor_cursor_arguments([("custom-reviewer", 0)])
        with self.assertRaises(OrchestrationError):
            parser.parse_args(["start", "--task", "test", "--custom-role", self.name])

    def test_retained_status_supervisor_and_events_need_no_live_resources_or_tmux(self):
        self.initialize()
        with broker_store.connect_broker_database(self.coord) as database:
            for _ in range(3):
                broker_store.record_event(
                    database, "worker_lifecycle", role=self.name, status="recovering"
                )
            database.execute(
                "UPDATE roles SET state='recovering' WHERE role=?", (self.name,)
            )
        self.registry.unlink()
        Path(self.prompt["path"]).unlink()
        with (
            mock.patch.object(commands, "tmux", side_effect=AssertionError("no tmux")),
            mock.patch(
                "pi_tmux_orchestrator.custom_role_resources.load_registry",
                side_effect=AssertionError("no registry"),
            ),
        ):
            roles = commands.status_roles(self.coord, self.manifest)
            snapshot = supervisor_api.supervisor_snapshot(
                self.manifest["session"], self.coord.name
            )
            usage = supervisor_api.supervisor_usage(
                self.manifest["session"], self.coord.name, limit=1
            )
            events = supervisor_api.supervisor_event_batch(
                self.manifest["session"],
                self.coord.name,
                requested_roles=[self.name],
                cursors={self.name: 0},
                limit=1,
            )
        custom = next(role for role in roles if role["name"] == self.name)
        self.assertEqual(custom["specialist_contract"], "probe")
        self.assertEqual(custom["resource_verification"], "not_checked")
        self.assertEqual(custom["tool_policy"], "custom-read-only-no-shell")
        self.assertEqual(custom["selection_source"], "unavailable")
        self.assertEqual(custom["thinking_source"], "unavailable")
        self.assertEqual(custom["activation_source"], "legacy-always-run")
        self.assertEqual(custom["activation_state"], "enabled")
        retained = next(role for role in snapshot["roles"] if role["name"] == self.name)
        self.assertEqual(retained["runtime"]["liveness"], "not-observed")
        self.assertEqual(retained["runtime"]["state"]["state"], "recovering")
        self.assertEqual(len(events["roles"]), 1)
        self.assertEqual(len(events["roles"][0]["events"]), 1)
        self.assertTrue(events["roles"][0]["cursor"]["truncated"])
        serialized = json.dumps([roles, snapshot, usage, events])
        for excluded in (
            "PRIVATE_",
            str(self.registry),
            self.prompt["path"],
            self.skill["path"],
            "custom_role",
        ):
            self.assertNotIn(excluded, serialized)
        with self.assertRaises(OrchestrationError):
            supervisor_api.supervisor_event_batch(
                self.manifest["session"],
                self.coord.name,
                requested_roles=["custom-unselected"],
                cursors={},
                limit=1,
            )

    def test_cli_send_abort_and_restart_preserve_exact_selected_identity(self):
        parser = build_parser()
        for transport in ("tui", "rpc"):
            self.manifest["transport"] = transport
            save_manifest(self.coord, self.manifest)
            with (
                mock.patch.object(
                    commands,
                    "resolve_session",
                    return_value=(self.manifest["session"], self.coord),
                ),
                mock.patch.object(
                    commands,
                    "broker_control_request",
                    return_value={
                        "id": "c" * 32,
                        "status": "accepted",
                        "duplicate": False,
                    },
                ) as control,
                mock.patch.object(commands, "tmux") as tmux,
                mock.patch.object(
                    commands,
                    "revalidate_worker_resources",
                    wraps=commands.revalidate_worker_resources,
                ) as verify,
            ):
                for action in ("send", "abort", "restart"):
                    argv = [action, self.manifest["session"], "--role", self.name]
                    if action == "send":
                        argv += ["--message", "PRIVATE_OPERATOR_CANARY"]
                    if action == "restart":
                        with self.assertRaisesRegex(OrchestrationError, "pass --yes"):
                            commands.restart_command(parser.parse_args(argv))
                        argv += ["--yes", "--skip-model-check"]
                    parsed = parser.parse_args(argv)
                    result = parsed.handler(parsed)
                    self.assertEqual(
                        control.call_args.args[:3], (self.coord, self.name, action)
                    )
                    self.assertNotIn("PRIVATE_", json.dumps(result.data))
                verify.assert_called_once_with(self.manifest, self.name)
                argv = tmux.call_args.args[0]
                self.assertEqual(argv[:4], ["respawn-pane", "-k", "-t", "%4"])
                self.assertIn(f"--role {self.name}", argv[4])
                self.assertNotIn("PRIVATE_", argv[4])
                control.reset_mock()
                with self.assertRaisesRegex(OrchestrationError, "not in"):
                    commands.send_command(
                        parser.parse_args(
                            [
                                "send",
                                self.manifest["session"],
                                "--role",
                                "custom-unselected",
                                "--message",
                                "private",
                            ]
                        )
                    )
                control.assert_not_called()

    def control_harness(self):
        self.initialize()
        harness = WorkflowHarness(self.coord, self.manifest)
        harness.worker_baselines = {self.name: "PRIVATE_BASELINE_CANARY"}
        harness.send = mock.AsyncMock()
        harness.send_raw = mock.AsyncMock()
        harness.refresh_dashboard = mock.Mock()
        harness.clients[self.name].writer = mock.Mock()
        for name in (
            "current_round",
            "_mark_handover_uncertain",
            "_handle_operator_send",
        ):
            setattr(harness, name, MethodType(getattr(Broker, name), harness))
        return harness

    def control_message(self, action="send", **changes):
        return {
            "version": 1,
            "type": "control",
            "token": "b" * 32,
            "id": secrets.token_hex(16),
            "action": action,
            "role": self.name,
            "delivery": "steer" if action == "send" else None,
            "message": "PRIVATE_OPERATOR_CANARY" if action == "send" else None,
            **changes,
        }

    async def test_custom_operator_control_is_authenticated_idempotent_and_body_free(
        self,
    ):
        harness = self.control_harness()
        harness.create_assignment(self.name, "probe")
        message = self.control_message()
        for changes in (
            {"token": "a" * 32},
            {"role": "custom-unselected"},
            {"role": []},
            {"contract": "reviewer"},
        ):
            with self.subTest(changes=changes), self.assertRaises(OrchestrationError):
                await BrokerControlSupport.handle_control(
                    harness, mock.Mock(), mock.Mock(), {**message, **changes}
                )
        with self.assertRaises(OrchestrationError):
            await BrokerControlSupport.handle_control(
                harness, mock.Mock(), mock.Mock(), self.control_message("continue")
            )
        harness.deliver.assert_not_awaited()
        for _ in range(2):
            await BrokerControlSupport.handle_control(
                harness, mock.Mock(), mock.Mock(), message
            )
        harness.deliver.assert_awaited_once()
        self.assertEqual(harness.deliver.await_args.args[0], self.name)
        self.assertTrue(harness.send_raw.await_args.args[1]["duplicate"])
        command = supervisor_api.supervisor_command_status(
            self.manifest["session"],
            self.coord.name,
            role=self.name,
            command_id=message["id"],
        )
        self.assertEqual(command["command"]["status"], "accepted")
        self.assertNotIn("PRIVATE_", json.dumps(command))
        with self.assertRaises(OrchestrationError):
            supervisor_api.supervisor_command_status(
                self.manifest["session"],
                self.coord.name,
                role="reviewer",
                command_id=message["id"],
            )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0],
                1,
            )
            self.assertNotIn("PRIVATE_", "\n".join(database.iterdump()))
        harness.assign.assert_not_awaited()
        harness.broadcast_workflow.assert_not_awaited()

    async def test_broker_restart_revalidates_before_generation_handover_and_failed_recovery(
        self,
    ):
        harness = self.control_harness()
        request = self.control_message("restart")
        before = (self.coord / "manifest.json").read_bytes()
        original = Path(self.prompt["path"]).read_bytes()
        Path(self.prompt["path"]).write_text("REVOKED_GUIDANCE")
        with self.assertRaises(OrchestrationError):
            await BrokerControlSupport.handle_control(
                harness, mock.Mock(), mock.Mock(), request
            )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT generation FROM roles WHERE role=?", (self.name,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0],
                0,
            )
        harness.clients[self.name].writer.close.assert_not_called()
        self.assertEqual((self.coord / "manifest.json").read_bytes(), before)
        Path(self.prompt["path"]).write_bytes(original)
        await BrokerControlSupport.handle_control(
            harness, mock.Mock(), mock.Mock(), request
        )
        self.registry.unlink()
        await BrokerControlSupport.handle_control(
            harness, mock.Mock(), mock.Mock(), request
        )
        harness.clients[self.name].writer.close.assert_called_once()
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT generation FROM roles WHERE role=?", (self.name,)
                ).fetchone()[0],
                2,
            )
        with self.assertRaises(OrchestrationError):
            await BrokerControlSupport.handle_control(
                harness, mock.Mock(), mock.Mock(), self.control_message("restart")
            )
        await BrokerControlSupport.handle_control(
            harness, mock.Mock(), mock.Mock(), self.control_message("restart_failed")
        )
        harness.broadcast_workflow.assert_awaited_once_with("uncertain", 1)
        self.assertEqual(
            broker_store.public_broker_snapshot(self.coord)["workflow"]["state"],
            "uncertain",
        )

    def test_maximum_custom_dashboard_and_observer_snapshot_are_bounded_metadata(self):
        original = self.manifest["roles"].pop(self.name)
        for index in range(8):
            name = f"custom-security-inspection-xxx-{index}"
            role = copy.deepcopy(original)
            role.update(
                pane_id=f"%{index + 7}",
                session_id=name,
                session_dir=str(self.coord / "sessions" / name),
            )
            role["custom_role"]["id"] = name
            self.manifest["roles"][name] = role
        self.enable_specialists("probe", "playwright", "django")
        retained_custom_contracts(self.manifest, self.coord)
        self.initialize()
        snapshot = broker_store.public_broker_snapshot(self.coord)
        observer = BrokerObserverSupport.observer_snapshot(
            mock.Mock(
                coord=self.coord,
                manifest=self.manifest,
                recent_reports=[],
            )
        )
        self.assertEqual(len(observer["roles"]), 13)
        self.assertLess(len(json.dumps(observer)), 2048)
        self.assertNotIn("PRIVATE_", json.dumps(observer))
        for width, height in ((180, 40), (100, 24), (80, 18), (40, 8), (20, 3)):
            rendered = render_dashboard(
                self.manifest, snapshot, [], width=width, height=height, color=False
            )
            self.assertLessEqual(len(rendered.splitlines()), height)
            self.assertTrue(
                all(len(line) <= width - 1 for line in rendered.splitlines())
            )
            self.assertNotIn("PRIVATE_", rendered)
            if height >= 24:
                for name in self.manifest["roles"]:
                    self.assertIn(name, rendered)
                self.assertIn("[read-only]", rendered)
            elif height == 8:
                self.assertIn("roles", rendered)
