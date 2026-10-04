"""Model-free recovery contracts: exact runs, receipts, collisions, and transport."""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import broker_client, commands, recovery
from pi_tmux_orchestrator.cli import build_parser
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.tmux import validate_session_name


class RecoveryControlsTests(unittest.TestCase):
    def manifest(self):
        return {
            "version": 3,
            "session": "pi-exact",
            "transport": "tui",
            "roles": {
                "implementer": {
                    "provider": "synthetic",
                    "model": "writer",
                    "thinking": "high",
                    "tools": None,
                    "pane_id": "%1",
                }
            },
        }

    def test_session_bounds_and_id_parser(self):
        for value in ("pi-exact", "-exact", "_.", "a" * 128):
            self.assertEqual(validate_session_name(value), value)
        for value in (
            "",
            "a" * 129,
            "pi:agents",
            ".",
            "..",
            "=pi",
            "pi*",
            "pi exact",
            "pi\n",
            None,
        ):
            with self.assertRaises(OrchestrationError):
                validate_session_name(value)
        for action in ("abort", "restart", "stop"):
            argv = [action, "pi-exact", "--command-id", "a" * 32, "--run", "run-1"]
            if action != "stop":
                argv += ["--role", "implementer"]
            self.assertEqual(build_parser().parse_args(argv).command_id, "a" * 32)

    def test_stop_receipts_are_private_duplicate_safe_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            receipt, duplicate, status = recovery.claim_stop(coord, "a" * 32)
            self.assertFalse(duplicate)
            self.assertEqual(status, "uncertain")
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                recovery.claim_stop(coord, "a" * 32)[1:], (True, "uncertain")
            )
            recovery.complete_stop(receipt)
            self.assertEqual(
                recovery.claim_stop(coord, "a" * 32)[1:], (True, "completed")
            )
            receipt.write_text("private malformed receipt")
            with self.assertRaisesRegex(OrchestrationError, "receipt is unavailable"):
                recovery.claim_stop(coord, "a" * 32)
            receipt.unlink()
            receipt.symlink_to(coord / "absent")
            with self.assertRaises(OrchestrationError):
                recovery.claim_stop(coord, "a" * 32)

    def test_stop_duplicate_after_retention_never_kills_a_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            args = build_parser().parse_args(
                [
                    "stop",
                    "pi-exact",
                    "--run",
                    "run-1",
                    "--command-id",
                    "b" * 32,
                    "--yes",
                ]
            )
            with (
                mock.patch.object(
                    commands,
                    "resolve_supervisor_target",
                    return_value=(coord, self.manifest()),
                ),
                mock.patch.object(
                    commands, "live_stop_target", return_value="$42"
                ) as resolve,
                mock.patch.object(commands, "tmux") as tmux,
                mock.patch.object(
                    commands,
                    "broker_paths",
                    return_value={"socket": coord / "missing.sock"},
                ),
            ):
                first = commands.stop_command(args)
                self.assertEqual(first.data["completion"], "completed")
                resolve.side_effect = AssertionError(
                    "duplicate must not resolve a replacement"
                )
                duplicate = commands.stop_command(args)
                self.assertTrue(duplicate.data["duplicate"])
                tmux.assert_called_once_with(["kill-session", "-t", "$42"])

    def test_stale_run_invalid_role_and_missing_confirmation_do_not_mutate(self):
        manifest = self.manifest()
        with (
            mock.patch.object(
                commands,
                "control_target",
                return_value=("pi-exact", Path("/old-run"), manifest),
            ),
            mock.patch.object(
                commands, "resolve_session", return_value=("pi-exact", Path("/new-run"))
            ),
            mock.patch.object(commands, "tmux") as tmux,
            mock.patch.object(commands, "save_manifest") as save,
        ):
            for action in ("stop", "restart"):
                argv = [action, "pi-exact", "--run", "old-run", "--yes"]
                if action == "restart":
                    argv += ["--role", "implementer", "--skip-model-check"]
                with self.assertRaisesRegex(OrchestrationError, "exact run"):
                    getattr(commands, f"{action}_command")(
                        build_parser().parse_args(argv)
                    )
                argv.remove("--yes")
                with self.assertRaisesRegex(OrchestrationError, "pass --yes"):
                    getattr(commands, f"{action}_command")(
                        build_parser().parse_args(argv)
                    )
            tmux.assert_not_called()
            save.assert_not_called()

    def test_duplicate_restart_ack_never_respawns_or_claims_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            args = build_parser().parse_args(
                [
                    "restart",
                    "pi-exact",
                    "--role",
                    "implementer",
                    "--yes",
                    "--skip-model-check",
                    "--command-id",
                    "c" * 32,
                ]
            )
            with (
                mock.patch.object(
                    commands,
                    "control_target",
                    return_value=("pi-exact", coord, self.manifest()),
                ),
                mock.patch.object(commands, "save_manifest"),
                mock.patch.object(
                    commands,
                    "broker_control_request",
                    return_value={
                        "id": "c" * 32,
                        "status": "accepted",
                        "duplicate": True,
                    },
                ) as control,
                mock.patch.object(commands, "tmux") as tmux,
            ):
                result = commands.restart_command(args)
                self.assertTrue(result.data["acknowledged"])
                self.assertFalse(result.data["restarted"])
                self.assertEqual(result.data["completion"], "uncertain")
                tmux.assert_not_called()
                control.assert_called_once_with(
                    coord, "implementer", "restart", command_id="c" * 32
                )

    def test_collision_only_projects_safe_known_metadata_and_valid_actions(self):
        private = "SYNTHETIC_PRIVATE_CANARY"
        snapshot = {
            "workflow": {
                "state": "active",
                "round": 1,
                "implementation_flow": "phased",
                "prompt": private,
            },
            "roles": [
                {
                    "role": "implementer",
                    "state": "active",
                    "generation": 2,
                    "report": private,
                }
            ],
        }
        with (
            mock.patch.object(
                recovery, "session_option", return_value="/private/run-1"
            ),
            mock.patch.object(recovery, "load_manifest", return_value=self.manifest()),
            mock.patch.object(
                recovery, "public_broker_snapshot", return_value=snapshot
            ),
        ):
            value = recovery.collision_metadata("pi-exact")
            self.assertEqual(value["workflow"]["state"], "active")
            self.assertEqual(
                value["roles"],
                [{"role": "implementer", "state": "active", "generation": 2}],
            )
            self.assertEqual(
                [v["action"] for v in value["next_actions"]],
                ["status", "attach", "stop", "start"],
            )
            self.assertNotIn(private, json.dumps(value))
            self.assertNotIn("/private", json.dumps(value))
            snapshot["workflow"]["state"] = private
            snapshot["roles"][0]["state"] = private
            self.assertNotIn(
                private, json.dumps(recovery.collision_metadata("pi-exact"))
            )
        for option in (None, "/unsafe"):
            with (
                mock.patch.object(recovery, "session_option", return_value=option),
                mock.patch.object(
                    recovery, "load_manifest", side_effect=OrchestrationError(private)
                ),
            ):
                value = recovery.collision_metadata("pi-exact")
                self.assertEqual(
                    [v["action"] for v in value["next_actions"]], ["start"]
                )
                self.assertNotIn(private, json.dumps(value))

    def test_legacy_collision_does_not_offer_unsupported_model_attach(self):
        manifest = self.manifest()
        manifest["version"] = 2
        with (
            mock.patch.object(
                recovery, "session_option", return_value="/private/run-1"
            ),
            mock.patch.object(recovery, "load_manifest", return_value=manifest),
        ):
            value = recovery.collision_metadata("pi-exact")
            self.assertEqual(
                [v["action"] for v in value["next_actions"]],
                ["status", "stop", "start"],
            )
            self.assertIsNone(value["workflow"])

    def test_interrupted_stop_can_reconcile_the_same_id_and_original_run(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            args = build_parser().parse_args(
                [
                    "stop",
                    "pi-exact",
                    "--run",
                    "run-1",
                    "--command-id",
                    "b" * 32,
                    "--yes",
                ]
            )
            with (
                mock.patch.object(
                    commands,
                    "control_target",
                    return_value=("pi-exact", coord, self.manifest()),
                ),
                mock.patch.object(commands, "live_stop_target", return_value="$42"),
                mock.patch.object(
                    commands,
                    "broker_paths",
                    return_value={"socket": coord / "missing.sock"},
                ),
                mock.patch.object(
                    commands,
                    "tmux",
                    side_effect=[OSError("SYNTHETIC_PRIVATE_CANARY"), None],
                ) as tmux,
            ):
                with self.assertRaises(OrchestrationError) as caught:
                    commands.stop_command(args)
                self.assertEqual(caught.exception.code, "broker_uncertain")
                self.assertFalse(caught.exception.data["duplicate"])
                self.assertEqual(caught.exception.data["completion"], "uncertain")
                self.assertEqual(caught.exception.data["retry"], "same_command_id")
                self.assertNotIn("CANARY", str(caught.exception))
                retried = commands.stop_command(args)
                self.assertTrue(retried.data["duplicate"])
                self.assertTrue(retried.data["stop_attempted"])
                self.assertEqual(retried.data["completion"], "completed")
                replay = commands.stop_command(args)
                self.assertFalse(replay.data["stop_attempted"])
                self.assertEqual(
                    tmux.call_args_list, [mock.call(["kill-session", "-t", "$42"])] * 2
                )

    def test_interrupted_stop_after_kill_can_complete_from_verified_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            args = build_parser().parse_args(
                [
                    "stop",
                    "pi-exact",
                    "--run",
                    "run-1",
                    "--command-id",
                    "b" * 32,
                    "--yes",
                ]
            )
            with (
                mock.patch.object(
                    commands,
                    "control_target",
                    return_value=("pi-exact", coord, self.manifest()),
                ),
                mock.patch.object(
                    commands, "live_stop_target", side_effect=["$42", None]
                ),
                mock.patch.object(
                    commands,
                    "broker_paths",
                    return_value={"socket": coord / "missing.sock"},
                ),
                mock.patch.object(commands, "tmux") as tmux,
                mock.patch.object(
                    commands,
                    "complete_stop",
                    side_effect=[OSError("SYNTHETIC_PRIVATE_CANARY"), None],
                ),
            ):
                with self.assertRaises(OrchestrationError):
                    commands.stop_command(args)
                retried = commands.stop_command(args)
                self.assertTrue(retried.data["duplicate"])
                self.assertFalse(retried.data["stop_attempted"])
                self.assertEqual(retried.data["completion"], "completed")
                tmux.assert_called_once_with(["kill-session", "-t", "$42"])

    def test_interrupted_stop_does_not_kill_a_replacement_or_infer_unavailable_absence(
        self,
    ):
        for code in ("broker_not_live", "broker_uncertain"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                coord = Path(directory)
                recovery.claim_stop(coord, "b" * 32)
                args = build_parser().parse_args(
                    [
                        "stop",
                        "pi-exact",
                        "--run",
                        "run-1",
                        "--command-id",
                        "b" * 32,
                        "--yes",
                    ]
                )
                with (
                    mock.patch.object(
                        commands,
                        "control_target",
                        return_value=("pi-exact", coord, self.manifest()),
                    ),
                    mock.patch.object(
                        commands,
                        "live_stop_target",
                        side_effect=OrchestrationError(
                            "SYNTHETIC_PRIVATE_CANARY", code
                        ),
                    ),
                    mock.patch.object(commands, "tmux") as tmux,
                ):
                    with self.assertRaises(OrchestrationError) as caught:
                        commands.stop_command(args)
                    self.assertEqual(caught.exception.code, code)
                    self.assertTrue(caught.exception.data["duplicate"])
                    self.assertEqual(caught.exception.data["completion"], "uncertain")
                    self.assertNotIn("CANARY", str(caught.exception))
                    tmux.assert_not_called()
                    self.assertEqual(
                        recovery.claim_stop(coord, "b" * 32)[2], "uncertain"
                    )

    def test_stop_reconciliation_serializes_without_waiting_and_releases_after_failure(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            with recovery.locked_stop_claim(coord, "b" * 32):
                with self.assertRaises(OrchestrationError) as caught:
                    with recovery.locked_stop_claim(coord, "c" * 32):
                        self.fail("concurrent stop must not acquire claim")
                self.assertEqual(caught.exception.code, "broker_uncertain")
                self.assertFalse(
                    (coord / "stop-receipts" / f"{'c' * 32}.json").exists()
                )
            with (
                self.assertRaises(OSError),
                recovery.locked_stop_claim(coord, "b" * 32),
            ):
                raise OSError("synthetic crash")
            with recovery.locked_stop_claim(coord, "b" * 32) as (_, duplicate, status):
                self.assertTrue(duplicate)
                self.assertEqual(status, "uncertain")

    def test_live_stop_target_binds_immutable_identity_and_requires_successful_observation(
        self,
    ):
        coord = Path("/synthetic/run-1")
        for stdout, expected in (
            ("pi-exact\t$42\t/synthetic/run-1\npi-other\t$43\t/other", "$42"),
            ("pi-other\t$43\t/other", None),
        ):
            with mock.patch.object(
                recovery, "tmux", return_value=mock.Mock(returncode=0, stdout=stdout)
            ):
                self.assertEqual(recovery.live_stop_target("pi-exact", coord), expected)
        for result, code in (
            (
                mock.Mock(returncode=1, stdout="SYNTHETIC_PRIVATE_CANARY"),
                "broker_uncertain",
            ),
            (
                mock.Mock(returncode=0, stdout="pi-exact\t$99\t/synthetic/replacement"),
                "broker_not_live",
            ),
            (
                mock.Mock(returncode=0, stdout="pi-exact\t=pi-exact\t/synthetic/run-1"),
                "broker_not_live",
            ),
            (mock.Mock(returncode=0, stdout="pi-exact"), "broker_not_live"),
        ):
            with mock.patch.object(recovery, "tmux", return_value=result):
                with self.assertRaises(OrchestrationError) as caught:
                    recovery.live_stop_target("pi-exact", coord)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("CANARY", str(caught.exception))

    def test_restart_unavailable_invalid_role_and_override_id_never_respawn(self):
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            with (
                mock.patch.object(
                    commands,
                    "control_target",
                    return_value=("pi-exact", coord, self.manifest()),
                ),
                mock.patch.object(commands, "save_manifest") as save,
                mock.patch.object(commands, "tmux") as tmux,
                mock.patch.object(
                    commands,
                    "broker_control_request",
                    side_effect=OrchestrationError(
                        "Broker unavailable", "broker_not_ready"
                    ),
                ) as control,
            ):
                argv = [
                    "restart",
                    "pi-exact",
                    "--role",
                    "implementer",
                    "--yes",
                    "--skip-model-check",
                    "--command-id",
                    "c" * 32,
                ]
                with self.assertRaises(OrchestrationError) as caught:
                    commands.restart_command(build_parser().parse_args(argv))
                self.assertEqual(caught.exception.code, "broker_not_ready")
                for extra in (["--model", "new-model"], ["--role", "reviewer"]):
                    with self.assertRaises(OrchestrationError):
                        commands.restart_command(
                            build_parser().parse_args(argv + extra)
                        )
                save.assert_not_called()
                tmux.assert_not_called()
                control.assert_called_once()

    def test_stale_broker_abort_run_cannot_send(self):
        args = build_parser().parse_args(
            [
                "abort",
                "pi-exact",
                "--run",
                "old-run",
                "--role",
                "implementer",
                "--command-id",
                "d" * 32,
            ]
        )
        with (
            mock.patch.object(
                commands,
                "control_target",
                return_value=("pi-exact", Path("/old-run"), self.manifest()),
            ),
            mock.patch.object(
                commands, "resolve_session", return_value=("pi-exact", Path("/new-run"))
            ),
            mock.patch.object(commands, "broker_control_request") as control,
        ):
            with self.assertRaises(OrchestrationError) as caught:
                commands.abort_command(args)
            self.assertEqual(caught.exception.code, "broker_not_live")
            control.assert_not_called()

    def test_stored_broker_refusal_preserves_duplicate_and_fresh_id_guidance(self):
        for status in ("uncertain", "conflict"):
            for duplicate in (False, True):
                response = {
                    "version": 1,
                    "type": "response",
                    "id": "b" * 32,
                    "success": False,
                    "status": status,
                    "duplicate": duplicate,
                }
                payload = json.dumps(response).encode()
                stream = mock.MagicMock()
                stream.recv.side_effect = [len(payload).to_bytes(4, "big"), payload]
                with (
                    mock.patch.object(
                        broker_client, "read_regular_file", return_value=b"a" * 32
                    ),
                    mock.patch.object(
                        broker_client,
                        "broker_paths",
                        return_value={"socket": Path("/synthetic/socket")},
                    ),
                    mock.patch.object(
                        broker_client.socket, "socket", return_value=stream
                    ),
                ):
                    with self.assertRaises(OrchestrationError) as caught:
                        broker_client.broker_control_request(
                            Path("/synthetic"),
                            "implementer",
                            "restart",
                            command_id="b" * 32,
                        )
                    self.assertEqual(
                        caught.exception.data,
                        {
                            "command_id": "b" * 32,
                            "command_status": status,
                            "duplicate": duplicate,
                            "completion": "uncertain",
                            "retry": "new_command_id"
                            if status == "uncertain"
                            else "inspect_exact_run",
                        },
                    )
                    self.assertEqual(
                        caught.exception.code,
                        "broker_uncertain"
                        if status == "uncertain"
                        else "broker_rejected",
                    )
                    stream.close.assert_called_once()

    def test_malformed_or_inconsistent_broker_acknowledgements_are_uncertain(self):
        valid = {
            "version": 1,
            "type": "response",
            "id": "b" * 32,
            "success": True,
            "status": "accepted",
            "duplicate": False,
        }
        for response in (
            "SYNTHETIC_PRIVATE_CANARY",
            {**valid, "status": "uncertain"},
            {**valid, "id": "c" * 32},
            {**valid, "duplicate": "no"},
        ):
            payload = json.dumps(response).encode()
            stream = mock.MagicMock()
            stream.recv.side_effect = [len(payload).to_bytes(4, "big"), payload]
            with (
                mock.patch.object(
                    broker_client, "read_regular_file", return_value=b"a" * 32
                ),
                mock.patch.object(
                    broker_client,
                    "broker_paths",
                    return_value={"socket": Path("/synthetic/socket")},
                ),
                mock.patch.object(broker_client.socket, "socket", return_value=stream),
            ):
                with self.assertRaises(OrchestrationError) as caught:
                    broker_client.broker_control_request(
                        Path("/synthetic"),
                        "implementer",
                        "restart",
                        command_id="b" * 32,
                    )
                self.assertEqual(caught.exception.code, "broker_uncertain")
                self.assertNotIn("CANARY", str(caught.exception))

    def test_timeout_after_connection_is_uncertain_before_connection_unavailable(self):
        for connected in (False, True):
            stream = mock.MagicMock()
            if connected:
                stream.recv.side_effect = socket.timeout("SYNTHETIC_PRIVATE_CANARY")
            else:
                stream.connect.side_effect = socket.timeout("SYNTHETIC_PRIVATE_CANARY")
            with (
                mock.patch.object(
                    broker_client, "read_regular_file", return_value=b"a" * 32
                ),
                mock.patch.object(broker_client.socket, "socket", return_value=stream),
                mock.patch.object(
                    broker_client,
                    "broker_paths",
                    return_value={"socket": Path("/synthetic/socket")},
                ),
            ):
                with self.assertRaises(OrchestrationError) as caught:
                    broker_client.broker_control_request(
                        Path("/synthetic"), "implementer", "abort", command_id="b" * 32
                    )
                self.assertEqual(
                    caught.exception.code,
                    "broker_uncertain" if connected else "broker_not_ready",
                )
                self.assertNotIn("CANARY", str(caught.exception))
                stream.close.assert_called_once()
