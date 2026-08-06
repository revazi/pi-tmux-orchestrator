from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-tmux-agents.py"
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("pi_tmux_orchestrator_json", SCRIPT)
assert SPEC and SPEC.loader
ORCHESTRATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ORCHESTRATOR)


class JsonMainTests(unittest.TestCase):
    def tearDown(self) -> None:
        ORCHESTRATOR.JSON_MODE = False

    def run_main(self, argv: list[str]) -> tuple[int, dict[str, object], str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT), *argv]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = ORCHESTRATOR.main()
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1, stdout.getvalue())
        return code, json.loads(lines[0]), stdout.getvalue(), stderr.getvalue()

    def assert_envelope(self, envelope: dict[str, object], command: str, success: bool) -> None:
        self.assertEqual(
            set(envelope),
            {"schema_version", "command", "success", "data", "error"},
        )
        self.assertEqual(envelope["schema_version"], "1")
        self.assertEqual(envelope["command"], command)
        self.assertIs(envelope["success"], success)
        if success:
            self.assertIsNone(envelope["error"])
        else:
            self.assertIsInstance(envelope["error"], dict)
            self.assertLessEqual(len(envelope["error"]["message"]), ORCHESTRATOR.MAX_ERROR_CHARS)

    def test_parser_accepts_json_for_every_public_command(self) -> None:
        parser = ORCHESTRATOR.build_parser()
        cases = {
            "doctor": ["doctor"],
            "list": ["list"],
            "status": ["status"],
            "start": ["start", "--task", "synthetic"],
            "attach": ["attach"],
            "send": ["send", "session", "--role", "reviewer", "--message", "synthetic"],
            "restart": ["restart", "session", "--role", "reviewer", "--yes"],
            "stop": ["stop", "session", "--yes"],
        }
        for command, arguments in cases.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(["--json", *arguments])
                self.assertTrue(parsed.json_output)
                self.assertEqual(parsed.command, command)

    def test_start_dry_run_is_structured_and_redacts_task_body(self) -> None:
        canary = "PRIVATE_TASK_CANARY_JSON_21f3"
        with (
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/true"),
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
        ):
            code, envelope, raw, stderr = self.run_main(
                [
                    "--json",
                    "start",
                    "--project",
                    str(ROOT),
                    "--task",
                    canary,
                    "--skip-model-check",
                    "--dry-run",
                    "--with-probe",
                    "--with-playwright",
                    "--with-django-expert",
                ]
            )
        self.assertEqual(code, 0)
        self.assert_envelope(envelope, "start", True)
        self.assertEqual(stderr, "")
        self.assertNotIn(canary, raw)
        data = envelope["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual([role["name"] for role in data["roles"]], [
            "implementer", "reviewer", "probe", "playwright", "django"
        ])
        self.assertIsNone(data["paths"]["coordination"])

    def test_start_success_returns_paths_without_payload_bodies(self) -> None:
        canary = "PRIVATE_FULL_START_CANARY_JSON_a12d"
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with (
                mock.patch.object(ORCHESTRATOR, "STATE_ROOT", state_root),
                mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/true"),
                mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
                mock.patch.object(ORCHESTRATOR, "create_tmux_grid"),
            ):
                code, envelope, raw, stderr = self.run_main(
                    [
                        "--json",
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        canary,
                        "--session",
                        "pi-json-success",
                        "--skip-model-check",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assert_envelope(envelope, "start", True)
            self.assertFalse(envelope["data"]["dry_run"])
            coordination = Path(envelope["data"]["paths"]["coordination"])
            self.assertTrue(coordination.is_relative_to(state_root.resolve()))
            self.assertNotIn(canary, raw)

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
            message_file.write_text("PRIVATE_MESSAGE_CANARY_JSON_7de1", encoding="utf-8")

            with (
                mock.patch.object(ORCHESTRATOR, "orchestrated_sessions", return_value=[("pi-test", coord)]),
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
                mock.patch.object(ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)),
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
                mock.patch.object(ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "send_keys") as send_keys,
            ):
                code, envelope, raw, _ = self.run_main(
                    ["--json", "send", "pi-test", "--role", "reviewer", "--message-file", str(message_file)]
                )
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "send", True)
            self.assertNotIn("PRIVATE_MESSAGE_CANARY_JSON_7de1", raw)
            send_keys.assert_called_once()

            with (
                mock.patch.object(ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "save_manifest"),
                mock.patch.object(ORCHESTRATOR, "tmux"),
            ):
                code, envelope, _, _ = self.run_main(
                    ["--json", "restart", "pi-test", "--role", "reviewer", "--yes", "--skip-model-check"]
                )
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "restart", True)
            self.assertTrue(envelope["data"]["restarted"])

            with (
                mock.patch.object(ORCHESTRATOR, "resolve_session", return_value=("pi-test", coord)),
                mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
                mock.patch.object(ORCHESTRATOR, "tmux"),
            ):
                code, envelope, _, _ = self.run_main(["--json", "stop", "pi-test", "--yes"])
            self.assertEqual(code, 0)
            self.assert_envelope(envelope, "stop", True)
            self.assertTrue(envelope["data"]["state_retained"])

    def test_doctor_has_structured_commands_models_and_paths(self) -> None:
        tmux_version = subprocess.CompletedProcess([], 0, "tmux 3.5\n", "")
        with (
            mock.patch.object(ORCHESTRATOR.shutil, "which", side_effect=lambda name: f"/bin/{name}"),
            mock.patch.object(ORCHESTRATOR, "run", return_value=tmux_version),
            mock.patch.object(ORCHESTRATOR, "list_tmux_sessions", return_value=[]),
            mock.patch.object(ORCHESTRATOR, "model_available", return_value=(True, "available")),
        ):
            code, envelope, _, stderr = self.run_main(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "doctor", True)
        self.assertIsInstance(envelope["data"]["commands"], list)
        self.assertIsInstance(envelope["data"]["model_checks"], list)
        self.assertIsInstance(envelope["data"]["paths"], dict)

    def test_unexpected_exception_fails_closed_as_one_generic_json_object(self) -> None:
        canary = "PRIVATE_UNEXPECTED_EXCEPTION_CANARY_1c8e"
        with mock.patch.object(
            ORCHESTRATOR,
            "list_command",
            side_effect=RuntimeError(canary),
        ):
            code, envelope, raw, stderr = self.run_main(["--json", "list"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        self.assertNotIn(canary, raw)
        self.assert_envelope(envelope, "list", False)
        self.assertEqual(envelope["error"]["code"], "internal_error")
        self.assertEqual(envelope["data"], None)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT), "list"]),
            mock.patch.object(
                ORCHESTRATOR,
                "list_command",
                side_effect=RuntimeError(canary),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            human_code = ORCHESTRATOR.main()
        self.assertEqual(human_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(canary, stderr.getvalue())
        self.assertIn("unexpected internal error", stderr.getvalue().lower())
        self.assertLessEqual(len(stderr.getvalue()), ORCHESTRATOR.MAX_ERROR_CHARS + 16)

    def test_requested_command_uses_only_the_command_position(self) -> None:
        self.assertEqual(
            ORCHESTRATOR.requested_command(["--json", "--not-a-command", "stop"]),
            "unknown",
        )
        self.assertEqual(
            ORCHESTRATOR.requested_command(
                ["--json", "start", "--task", "stop", "--unknown-option"]
            ),
            "start",
        )
        code, envelope, _, stderr = self.run_main(
            ["--json", "--not-a-command", "stop"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "unknown", False)
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")

        code, envelope, _, stderr = self.run_main(
            ["--json", "start", "--task", "stop", "--unknown-option"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "start", False)
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")

    def test_json_version_and_help_also_keep_the_single_object_contract(self) -> None:
        code, envelope, _, stderr = self.run_main(["--json", "--version"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "version", True)
        self.assertEqual(envelope["data"]["version"], "0.4.0")

        code, envelope, _, stderr = self.run_main(["--json", "--help"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "unknown", False)
        self.assertEqual(envelope["error"]["code"], "interactive_help_only")

    def test_json_failures_are_exact_bounded_and_never_duplicate_stderr(self) -> None:
        code, envelope, _, stderr = self.run_main(["--json", "attach"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "attach", False)
        self.assertEqual(envelope["error"]["code"], "interactive_only")

        canary = "PRIVATE_SUBPROCESS_STDERR_CANARY_4f80"
        manifest = {"window": ORCHESTRATOR.WINDOW, "roles": {}, "project": str(ROOT)}
        with (
            mock.patch.object(ORCHESTRATOR, "resolve_session", return_value=("pi-test", ROOT)),
            mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
            mock.patch.object(
                ORCHESTRATOR,
                "tmux",
                side_effect=subprocess.CalledProcessError(9, ["tmux"], stderr=canary),
            ),
        ):
            code, envelope, raw, stderr = self.run_main(["--json", "status", "pi-test"])
        self.assertEqual(code, 9)
        self.assertEqual(stderr, "")
        self.assertNotIn(canary, raw)
        self.assert_envelope(envelope, "status", False)
        self.assertEqual(envelope["error"]["code"], "subprocess_failed")

        code, envelope, _, stderr = self.run_main(["--json", "unknown-command"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "unknown", False)
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
