"""Explicit dry-run planning only; no connected lifecycle acceptance claim."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import commands, runtime
from pi_tmux_orchestrator.custom_role_resources import select_custom_start
from pi_tmux_orchestrator.models import OrchestrationError
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

    def assert_no_start(self):
        self.grid.assert_not_called()
        self.initialize.assert_not_called()
        self.assertFalse((runtime.STATE_ROOT / "pi-custom-preview").exists())

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
                    },
                )
                self.assertFalse(data["custom_role_selection"]["launch_supported"])
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

    def test_live_selection_fails_before_dependency_checks_reads_or_mutation(self):
        for options in ([], ["--rpc-workers", "--approve-project"]):
            with mock.patch.object(commands, "select_custom_start") as select:
                code, envelope, raw, stderr = self.run_start(*options, dry_run=False)
                self.assertEqual((code, stderr), (2, ""), raw)
                self.assertEqual(envelope["error"]["code"], "custom_start_not_enabled")
                select.assert_not_called()
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
            ["--force-specialist", self.name],
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
