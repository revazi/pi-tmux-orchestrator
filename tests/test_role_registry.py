"""Model-free strict registry, filesystem boundary, and CLI regressions."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import cli, role_registry, runtime
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.registry_resources import read_global_resource
from pi_tmux_orchestrator.role_registry import load_registry, validate_registry


class RoleRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.registry = self.root / "registry.json"
        self.enterContext(mock.patch.object(runtime, "PI_HOME", self.root))
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop(role_registry.REGISTRY_ENV, None)
        self.enterContext(mock.patch.object(runtime, "JSON_MODE", True))
        self.prompt = self.resource(
            "prompt.md", "PRIVATE_PROMPT_CANARY: inspect without writing.\n"
        )
        self.skill = self.resource(
            "skill.md", "PRIVATE_SKILL_CANARY: reviewed guidance.\n"
        )
        self.definition = {
            "version": 1,
            "roles": [
                {
                    "id": "custom-security",
                    "contract": "probe",
                    "prompt": self.prompt,
                    "skills": [self.skill],
                }
            ],
        }

    def resource(self, name, body):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o600)
        return {"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest()}

    def save(self, value=None):
        self.registry.write_text(
            json.dumps(self.definition if value is None else value), encoding="utf-8"
        )
        self.registry.chmod(0o600)
        return str(self.registry)

    def test_absent_default_is_empty_but_explicit_missing_files_fail(self):
        result = load_registry(self.project)
        self.assertEqual(result["roles"], [])
        self.assertFalse(result["configured"])
        self.assertFalse(result["launch_supported"])
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(self.registry))
        os.environ[role_registry.REGISTRY_ENV] = str(self.registry)
        with self.assertRaises(OrchestrationError):
            load_registry(self.project)

    def test_valid_registry_returns_only_metadata_and_explicit_precedence(self):
        os.environ[role_registry.REGISTRY_ENV] = "/missing/ignored.json"
        result = load_registry(self.project, self.save())
        self.assertEqual(result["roles"], self.definition["roles"])
        self.assertTrue(result["configured"])
        self.assertFalse(result["launch_supported"])
        self.assertNotIn("PRIVATE_PROMPT_CANARY", json.dumps(result))
        self.assertNotIn("PRIVATE_SKILL_CANARY", json.dumps(result))
        os.environ[role_registry.REGISTRY_ENV] = str(self.registry)
        self.assertEqual(load_registry(self.project), result)
        value = copy.deepcopy(self.definition)
        del value["roles"][0]["skills"]
        self.assertEqual(
            validate_registry(value, self.project)["roles"][0]["skills"], []
        )

    def test_versions_unknown_fields_credentials_and_write_authority_are_rejected(self):
        values = [
            None,
            [],
            {},
            {"version": True, "roles": []},
            {"version": 0, "roles": []},
            {"version": 2, "roles": []},
        ]
        for field in (
            "provider",
            "model",
            "apiKey",
            "tools",
            "write",
            "authority",
            "report_schema",
        ):
            root = copy.deepcopy(self.definition)
            root[field] = "forbidden"
            values.append(root)
            role = copy.deepcopy(self.definition)
            role["roles"][0][field] = "forbidden"
            values.append(role)
        for value in values:
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                validate_registry(value, self.project)

    def test_identifiers_contracts_and_counts_are_bounded(self):
        for identifier in (
            "implementer",
            "reviewer",
            "custom-reviewer",
            "custom-writer",
            "custom-parent",
            "custom-broker",
            "custom-foo:1",
            "custom-foo.bar",
            "custom-foo--bar",
            "custom-",
            "custom-" + "x" * 26,
        ):
            value = copy.deepcopy(self.definition)
            value["roles"][0]["id"] = identifier
            with (
                self.subTest(identifier=identifier),
                self.assertRaises(OrchestrationError),
            ):
                validate_registry(value, self.project)
        for contract in ("implementer", "reviewer", "write", None, {}):
            value = copy.deepcopy(self.definition)
            value["roles"][0]["contract"] = contract
            with self.assertRaises(OrchestrationError):
                validate_registry(value, self.project)
        for contract in ("probe", "playwright", "django"):
            value = copy.deepcopy(self.definition)
            value["roles"][0]["contract"] = contract
            self.assertEqual(
                validate_registry(value, self.project)["roles"][0]["contract"], contract
            )
        for count in (2, 9):
            with self.assertRaises(OrchestrationError):
                validate_registry(
                    {"version": 1, "roles": self.definition["roles"] * count},
                    self.project,
                )

    def test_duplicate_json_fields_and_invalid_utf8_fail_before_resource_reads(self):
        for raw in (
            b'{"version":1,"roles":[],"roles":[]}',
            b'{"version":1,"roles":[{"id":"a","id":"b"}]}',
            b"\xff",
            b" ",
        ):
            self.registry.write_bytes(raw)
            with (
                self.subTest(raw=raw),
                mock.patch.object(role_registry, "_verify_resource") as verify,
            ):
                with self.assertRaises(OrchestrationError):
                    load_registry(self.project, str(self.registry))
                verify.assert_not_called()

    def test_invalid_paths_and_resource_metadata_are_rejected_before_reads(self):
        for path in (
            "relative.md",
            str(self.project / "local.md"),
            str(self.root / "a/../prompt.md"),
            str(self.root) + "//prompt.md",
            str(self.root / "prompt.txt"),
            "/" + "x" * 1024,
            str(self.root) + "/bad\n.md",
        ):
            value = copy.deepcopy(self.definition)
            value["roles"][0]["prompt"]["path"] = path
            with self.subTest(path=path), self.assertRaises(OrchestrationError):
                validate_registry(value, self.project)
        for digest in ("x" * 64, "A" * 64, "a" * 63, None):
            value = copy.deepcopy(self.definition)
            value["roles"][0]["prompt"]["sha256"] = digest
            with self.assertRaises(OrchestrationError):
                validate_registry(value, self.project)
        value = copy.deepcopy(self.definition)
        value["roles"][0]["skills"] = [self.prompt]
        with self.assertRaises(OrchestrationError):
            validate_registry(value, self.project)
        value["roles"][0]["skills"] = [self.skill] * 5
        with self.assertRaises(OrchestrationError):
            validate_registry(value, self.project)

    def test_full_capacity_and_concurrent_file_change(self):
        roles = []
        for index in range(role_registry.MAX_CUSTOM_ROLES):
            value = copy.deepcopy(self.definition["roles"][0])
            value["id"] = f"custom-check-{index}"
            roles.append(value)
        self.assertEqual(
            len(
                validate_registry({"version": 1, "roles": roles}, self.project)["roles"]
            ),
            8,
        )
        original_read = os.read
        changed = False

        def mutate_after_read(descriptor, size):
            nonlocal changed
            data = original_read(descriptor, size)
            if not changed:
                Path(self.prompt["path"]).write_text("changed while validating")
                changed = True
            return data

        with mock.patch(
            "pi_tmux_orchestrator.registry_resources.os.read",
            side_effect=mutate_after_read,
        ):
            with self.assertRaisesRegex(
                OrchestrationError, "changed during validation"
            ):
                read_global_resource(
                    Path(self.prompt["path"]), 1024, project=self.project
                )

    def test_resource_tampering_or_missing_resource_fails_on_every_load(self):
        self.save()
        self.assertTrue(load_registry(self.project, str(self.registry))["configured"])
        Path(self.prompt["path"]).write_text("changed after review")
        with self.assertRaisesRegex(OrchestrationError, "reviewed digest"):
            load_registry(self.project, str(self.registry))
        Path(self.prompt["path"]).unlink()
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(self.registry))

    def test_project_local_registry_symlinks_and_hardlinks_are_rejected(self):
        self.save()
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(self.project / "registry.json"))
        link = self.root / "link.json"
        link.symlink_to(self.registry)
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(link))
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(alias / "registry.json"))
        linked = self.root / "hardlink.json"
        os.link(self.registry, linked)
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(linked))

    def test_permissions_fifo_size_and_encoding_fail_closed(self):
        self.save()
        self.registry.chmod(0o666)
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(self.registry))
        self.registry.chmod(0o600)
        with mock.patch.object(role_registry, "MAX_REGISTRY_BYTES", 4):
            with self.assertRaises(OrchestrationError):
                load_registry(self.project, str(self.registry))
        with mock.patch.object(role_registry, "MAX_PROMPT_BYTES", 4):
            with self.assertRaises(OrchestrationError):
                load_registry(self.project, str(self.registry))
        fifo = self.root / "fifo.md"
        os.mkfifo(fifo)
        with self.assertRaises(OrchestrationError):
            read_global_resource(fifo, 1024, project=self.project)
        Path(self.prompt["path"]).write_bytes(b"\xff")
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(self.registry))

    def test_descriptor_identity_blocks_project_aliases(self):
        # The reader rejects the actual directory identity independently of the
        # lexical path filter (including case aliases and bind mounts).
        for project in (self.root, Path("/")):
            with self.assertRaisesRegex(
                OrchestrationError, "aliases the target project"
            ):
                read_global_resource(Path(self.prompt["path"]), 1024, project=project)
        missing = self.root / "missing-project"
        with self.assertRaisesRegex(OrchestrationError, "project is unavailable"):
            read_global_resource(
                self.root / "absent.json", 1024, project=missing, missing_ok=True
            )
        alias = self.project.with_name("PROJECT")
        if alias.exists() and alias.samefile(self.project):
            local = self.project / "registry.json"
            local.write_text(json.dumps(self.definition))
            with self.assertRaises(OrchestrationError):
                load_registry(self.project, str(alias / local.name))

    def test_untrusted_directory_and_foreign_owner_are_rejected(self):
        self.save()
        directory = self.root / "unsafe"
        directory.mkdir(mode=0o700)
        target = directory / "registry.json"
        target.write_bytes(self.registry.read_bytes())
        directory.chmod(0o777)
        with self.assertRaises(OrchestrationError):
            load_registry(self.project, str(target))
        with mock.patch(
            "pi_tmux_orchestrator.registry_resources.os.getuid",
            return_value=os.getuid() + 1,
        ):
            with self.assertRaises(OrchestrationError):
                load_registry(self.project, str(self.registry))

    def test_cli_metadata_only_and_builtin_start_role_choices_remain_closed(self):
        output = io.StringIO()
        errors = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "pi-tmux-agents",
                    "--json",
                    "role-registry",
                    "--project",
                    str(self.project),
                    "--registry",
                    self.save(),
                ],
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
            mock.patch("subprocess.Popen") as spawn,
        ):
            self.assertEqual(cli.main(), 0)
            spawn.assert_not_called()
        envelope = json.loads(output.getvalue())
        self.assertEqual(envelope["command"], "role-registry")
        self.assertTrue(envelope["success"])
        self.assertFalse(envelope["data"]["launch_supported"])
        self.assertNotIn("PRIVATE_PROMPT_CANARY", output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        with self.assertRaises(OrchestrationError):
            cli.build_parser().parse_args(["start", "--with-role", "custom-security"])
