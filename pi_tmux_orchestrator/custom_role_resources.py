"""Metadata-only custom role bindings and fresh, digest-checked launch inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

from .constants import CUSTOM_READ_ONLY_TOOLS, THINKING_LEVELS
from .models import OrchestrationError
from .registry_resources import global_resource_path, read_global_resource
from .role_registry import (
    MAX_CUSTOM_ROLES,
    MAX_PROMPT_BYTES,
    MAX_SKILL_BYTES,
    load_registry,
    valid_custom_role_id,
    validate_registry,
)


@dataclass(frozen=True)
class VerifiedCustomResources:
    role: str
    contract: str
    prompt: str = field(repr=False)
    skills: tuple[str, ...] = field(repr=False)


def validate_custom_definition(
    value: object, role: str, project: Path
) -> dict[str, Any]:
    """Validate retained metadata only: source files need not still exist."""
    definition = validate_registry({"version": 1, "roles": [value]}, project)["roles"][
        0
    ]
    if definition["id"] != role or definition != value:
        raise OrchestrationError("Retained custom role identity is invalid")
    return definition


def select_custom_roles(
    project: Path, names: list[str], registry: str | None = None
) -> dict[str, Any]:
    """Bind explicit selections; omission neither discovers nor enables roles."""
    if (
        not isinstance(names, list)
        or len(names) > MAX_CUSTOM_ROLES
        or any(not valid_custom_role_id(name) for name in names)
        or len(set(names)) != len(names)
    ):
        raise OrchestrationError("Custom role selection is invalid")
    if not names:
        return {"registry_path": None, "roles": {}}
    loaded = load_registry(project, registry)
    definitions = {role["id"]: role for role in loaded["roles"]}
    if not set(names) <= set(definitions):
        raise OrchestrationError("Selected custom role is not registered")
    return {
        "registry_path": loaded["registry_path"],
        "roles": {name: definitions[name] for name in names},
    }


def select_custom_start(
    project: Path, selections: list[list[str]], registry: str | None = None
) -> dict[str, Any]:
    """Resolve explicit models and verified bindings, never profile/model defaults."""
    if not isinstance(selections, list) or len(selections) > MAX_CUSTOM_ROLES:
        raise OrchestrationError(
            "Custom start selection is invalid", "invalid_arguments"
        )
    names = []
    configs = {}
    for selection in selections:
        if (
            not isinstance(selection, list)
            or len(selection) != 4
            or not valid_custom_role_id(selection[0])
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > 256
                or value.startswith("-")
                or any(
                    character.isspace() or not character.isprintable()
                    for character in value
                )
                for value in selection[1:3]
            )
            or not isinstance(selection[3], str)
            or selection[3] not in THINKING_LEVELS
        ):
            raise OrchestrationError(
                "Custom start selection is invalid", "invalid_arguments"
            )
        name, provider, model, thinking = selection
        names.append(name)
        configs[name] = {
            "provider": provider,
            "model": model,
            "thinking": thinking,
            "tools": CUSTOM_READ_ONLY_TOOLS,
            "pane_id": None,
        }
    if registry is not None and not names:
        raise OrchestrationError(
            "--role-registry requires --custom-role", "invalid_arguments"
        )
    selected = select_custom_roles(project, names, registry)
    for name, definition in selected["roles"].items():
        configs[name]["custom_role"] = definition
    return {"registry_path": selected["registry_path"], "roles": configs}


def retained_custom_definitions(manifest: dict[str, Any]) -> dict[str, Any]:
    """Strict versioned binding, without consulting ambient policy or live files."""
    version = manifest.get("version")
    if type(version) is not int or version not in {1, 2, 3, 4, 5, 6}:
        raise OrchestrationError("Unsupported custom role binding manifest version")
    project = Path(manifest["project"])
    roles = manifest["roles"]
    if not isinstance(roles, dict) or any(
        not isinstance(role, dict) for role in roles.values()
    ):
        raise OrchestrationError("Retained worker roles are invalid")
    custom = {name: role for name, role in roles.items() if valid_custom_role_id(name)}
    if manifest["version"] < 6:
        if custom or "custom_role_registry" in manifest:
            raise OrchestrationError("Legacy manifests cannot bind custom roles")
        return {}
    if "custom_role_registry" not in manifest or len(custom) > MAX_CUSTOM_ROLES:
        raise OrchestrationError("Retained custom role registry binding is invalid")
    registry = manifest["custom_role_registry"]
    if not custom:
        if registry is not None:
            raise OrchestrationError(
                "An unused custom role registry cannot be retained"
            )
        return {}
    global_resource_path(registry, project)
    return {
        name: validate_custom_definition(role.get("custom_role"), name, project)
        for name, role in custom.items()
    }


def retained_custom_contracts(manifest: dict[str, Any], coord: Path) -> dict[str, str]:
    """Derive broker authority only from a fully validated retained manifest.

    This metadata read deliberately does not reopen resources. Launch/restart
    must still perform fresh resource verification; worker frames are never inputs.
    """
    from .storage import validate_manifest

    validated = validate_manifest(manifest, coord)
    return {
        name: definition["contract"]
        for name, definition in retained_custom_definitions(validated).items()
    }


def _verified_text(resource: dict[str, str], limit: int, project: Path) -> str:
    content = read_global_resource(Path(resource["path"]), limit, project=project)
    if content is None or hashlib.sha256(content).hexdigest() != resource["sha256"]:
        raise OrchestrationError(
            "Custom role resource no longer matches its reviewed digest"
        )
    return content.decode("utf-8")


def verify_custom_role(manifest: dict[str, Any], role: str) -> VerifiedCustomResources:
    definitions = retained_custom_definitions(manifest)
    if role not in definitions:
        raise OrchestrationError("Custom worker has no retained role binding")
    project = Path(manifest["project"])
    # Pin the original registry path, not a newly selected global/environment default.
    loaded = load_registry(project, manifest["custom_role_registry"])
    current = {item["id"]: item for item in loaded["roles"]}
    if any(current.get(name) != definition for name, definition in definitions.items()):
        raise OrchestrationError("Custom role definition changed after selection")
    definition = definitions[role]
    # Re-read into launch-local bytes. Never give Pi the mutable external paths.
    return VerifiedCustomResources(
        role=role,
        contract=definition["contract"],
        prompt=_verified_text(definition["prompt"], MAX_PROMPT_BYTES, project),
        skills=tuple(
            _verified_text(skill, MAX_SKILL_BYTES, project)
            for skill in definition["skills"]
        ),
    )
