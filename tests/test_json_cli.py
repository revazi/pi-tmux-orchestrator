from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "pi-tmux-agents"


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

    def assert_envelope(
        self, envelope: dict[str, object], command: str, success: bool
    ) -> None:
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
            self.assertLessEqual(
                len(envelope["error"]["message"]), ORCHESTRATOR.MAX_ERROR_CHARS
            )

    def test_parser_accepts_json_for_every_public_command(self) -> None:
        parser = ORCHESTRATOR.build_parser()
        cases = {
            "doctor": ["doctor"],
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

    def test_start_dry_run_is_structured_and_redacts_workflow_bodies(self) -> None:
        canary = "PRIVATE_TASK_CANARY_JSON_21f3"
        context_canary = "PRIVATE_CONTEXT_CAPSULE_CANARY_JSON_87cd"
        with (
            mock.patch.object(
                ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
            ),
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
                    "--profile",
                    "economy",
                    "--implementation-flow",
                    "phased",
                    "--context-capsule",
                    context_canary,
                    "--workspace-capsule",
                    "--workspace-relevant-path",
                    "pi_tmux_orchestrator/broker.py",
                    "--skip-model-check",
                    "--dry-run",
                    "--rpc-workers",
                    "--worker-skill",
                    f"reviewer={ROOT / 'SKILL.md'}",
                    "--with-probe",
                    "--with-playwright",
                    "--force-specialist",
                    "playwright",
                    "--with-django-expert",
                ]
            )
        self.assertEqual(code, 0)
        self.assert_envelope(envelope, "start", True)
        self.assertEqual(stderr, "")
        self.assertNotIn(canary, raw)
        self.assertNotIn(context_canary, raw)
        data = envelope["data"]
        self.assertTrue(data["dry_run"])
        self.assertEqual(
            data["context_capsule"],
            {"present": True, "chars": len(context_canary)},
        )
        self.assertEqual(
            {
                key: data["workspace_capsule"][key]
                for key in (
                    "enabled",
                    "schema_version",
                    "validation",
                    "relevant_path_count",
                )
            },
            {
                "enabled": True,
                "schema_version": 1,
                "validation": "validated",
                "relevant_path_count": 1,
            },
        )
        self.assertRegex(data["workspace_capsule"]["digest"], r"^[0-9a-f]{64}$")
        self.assertNotIn("relevant_paths", data["workspace_capsule"])
        self.assertNotIn("pi_tmux_orchestrator/broker.py", raw)
        self.assertEqual(data["transport"], "rpc")
        self.assertEqual(data["implementation_flow"], "phased")
        self.assertEqual(data["forced_specialists"], ["playwright"])
        self.assertEqual(
            data["execution_profile"],
            {"name": "economy", "kind": "packaged", "source": "per-run"},
        )
        self.assertEqual(
            {role["name"]: role["thinking"] for role in data["roles"]},
            {
                "implementer": "medium",
                "reviewer": "medium",
                "probe": "low",
                "playwright": "medium",
                "django": "medium",
            },
        )
        self.assertFalse(data["worker_resources"]["skill_discovery"])
        self.assertEqual(
            data["worker_resources"]["skills"]["reviewer"],
            [str(ROOT / "SKILL.md")],
        )
        self.assertEqual(
            data["trust"]["policy"],
            "saved-or-global-policy",
        )
        self.assertTrue(all(role["transport"] == "rpc" for role in data["roles"]))
        self.assertEqual(
            [role["name"] for role in data["roles"]],
            ["implementer", "reviewer", "probe", "playwright", "django"],
        )
        self.assertIsNone(data["paths"]["coordination"])
        self.assertIsNone(data["paths"]["observer_socket"])

    def test_workspace_relevant_paths_require_explicit_capsule_opt_in(self) -> None:
        with mock.patch.object(
            ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
        ):
            code, envelope, raw, stderr = self.run_main(
                [
                    "--json",
                    "start",
                    "--project",
                    str(ROOT),
                    "--task",
                    "Synthetic",
                    "--workspace-relevant-path",
                    "README.md",
                    "--skip-model-check",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "")
        self.assert_envelope(envelope, "start", False)
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")
        self.assertNotIn("README.md", raw)

    def test_start_model_policy_uses_config_with_explicit_override_precedence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "models.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "defaults": {
                            "provider": "anthropic",
                            "model": "configured-model",
                            "thinking": "medium",
                        },
                        "roles": {
                            "reviewer": {
                                "provider": "google",
                                "model": "configured-reviewer",
                                "thinking": "low",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"PI_TMUX_ORCHESTRATOR_CONFIG": str(config_path)},
                ),
                mock.patch.object(
                    ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
                ),
                mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
            ):
                code, envelope, _, stderr = self.run_main(
                    [
                        "--json",
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "synthetic",
                        "--implementer-provider",
                        "openrouter",
                        "--implementer-model",
                        "explicit/model",
                        "--implementer-thinking",
                        "high",
                        "--skip-model-check",
                        "--dry-run",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        roles = {role["name"]: role for role in envelope["data"]["roles"]}
        self.assertEqual(
            (
                roles["implementer"]["provider"],
                roles["implementer"]["model"],
                roles["implementer"]["thinking"],
            ),
            ("openrouter", "explicit/model", "high"),
        )
        self.assertEqual(
            (
                roles["reviewer"]["provider"],
                roles["reviewer"]["model"],
                roles["reviewer"]["thinking"],
            ),
            ("google", "configured-reviewer", "low"),
        )

    def test_start_applies_exact_project_defaults_with_explicit_precedence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "models.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "defaultProfile": "economy",
                        "profiles": {},
                        "defaults": {},
                        "roles": {},
                        "projects": [
                            {
                                "directory": str(ROOT),
                                "profile": "balanced",
                                "defaults": {
                                    "provider": "project-provider",
                                    "model": "project-model",
                                },
                                "roles": {"reviewer": {"thinking": "xhigh"}},
                                "implementationFlow": "phased",
                                "specialists": ["probe", "django"],
                                "workspaceCapsule": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"PI_TMUX_ORCHESTRATOR_CONFIG": str(config_path)},
                ),
                mock.patch.object(
                    ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
                ),
                mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
            ):
                code, envelope, _, stderr = self.run_main(
                    [
                        "--json",
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "synthetic",
                        "--skip-model-check",
                        "--dry-run",
                    ]
                )
                explicit_code, explicit, _, explicit_stderr = self.run_main(
                    [
                        "--json",
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "synthetic",
                        "--profile",
                        "economy",
                        "--implementation-flow",
                        "single",
                        "--without-probe",
                        "--without-django-expert",
                        "--skip-model-check",
                        "--dry-run",
                    ]
                )
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(envelope["data"]["project_config"]["matched"])
        self.assertEqual(envelope["data"]["implementation_flow"], "phased")
        self.assertEqual(envelope["data"]["execution_profile"]["source"], "project")
        roles = {role["name"]: role for role in envelope["data"]["roles"]}
        self.assertEqual(set(roles), {"implementer", "reviewer", "probe", "django"})
        self.assertEqual(roles["implementer"]["provider"], "project-provider")
        self.assertEqual(roles["reviewer"]["thinking"], "xhigh")
        self.assertEqual(
            envelope["data"]["orchestration_config"]["path"], str(config_path)
        )

        self.assertEqual((explicit_code, explicit_stderr), (0, ""))
        self.assertEqual(explicit["data"]["implementation_flow"], "single")
        self.assertEqual(explicit["data"]["execution_profile"]["source"], "per-run")
        self.assertEqual(
            {role["name"] for role in explicit["data"]["roles"]},
            {"implementer", "reviewer"},
        )

    def test_start_budget_policy_uses_global_then_explicit_override_precedence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            budget_path = Path(directory) / "budgets.json"
            budget_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "warning": {"run": {"provider_calls": 10}},
                        "hard": {"run": {"provider_calls": 20}},
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG": str(budget_path)},
                ),
                mock.patch.object(
                    ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
                ),
                mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
            ):
                code, envelope, raw, stderr = self.run_main(
                    [
                        "--json",
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "synthetic",
                        "--budget-enforcement",
                        "hard",
                        "--budget-override",
                        "warning.run.provider_calls=12",
                        "--budget-override",
                        "hard.assignment.cost_total=2.5",
                        "--skip-model-check",
                        "--dry-run",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        policy = envelope["data"]["budget_policy"]
        self.assertEqual(policy["enforcement"], "hard")
        self.assertEqual(policy["warning"]["run"]["provider_calls"], 12)
        self.assertEqual(policy["hard"]["run"]["provider_calls"], 20)
        self.assertEqual(policy["hard"]["assignment"]["cost_total"], 2.5)
        self.assertEqual(policy["warning"]["role"]["operational_tokens"], 200_000)
        self.assertNotIn("apiKey", raw)
        self.assertNotIn("endpoint", raw)

    def test_start_success_returns_paths_without_payload_bodies(self) -> None:
        canary = "PRIVATE_FULL_START_CANARY_JSON_a12d"
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "state"
            with (
                mock.patch.object(ORCHESTRATOR, "STATE_ROOT", state_root),
                mock.patch.object(
                    ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
                ),
                mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
                mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as create_grid,
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
                expected_socket = str(
                    ORCHESTRATOR.broker_paths(
                        Path(envelope["data"]["paths"]["coordination"])
                    )["socket"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assert_envelope(envelope, "start", True)
            self.assertFalse(envelope["data"]["dry_run"])
            coordination = Path(envelope["data"]["paths"]["coordination"])
            self.assertTrue(coordination.is_relative_to(state_root.resolve()))
            self.assertEqual(
                envelope["data"]["paths"]["observer_socket"], expected_socket
            )
            manifest = create_grid.call_args.args[4]
            self.assertEqual(manifest["version"], 5)
            self.assertEqual(
                manifest["execution_profile"],
                {
                    "name": "thorough",
                    "kind": "packaged",
                    "source": "packaged-default",
                },
            )
            self.assertFalse(manifest["project_config"]["matched"])
            self.assertEqual(
                manifest["orchestration_config"],
                envelope["data"]["orchestration_config"],
            )
            self.assertNotIn(canary, raw)

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
            "workflow": {"state": "ready", "round": 2},
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
        self.assertEqual(envelope["data"]["version"], "0.9.2")

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


if __name__ == "__main__":
    unittest.main()
