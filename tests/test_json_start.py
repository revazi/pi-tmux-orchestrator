from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR
from json_cli_support import JsonCliFixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "pi-tmux-agents"


class JsonStartTests(JsonCliFixture):
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
                    "--max-repair-rounds",
                    "2",
                    "--worker-context",
                    "reviewer=retain",
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
        self.assertEqual(
            data["continuation_policy"], {"version": 1, "max_repair_rounds": 2}
        )
        self.assertEqual(
            data["worker_context_policy"],
            {"version": 1, "overrides": {"reviewer": "retain"}},
        )
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
