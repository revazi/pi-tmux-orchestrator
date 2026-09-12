from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR
from json_cli_support import JsonCliFixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "pi-tmux-agents"


class JsonSessionCommandTests(JsonCliFixture):
    def test_running_orchestration_dashboard_summary_is_bounded_metadata(self) -> None:
        snapshot = {
            "workflow": {
                "state": "active",
                "round": 2,
                "implementation_flow": "phased",
            },
            "usage": {"provider_calls": 9, "total_tokens": 12345},
            "roles": [
                {
                    "connected": True,
                    "cost_total": 0.25,
                    "context_percent": 42.5,
                },
                {
                    "connected": False,
                    "cost_total": 0.1,
                    "context_percent": None,
                },
            ],
        }
        with mock.patch.object(
            ORCHESTRATOR, "public_broker_snapshot", return_value=snapshot
        ):
            summary = ORCHESTRATOR.orchestration_dashboard_summary(
                Path("/private/run"), {"version": 5}
            )
        self.assertEqual(
            summary,
            {
                "available": True,
                "workflow": {
                    "state": "active",
                    "round": 2,
                    "implementation_flow": "phased",
                },
                "usage": {
                    "provider_calls": 9,
                    "operational_tokens": 12345,
                    "cost_total": 0.35,
                    "context_percent": 42.5,
                    "actual_provider_usage_only": True,
                },
                "roles": {"total": 2, "connected": 1},
            },
        )
        snapshot["roles"][1]["cost_total"] = None
        with mock.patch.object(
            ORCHESTRATOR, "public_broker_snapshot", return_value=snapshot
        ):
            unavailable_cost = ORCHESTRATOR.orchestration_dashboard_summary(
                Path("/private/run"), {"version": 5}
            )
        self.assertIsNone(unavailable_cost["usage"]["cost_total"])
        self.assertEqual(
            ORCHESTRATOR.orchestration_dashboard_summary(
                Path("/private/run"), {"version": 2}
            ),
            {"available": False, "reason": "legacy-run"},
        )
        with mock.patch.object(
            ORCHESTRATOR,
            "public_broker_snapshot",
            side_effect=RuntimeError("PRIVATE_DASHBOARD_FAILURE_CANARY"),
        ):
            self.assertEqual(
                ORCHESTRATOR.orchestration_dashboard_summary(
                    Path("/private/run"), {"version": 5}
                ),
                {"available": False, "reason": "temporarily-unavailable"},
            )

    def test_list_status_send_restart_and_stop_return_structured_metadata(self) -> None:
        manifest = {
            "project": str(ROOT),
            "window": ORCHESTRATOR.WINDOW,
            "roles": {
                "implementer": {
                    "provider": "provider",
                    "model": "writer",
                    "thinking": "high",
                    "tools": None,
                    "pane_id": "%1",
                },
                "reviewer": {
                    "provider": "provider",
                    "model": "reviewer",
                    "thinking": "high",
                    "tools": ORCHESTRATOR.READ_ONLY_TOOLS,
                    "pane_id": "%2",
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            message_file = coord / "message.txt"
            message_file.write_text(
                "PRIVATE_MESSAGE_CANARY_JSON_7de1", encoding="utf-8"
            )

            with (
                mock.patch.object(
                    ORCHESTRATOR,
                    "orchestrated_sessions",
                    return_value=[("pi-test", coord)],
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
            ):
                code, envelope, _, stderr = self.run_main(["--json", "list"])
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assert_envelope(envelope, "list", True)
            self.assertIsInstance(envelope["data"]["sessions"][0]["roles"], list)
            self.assertEqual(
                envelope["data"]["sessions"][0]["roles"][1]["tool_policy"],
                "workflow-read-only-with-bash",
            )

            pane_output = "0\t%1\t123\tpython3\t0\tIMPLEMENTER\n1\t%2\t124\tpython3\t0\tREVIEWER\n"
            tmux_result = subprocess.CompletedProcess([], 0, pane_output, "")
            with (
                mock.patch.object(
                    ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "tmux", return_value=tmux_result),
                mock.patch.object(ORCHESTRATOR, "coordination_files", return_value=[]),
            ):
                code, envelope, _, _ = self.run_main(["--json", "status", "pi-test"])
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "status", True)
            self.assertEqual(envelope["data"]["panes"][0]["id"], "%1")
            self.assertEqual(envelope["data"]["files"], [])

            with (
                mock.patch.object(
                    ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "send_keys") as send_keys,
            ):
                code, envelope, raw, _ = self.run_main(
                    [
                        "--json",
                        "send",
                        "pi-test",
                        "--role",
                        "reviewer",
                        "--message-file",
                        str(message_file),
                    ]
                )
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "send", True)
            self.assertNotIn("PRIVATE_MESSAGE_CANARY_JSON_7de1", raw)
            send_keys.assert_called_once()

            with (
                mock.patch.object(
                    ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "save_manifest"),
                mock.patch.object(ORCHESTRATOR, "tmux"),
            ):
                code, envelope, _, _ = self.run_main(
                    [
                        "--json",
                        "restart",
                        "pi-test",
                        "--role",
                        "reviewer",
                        "--yes",
                        "--skip-model-check",
                    ]
                )
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "restart", True)
            self.assertTrue(envelope["data"]["restarted"])

            with (
                mock.patch.object(
                    ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "tmux"),
            ):
                code, envelope, _, _ = self.run_main(
                    ["--json", "stop", "pi-test", "--yes"]
                )
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "stop", True)
            self.assertTrue(envelope["data"]["state_retained"])

    def test_broker_restart_uses_authoritative_generation_handover(self) -> None:
        manifest = {
            "version": 3,
            "project": str(ROOT),
            "window": ORCHESTRATOR.WINDOW,
            "transport": "tui",
            "roles": {
                "implementer": {
                    "provider": "provider",
                    "model": "writer",
                    "thinking": "high",
                    "tools": None,
                    "pane_id": "%1",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            with (
                mock.patch.object(
                    ORCHESTRATOR,
                    "resolve_session",
                    return_value=("pi-test", coord),
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "save_manifest"),
                mock.patch.object(
                    ORCHESTRATOR,
                    "broker_control_request",
                    return_value={"status": "accepted"},
                ) as broker_control,
                mock.patch.object(ORCHESTRATOR, "tmux") as tmux,
            ):
                code, envelope, _, _ = self.run_main(
                    [
                        "--json",
                        "restart",
                        "pi-test",
                        "--role",
                        "implementer",
                        "--yes",
                        "--skip-model-check",
                    ]
                )
        self.assertEqual(code, 0)
        self.assert_envelope(envelope, "restart", True)
        broker_control.assert_called_once_with(coord, "implementer", "restart")
        tmux.assert_called_once()

    def test_failed_broker_restart_respawn_fails_handover_closed(self) -> None:
        manifest = {
            "version": 3,
            "project": str(ROOT),
            "window": ORCHESTRATOR.WINDOW,
            "transport": "tui",
            "roles": {
                "implementer": {
                    "provider": "provider",
                    "model": "writer",
                    "thinking": "high",
                    "tools": None,
                    "pane_id": "%1",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            with (
                mock.patch.object(
                    ORCHESTRATOR,
                    "resolve_session",
                    return_value=("pi-test", coord),
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "save_manifest"),
                mock.patch.object(
                    ORCHESTRATOR,
                    "broker_control_request",
                    side_effect=[
                        {"status": "accepted"},
                        {"status": "accepted"},
                    ],
                ) as broker_control,
                mock.patch.object(
                    ORCHESTRATOR,
                    "tmux",
                    side_effect=subprocess.CalledProcessError(1, ["tmux"]),
                ),
            ):
                code, envelope, _, _ = self.run_main(
                    [
                        "--json",
                        "restart",
                        "pi-test",
                        "--role",
                        "implementer",
                        "--yes",
                        "--skip-model-check",
                    ]
                )
        self.assertEqual(code, 1)
        self.assert_envelope(envelope, "restart", False)
        self.assertEqual(
            broker_control.call_args_list,
            [
                mock.call(coord, "implementer", "restart"),
                mock.call(coord, "implementer", "restart_failed"),
            ],
        )

    def test_broker_status_exposes_parent_observer_endpoint(self) -> None:
        manifest = {
            "version": 3,
            "project": str(ROOT),
            "window": ORCHESTRATOR.WINDOW,
            "transport": ORCHESTRATOR.TUI_TRANSPORT,
            "coordination": ORCHESTRATOR.BROKER_COORDINATION,
            "roles": {},
        }
        broker_snapshot = {
            "workflow": {
                "state": "ready",
                "round": 2,
                "worker_context_policy": {"version": 1, "overrides": {}},
            },
            "usage": {
                "total_tokens": 0,
                "soft_total_budget_exceeded": False,
            },
            "roles": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            tmux_result = subprocess.CompletedProcess([], 0, "", "")
            expected_socket = coord / "broker.sock"
            with (
                mock.patch.object(
                    ORCHESTRATOR,
                    "resolve_session",
                    return_value=("pi-test", coord),
                ),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "tmux", return_value=tmux_result),
                mock.patch.object(
                    ORCHESTRATOR,
                    "public_broker_snapshot",
                    return_value=broker_snapshot,
                ),
                mock.patch.object(ORCHESTRATOR, "status_roles", return_value=[]),
                mock.patch.object(
                    ORCHESTRATOR,
                    "broker_paths",
                    return_value={"socket": expected_socket},
                ),
            ):
                code, envelope, raw, stderr = self.run_main(
                    ["--json", "status", "pi-test"]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "status", True)
        self.assertEqual(
            envelope["data"]["paths"]["observer_socket"],
            str(expected_socket),
        )
        self.assertNotIn("control_token", raw)
        self.assertNotIn("auth_token", raw)

    def test_json_attach_switches_the_current_tmux_client(self) -> None:
        manifest = {
            "version": 3,
            "project": str(ROOT),
            "transport": ORCHESTRATOR.TUI_TRANSPORT,
            "roles": {},
        }
        with (
            mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux"}),
            mock.patch.object(
                ORCHESTRATOR,
                "resolve_session",
                return_value=("pi-test", ROOT),
            ),
            mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
            mock.patch.object(ORCHESTRATOR, "attach_session") as attach_session,
            mock.patch.object(ORCHESTRATOR, "tmux") as tmux,
        ):
            code, envelope, raw, stderr = self.run_main(["--json", "attach", "pi-test"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "attach", True)
        self.assertEqual(envelope["data"]["mode"], "switch-client")
        self.assertEqual(envelope["data"]["transport"], "tui")
        self.assertIn("prefix", envelope["data"]["return_hint"])
        self.assertNotIn(str(ROOT / "control.token"), raw)
        attach_session.assert_called_once_with("pi-test")
        tmux.assert_called_once_with(
            [
                "display-message",
                "-d",
                "5000",
                "Attached to pi-test · prefix then L detaches back without stopping workers",
            ],
            check=False,
        )

    def test_rpc_send_and_abort_return_acknowledged_metadata(self) -> None:
        manifest = {
            "version": 2,
            "transport": ORCHESTRATOR.RPC_TRANSPORT,
            "roles": {"implementer": {"pane_id": "%1"}},
        }
        coord = ROOT
        with (
            mock.patch.object(
                ORCHESTRATOR,
                "resolve_session",
                return_value=("pi-rpc", coord),
            ),
            mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
            mock.patch.object(
                ORCHESTRATOR,
                "rpc_control_request",
                side_effect=[
                    {
                        "version": 2,
                        "id": "1" * 32,
                        "command": "prompt",
                        "success": True,
                        "status": "accepted",
                        "duplicate": False,
                        "event_sequence": 4,
                    },
                    {
                        "version": 2,
                        "id": "2" * 32,
                        "command": "abort",
                        "success": True,
                        "status": "completed",
                        "duplicate": False,
                        "event_sequence": 6,
                    },
                ],
            ) as rpc_request,
        ):
            code, envelope, raw, stderr = self.run_main(
                [
                    "--json",
                    "send",
                    "pi-rpc",
                    "--role",
                    "implementer",
                    "--message",
                    "PRIVATE_RPC_JSON_MESSAGE",
                    "--delivery",
                    "follow-up",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertNotIn("PRIVATE_RPC_JSON_MESSAGE", raw)
            self.assert_envelope(envelope, "send", True)
            self.assertTrue(envelope["data"]["acknowledged"])
            self.assertEqual(envelope["data"]["delivery"], "follow-up")

            code, envelope, _, stderr = self.run_main(
                ["--json", "abort", "pi-rpc", "--role", "implementer"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assert_envelope(envelope, "abort", True)
            self.assertTrue(envelope["data"]["acknowledged"])
        self.assertEqual(rpc_request.call_count, 2)

    def test_events_api_reads_retained_state_with_a_stable_cursor(self) -> None:
        canary = "PRIVATE_EVENT_API_CANARY_d10c"
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            project = Path(directory) / "project"
            project.mkdir()
            with mock.patch.object(ORCHESTRATOR, "STATE_ROOT", state_root):
                root = ORCHESTRATOR.canonical_state_root(create=True)
                session = "pi-events-api"
                session_root = ORCHESTRATOR.ensure_private_directory(root / session)
                coord = ORCHESTRATOR.ensure_private_directory(session_root / "run-1")
                prompt = coord / "implementer.prompt.md"
                ORCHESTRATOR.secure_write(prompt, f"Synthetic prompt {canary}.\n")
                session_dir = ORCHESTRATOR.ensure_private_directory(
                    coord / "sessions" / "implementer",
                    parents=True,
                )
                ORCHESTRATOR.secure_write(coord / "reviewer.prompt.md", "Review.\n")
                reviewer_dir = ORCHESTRATOR.ensure_private_directory(
                    coord / "sessions" / "reviewer"
                )
                manifest = {
                    "version": 2,
                    "created_at": "2026-08-09T12:00:00+00:00",
                    "session": session,
                    "window": ORCHESTRATOR.WINDOW,
                    "project": str(project.resolve()),
                    "coord": str(coord),
                    "approve_project": False,
                    "transport": ORCHESTRATOR.RPC_TRANSPORT,
                    "monitor_pane_id": "%9",
                    "roles": {
                        "implementer": {
                            "provider": "provider",
                            "model": "model",
                            "thinking": "high",
                            "tools": None,
                            "pane_id": "%1",
                            "prompt_path": str(prompt),
                            "session_dir": str(session_dir),
                        },
                        "reviewer": {
                            "provider": "provider",
                            "model": "model",
                            "thinking": "high",
                            "tools": ORCHESTRATOR.READ_ONLY_TOOLS,
                            "pane_id": "%2",
                            "prompt_path": str(coord / "reviewer.prompt.md"),
                            "session_dir": str(reviewer_dir),
                        },
                    },
                }
                ORCHESTRATOR.save_manifest(coord, manifest)
                paths = ORCHESTRATOR.rpc_role_paths(coord, "implementer", create=True)
                registry = ORCHESTRATOR.initialize_rpc_registry(
                    coord,
                    paths,
                    "implementer",
                    456,
                )
                ORCHESTRATOR.record_rpc_event(
                    paths,
                    registry,
                    "implementer",
                    "command_received",
                    command_id="c" * 32,
                    command="prompt",
                    delivery="steer",
                )
                code, envelope, raw, stderr = self.run_main(
                    [
                        "--json",
                        "events",
                        session,
                        "--role",
                        "implementer",
                        "--run",
                        "run-1",
                        "--after",
                        "0",
                        "--limit",
                        "1",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "events", True)
        self.assertNotIn(canary, raw)
        self.assertEqual(len(envelope["data"]["events"]), 1)
        self.assertTrue(envelope["data"]["cursor"]["truncated"])
        self.assertEqual(envelope["data"]["cursor"]["next"], 1)
        self.assertEqual(
            envelope["data"]["registry"]["worker_id"], registry["worker_id"]
        )
