"""Strict user-global definitions for future read-only custom specialists."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import runtime
from .configuration import unique_json_object
from .constants import KNOWN_ROLES
from .models import CommandResult, OrchestrationError
from .output import human_print
from .registry_resources import global_resource_path, read_global_resource

REGISTRY_ENV = "PI_TMUX_ORCHESTRATOR_ROLE_REGISTRY"
MAX_REGISTRY_BYTES = 64 * 1024
MAX_CUSTOM_ROLES = 8
MAX_ROLE_SKILLS = 4
MAX_PROMPT_BYTES = 16 * 1024
MAX_SKILL_BYTES = 32 * 1024
CONTRACTS = frozenset({"probe", "playwright", "django"})
RESERVED_SUFFIXES = KNOWN_ROLES | {
    "all",
    "parent",
    "controller",
    "broker",
    "monitor",
    "orchestrator",
    "coordinator",
    "writer",
    "review",
    "system",
    "user",
    "assistant",
    "tool",
    "django-expert",
    "playwright-tester",
    "technical-probe",
}


def valid_custom_role_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 32
        and re.fullmatch(r"custom-[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) is not None
        and value.removeprefix("custom-") not in RESERVED_SUFFIXES
    )


def registry_path(project: Path, explicit: str | None = None) -> Path:
    value = explicit if explicit is not None else os.environ.get(REGISTRY_ENV)
    if value is None:
        value = str(runtime.PI_HOME / "tmux-orchestrator-roles.json")
    return global_resource_path(value, project)


def _resource_metadata(value: object, project: Path) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise OrchestrationError("Role resources require only path and sha256")
    path = global_resource_path(value["path"], project)
    digest = value["sha256"]
    if (
        path.suffix.lower() != ".md"
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise OrchestrationError(
            "Role resources require Markdown paths and lowercase SHA-256 digests"
        )
    return {"path": str(path), "sha256": digest}


def _role_definition(value: object, project: Path) -> dict[str, Any]:
    required = {"id", "contract", "prompt"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"skills"}
    ):
        raise OrchestrationError("Custom role definition has invalid fields")
    identifier = value["id"]
    if not valid_custom_role_id(identifier):
        raise OrchestrationError("Custom role ID is invalid or reserved")
    contract = value["contract"]
    if not isinstance(contract, str) or contract not in CONTRACTS:
        raise OrchestrationError(
            "Custom roles require a built-in read-only specialist contract"
        )
    prompt = _resource_metadata(value["prompt"], project)
    skills = value.get("skills", [])
    if not isinstance(skills, list) or len(skills) > MAX_ROLE_SKILLS:
        raise OrchestrationError("Custom role skills exceed the bounded list contract")
    skills = [_resource_metadata(skill, project) for skill in skills]
    paths = [prompt["path"], *(skill["path"] for skill in skills)]
    if len(set(paths)) != len(paths):
        raise OrchestrationError("Custom role resource paths must be unique")
    return {"id": identifier, "contract": contract, "prompt": prompt, "skills": skills}


def validate_registry(value: object, project: Path) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "roles"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["roles"], list)
        or len(value["roles"]) > MAX_CUSTOM_ROLES
    ):
        raise OrchestrationError(
            "Custom-role registry must use bounded version-1 definitions"
        )
    roles = [_role_definition(role, project) for role in value["roles"]]
    if len({role["id"] for role in roles}) != len(roles):
        raise OrchestrationError("Custom role IDs must be unique")
    return {"version": 1, "roles": roles}


def _verify_resource(resource: dict[str, str], limit: int, project: Path) -> None:
    content = read_global_resource(Path(resource["path"]), limit, project=project)
    if hashlib.sha256(content).hexdigest() != resource["sha256"]:
        raise OrchestrationError(
            "Custom role resource no longer matches its reviewed digest"
        )


def load_registry(project: Path, explicit: str | None = None) -> dict[str, Any]:
    project = project.resolve(strict=True)
    if not project.is_dir():
        raise OrchestrationError("Registry validation requires a project directory")
    path = registry_path(project, explicit)
    raw = read_global_resource(
        path,
        MAX_REGISTRY_BYTES,
        project=project,
        missing_ok=explicit is None and REGISTRY_ENV not in os.environ,
    )
    if raw is None:
        registry = {"version": 1, "roles": []}
    else:
        try:
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=unique_json_object
            )
        except (ValueError, UnicodeError, RecursionError) as error:
            raise OrchestrationError(
                "Custom-role registry is not strict UTF-8 JSON"
            ) from error
        registry = validate_registry(value, project)
        # Validate the entire schema before accessing any referenced resource.
        for role in registry["roles"]:
            _verify_resource(role["prompt"], MAX_PROMPT_BYTES, project)
            for skill in role["skills"]:
                _verify_resource(skill, MAX_SKILL_BYTES, project)
    return {
        **registry,
        "configured": raw is not None,
        "registry_path": str(path),
        "launch_supported": False,
    }


def role_registry_command(args: Any) -> CommandResult:
    try:
        project = Path(args.project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise OrchestrationError(
            "Registry validation project is unavailable"
        ) from error
    data = load_registry(project, args.registry)
    human_print(f"Custom-role registry: {data['registry_path']}")
    human_print(
        f"Validated definitions: {len(data['roles'])}; custom-role launch is not yet supported"
    )
    for role in data["roles"]:
        human_print(
            f"  {role['id']}: read-only {role['contract']} contract; {len(role['skills'])} skills"
        )
    return CommandResult(data=data)
