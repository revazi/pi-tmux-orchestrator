"""Deterministic, bounded, ephemeral workspace discovery capsules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from .models import OrchestrationError

WORKSPACE_CAPSULE_SCHEMA_VERSION = 1
MAX_WORKSPACE_CAPSULE_BYTES = 8 * 1024
MAX_WORKSPACE_RELEVANT_PATHS = 16
MAX_WORKSPACE_INSTRUCTION_PATHS = 16
MAX_WORKSPACE_MARKERS = 16
MAX_WORKSPACE_PATH_BYTES = 256
MAX_WORKSPACE_ROOT_BYTES = 1024
MAX_WORKSPACE_INSTRUCTION_BYTES = 128 * 1024
MAX_WORKSPACE_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 5

INSTRUCTION_CANDIDATES = (
    "AGENTS.override.md",
    "AGENTS.md",
    "AGENTS.MD",
    "CLAUDE.md",
    "CLAUDE.MD",
)
TOP_LEVEL_MARKERS = (
    "Cargo.toml",
    "Makefile",
    "Taskfile.yml",
    "build.gradle",
    "build.gradle.kts",
    "deno.json",
    "go.mod",
    "justfile",
    "package.json",
    "pnpm-workspace.yaml",
    "pom.xml",
    "pyproject.toml",
    "pytest.ini",
    "requirements.txt",
    "setup.cfg",
    "tox.ini",
)

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_HEAD_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _invalid(message: str) -> OrchestrationError:
    return OrchestrationError(message, "workspace_capsule_invalid")


def canonical_project_root(project: Path) -> Path:
    """Return a canonical, non-symlink project root or fail closed."""

    expanded = Path(project).expanduser()
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    try:
        metadata = absolute.lstat()
        canonical = absolute.resolve(strict=True)
    except OSError as error:
        raise _invalid("Workspace capsule project root is unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _invalid("Workspace capsule project root must be a non-symlink directory")
    if canonical != absolute:
        raise _invalid("Workspace capsule project root must be canonical")
    try:
        encoded = os.fspath(canonical).encode("utf-8")
    except UnicodeEncodeError as error:
        raise _invalid("Workspace capsule project root is not valid UTF-8") from error
    if len(encoded) > MAX_WORKSPACE_ROOT_BYTES:
        raise _invalid("Workspace capsule project root exceeds the byte limit")
    return canonical


def _validate_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"Workspace capsule {label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _invalid(f"Workspace capsule {label} is not valid UTF-8") from error
    if len(encoded) > MAX_WORKSPACE_PATH_BYTES:
        raise _invalid(f"Workspace capsule {label} exceeds the byte limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid(f"Workspace capsule {label} contains a control character")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith(("/", "\\")):
        raise _invalid(f"Workspace capsule {label} must be project-relative")
    if value != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise _invalid(f"Workspace capsule {label} is not normalized")
    return value


def _safe_project_path(
    root: Path,
    relative: str,
    label: str,
    *,
    regular_file: bool = False,
) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise _invalid(f"Workspace capsule {label} is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise _invalid(f"Workspace capsule {label} cannot use symlinks")
    try:
        canonical = current.resolve(strict=True)
        canonical.relative_to(root)
    except (OSError, ValueError) as error:
        raise _invalid(f"Workspace capsule {label} escapes the project root") from error
    if canonical != current:
        raise _invalid(f"Workspace capsule {label} must be canonical")
    if regular_file and not stat.S_ISREG(current.lstat().st_mode):
        raise _invalid(f"Workspace capsule {label} must be a regular file")
    return current


def _hash_regular_file(path: Path, label: str) -> str:
    try:
        expected = path.lstat()
    except OSError as error:
        raise _invalid(f"Cannot inspect workspace capsule {label}") from error
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
        raise _invalid(f"Workspace capsule {label} must be a non-symlink regular file")
    if expected.st_size > MAX_WORKSPACE_INSTRUCTION_BYTES:
        raise _invalid(f"Workspace capsule {label} exceeds the byte limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _invalid(f"Cannot safely open workspace capsule {label}") from error
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            raise _invalid(f"Workspace capsule {label} changed while opening")
        while True:
            chunk = os.read(descriptor, 8192)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_WORKSPACE_INSTRUCTION_BYTES:
                raise _invalid(f"Workspace capsule {label} exceeds the byte limit")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _git_output(root: Path, arguments: list[str], label: str) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise _invalid("Workspace capsule requires Git")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    command = [
        git,
        "-C",
        str(root),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _invalid(f"Cannot determine workspace capsule Git {label}") from error
    if result.returncode != 0:
        raise _invalid(f"Cannot determine workspace capsule Git {label}")
    if len(result.stdout) > MAX_WORKSPACE_GIT_OUTPUT_BYTES:
        raise _invalid(f"Workspace capsule Git {label} exceeds the byte limit")
    return result.stdout


def _git_identity(root: Path) -> dict[str, str]:
    git_root_bytes = _git_output(root, ["rev-parse", "--show-toplevel"], "root")
    try:
        git_root = Path(git_root_bytes.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as error:
        raise _invalid("Workspace capsule Git root is invalid") from error
    if git_root != root:
        raise _invalid("Workspace capsule project root must be the Git worktree root")

    head_bytes = _git_output(root, ["rev-parse", "--verify", "HEAD"], "HEAD")
    try:
        head = head_bytes.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise _invalid("Workspace capsule Git HEAD is invalid") from error
    if not _HEAD_PATTERN.fullmatch(head):
        raise _invalid("Workspace capsule Git HEAD is invalid")

    status = _git_output(
        root,
        [
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=none",
        ],
        "state",
    )
    return {"head": head, "state": "dirty" if status else "clean"}


def _selected_instruction(directory: Path, root: Path) -> str | None:
    for name in INSTRUCTION_CANDIDATES:
        candidate = directory / name
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _invalid(
                "Cannot inspect a governing instruction candidate"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise _invalid("Governing instruction candidates cannot be symlinks")
        if not stat.S_ISREG(metadata.st_mode):
            continue
        relative = candidate.relative_to(root).as_posix()
        _validate_relative_path(relative, "instruction path")
        return relative
    return None


def _instruction_directories(root: Path, relevant_paths: list[str]) -> list[Path]:
    directories = {root}
    for relative in relevant_paths:
        candidate = _safe_project_path(root, relative, "relevant path")
        directory = candidate if candidate.is_dir() else candidate.parent
        while True:
            directories.add(directory)
            if directory == root:
                break
            directory = directory.parent
    return sorted(
        directories, key=lambda value: (len(value.relative_to(root).parts), str(value))
    )


def _instruction_identities(
    root: Path, relevant_paths: list[str]
) -> list[dict[str, str]]:
    identities: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in _instruction_directories(root, relevant_paths):
        relative = _selected_instruction(directory, root)
        if relative is None or relative in seen:
            continue
        seen.add(relative)
        path = _safe_project_path(
            root,
            relative,
            "instruction path",
            regular_file=True,
        )
        identities.append(
            {"path": relative, "sha256": _hash_regular_file(path, "instruction file")}
        )
    if len(identities) > MAX_WORKSPACE_INSTRUCTION_PATHS:
        raise _invalid("Workspace capsule instruction count exceeds the limit")
    return identities


def _top_level_markers(root: Path) -> list[str]:
    markers: list[str] = []
    for name in TOP_LEVEL_MARKERS:
        candidate = root / name
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _invalid("Cannot inspect a workspace build/test marker") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise _invalid("Workspace build/test markers cannot be symlinks")
        if stat.S_ISREG(metadata.st_mode):
            markers.append(name)
    if len(markers) > MAX_WORKSPACE_MARKERS:
        raise _invalid("Workspace capsule marker count exceeds the limit")
    return markers


def serialize_workspace_capsule(capsule: object) -> str:
    try:
        serialized = json.dumps(
            capsule,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        encoded = serialized.encode("utf-8")
    except (TypeError, UnicodeEncodeError) as error:
        raise _invalid("Workspace capsule is not canonical UTF-8 JSON") from error
    if len(encoded) > MAX_WORKSPACE_CAPSULE_BYTES:
        raise _invalid("Workspace capsule exceeds the byte limit")
    return serialized


def construct_workspace_capsule(
    project: Path,
    relevant_paths: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Construct one deterministic workspace capsule without scanning the tree."""

    root = canonical_project_root(project)
    if not isinstance(relevant_paths, (list, tuple)):
        raise _invalid("Workspace capsule relevant paths must be a list")
    if len(relevant_paths) > MAX_WORKSPACE_RELEVANT_PATHS:
        raise _invalid("Workspace capsule relevant path count exceeds the limit")
    normalized = [
        _validate_relative_path(value, "relevant path") for value in relevant_paths
    ]
    if len(set(normalized)) != len(normalized):
        raise _invalid("Workspace capsule relevant paths must be unique")
    normalized.sort()
    for relative in normalized:
        _safe_project_path(root, relative, "relevant path")

    capsule: dict[str, Any] = {
        "schema_version": WORKSPACE_CAPSULE_SCHEMA_VERSION,
        "project_root_sha256": hashlib.sha256(os.fsencode(root)).hexdigest(),
        "git": _git_identity(root),
        "instructions": _instruction_identities(root, normalized),
        "markers": _top_level_markers(root),
        "relevant_paths": normalized,
    }
    serialize_workspace_capsule(capsule)
    return capsule


def _validate_capsule_shape(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_root_sha256",
        "git",
        "instructions",
        "markers",
        "relevant_paths",
    }:
        raise _invalid("Workspace capsule has missing or unknown fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise _invalid("Workspace capsule schema version is unsupported")
    root_digest = value["project_root_sha256"]
    if not isinstance(root_digest, str) or not _DIGEST_PATTERN.fullmatch(root_digest):
        raise _invalid("Workspace capsule project root identity is invalid")
    git = value["git"]
    if not isinstance(git, dict) or set(git) != {"head", "state"}:
        raise _invalid("Workspace capsule Git identity is invalid")
    if not isinstance(git["head"], str) or not _HEAD_PATTERN.fullmatch(git["head"]):
        raise _invalid("Workspace capsule Git HEAD is invalid")
    if not isinstance(git["state"], str) or git["state"] not in {"clean", "dirty"}:
        raise _invalid("Workspace capsule Git state is invalid")
    relevant_paths = value["relevant_paths"]
    if (
        not isinstance(relevant_paths, list)
        or len(relevant_paths) > MAX_WORKSPACE_RELEVANT_PATHS
    ):
        raise _invalid("Workspace capsule relevant paths are invalid")
    normalized = [
        _validate_relative_path(path, "relevant path") for path in relevant_paths
    ]
    if normalized != sorted(normalized) or len(set(normalized)) != len(normalized):
        raise _invalid("Workspace capsule relevant paths are not unique and sorted")

    markers = value["markers"]
    if not isinstance(markers, list) or len(markers) > MAX_WORKSPACE_MARKERS:
        raise _invalid("Workspace capsule markers are invalid")
    marker_values = [_validate_relative_path(marker, "marker") for marker in markers]
    if (
        marker_values != sorted(marker_values)
        or len(set(marker_values)) != len(marker_values)
        or any(marker not in TOP_LEVEL_MARKERS for marker in marker_values)
    ):
        raise _invalid("Workspace capsule markers are not an allowlisted sorted set")

    instructions = value["instructions"]
    if (
        not isinstance(instructions, list)
        or len(instructions) > MAX_WORKSPACE_INSTRUCTION_PATHS
    ):
        raise _invalid("Workspace capsule instructions are invalid")
    instruction_paths: list[str] = []
    for instruction in instructions:
        if not isinstance(instruction, dict) or set(instruction) != {"path", "sha256"}:
            raise _invalid("Workspace capsule instruction identity is invalid")
        instruction_paths.append(
            _validate_relative_path(instruction["path"], "instruction path")
        )
        if not isinstance(instruction["sha256"], str) or not _DIGEST_PATTERN.fullmatch(
            instruction["sha256"]
        ):
            raise _invalid("Workspace capsule instruction digest is invalid")
    expected_order = sorted(
        instruction_paths,
        key=lambda path: (len(PurePosixPath(path).parts), path),
    )
    if instruction_paths != expected_order or len(set(instruction_paths)) != len(
        instruction_paths
    ):
        raise _invalid("Workspace capsule instruction paths are not unique and sorted")
    serialize_workspace_capsule(value)
    return value


def validate_workspace_capsule(value: object, project: Path) -> dict[str, Any]:
    """Strictly validate shape and current project/Git/instruction identity."""

    capsule = _validate_capsule_shape(value)
    expected = construct_workspace_capsule(project, capsule["relevant_paths"])
    # Clean/dirty is an initial observation, not an invalidation key: normal task edits
    # must not make late worker delivery or restart replay fail. Canonical root, HEAD,
    # instruction identities, marker set, and relevant paths remain strictly current.
    expected["git"]["state"] = capsule["git"]["state"]
    if capsule != expected:
        raise OrchestrationError(
            "Workspace capsule is stale or does not match the canonical project",
            "workspace_capsule_stale",
        )
    return capsule


def workspace_capsule_metadata(capsule: dict[str, Any] | None) -> dict[str, Any]:
    if capsule is None:
        return {
            "enabled": False,
            "schema_version": None,
            "validation": "disabled",
            "instruction_count": 0,
            "marker_count": 0,
            "relevant_path_count": 0,
            "bytes": 0,
            "digest": None,
        }
    serialized = serialize_workspace_capsule(capsule)
    return {
        "enabled": True,
        "schema_version": WORKSPACE_CAPSULE_SCHEMA_VERSION,
        "validation": "validated",
        "instruction_count": len(capsule["instructions"]),
        "marker_count": len(capsule["markers"]),
        "relevant_path_count": len(capsule["relevant_paths"]),
        "bytes": len(serialized.encode("utf-8")),
        "digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def render_workspace_capsule(capsule: object, project: Path) -> str:
    accepted = validate_workspace_capsule(capsule, project)
    instruction_lines = [
        f"- {json.dumps(item['path'], ensure_ascii=False)} sha256={item['sha256']}"
        for item in accepted["instructions"]
    ] or ["- none"]
    marker_lines = [
        f"- {json.dumps(path, ensure_ascii=False)}" for path in accepted["markers"]
    ] or ["- none"]
    relevant_lines = [
        f"- {json.dumps(path, ensure_ascii=False)}"
        for path in accepted["relevant_paths"]
    ] or ["- none supplied"]
    return "\n".join(
        [
            f"Schema: {WORKSPACE_CAPSULE_SCHEMA_VERSION}",
            f"Project-root identity: {accepted['project_root_sha256']}",
            f"Initial Git HEAD: {accepted['git']['head']}",
            f"Initial Git state: {accepted['git']['state']}",
            "Governing instruction identities (paths and hashes only):",
            *instruction_lines,
            "Allowlisted top-level build/test markers:",
            *marker_lines,
            "Parent-supplied relevant project-relative paths:",
            *relevant_lines,
            "Discovery hints only: this capsule neither authorizes access nor substitutes for discovering and reading governing AGENTS.md/CLAUDE.md through Pi/project mechanisms. Verify all paths and instructions in the worktree.",
        ]
    )
