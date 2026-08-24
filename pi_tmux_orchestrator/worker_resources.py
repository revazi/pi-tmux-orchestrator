"""Strict worker resource opt-ins and launch-time verification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from .constants import (
    KNOWN_ROLES,
    MAX_WORKER_SKILL_BYTES,
    MAX_WORKER_SKILL_PATH_CHARS,
    MAX_WORKER_SKILLS_PER_ROLE,
)
from .models import OrchestrationError
from .storage import absolute_path, read_regular_file, require_regular_file


def worker_skill_argument(value: str) -> tuple[str, str]:
    """Parse one explicit ROLE=PATH worker skill selection."""
    if "=" not in value:
        raise OrchestrationError("Worker skills must use ROLE=PATH")
    role, raw_path = value.split("=", 1)
    if role not in KNOWN_ROLES:
        raise OrchestrationError(f"Unknown worker skill role: {role}")
    if (
        not raw_path
        or len(raw_path) > MAX_WORKER_SKILL_PATH_CHARS
        or any(char in raw_path for char in "\r\n\0")
    ):
        raise OrchestrationError("Worker skill path is invalid")
    return role, raw_path


def _verified_skill(path_value: str) -> dict[str, str]:
    path = absolute_path(Path(path_value))
    metadata = require_regular_file(path, "worker skill", nonempty=True)
    if path.suffix.lower() != ".md":
        raise OrchestrationError("Worker skills must be Markdown files")
    if metadata.st_size > MAX_WORKER_SKILL_BYTES:
        raise OrchestrationError(
            f"Worker skill exceeds the {MAX_WORKER_SKILL_BYTES // 1024} KiB limit"
        )
    try:
        content = read_regular_file(path, "worker skill", MAX_WORKER_SKILL_BYTES)
        content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise OrchestrationError("Worker skill must be readable UTF-8") from error
    return {"path": str(path), "sha256": hashlib.sha256(content).hexdigest()}


def resolve_worker_skills(
    values: list[tuple[str, str]] | None,
    enabled_roles: list[str],
) -> dict[str, list[dict[str, str]]]:
    """Resolve explicit per-role skills and bind each reviewed file to its digest."""
    result = {role: [] for role in enabled_roles}
    seen: set[tuple[str, str]] = set()
    for role, raw_path in values or []:
        if role not in result:
            raise OrchestrationError(
                f"Worker skill targets disabled role {role}; enable that role first"
            )
        skill = _verified_skill(raw_path)
        identity = (role, skill["path"])
        if identity in seen:
            raise OrchestrationError(
                f"Duplicate worker skill for {role}: {skill['path']}"
            )
        if len(result[role]) >= MAX_WORKER_SKILLS_PER_ROLE:
            raise OrchestrationError(
                f"Worker role {role} exceeds the {MAX_WORKER_SKILLS_PER_ROLE}-skill limit"
            )
        seen.add(identity)
        result[role].append(skill)
    return result


def validate_retained_worker_skills(value: object, role: str) -> list[dict[str, str]]:
    """Validate bounded metadata without requiring retained external files to exist."""
    if not isinstance(value, list) or len(value) > MAX_WORKER_SKILLS_PER_ROLE:
        raise OrchestrationError(f"Manifest role {role} has invalid worker skills")
    validated: list[dict[str, str]] = []
    paths: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise OrchestrationError(f"Manifest role {role} has invalid worker skills")
        path_value = item["path"]
        digest = item["sha256"]
        if (
            not isinstance(path_value, str)
            or len(path_value) > MAX_WORKER_SKILL_PATH_CHARS
            or not Path(path_value).is_absolute()
            or os.path.normpath(path_value) != path_value
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or path_value in paths
        ):
            raise OrchestrationError(f"Manifest role {role} has invalid worker skills")
        paths.add(path_value)
        validated.append({"path": path_value, "sha256": digest})
    return validated


def append_worker_resource_args(
    command: list[str],
    role: dict[str, Any],
    role_name: str,
    extension_path: Path,
    system_prompt_path: Path,
) -> None:
    """Apply the same lean prompt and explicit-skill policy to TUI and RPC workers."""
    command.extend(
        [
            "--extension",
            str(extension_path),
            "--no-skills",
            "--system-prompt",
            str(system_prompt_path),
        ]
    )
    for skill_path in verified_worker_skill_paths(role, role_name):
        command.extend(["--skill", skill_path])


def verified_worker_skill_paths(role: dict[str, Any], role_name: str) -> list[str]:
    """Fail closed if an opted-in skill changed or disappeared before worker launch."""
    skills = validate_retained_worker_skills(role.get("skills", []), role_name)
    paths: list[str] = []
    for skill in skills:
        current = _verified_skill(skill["path"])
        if current["sha256"] != skill["sha256"]:
            raise OrchestrationError(
                f"Worker skill changed after approval for role {role_name}: {skill['path']}"
            )
        paths.append(skill["path"])
    return paths
