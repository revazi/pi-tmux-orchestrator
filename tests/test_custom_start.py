"""Explicit custom specialist selection and live-start integration."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import socket
from unittest import mock

from pi_tmux_orchestrator import commands, runtime
from pi_tmux_orchestrator.custom_role_resources import select_custom_start
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.profiles import (
    PACKAGED_EXECUTION_PROFILES,
    resolve_custom_thinking,
)
from pi_tmux_orchestrator.storage import validate_manifest
from test_custom_role_resources import CustomRoleResourceFixture
import test_json_cli


class CustomStartTests(CustomRoleResourceFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, runtime, "JSON_MODE", runtime.JSON_MODE)
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "PI_TMUX_ORCHESTRATOR_CONFIG": str(
                        self.root / "absent-models.json"
                    ),
                    "PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG": str(
                        self.root / "absent-budgets.json"
                    ),
                    "PI_TMUX_ORCHESTRATOR_ROLE_REGISTRY": str(
                        self.root / "absent-registry.json"
                    ),
                },
            )
        )
        self.command_path = self.enterContext(
            mock.patch.object(commands, "command_path", return_value="/fixture/command")
        )
        self.session_exists = self.enterContext(
            mock.patch.object(commands, "session_exists", return_value=False)
        )
        self.grid = self.enterContext(mock.patch.object(commands, "create_tmux_grid"))
        self.initialize = self.enterContext(
            mock.patch.object(commands, "initialize_broker_run")
        )
        self.spec = [self.name, "fixture-provider", "fixture/model", "off"]

    def run_start(self, *options, custom=True, dry_run=True, model_check=False):
        arguments = [
            "--json",
            "start",
            "--project",
            str(self.project),
            "--session",
            "pi-custom-preview",
            "--task",
            "PRIVATE_TASK_CANARY",
        ]
        if custom:
            arguments.extend(
                ["--custom-role", *self.spec, "--role-registry", str(self.registry)]
            )
        if dry_run:
            arguments.append("--dry-run")
        if not model_check:
            arguments.append("--skip-model-check")
        return test_json_cli.JsonMainTests.run_main(self, [*arguments, *options])

    def write_model_config(self, profiles, *, default="custom-careful"):
        path = self.root / "absent-models.json"
        path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "defaultProfile": default,
                    "profiles": profiles,
                    "defaults": {},
                    "roles": {},
                }
            ),
            encoding="utf-8",
        )
        return path

    def assert_no_start(self):
        self.grid.assert_not_called()
        self.initialize.assert_not_called()
        self.assertFalse((runtime.STATE_ROOT / "pi-custom-preview").exists())

    def internal_selection(self):
        return select_custom_start(self.project, [self.spec], str(self.registry))

    def test_public_live_start_waits_for_custom_admission_before_running(self):
        with mock.patch.object(commands, "wait_for_custom_startup") as wait:
            code, envelope, raw, stderr = self.run_start(dry_run=False)
        self.assertEqual((code, stderr), (0, ""), raw)
        self.initialize.assert_called_once()
        self.grid.assert_called_once()
        wait.assert_called_once()
        coord = Path(envelope["data"]["paths"]["coordination"])
        self.assertEqual((coord / "startup-state").read_text(), "RUNNING\n")
        self.assertEqual(wait.call_args.args[3]["version"], 7)
        self.assertEqual(
            envelope["data"]["custom_role_selection"],
            {
                "launch_supported": True,
                "resource_verification": "checked_at_selection_and_launch",
                "models": "explicit-or-profile-thinking",
                "roles": {
                    self.name: {
                        "state": "enabled",
                        "selection_source": "per-run",
                        "thinking_source": "per-run-override",
                        "activation_source": "deterministic-contract-rule",
                    }
                },
                "skills": {self.name: {"source": "registry-bound", "count": 1}},
            },
        )

    def test_public_custom_failure_before_grid_is_retained_and_body_free(self):
        self.initialize.side_effect = OrchestrationError("PRIVATE_FAILURE_CANARY")
        with (
            mock.patch.object(commands, "tmux") as tmux,
            mock.patch.object(commands, "wait_for_custom_startup") as wait,
        ):
            code, envelope, raw, stderr = self.run_start(dry_run=False)
        self.assertEqual((code, stderr), (2, ""), raw)
        self.grid.assert_not_called()
        wait.assert_not_called()
        tmux.assert_called_once_with(
            ["kill-session", "-t", "=pi-custom-preview"],
            check=False,
            capture=True,
        )
        runs = list((runtime.STATE_ROOT / "pi-custom-preview").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0] / "startup-state").read_text(), "FAILED\n")
        retained = "\n".join(
            path.read_text(errors="replace")
            for path in runs[0].rglob("*")
            if path.is_file()
        )
        self.assertNotIn("PRIVATE_FAILURE_CANARY", retained)

    def test_custom_admission_bounds_one_and_eight_role_fanout(self):
        for custom_count in (1, 8):
            with self.subTest(custom_count=custom_count):
                coord = runtime.STATE_ROOT / f"health-{custom_count}" / "run-1"
                coord.mkdir(mode=0o700, parents=True)
                role_names = ["implementer", "reviewer"] + [
                    f"custom-specialist-{index}" for index in range(custom_count)
                ]
                manifest = {
                    "version": 6,
                    "transport": "rpc" if custom_count == 8 else "tui",
                    "monitor_pane_id": "%99",
                    "roles": {
                        role: {"pane_id": f"%{index}"}
                        for index, role in enumerate(role_names)
                    },
                }
                socket_path = commands.broker_paths(coord)["socket"]
                socket_path.parent.mkdir(parents=True, exist_ok=True)
                server = socket.socket(socket.AF_UNIX)
                server.bind(str(socket_path))
                try:
                    pane = mock.Mock(returncode=0, stdout="0\n")
                    snapshot = {
                        "workflow": {"state": "active"},
                        "roles": [
                            {"role": role, "connected": True} for role in role_names
                        ],
                    }
                    with (
                        mock.patch.object(commands, "tmux", return_value=pane),
                        mock.patch.object(
                            commands, "public_broker_snapshot", return_value=snapshot
                        ),
                        mock.patch.object(commands, "CUSTOM_STARTUP_STABLE_SECONDS", 0),
                    ):
                        commands.wait_for_custom_startup(
                            "pi-custom-health",
                            coord,
                            role_names,
                            manifest,
                            timeout=0.5,
                        )
                finally:
                    server.close()
                    socket_path.unlink(missing_ok=True)

    def test_public_custom_admission_failure_rolls_back_exact_session(self):
        with (
            mock.patch.object(commands, "tmux") as tmux,
            mock.patch.object(
                commands,
                "wait_for_custom_startup",
                side_effect=OrchestrationError("worker unavailable", "startup_failed"),
            ),
        ):
            code, envelope, raw, stderr = self.run_start(dry_run=False)
        self.assertEqual((code, stderr), (2, ""), raw)
        self.grid.assert_called_once()
        self.assertEqual(
            tmux.call_args_list,
            [
                mock.call(
                    ["kill-session", "-t", "=pi-custom-preview"],
                    check=False,
                    capture=True,
                )
            ],
        )
        runs = list((runtime.STATE_ROOT / "pi-custom-preview").iterdir())
        self.assertEqual((runs[0] / "startup-state").read_text(), "FAILED\n")
        self.assertFalse(envelope["success"])

    def test_both_transport_previews_preserve_authority_and_exclude_resources(self):
        before = sorted(self.root.rglob("*"))
        for transport in ("tui", "rpc"):
            with self.subTest(transport=transport):
                options = ["--rpc-workers"] if transport == "rpc" else []
                code, envelope, raw, stderr = self.run_start(*options)
                self.assertEqual((code, stderr), (0, ""), raw)
                data = envelope["data"]
                roles = data["roles"]
                self.assertEqual(
                    [role["name"] for role in roles],
                    ["implementer", "reviewer", self.name],
                )
                self.assertEqual(roles[0]["tool_policy"], "default")
                self.assertEqual(
                    roles[1]["tool_policy"], "workflow-read-only-with-bash"
                )
                self.assertEqual(
                    roles[2],
                    {
                        "name": self.name,
                        "provider": self.spec[1],
                        "model": self.spec[2],
                        "thinking": "off",
                        "transport": transport,
                        "tool_policy": "custom-read-only-no-shell",
                        "specialist_contract": "probe",
                        "resource_verification": "not_checked",
                        "selection_source": "per-run",
                        "thinking_source": "per-run-override",
                        "activation_source": "deterministic-contract-rule",
                        "activation_state": "enabled",
                    },
                )
                self.assertTrue(data["custom_role_selection"]["launch_supported"])
                self.assertEqual(
                    data["custom_role_selection"]["resource_verification"],
                    "checked_at_selection",
                )
                self.assertEqual(
                    data["custom_role_selection"]["skills"][self.name],
                    {"source": "registry-bound", "count": 1},
                )
                self.assertEqual(
                    data["worker_context_policy"], {"version": 1, "overrides": {}}
                )
                for excluded in (
                    "PRIVATE_",
                    str(self.registry),
                    self.prompt["path"],
                    self.skill["path"],
                    self.prompt["sha256"],
                    self.skill["sha256"],
                ):
                    self.assertNotIn(excluded, raw)
                self.assertEqual(sorted(self.root.rglob("*")), before)
                self.assert_no_start()

    def test_internal_launch_projection_is_strict_body_free_manifest_v7(self):
        custom = select_custom_start(self.project, [self.spec], str(self.registry))
        roles = ["implementer", "reviewer", self.name]
        configs = {
            role: {
                "provider": "fixture-provider",
                "model": "fixture/model",
                "thinking": "off",
                "tools": None if role == "implementer" else "read,bash,grep,find,ls",
                "pane_id": None,
            }
            for role in roles[:2]
        }
        configs.update(custom["roles"])
        before = copy.deepcopy(configs)
        manifest = commands.construct_start_manifest(
            self.coord,
            self.project,
            self.manifest["session"],
            "rpc",
            approve_project=False,
            execution_profile=self.manifest["execution_profile"],
            project_config=self.manifest["project_config"],
            orchestration_config=self.manifest["orchestration_config"],
            roles=roles,
            configs=configs,
            custom_role_registry=custom["registry_path"],
        )
        manifest["monitor_pane_id"] = "%9"
        for index, role in enumerate(roles, start=1):
            manifest["roles"][role]["pane_id"] = f"%{index}"
        self.assertEqual(validate_manifest(manifest, self.coord), manifest)
        self.assertEqual(configs, before)
        self.assertEqual(manifest["version"], 7)
        self.assertEqual(manifest["custom_role_registry"], str(self.registry))
        self.assertEqual(list(manifest["roles"]), roles)
        self.assertEqual(manifest["roles"][self.name]["custom_role"], self.definition)
        self.assertEqual(manifest["roles"][self.name]["tools"], "read,grep,find,ls")
        self.assertEqual(
            manifest["roles"][self.name]["custom_policy"],
            {
                "selection_source": "per-run",
                "thinking_source": "per-run-override",
                "activation_source": "deterministic-contract-rule",
            },
        )
        for mutation in ("missing", "unknown", "bad-source"):
            invalid_manifest = copy.deepcopy(manifest)
            policy = invalid_manifest["roles"][self.name]["custom_policy"]
            if mutation == "missing":
                policy.pop("thinking_source")
            elif mutation == "unknown":
                policy["extra"] = "unsafe"
            else:
                policy["activation_source"] = "project"
            with self.subTest(mutation=mutation), self.assertRaises(OrchestrationError):
                validate_manifest(invalid_manifest, self.coord)
        retained = json.dumps(manifest)
        self.assertNotIn("PRIVATE_GUIDANCE_CANARY", retained)
        self.assertNotIn("PRIVATE_SKILL_CANARY", retained)
        for registry in (None, str(self.registry)):
            inconsistent = roles if registry is None else roles[:2]
            with self.subTest(registry=registry), self.assertRaises(OrchestrationError):
                commands.construct_start_manifest(
                    self.coord,
                    self.project,
                    self.manifest["session"],
                    "rpc",
                    approve_project=False,
                    execution_profile=self.manifest["execution_profile"],
                    project_config=self.manifest["project_config"],
                    orchestration_config=self.manifest["orchestration_config"],
                    roles=inconsistent,
                    configs={role: configs[role] for role in inconsistent},
                    custom_role_registry=registry,
                )

    def test_invalid_live_selection_fails_before_dependencies_or_mutation(self):
        Path(self.prompt["path"]).write_text("TAMPERED")
        code, envelope, raw, stderr = self.run_start(dry_run=False)
        self.assertEqual((code, stderr), (2, ""), raw)
        self.assertFalse(envelope["success"])
        self.command_path.assert_not_called()
        self.session_exists.assert_not_called()
        self.assert_no_start()

    def test_omission_never_loads_ambient_registry(self):
        with mock.patch(
            "pi_tmux_orchestrator.custom_role_resources.load_registry"
        ) as load:
            code, envelope, raw, stderr = self.run_start(custom=False)
            self.assertEqual((code, stderr), (0, ""), raw)
            self.assertNotIn("custom_role_selection", envelope["data"])
            self.assertEqual(
                [role["name"] for role in envelope["data"]["roles"]],
                ["implementer", "reviewer"],
            )
            load.assert_not_called()
        self.assert_no_start()

    def test_custom_profile_thinking_is_opt_in_and_explicit_override_wins(self):
        mapping = dict(PACKAGED_EXECUTION_PROFILES["balanced"])
        mapping[self.name] = "high"
        self.write_model_config({"custom-careful": mapping})

        self.spec[3] = "profile"
        code, envelope, raw, _ = self.run_start("--profile", "custom-careful")
        self.assertEqual(code, 0, raw)
        role = envelope["data"]["roles"][-1]
        self.assertEqual(
            (role["thinking"], role["thinking_source"]), ("high", "execution-profile")
        )
        self.assertEqual(
            envelope["data"]["execution_profile"],
            {"name": "custom-careful", "kind": "custom", "source": "per-run"},
        )

        self.spec[3] = "off"
        code, envelope, raw, _ = self.run_start("--profile", "custom-careful")
        self.assertEqual(code, 0, raw)
        role = envelope["data"]["roles"][-1]
        self.assertEqual(
            (role["thinking"], role["thinking_source"]), ("off", "per-run-override")
        )

    def test_custom_profile_mapping_does_not_implicitly_select_a_role(self):
        mapping = dict(PACKAGED_EXECUTION_PROFILES["balanced"])
        mapping[self.name] = "high"
        self.write_model_config({"custom-careful": mapping})
        with mock.patch("pi_tmux_orchestrator.role_registry.load_registry") as load:
            code, envelope, raw, _ = self.run_start(
                "--profile", "custom-careful", custom=False
            )
        self.assertEqual(code, 0, raw)
        load.assert_not_called()
        self.assertEqual(
            [role["name"] for role in envelope["data"]["roles"]],
            ["implementer", "reviewer"],
        )
        self.assertNotIn("custom_role_selection", envelope["data"])

    def test_custom_profile_missing_malformed_and_project_sources_fail_closed(self):
        self.write_model_config(
            {"custom-careful": dict(PACKAGED_EXECUTION_PROFILES["balanced"])}
        )
        self.spec[3] = "profile"
        code, envelope, raw, _ = self.run_start("--profile", "custom-careful")
        self.assertEqual(code, 2, raw)
        self.assertIn("no mapping", envelope["error"]["message"])

        malformed = dict(PACKAGED_EXECUTION_PROFILES["balanced"])
        malformed["custom-Bad"] = "high"
        self.write_model_config({"custom-careful": malformed})
        code, envelope, raw, _ = self.run_start("--profile", "custom-careful")
        self.assertEqual(code, 2, raw)
        self.assertIn("invalid custom role mappings", envelope["error"]["message"])

        with self.assertRaisesRegex(OrchestrationError, "Project profile"):
            resolve_custom_thinking(
                self.name,
                "profile",
                {
                    "name": "custom-careful",
                    "kind": "custom",
                    "source": "project",
                    "thinking": {self.name: "high"},
                },
            )

    def test_forced_custom_role_is_selected_enabled_and_retained(self):
        code, envelope, raw, _ = self.run_start("--force-specialist", self.name)
        self.assertEqual(code, 0, raw)
        self.assertEqual(envelope["data"]["forced_specialists"], [self.name])
        self.assertEqual(
            envelope["data"]["roles"][-1]["activation_source"], "per-run-force"
        )
        self.assertEqual(
            envelope["data"]["custom_role_selection"]["roles"][self.name][
                "activation_source"
            ],
            "per-run-force",
        )

        code, envelope, raw, _ = self.run_start(
            "--force-specialist", self.name, custom=False
        )
        self.assertEqual(code, 2, raw)
        self.assertIn("enabled", envelope["error"]["message"])

    def test_explicit_model_values_ignore_profile_and_builtin_overrides(self):
        with mock.patch.object(commands, "validate_model") as validate:
            code, envelope, raw, _ = self.run_start(
                "--profile",
                "economy",
                "--probe-provider",
                "unrelated",
                "--probe-model",
                "other",
                "--probe-thinking",
                "high",
                model_check=True,
            )
            self.assertEqual(code, 0, raw)
            role = envelope["data"]["roles"][-1]
            self.assertEqual(
                [role[key] for key in ("name", "provider", "model", "thinking")],
                self.spec,
            )
            self.assertEqual(
                [call.args[0] for call in validate.call_args_list],
                ["implementer", "reviewer", self.name],
            )
            config = validate.call_args_list[-1].args[1]
            self.assertEqual(config["tools"], "read,grep,find,ls")
            self.assertNotIn("skills", config)
        self.assert_no_start()

    def test_models_are_not_silently_accepted_when_catalog_check_fails(self):
        with mock.patch.object(
            commands,
            "validate_model",
            side_effect=OrchestrationError("fixture model unavailable"),
        ):
            code, envelope, raw, _ = self.run_start(model_check=True)
            self.assertEqual(code, 2, raw)
            self.assertFalse(envelope["success"])
        self.assert_no_start()

    def test_registry_and_resources_are_freshly_checked_for_each_preview(self):
        for path in (
            Path(self.prompt["path"]),
            Path(self.skill["path"]),
            self.registry,
        ):
            original = path.read_bytes()
            path.write_text("TAMPERED")
            code, envelope, raw, stderr = self.run_start()
            self.assertEqual((code, stderr), (2, ""), raw)
            self.assertIsNone(envelope["data"])
            self.assertNotIn("PRIVATE_", raw)
            self.assert_no_start()
            path.write_bytes(original)

    def test_full_bounded_selection_includes_all_contracts_and_required_roles(self):
        definitions = []
        options = [
            "--with-probe",
            "--with-playwright",
            "--with-django-expert",
            "--implementation-flow",
            "phased",
            "--role-registry",
            str(self.registry),
        ]
        for index in range(8):
            definition = copy.deepcopy(self.definition)
            definition.update(
                id=f"custom-specialist-{index}",
                contract=("probe", "playwright", "django")[index % 3],
            )
            definitions.append(definition)
            options.extend(["--custom-role", definition["id"], *self.spec[1:]])
        self.registry.write_text(json.dumps({"version": 1, "roles": definitions}))
        code, envelope, raw, _ = self.run_start(*options, custom=False)
        self.assertEqual(code, 0, raw)
        self.assertEqual(len(envelope["data"]["roles"]), 13)
        self.assertEqual(
            [role["specialist_contract"] for role in envelope["data"]["roles"][5:]],
            [definition["contract"] for definition in definitions],
        )
        self.assertEqual(envelope["data"]["forced_specialists"], [])
        self.assert_no_start()

    def test_rejects_ambiguous_unbounded_or_incomplete_native_selections(self):
        invalid = [
            None,
            {},
            [None],
            [self.spec[:-1]],
            [self.spec + ["extra"]],
            [self.spec, self.spec],
            [self.spec] * 9,
        ]
        for index, values in (
            (
                0,
                [
                    "reviewer",
                    "custom-reviewer",
                    "custom-security\n",
                    "custom-unknown",
                    {},
                ],
            ),
            (1, ["", None, "a" * 257, "unsafe\nprovider", "--provider x", "x\x7fy"]),
            (2, ["", {}, "a" * 257, " unsafe", "x\x00y"]),
            (3, ["", None, {}, "HIGH", "high\n"]),
        ):
            for value in values:
                spec = self.spec.copy()
                spec[index] = value
                invalid.append([spec])
        for selections in invalid:
            with (
                self.subTest(selections=selections),
                self.assertRaises(OrchestrationError),
            ):
                select_custom_start(self.project, selections, str(self.registry))
        with self.assertRaises(OrchestrationError):
            select_custom_start(self.project, [], str(self.registry))
        self.assert_no_start()

    def test_cli_rejects_duplicate_missing_and_unregistered_selections(self):
        for options in (
            ["--custom-role", *self.spec],
            ["--custom-role", "custom-missing", *self.spec[1:]],
            ["--custom-role", self.name, self.spec[1]],
            ["--worker-skill", f"{self.name}={self.skill['path']}"],
            ["--worker-context", f"{self.name}=retain"],
        ):
            with self.subTest(options=options):
                code, envelope, raw, _ = self.run_start(*options)
                self.assertEqual(code, 2, raw)
                self.assertFalse(envelope["success"])
                self.assert_no_start()
        code, envelope, raw, _ = self.run_start(
            "--role-registry", str(self.registry), custom=False
        )
        self.assertEqual(code, 2, raw)
        self.assertEqual(envelope["error"]["code"], "invalid_arguments")
