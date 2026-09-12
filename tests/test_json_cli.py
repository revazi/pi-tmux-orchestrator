from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR
from json_cli_support import JsonCliFixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "pi-tmux-agents"


class JsonMainTests(JsonCliFixture):
    def test_parser_accepts_json_for_every_public_command(self) -> None:
        parser = ORCHESTRATOR.build_parser()
        cases = {
            "doctor": ["doctor"],
            "role-registry": ["role-registry"],
            "controller": ["controller", "status"],
            "supervisor": ["supervisor", "capabilities"],
            "list": ["list"],
            "status": ["status"],
            "events": ["events", "pi-test", "--role", "reviewer"],
            "start": ["start", "--task", "synthetic"],
            "attach": ["attach"],
            "send": ["send", "session", "--role", "reviewer", "--message", "synthetic"],
            "abort": ["abort", "session", "--role", "reviewer"],
            "restart": ["restart", "session", "--role", "reviewer", "--yes"],
            "stop": ["stop", "session", "--yes"],
        }
        for command, arguments in cases.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(["--json", *arguments])
                self.assertTrue(parsed.json_output)
                self.assertEqual(parsed.command, command)

    def test_restart_confirmation_describes_preserved_brokered_history(self) -> None:
        code, envelope, _, stderr = self.run_main(
            ["--json", "restart", "pi-test", "--role", "implementer"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "restart", False)
        self.assertEqual(
            envelope["error"]["message"],
            "restart respawns the role's worker process and preserves its brokered "
            "Pi conversation and JSONL history; pass --yes",
        )

    def test_supervisor_capabilities_keep_the_versioned_json_boundary(self) -> None:
        code, envelope, raw, stderr = self.run_main(
            ["--json", "supervisor", "capabilities"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "supervisor", True)
        self.assertEqual(envelope["data"]["api_version"], "2")
        self.assertTrue(envelope["data"]["metadata_only"])
        self.assertNotIn("tmux list", raw)

    def test_supervisor_usage_json_keeps_assignment_categories_separate(self) -> None:
        canary = "PRIVATE_USAGE_BODY_CANARY"
        data = {
            "api_version": "2",
            "session": "pi-test",
            "run_id": "run-1",
            "available": True,
            "availability": "available",
            "cumulative": {
                "provider_calls": 2,
                "input_tokens": 40,
                "output_tokens": 10,
                "cache_read_tokens": 120,
                "cache_write_tokens": 5,
                "reasoning_tokens": None,
                "cost_total": 0.25,
                "operational_tokens": 175,
            },
            "roles": [
                {
                    "role": "reviewer",
                    "cumulative": {
                        "provider_calls": 2,
                        "input_tokens": 40,
                        "output_tokens": 10,
                        "cache_read_tokens": 120,
                        "cache_write_tokens": 5,
                        "reasoning_tokens": None,
                        "cost_total": 0.25,
                        "operational_tokens": 175,
                    },
                    "assignments": [
                        {
                            "assignment_id": "a" * 32,
                            "round": 1,
                            "kind": "review",
                            "usage": {
                                "provider_calls": 1,
                                "input_tokens": 20,
                                "output_tokens": 5,
                                "cache_read_tokens": 60,
                                "cache_write_tokens": 0,
                                "reasoning_tokens": None,
                                "cost_total": 0.1,
                                "operational_tokens": 85,
                            },
                        }
                    ],
                }
            ],
            "assignment_count": 1,
            "assignment_usage_unavailable": 0,
            "truncated": False,
            "limit": 10,
            "semantics": {"payload_bodies_included": False},
            "paths": {"coordination": "/private/run"},
        }
        with mock.patch.object(ORCHESTRATOR, "supervisor_usage", return_value=data):
            code, envelope, raw, stderr = self.run_main(
                [
                    "--json",
                    "supervisor",
                    "usage",
                    "pi-test",
                    "--run",
                    "run-1",
                    "--limit",
                    "10",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "supervisor", True)
        assignment = envelope["data"]["roles"][0]["assignments"][0]
        self.assertEqual(assignment["usage"]["input_tokens"], 20)
        self.assertEqual(assignment["usage"]["cache_read_tokens"], 60)
        self.assertNotIn(canary, raw)

    def test_supervisor_parser_failures_are_attributed_and_bounded(self) -> None:
        code, envelope, _, stderr = self.run_main(
            [
                "--json",
                "supervisor",
                "events",
                "pi-test",
                "--cursor",
                "reviewer=-1",
            ]
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "supervisor", False)
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")

    def test_controller_status_stop_and_attach_keep_the_json_contract(self) -> None:
        with (
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
            mock.patch.object(
                ORCHESTRATOR, "retained_controller_state", return_value=None
            ),
        ):
            code, envelope, _, stderr = self.run_main(
                ["--json", "controller", "status"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "controller", True)
        self.assertFalse(envelope["data"]["running"])
        self.assertFalse(envelope["data"]["state_retained"])
        self.assertEqual(
            envelope["data"]["pi_session_id"],
            ORCHESTRATOR.CONTROLLER_PI_SESSION_ID,
        )

        code, envelope, _, stderr = self.run_main(["--json", "controller", "stop"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "controller", False)
        self.assertIn("--confirm", envelope["error"]["message"])

        code, envelope, _, stderr = self.run_main(["--json", "controller", "attach"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "controller", False)
        self.assertEqual(envelope["error"]["code"], "interactive_only")

    def test_doctor_has_structured_commands_models_and_paths(self) -> None:
        tmux_version = subprocess.CompletedProcess([], 0, "tmux 3.5\n", "")
        with (
            mock.patch.object(
                ORCHESTRATOR.shutil, "which", side_effect=lambda name: f"/bin/{name}"
            ),
            mock.patch.object(ORCHESTRATOR, "run", return_value=tmux_version),
            mock.patch.object(ORCHESTRATOR, "list_tmux_sessions", return_value=[]),
            mock.patch.object(
                ORCHESTRATOR, "model_available", return_value=(True, "available")
            ),
        ):
            code, envelope, _, stderr = self.run_main(["doctor", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "doctor", True)
        self.assertIsInstance(envelope["data"]["commands"], list)
        self.assertIsInstance(envelope["data"]["model_checks"], list)
        self.assertEqual(
            envelope["data"]["budget_policy"]["effective"]["enforcement"],
            "warn-only",
        )
        self.assertIn("budget_config", envelope["data"]["paths"])
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
        code, envelope, _, stderr = self.run_main(["--json", "--not-a-command", "stop"])
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
        self.assertEqual(envelope["data"]["version"], "0.9.5")

        code, envelope, _, stderr = self.run_main(["--json", "--help"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "unknown", False)
        self.assertEqual(envelope["error"]["code"], "interactive_help_only")

    def test_json_failures_are_exact_bounded_and_never_duplicate_stderr(self) -> None:
        with mock.patch.dict(os.environ, {"TMUX": ""}):
            code, envelope, _, stderr = self.run_main(["--json", "attach"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "attach", False)
        self.assertEqual(envelope["error"]["code"], "interactive_only")

        canary = "PRIVATE_SUBPROCESS_STDERR_CANARY_4f80"
        manifest = {"window": ORCHESTRATOR.WINDOW, "roles": {}, "project": str(ROOT)}
        with (
            mock.patch.object(
                ORCHESTRATOR, "resolve_session", return_value=("pi-test", ROOT)
            ),
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
