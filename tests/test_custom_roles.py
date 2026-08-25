from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator import commands, custom_roles
from pi_tmux_orchestrator.cli import build_parser
from pi_tmux_orchestrator.models import OrchestrationError


class CustomRoleRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.resources = self.root / "resources"
        self.resources.mkdir()
        self.prompt_body = (
            "PRIVATE_CUSTOM_PROMPT_CANARY_59\nRead-only audit guidance.\n"
        )
        self.skill_body = "# Reviewed skill\n\nInspect synthetic evidence only.\n"
        self.prompt = self.write_resource(
            self.resources / "prompt.md", self.prompt_body, 0o600
        )
        self.skill = self.write_resource(
            self.resources / "skill.md", self.skill_body, 0o644
        )
        self.registry_path = self.root / "tmux-orchestrator-roles.json"

    def write_resource(self, path: Path, body: str, mode: int) -> Path:
        path.write_text(body, encoding="utf-8")
        path.chmod(mode)
        return path

    def resource(self, path: Path) -> dict[str, str]:
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def role(
        self,
        role_id: str = "security-auditor",
        *,
        prompt: dict[str, str] | None = None,
        skills: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        return {
            "id": role_id,
            "description": "Independent security evidence specialist.",
            "assignmentRule": "Use only for bounded security-relevant review evidence.",
            "prompt": prompt if prompt is not None else self.resource(self.prompt),
            "skills": skills if skills is not None else [self.resource(self.skill)],
        }

    def registry(
        self, roles: list[dict[str, object]] | None = None
    ) -> dict[str, object]:
        return {
            "version": custom_roles.CUSTOM_ROLE_REGISTRY_VERSION,
            "roles": roles if roles is not None else [self.role()],
        }

    def write_registry(self, value: object, *, mode: int = 0o600) -> Path:
        self.registry_path.write_text(json.dumps(value), encoding="utf-8")
        self.registry_path.chmod(mode)
        return self.registry_path

    def test_absent_registry_preserves_empty_compatibility_state(self) -> None:
        missing = self.root / "missing.json"
        loaded = custom_roles.load_custom_role_registry(missing, project=self.project)
        self.assertEqual(loaded, custom_roles.empty_custom_role_registry())
        self.assertEqual(
            custom_roles.public_custom_role_registry(loaded),
            {
                "version": 1,
                "configured": False,
                "count": 0,
                "names": [],
                "roles": [],
                "lifecycle": "registry-only",
                "launchable": False,
            },
        )

    def test_default_path_environment_precedence_and_registry_symlinks_are_strict(
        self,
    ) -> None:
        pi_home = self.root / "pi-home"
        pi_home.mkdir()
        default_path = pi_home / "tmux-orchestrator-roles.json"
        explicit_path = self.root / "explicit-roles.json"
        with (
            mock.patch.object(custom_roles.runtime, "PI_HOME", pi_home),
            mock.patch.dict(
                os.environ,
                {custom_roles.CUSTOM_ROLE_REGISTRY_ENV: ""},
            ),
        ):
            self.assertEqual(custom_roles.custom_role_registry_path(), default_path)
        with mock.patch.dict(
            os.environ,
            {custom_roles.CUSTOM_ROLE_REGISTRY_ENV: str(explicit_path)},
        ):
            self.assertEqual(custom_roles.custom_role_registry_path(), explicit_path)
        for invalid_path in (
            "relative/roles.json",
            str(self.root / "nested" / ".." / "roles.json"),
            str(self.root / "roles.txt"),
            "/" + "x" * custom_roles.MAX_CUSTOM_ROLE_PATH_CHARS + ".json",
        ):
            with (
                self.subTest(path=invalid_path),
                mock.patch.dict(
                    os.environ,
                    {custom_roles.CUSTOM_ROLE_REGISTRY_ENV: invalid_path},
                ),
                self.assertRaisesRegex(OrchestrationError, "absolute"),
            ):
                custom_roles.custom_role_registry_path()

        target = self.write_registry(self.registry())
        linked = self.root / "linked-roles.json"
        linked.symlink_to(target)
        with self.assertRaisesRegex(OrchestrationError, "opened safely"):
            custom_roles.load_custom_role_registry(linked, project=self.project)

        real_parent = self.root / "real-registry-parent"
        real_parent.mkdir()
        nested_registry = real_parent / "roles.json"
        nested_registry.write_text(json.dumps(self.registry()), encoding="utf-8")
        nested_registry.chmod(0o600)
        linked_parent = self.root / "linked-registry-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(OrchestrationError, "canonical non-symlink"):
            custom_roles.load_custom_role_registry(
                linked_parent / "roles.json", project=self.project
            )

    def test_valid_registry_is_deterministic_digest_bound_and_body_free(self) -> None:
        second_prompt = self.write_resource(
            self.resources / "accessibility-prompt.md",
            "Private accessibility review instructions.\n",
            0o600,
        )
        value = self.registry(
            [
                self.role(),
                self.role(
                    "accessibility-auditor",
                    prompt=self.resource(second_prompt),
                    skills=[],
                ),
            ]
        )
        loaded = custom_roles.load_custom_role_registry(
            self.write_registry(value), project=self.project
        )
        self.assertEqual(
            [role["id"] for role in loaded["roles"]],
            ["accessibility-auditor", "security-auditor"],
        )
        self.assertTrue(all(role["launchable"] is False for role in loaded["roles"]))
        self.assertTrue(
            all(role["contract"] == "read-only-specialist" for role in loaded["roles"])
        )
        serialized = json.dumps(loaded)
        self.assertNotIn(self.prompt_body, serialized)
        self.assertNotIn(self.skill_body, serialized)

        public = custom_roles.public_custom_role_registry(loaded)
        public_text = json.dumps(public)
        self.assertEqual(public["count"], 2)
        self.assertEqual(public["names"], ["accessibility-auditor", "security-auditor"])
        self.assertNotIn(str(self.prompt), public_text)
        self.assertNotIn(str(self.skill), public_text)
        self.assertNotIn("description", public_text)
        self.assertNotIn("assignment", public_text)
        self.assertRegex(public["roles"][1]["prompt_sha256"], r"^[0-9a-f]{64}$")

    def test_schema_version_duplicates_and_legacy_shapes_fail_closed(self) -> None:
        for value in (
            {"version": True, "roles": []},
            {"version": 0, "roles": []},
            {"version": "1", "roles": []},
            {"version": 1, "roles": {}, "defaults": {}},
            {"version": 1, "roles": {}, "legacyRoles": []},
            {"version": 1},
            {"version": 1, "roles": [], "provider": "forbidden"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(OrchestrationError):
                    custom_roles.validate_custom_role_registry(
                        value, project=self.project
                    )

        duplicate = '{"version":1,"version":1,"roles":[]}'
        self.registry_path.write_text(duplicate, encoding="utf-8")
        self.registry_path.chmod(0o600)
        with self.assertRaisesRegex(OrchestrationError, "strict UTF-8 JSON"):
            custom_roles.load_custom_role_registry(
                self.registry_path, project=self.project
            )

        duplicate_ids = self.registry([self.role(), self.role()])
        with self.assertRaisesRegex(OrchestrationError, "unique"):
            custom_roles.validate_custom_role_registry(
                duplicate_ids, project=self.project
            )

    def test_definition_fields_cannot_select_authority_tools_commands_or_models(
        self,
    ) -> None:
        forbidden_fields = (
            "authority",
            "tools",
            "command",
            "provider",
            "model",
            "thinking",
            "profile",
            "reviewer",
            "implementer",
            "endpoint",
            "apiKey",
        )
        for field in forbidden_fields:
            with self.subTest(field=field):
                role = self.role()
                role[field] = "forbidden"
                with self.assertRaises(OrchestrationError):
                    custom_roles.validate_custom_role_registry(
                        self.registry([role]), project=self.project
                    )

    def test_all_identifier_text_count_and_serialized_bounds_are_enforced(self) -> None:
        invalid_ids = (
            "ab",
            "UPPERCASE",
            "trailing-",
            "double--hyphen",
            "a" * (custom_roles.MAX_CUSTOM_ROLE_ID_CHARS + 1),
            "security_auditor",
            "sécurity-auditor",
        )
        for role_id in invalid_ids:
            with self.subTest(role_id=role_id):
                with self.assertRaises(OrchestrationError):
                    custom_roles.validate_custom_role_registry(
                        self.registry([self.role(role_id)]), project=self.project
                    )

        too_many = [self.role(f"audit-{index:02d}") for index in range(20)]
        with self.assertRaisesRegex(OrchestrationError, "at most"):
            custom_roles.validate_custom_role_registry(
                self.registry(too_many), project=self.project
            )

        role = self.role()
        role["description"] = "d" * (custom_roles.MAX_CUSTOM_ROLE_DESCRIPTION_CHARS + 1)
        with self.assertRaises(OrchestrationError):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )
        role = self.role()
        role["assignmentRule"] = "r" * (custom_roles.MAX_CUSTOM_ROLE_RULE_CHARS + 1)
        with self.assertRaises(OrchestrationError):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )
        role = self.role()
        role["skills"] = [self.resource(self.skill)] * (
            custom_roles.MAX_CUSTOM_ROLE_SKILLS + 1
        )
        with self.assertRaisesRegex(OrchestrationError, "at most"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )
        role = self.role()
        role["prompt"] = [self.resource(self.prompt)]
        with self.assertRaises(OrchestrationError):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

        self.registry_path.write_bytes(
            b" " * (custom_roles.MAX_CUSTOM_ROLE_REGISTRY_BYTES + 1)
        )
        self.registry_path.chmod(0o600)
        with self.assertRaisesRegex(OrchestrationError, "64 KiB"):
            custom_roles.load_custom_role_registry(
                self.registry_path, project=self.project
            )

    def test_reserved_roles_aliases_controls_commands_and_prefixes_are_rejected(
        self,
    ) -> None:
        reserved = (
            "implementer",
            "reviewer",
            "probe",
            "playwright",
            "django",
            "writer",
            "security-reviewer",
            "broker",
            "monitor",
            "controller",
            "start",
            "snapshot",
            "orchestrate",
            "orchestrator",
            "builtin",
            "or-security",
            "tmux-auditor",
            "pi-auditor",
            "legacy-auditor",
        )
        for role_id in reserved:
            with self.subTest(role_id=role_id):
                with self.assertRaisesRegex(OrchestrationError, "reserved"):
                    custom_roles.custom_role_id(role_id)

    def test_unsafe_unicode_controls_and_unknown_resource_fields_are_rejected(
        self,
    ) -> None:
        for field, text in (
            ("description", "hidden\u202erole"),
            ("assignmentRule", "unsafe\x01rule"),
            ("description", "line\nbreak"),
        ):
            with self.subTest(field=field):
                role = self.role()
                role[field] = text
                with self.assertRaises(OrchestrationError):
                    custom_roles.validate_custom_role_registry(
                        self.registry([role]), project=self.project
                    )
        role = self.role()
        role["prompt"] = {**self.resource(self.prompt), "type": "prompt"}
        with self.assertRaisesRegex(OrchestrationError, "exactly"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

    def test_canonical_absolute_paths_symlink_components_and_extensions_are_strict(
        self,
    ) -> None:
        role = self.role()
        role["prompt"] = {
            "path": "relative/prompt.md",
            "sha256": self.resource(self.prompt)["sha256"],
        }
        with self.assertRaisesRegex(OrchestrationError, "canonical absolute"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

        linked_resources = self.root / "linked-resources"
        linked_resources.symlink_to(self.resources, target_is_directory=True)
        linked_prompt = linked_resources / self.prompt.name
        role = self.role(
            prompt={
                "path": str(linked_prompt),
                "sha256": self.resource(self.prompt)["sha256"],
            }
        )
        with self.assertRaisesRegex(OrchestrationError, "symlink components"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

        text_prompt = self.write_resource(
            self.resources / "prompt.txt", "Private prompt.\n", 0o600
        )
        with self.assertRaisesRegex(OrchestrationError, "Markdown"):
            custom_roles.validate_custom_role_registry(
                self.registry([self.role(prompt=self.resource(text_prompt))]),
                project=self.project,
            )

        oversized_path = "/" + "a" * custom_roles.MAX_CUSTOM_ROLE_PATH_CHARS + ".md"
        role = self.role(prompt={"path": oversized_path, "sha256": "0" * 64})
        with self.assertRaisesRegex(OrchestrationError, "oversized"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

    def test_target_project_registry_and_resource_paths_are_rejected(self) -> None:
        project_prompt = self.write_resource(
            self.project / "private-prompt.md", "Project prompt.\n", 0o600
        )
        with self.assertRaisesRegex(OrchestrationError, "outside the target project"):
            custom_roles.validate_custom_role_registry(
                self.registry([self.role(prompt=self.resource(project_prompt))]),
                project=self.project,
            )

        project_registry = self.project / "roles.json"
        project_registry.write_text(json.dumps(self.registry()), encoding="utf-8")
        project_registry.chmod(0o600)
        with self.assertRaisesRegex(OrchestrationError, "outside the target project"):
            custom_roles.load_custom_role_registry(
                project_registry, project=self.project
            )

    def test_ownership_and_mode_policy_rejects_unsafe_resources_and_registry(
        self,
    ) -> None:
        self.prompt.chmod(0o640)
        with self.assertRaisesRegex(OrchestrationError, "private mode"):
            custom_roles.validate_custom_role_registry(
                self.registry(), project=self.project
            )
        self.prompt.chmod(0o600)
        self.skill.chmod(0o666)
        with self.assertRaisesRegex(OrchestrationError, "group- or world-writable"):
            custom_roles.validate_custom_role_registry(
                self.registry(), project=self.project
            )
        self.skill.chmod(0o644)
        self.prompt.chmod(0o700)
        with self.assertRaisesRegex(OrchestrationError, "non-executable"):
            custom_roles.validate_custom_role_registry(
                self.registry(), project=self.project
            )
        self.prompt.chmod(0o4600)
        with self.assertRaisesRegex(OrchestrationError, "special mode"):
            custom_roles.validate_custom_role_registry(
                self.registry(), project=self.project
            )
        self.prompt.chmod(0o600)

        with mock.patch.object(
            custom_roles, "_current_uid", return_value=self.prompt.stat().st_uid + 1
        ):
            with self.assertRaisesRegex(OrchestrationError, "owned"):
                custom_roles.validate_custom_role_registry(
                    self.registry(), project=self.project
                )

        self.write_registry(self.registry(), mode=0o666)
        with self.assertRaisesRegex(OrchestrationError, "not group- or world-writable"):
            custom_roles.load_custom_role_registry(
                self.registry_path, project=self.project
            )

    def test_size_type_digest_and_body_safety_are_enforced_without_body_errors(
        self,
    ) -> None:
        oversized = self.resources / "oversized.md"
        oversized.write_bytes(b"x" * (custom_roles.MAX_CUSTOM_ROLE_PROMPT_BYTES + 1))
        oversized.chmod(0o600)
        with self.assertRaisesRegex(OrchestrationError, "size limit"):
            custom_roles.validate_custom_role_registry(
                self.registry([self.role(prompt=self.resource(oversized))]),
                project=self.project,
            )

        directory = self.resources / "directory.md"
        directory.mkdir()
        with self.assertRaisesRegex(OrchestrationError, "regular"):
            custom_roles.validate_custom_role_registry(
                self.registry(
                    [self.role(prompt={"path": str(directory), "sha256": "0" * 64})]
                ),
                project=self.project,
            )

        changed_canary = "CHANGED_PRIVATE_BODY_CANARY_59"
        configured = self.registry()
        self.prompt.write_text(changed_canary + "\n", encoding="utf-8")
        with self.assertRaises(OrchestrationError) as raised:
            custom_roles.validate_custom_role_registry(configured, project=self.project)
        self.assertIn("digest changed", str(raised.exception))
        self.assertNotIn(changed_canary, str(raised.exception))

        self.prompt.write_bytes(b"safe\x00hidden")
        digest = hashlib.sha256(self.prompt.read_bytes()).hexdigest()
        role = self.role(prompt={"path": str(self.prompt), "sha256": digest})
        with self.assertRaisesRegex(OrchestrationError, "unsafe Unicode"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

    def test_resource_paths_cannot_be_duplicated_or_shared(self) -> None:
        role = self.role(skills=[self.resource(self.prompt)])
        with self.assertRaisesRegex(OrchestrationError, "duplicated or shared"):
            custom_roles.validate_custom_role_registry(
                self.registry([role]), project=self.project
            )

        second_prompt = self.write_resource(
            self.resources / "second.md", "Second private prompt.\n", 0o600
        )
        roles = [
            self.role("security-auditor"),
            self.role("accessibility-auditor", prompt=self.resource(second_prompt)),
        ]
        with self.assertRaisesRegex(OrchestrationError, "duplicated or shared"):
            custom_roles.validate_custom_role_registry(
                self.registry(roles), project=self.project
            )

    def test_start_preflight_surfaces_registry_only_metadata_and_never_bodies(
        self,
    ) -> None:
        self.write_registry(self.registry())
        args = build_parser().parse_args(
            [
                "start",
                "--project",
                str(self.project),
                "--task",
                "Synthetic provider-free validation.",
                "--skip-model-check",
                "--dry-run",
            ]
        )
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {custom_roles.CUSTOM_ROLE_REGISTRY_ENV: str(self.registry_path)},
            ),
            mock.patch.object(commands, "command_path", return_value="/usr/bin/true"),
            mock.patch.object(commands, "session_exists", return_value=False),
            redirect_stdout(output),
        ):
            result = commands.start_command(args)
        metadata = result.data["custom_role_registry"]
        self.assertEqual(metadata["names"], ["security-auditor"])
        self.assertEqual(metadata["lifecycle"], "registry-only")
        self.assertFalse(metadata["launchable"])
        rendered = output.getvalue() + json.dumps(result.data)
        self.assertNotIn(self.prompt_body, rendered)
        self.assertNotIn(self.skill_body, rendered)
        self.assertNotIn(str(self.prompt), rendered)
        self.assertIn(self.resource(self.prompt)["sha256"], rendered)
        self.assertIn("not launchable", output.getvalue())

    def test_invalid_registry_fails_before_tmux_or_provider_preflight(self) -> None:
        value = self.registry()
        value["roles"][0]["prompt"]["sha256"] = "0" * 64
        self.write_registry(value)
        args = build_parser().parse_args(
            [
                "start",
                "--project",
                str(self.project),
                "--task",
                "Synthetic invalid registry.",
                "--skip-model-check",
                "--dry-run",
            ]
        )
        with (
            mock.patch.dict(
                os.environ,
                {custom_roles.CUSTOM_ROLE_REGISTRY_ENV: str(self.registry_path)},
            ),
            mock.patch.object(commands, "command_path") as command_path,
            mock.patch.object(commands, "session_exists") as session_exists,
            self.assertRaisesRegex(OrchestrationError, "digest changed"),
        ):
            commands.start_command(args)
        command_path.assert_not_called()
        session_exists.assert_not_called()

    def test_existing_start_and_control_surfaces_still_reject_custom_role_ids(
        self,
    ) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "send",
                        "pi-test",
                        "--role",
                        "security-auditor",
                        "--message",
                        "x",
                    ]
                )
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["restart", "pi-test", "--role", "security-auditor", "--yes"]
                )
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "start",
                        "--task",
                        "synthetic",
                        "--worker-skill",
                        f"security-auditor={self.skill}",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
