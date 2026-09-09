"""Resource-bound bootstrap evidence, not custom broker workflow acceptance."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import commands, rpc_supervisor, runtime
from pi_tmux_orchestrator.broker import Broker, initialize_broker_run
from pi_tmux_orchestrator.configuration import public_project_config
from pi_tmux_orchestrator.constants import CUSTOM_READ_ONLY_TOOLS, MAX_MANIFEST_BYTES
from pi_tmux_orchestrator.custom_role_resources import (
    retained_custom_definitions,
    select_custom_roles,
    verify_custom_role,
)
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.output import public_role
from pi_tmux_orchestrator.storage import load_manifest, save_manifest, validate_manifest
from pi_tmux_orchestrator.worker_resources import prepare_worker_resources
from test_broker import BrokerFixture


class CustomRoleResourceTests(BrokerFixture):
    def setUp(self):
        super().setUp()
        self.root = Path(self.temporary.name).resolve()
        self.project = Path(self.manifest["project"])
        self.legacy = copy.deepcopy(self.manifest)
        self.registry = self.root / "roles.json"
        self.prompt = self.resource("guidance.md", "PRIVATE_GUIDANCE_CANARY\n")
        self.skill = self.resource("skill.md", "PRIVATE_SKILL_CANARY\n")
        self.name = "custom-security"
        self.definition = {
            "id": self.name,
            "contract": "probe",
            "prompt": self.prompt,
            "skills": [self.skill],
        }
        self.save_registry()
        self.selection = select_custom_roles(
            self.project, [self.name], str(self.registry)
        )
        self.manifest.update(
            version=6,
            execution_profile={
                "name": "balanced",
                "kind": "packaged",
                "source": "packaged-default",
            },
            project_config=public_project_config(None),
            orchestration_config={
                "path": str(self.root / "model-config.json"),
                "version": 3,
            },
            custom_role_registry=self.selection["registry_path"],
        )
        self.role = {
            **self.manifest["roles"]["reviewer"],
            "pane_id": "%4",
            "tools": CUSTOM_READ_ONLY_TOOLS,
            "session_id": "run-1-custom-security",
            "session_dir": str(self.coord / "sessions" / self.name),
            "custom_role": self.selection["roles"][self.name],
        }
        self.manifest["roles"][self.name] = self.role
        save_manifest(self.coord, self.manifest)
        token = self.coord / f"{self.name}.token"
        token.write_text("a" * 32)
        token.chmod(0o600)

    def resource(self, name, body):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o600)
        return {"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest()}

    def save_registry(self):
        self.registry.write_text(
            json.dumps({"version": 1, "roles": [self.definition]}), encoding="utf-8"
        )
        self.registry.chmod(0o600)

    def test_selection_is_explicit_bounded_unique_and_body_free(self):
        for names in (
            None,
            "custom-security",
            [self.name, self.name],
            ["reviewer"],
            ["custom-missing"],
            [self.name] * 9,
            [{}],
        ):
            with self.subTest(names=names), self.assertRaises(OrchestrationError):
                select_custom_roles(self.project, names, str(self.registry))
        with mock.patch(
            "pi_tmux_orchestrator.custom_role_resources.load_registry"
        ) as load:
            self.assertEqual(
                select_custom_roles(self.project, []),
                {"registry_path": None, "roles": {}},
            )
            load.assert_not_called()
        self.assertNotIn("PRIVATE_", json.dumps(self.selection))
        self.assertNotIn("PRIVATE_", json.dumps(load_manifest(self.coord)))

    def test_retained_reads_do_not_consult_or_require_current_resources(self):
        self.registry.unlink()
        Path(self.prompt["path"]).unlink()
        Path(self.skill["path"]).unlink()
        self.assertEqual(load_manifest(self.coord), self.manifest)
        self.assertEqual(
            retained_custom_definitions(self.manifest), {self.name: self.definition}
        )
        with self.assertRaises(OrchestrationError):
            verify_custom_role(self.manifest, self.name)
        role = public_role(self.name, self.role)
        self.assertEqual(role["tool_policy"], "custom-read-only-no-shell")
        self.assertNotIn("custom_role", role)
        self.assertNotIn("registry", json.dumps(role))

    def test_legacy_manifests_remain_readable_without_custom_binding(self):
        self.assertEqual(validate_manifest(self.legacy, self.coord), self.legacy)
        self.assertEqual(retained_custom_definitions(self.legacy), {})
        old = copy.deepcopy(self.legacy)
        old["roles"][self.name] = self.role
        with self.assertRaises(OrchestrationError):
            validate_manifest(old, self.coord)
        empty = copy.deepcopy(self.manifest)
        del empty["roles"][self.name]
        empty["custom_role_registry"] = None
        self.assertEqual(validate_manifest(empty, self.coord), empty)
        del empty["custom_role_registry"]
        with self.assertRaises(OrchestrationError):
            validate_manifest(empty, self.coord)

    def test_manifest_rejects_missing_misbound_and_authority_bearing_metadata(self):
        for field, value in (
            ("id", "custom-other"),
            ("contract", "reviewer"),
            ("contract", "implementer"),
            ("skills", None),
            ("body", "PRIVATE"),
        ):
            manifest = copy.deepcopy(self.manifest)
            manifest["roles"][self.name]["custom_role"][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(OrchestrationError),
            ):
                validate_manifest(manifest, self.coord)
        for field, value in (
            ("custom_role", None),
            ("tools", None),
            ("tools", "read,bash,grep,find,ls"),
            ("skills", []),
        ):
            manifest = copy.deepcopy(self.manifest)
            manifest["roles"][self.name][field] = value
            with self.assertRaises(OrchestrationError):
                validate_manifest(manifest, self.coord)
        for path in (None, "relative.json", str(self.project / "roles.json")):
            with self.assertRaises(OrchestrationError):
                validate_manifest(
                    {**self.manifest, "custom_role_registry": path}, self.coord
                )
        manifest = copy.deepcopy(self.manifest)
        manifest["roles"]["reviewer"]["custom_role"] = self.definition
        with self.assertRaises(OrchestrationError):
            validate_manifest(manifest, self.coord)

    def test_manifest_bounds_roles_and_serialized_size(self):
        manifest = copy.deepcopy(self.manifest)
        del manifest["roles"][self.name]
        for index in range(8):
            name = f"custom-case-{index}"
            manifest["roles"][name] = {
                **copy.deepcopy(self.role),
                "pane_id": f"%{index + 4}",
                "session_id": name,
                "session_dir": str(self.coord / "sessions" / name),
                "custom_role": {**copy.deepcopy(self.definition), "id": name},
            }
        self.assertEqual(validate_manifest(manifest, self.coord), manifest)
        ninth = copy.deepcopy(manifest["roles"]["custom-case-0"])
        ninth["custom_role"]["id"] = "custom-extra"
        manifest["roles"]["custom-extra"] = ninth
        with self.assertRaises(OrchestrationError):
            validate_manifest(manifest, self.coord)
        del manifest["roles"]["custom-extra"]
        for name, role in manifest["roles"].items():
            if name.startswith("custom-"):
                role["provider"] = role["model"] = "😀" * 256
                role["custom_role"]["prompt"]["path"] = "/" + "a" * 990 + "/prompt.md"
                role["custom_role"]["skills"] = [
                    {"path": "/" + "a" * 990 + f"/skill-{index}.md", "sha256": "a" * 64}
                    for index in range(4)
                ]
        self.assertGreater(
            len(json.dumps(manifest, indent=2).encode()), MAX_MANIFEST_BYTES
        )
        with self.assertRaisesRegex(OrchestrationError, "safety limit"):
            save_manifest(self.coord, manifest)
        self.assertEqual(load_manifest(self.coord), self.manifest)

    def test_manifest_reader_rejects_ambiguous_and_non_utf8_bindings(self):
        original = json.dumps(self.manifest)
        for content in (
            original.replace(
                '"contract": "probe"', '"contract": "reviewer", "contract": "probe"'
            ).encode(),
            original.replace('"version": 6', '"version": 5, "version": 6').encode(),
            original.encode("utf-16"),
            ("[" * 2000).encode(),
        ):
            (self.coord / "manifest.json").write_bytes(content)
            with self.assertRaises(OrchestrationError):
                load_manifest(self.coord)

    def test_launch_pins_the_registry_despite_changed_ambient_defaults(self):
        with mock.patch.dict(
            os.environ,
            {
                "PI_TMUX_ORCHESTRATOR_ROLE_REGISTRY": str(
                    self.project / "untrusted.json"
                )
            },
        ):
            verified = verify_custom_role(self.manifest, self.name)
        self.assertEqual(verified.contract, "probe")
        self.assertIn("PRIVATE_GUIDANCE_CANARY", verified.prompt)
        self.assertNotIn("PRIVATE_", repr(verified))

    def test_changed_contract_path_skill_list_and_missing_role_fail_closed(self):
        original = copy.deepcopy(self.definition)
        for field, value in (
            ("contract", "django"),
            ("id", "custom-other"),
            ("skills", []),
            ("prompt", self.resource("replacement.md", "PRIVATE_GUIDANCE_CANARY\n")),
        ):
            self.definition = {**original, field: value}
            self.save_registry()
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(OrchestrationError, "definition changed"),
            ):
                verify_custom_role(self.manifest, self.name)
        self.definition = original
        self.save_registry()

    def test_changed_digest_permissions_symlinks_and_hardlinks_fail_on_revalidation(
        self,
    ):
        path = Path(self.prompt["path"])
        body = path.read_text()
        path.write_text("unreviewed")
        with self.assertRaises(OrchestrationError):
            verify_custom_role(self.manifest, self.name)
        path.write_text(body)
        path.chmod(0o622)
        with self.assertRaises(OrchestrationError):
            verify_custom_role(self.manifest, self.name)
        path.chmod(0o600)
        target = self.root / "other.md"
        target.write_text(body)
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(OrchestrationError):
            verify_custom_role(self.manifest, self.name)
        path.unlink()
        os.link(target, path)
        with self.assertRaises(OrchestrationError):
            verify_custom_role(self.manifest, self.name)

    def test_launch_uses_private_verified_snapshots_not_mutable_source_paths(self):
        command = []
        self.assertEqual(
            prepare_worker_resources(
                command,
                self.coord,
                self.manifest,
                self.name,
                runtime.WORKER_EXTENSION_PATH,
            ),
            "probe",
        )
        self.assertIn("--no-extensions", command)
        self.assertIn("--no-skills", command)
        self.assertIn("--no-prompt-templates", command)
        self.assertNotIn("--no-context-files", command)
        self.assertNotIn(self.skill["path"], command)
        self.assertNotIn(self.prompt["path"], command)
        system = Path(command[command.index("--system-prompt") + 1])
        skill = Path(command[command.index("--skill") + 1])
        for source in (self.prompt, self.skill):
            Path(source["path"]).write_text("UNREVIEWED")
        self.assertIn(f"Role: `{self.name}`", system.read_text())
        self.assertIn("PRIVATE_GUIDANCE_CANARY", system.read_text())
        self.assertIn("PRIVATE_SKILL_CANARY", skill.read_text())
        for path in (system, skill):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(path.parent, self.coord)
        before = system.read_bytes()
        with self.assertRaises(OrchestrationError):
            prepare_worker_resources(
                [], self.coord, self.manifest, self.name, runtime.WORKER_EXTENSION_PATH
            )
        self.assertEqual(system.read_bytes(), before)

    def test_launch_detects_changes_between_registry_validation_and_snapshot_read(self):
        from pi_tmux_orchestrator import custom_role_resources

        original = custom_role_resources.load_registry

        def raced(*args):
            loaded = original(*args)
            Path(self.prompt["path"]).write_text("unreviewed after registry validation")
            return loaded

        with mock.patch.object(
            custom_role_resources, "load_registry", side_effect=raced
        ):
            with self.assertRaisesRegex(OrchestrationError, "reviewed digest"):
                prepare_worker_resources(
                    [],
                    self.coord,
                    self.manifest,
                    self.name,
                    runtime.WORKER_EXTENSION_PATH,
                )
        self.assertFalse((self.coord / f"{self.name}.system.md").exists())

    def test_snapshot_symlinks_do_not_overwrite_external_files(self):
        target = self.root / "unrelated.md"
        target.write_text("preserve")
        snapshot = self.coord / f"{self.name}.system.md"
        snapshot.symlink_to(target)
        with self.assertRaises(OrchestrationError):
            prepare_worker_resources(
                [], self.coord, self.manifest, self.name, runtime.WORKER_EXTENSION_PATH
            )
        self.assertEqual(target.read_text(), "preserve")

    def test_restart_failure_precedes_manifest_write_handover_and_respawn(self):
        self.registry.unlink()
        before = (self.coord / "manifest.json").read_bytes()
        with (
            mock.patch.object(
                commands,
                "resolve_session",
                return_value=(self.manifest["session"], self.coord),
            ),
            mock.patch.object(commands, "broker_control_request") as control,
            mock.patch.object(commands, "tmux") as tmux,
            mock.patch.object(commands, "validate_model") as validate,
        ):
            with self.assertRaises(OrchestrationError):
                commands.restart_command(
                    argparse.Namespace(
                        yes=True,
                        session=self.manifest["session"],
                        role=self.name,
                        provider="changed",
                        model=None,
                        thinking=None,
                        skip_model_check=False,
                    )
                )
        control.assert_not_called()
        tmux.assert_not_called()
        validate.assert_not_called()
        self.assertEqual((self.coord / "manifest.json").read_bytes(), before)

    def test_both_launchers_derive_contract_and_read_only_tools_from_verified_binding(
        self,
    ):
        class CapturedLaunch(Exception):
            pass

        for transport in ("tui", "rpc"):
            self.manifest["transport"] = transport
            save_manifest(self.coord, self.manifest)
            with (
                mock.patch.dict(
                    os.environ, {"PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT": "reviewer"}
                ),
                mock.patch.object(commands, "broker_role_generation", return_value=1),
                mock.patch.object(commands, "worker_guardrail_policy", return_value={}),
                mock.patch.object(
                    commands, "worker_context_mode", return_value="prune"
                ),
                mock.patch.object(
                    rpc_supervisor, "worker_guardrail_policy", return_value={}
                ),
                mock.patch.object(
                    rpc_supervisor, "worker_context_mode", return_value="prune"
                ),
                mock.patch.object(commands, "command_path", return_value="pi"),
                mock.patch.object(rpc_supervisor, "command_path", return_value="pi"),
                mock.patch.object(commands.os, "chdir"),
                mock.patch.object(
                    commands.os, "execvpe", side_effect=CapturedLaunch
                ) as execute,
                mock.patch.object(
                    rpc_supervisor.subprocess, "Popen", side_effect=CapturedLaunch
                ) as popen,
            ):
                with self.assertRaises(CapturedLaunch):
                    commands.run_agent_command(
                        argparse.Namespace(
                            state_root=str(runtime.STATE_ROOT),
                            coord=str(self.coord),
                            role=self.name,
                        )
                    )
            if transport == "tui":
                argv, environment = execute.call_args.args[1:]
                popen.assert_not_called()
            else:
                argv = popen.call_args.args[0]
                environment = popen.call_args.kwargs["env"]
                execute.assert_not_called()
            self.assertEqual(environment["PI_TMUX_ORCHESTRATOR_ROLE"], self.name)
            self.assertEqual(
                environment["PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT"], "probe"
            )
            self.assertEqual(
                argv[argv.index("--tools") + 1], "read,grep,find,ls,orchestrator_report"
            )
            self.assertIn("--no-extensions", argv)
            self.assertNotIn("PRIVATE_", json.dumps(environment))
            self.assertNotIn("PRIVATE_", json.dumps(argv))

    def test_revoked_resources_never_reach_either_process_launch(self):
        self.registry.unlink()
        for transport in ("tui", "rpc"):
            self.manifest["transport"] = transport
            save_manifest(self.coord, self.manifest)
            with (
                mock.patch.object(commands, "broker_role_generation", return_value=1),
                mock.patch.object(commands, "command_path", return_value="pi"),
                mock.patch.object(rpc_supervisor, "command_path", return_value="pi"),
                mock.patch.object(commands.os, "execvpe") as execute,
                mock.patch.object(rpc_supervisor.subprocess, "Popen") as popen,
            ):
                with self.assertRaisesRegex(OrchestrationError, "resource is missing"):
                    commands.run_agent_command(
                        argparse.Namespace(
                            state_root=str(runtime.STATE_ROOT),
                            coord=str(self.coord),
                            role=self.name,
                        )
                    )
            execute.assert_not_called()
            popen.assert_not_called()

    def test_custom_manifest_cannot_accidentally_activate_unimplemented_broker_routing(
        self,
    ):
        before = set(self.coord.rglob("*"))
        with self.assertRaisesRegex(OrchestrationError, "routing is not enabled"):
            Broker(self.coord, self.manifest)
        with self.assertRaisesRegex(OrchestrationError, "routing is not enabled"):
            initialize_broker_run(self.coord, self.manifest, "synthetic", {})
        self.assertEqual(set(self.coord.rglob("*")), before)
