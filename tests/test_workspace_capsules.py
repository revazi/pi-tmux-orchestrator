from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pi_tmux_orchestrator.cli import build_parser
from pi_tmux_orchestrator.commands import start_command
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.workspace_capsules import (
    MAX_WORKSPACE_CAPSULE_BYTES,
    MAX_WORKSPACE_INSTRUCTION_PATHS,
    MAX_WORKSPACE_MARKERS,
    MAX_WORKSPACE_RELEVANT_PATHS,
    TOP_LEVEL_MARKERS,
    construct_workspace_capsule,
    render_workspace_capsule,
    serialize_workspace_capsule,
    validate_workspace_capsule,
    workspace_capsule_metadata,
)


class WorkspaceRepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve() / "project"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.write("AGENTS.md", "# Synthetic governing instructions\n")
        self.write("pyproject.toml", "[build-system]\nrequires = []\n")
        self.write("src/service.py", "VALUE = 1\n")
        self.write("tests/test_service.py", "def test_value():\n    assert True\n")
        self.write("private/irrelevant-secret.txt", "SOURCE_BODY_CANARY\n")
        self.commit("initial")

    def git(self, *arguments: str) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
            }
        )
        result = subprocess.run(
            ["git", "-C", str(self.project), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        return result.stdout.strip()

    def write(self, relative: str, content: str) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def commit(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)


class WorkspaceCapsuleTests(WorkspaceRepositoryFixture):
    def test_construction_is_deterministic_bounded_and_content_free(self) -> None:
        first = construct_workspace_capsule(
            self.project,
            ["tests/test_service.py", "src/service.py"],
        )
        second = construct_workspace_capsule(
            self.project,
            ["src/service.py", "tests/test_service.py"],
        )
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["git"]["state"], "clean")
        self.assertEqual(first["markers"], ["pyproject.toml"])
        self.assertEqual(
            first["relevant_paths"], ["src/service.py", "tests/test_service.py"]
        )
        self.assertEqual(
            [item["path"] for item in first["instructions"]], ["AGENTS.md"]
        )
        serialized = serialize_workspace_capsule(first)
        self.assertLessEqual(
            len(serialized.encode("utf-8")), MAX_WORKSPACE_CAPSULE_BYTES
        )
        self.assertNotIn("SOURCE_BODY_CANARY", serialized)
        self.assertNotIn("irrelevant-secret", serialized)
        self.assertNotIn("Synthetic governing instructions", serialized)
        self.assertEqual(validate_workspace_capsule(first, self.project), first)

        metadata = workspace_capsule_metadata(first)
        self.assertEqual(metadata["validation"], "validated")
        self.assertEqual(metadata["relevant_path_count"], 2)
        self.assertNotIn("paths", metadata)
        self.assertEqual(workspace_capsule_metadata(None)["validation"], "disabled")

    def test_nested_governing_instruction_selection_follows_relevant_paths_only(
        self,
    ) -> None:
        self.write("src/AGENTS.md", "unused because override wins\n")
        self.write("src/AGENTS.override.md", "nested instructions\n")
        self.write("unrelated/AGENTS.md", "not governing supplied paths\n")
        self.commit("nested instructions")

        capsule = construct_workspace_capsule(self.project, ["src/service.py"])
        self.assertEqual(
            [item["path"] for item in capsule["instructions"]],
            ["AGENTS.md", "src/AGENTS.override.md"],
        )
        serialized = serialize_workspace_capsule(capsule)
        self.assertNotIn("src/AGENTS.md", serialized)
        self.assertNotIn("unrelated/AGENTS.md", serialized)

    def test_rendering_keeps_discovery_and_instruction_trust_explicit(self) -> None:
        capsule = construct_workspace_capsule(self.project, ["src/service.py"])
        rendered = render_workspace_capsule(capsule, self.project)
        self.assertIn("paths and hashes only", rendered)
        self.assertIn("neither authorizes access nor substitutes", rendered)
        self.assertIn("reading governing AGENTS.md/CLAUDE.md", rendered)
        self.assertNotIn("Synthetic governing instructions", rendered)

    def test_unknown_malformed_duplicate_and_ordered_fields_are_rejected(self) -> None:
        capsule = construct_workspace_capsule(self.project, ["src/service.py"])
        cases = []
        unknown = copy.deepcopy(capsule)
        unknown["unknown"] = True
        cases.append(unknown)
        unknown_git = copy.deepcopy(capsule)
        unknown_git["git"]["unknown"] = "x"
        cases.append(unknown_git)
        duplicate = copy.deepcopy(capsule)
        duplicate["relevant_paths"].append("src/service.py")
        cases.append(duplicate)
        unsorted = construct_workspace_capsule(
            self.project, ["src/service.py", "tests/test_service.py"]
        )
        unsorted["relevant_paths"].reverse()
        cases.append(unsorted)
        wrong_type = copy.deepcopy(capsule)
        wrong_type["schema_version"] = True
        cases.append(wrong_type)
        for value in cases:
            with (
                self.subTest(fields=list(value)),
                self.assertRaises(OrchestrationError),
            ):
                validate_workspace_capsule(value, self.project)

    def test_all_counts_and_serialized_utf8_bytes_are_bounded(self) -> None:
        paths = []
        for index in range(MAX_WORKSPACE_RELEVANT_PATHS + 1):
            relative = f"src/path-{index}.py"
            self.write(relative, "pass\n")
            paths.append(relative)
        with self.assertRaisesRegex(OrchestrationError, "count"):
            construct_workspace_capsule(self.project, paths)
        with self.assertRaisesRegex(OrchestrationError, "byte limit"):
            construct_workspace_capsule(self.project, ["😀" * 65])

        capsule = construct_workspace_capsule(self.project)
        too_many_instructions = copy.deepcopy(capsule)
        too_many_instructions["instructions"] = [
            {"path": f"d{index}/AGENTS.md", "sha256": "a" * 64}
            for index in range(MAX_WORKSPACE_INSTRUCTION_PATHS + 1)
        ]
        with self.assertRaises(OrchestrationError):
            validate_workspace_capsule(too_many_instructions, self.project)
        too_many_markers = copy.deepcopy(capsule)
        too_many_markers["markers"] = list(TOP_LEVEL_MARKERS) + ["extra"] * (
            MAX_WORKSPACE_MARKERS + 1
        )
        with self.assertRaises(OrchestrationError):
            validate_workspace_capsule(too_many_markers, self.project)

        oversized = copy.deepcopy(capsule)
        oversized["unknown"] = "😀" * MAX_WORKSPACE_CAPSULE_BYTES
        with self.assertRaises(OrchestrationError):
            serialize_workspace_capsule(oversized)

    def test_absolute_traversal_outside_missing_and_symlink_paths_are_rejected(
        self,
    ) -> None:
        outside = Path(self.temporary.name).resolve() / "outside.py"
        outside.write_text("outside\n", encoding="utf-8")
        linked = self.project / "src" / "linked.py"
        linked.symlink_to(outside)
        for path in ("../outside.py", str(outside), "src/linked.py", "missing.py"):
            with self.subTest(path=path), self.assertRaises(OrchestrationError):
                construct_workspace_capsule(self.project, [path])

    def test_symlinked_instruction_and_marker_are_rejected(self) -> None:
        instruction = self.project / "AGENTS.md"
        instruction.unlink()
        outside = Path(self.temporary.name).resolve() / "outside-agents.md"
        outside.write_text("outside\n", encoding="utf-8")
        instruction.symlink_to(outside)
        with self.assertRaisesRegex(OrchestrationError, "symlink"):
            construct_workspace_capsule(self.project)

        instruction.unlink()
        self.write("AGENTS.md", "restored\n")
        marker = self.project / "package.json"
        marker.symlink_to(outside)
        with self.assertRaisesRegex(OrchestrationError, "symlink"):
            construct_workspace_capsule(self.project)

    def test_stale_head_instruction_and_marker_identity_are_rejected(self) -> None:
        clean = construct_workspace_capsule(self.project, ["src/service.py"])
        self.assertEqual(clean["git"]["state"], "clean")

        # Clean/dirty is retained as initial metadata, but normal task edits must not
        # break late worker delivery or restart replay while HEAD and hints still match.
        self.write("src/service.py", "VALUE = 2\n")
        self.assertEqual(validate_workspace_capsule(clean, self.project), clean)
        dirty = construct_workspace_capsule(self.project, ["src/service.py"])
        self.assertEqual(dirty["git"]["state"], "dirty")
        self.write("src/service.py", "VALUE = 3\n")
        self.assertEqual(validate_workspace_capsule(dirty, self.project), dirty)
        self.write("scratch.txt", "untracked body changes are not serialized\n")
        self.assertEqual(validate_workspace_capsule(dirty, self.project), dirty)

        self.commit("source change")
        after_source_commit = construct_workspace_capsule(
            self.project, ["src/service.py"]
        )
        self.write("AGENTS.md", "changed instructions\n")
        with self.assertRaisesRegex(OrchestrationError, "stale"):
            validate_workspace_capsule(after_source_commit, self.project)
        self.commit("instruction change")
        with self.assertRaisesRegex(OrchestrationError, "stale"):
            validate_workspace_capsule(after_source_commit, self.project)

        head_identity = construct_workspace_capsule(self.project)
        self.write("README.md", "new commit\n")
        self.commit("head change")
        with self.assertRaisesRegex(OrchestrationError, "stale"):
            validate_workspace_capsule(head_identity, self.project)

        marker_identity = construct_workspace_capsule(self.project)
        self.write("package.json", "{}\n")
        with self.assertRaisesRegex(OrchestrationError, "stale"):
            validate_workspace_capsule(marker_identity, self.project)

    def test_project_must_be_canonical_git_root(self) -> None:
        with self.assertRaisesRegex(OrchestrationError, "Git worktree root"):
            construct_workspace_capsule(self.project / "src", ["service.py"])
        linked_root = Path(self.temporary.name).resolve() / "linked-project"
        linked_root.symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(OrchestrationError, "non-symlink"):
            construct_workspace_capsule(linked_root)

    def test_cli_workspace_start_rejects_symlinked_project_before_resolution(
        self,
    ) -> None:
        linked_root = Path(self.temporary.name).resolve() / "linked-start-project"
        linked_root.symlink_to(self.project, target_is_directory=True)
        arguments = build_parser().parse_args(
            [
                "start",
                "--project",
                str(linked_root),
                "--task",
                "Synthetic cold assignment",
                "--workspace-capsule",
                "--skip-model-check",
                "--dry-run",
            ]
        )
        with (
            mock.patch(
                "pi_tmux_orchestrator.commands.command_path",
                return_value="/synthetic/command",
            ),
            self.assertRaisesRegex(OrchestrationError, "non-symlink"),
        ):
            start_command(arguments)

    def test_instruction_size_limit_and_unknown_marker_are_rejected(self) -> None:
        self.write("AGENTS.md", "x" * (128 * 1024 + 1))
        with self.assertRaisesRegex(OrchestrationError, "byte limit"):
            construct_workspace_capsule(self.project)

        self.write("AGENTS.md", "restored\n")
        capsule = construct_workspace_capsule(self.project)
        capsule["markers"].append("unknown.lock")
        with self.assertRaisesRegex(OrchestrationError, "allowlisted"):
            validate_workspace_capsule(capsule, self.project)


if __name__ == "__main__":
    unittest.main()
