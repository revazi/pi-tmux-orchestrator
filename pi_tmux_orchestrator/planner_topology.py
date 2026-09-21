"""Bounded preflight worker-topology policy projection."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .configuration import (
    load_model_config,
    model_config_path,
    project_model_config,
)
from .custom_role_resources import select_custom_start
from .models import CommandResult, OrchestrationError
from .output import human_print
from .planning import metadata_digest
from .profiles import resolve_execution_profile
from .specialist_activation import SPECIALIST_ROLES

PLANNER_TOPOLOGY_VERSION = 1
BUILTIN_ROLE_ORDER = ("implementer", "reviewer", *SPECIALIST_ROLES)


def _configured_role_constraint(
    role: str,
    config: dict[str, Any],
    project: dict[str, Any] | None,
    profile: dict[str, Any],
) -> dict[str, str]:
    constraint: dict[str, str] = {}
    if profile["source"] != "packaged-default":
        constraint["thinking"] = profile["thinking"][role]
    constraint.update(config["defaults"])
    constraint.update(config["roles"].get(role, {}))
    if project is not None:
        constraint.update(project["defaults"])
        constraint.update(project["roles"].get(role, {}))
    return constraint


def _project_custom_selections(
    project_config: dict[str, Any] | None,
) -> list[list[str]]:
    if project_config is None:
        return []
    return [
        [role["id"], role["provider"], role["model"], role["thinking"]]
        for role in project_config["custom_roles"]
    ]


def planner_topology_projection(
    project: Path,
    *,
    profile_name: str | None = None,
    include_project_custom_roles: bool = True,
) -> dict[str, Any]:
    try:
        project = project.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise OrchestrationError("Planner topology project is unavailable") from error
    if not project.is_dir():
        raise OrchestrationError("Planner topology requires a project directory")

    configured = load_model_config(project=project)
    matched = project_model_config(configured, project)
    profile = resolve_execution_profile(configured, profile_name, matched)
    configured_specialists = matched["specialists"] if matched is not None else None
    optional_roles = (
        list(SPECIALIST_ROLES)
        if configured_specialists is None
        else list(configured_specialists)
    )
    builtins = {
        role: {
            "constraint": _configured_role_constraint(
                role, configured, matched, profile
            )
        }
        for role in BUILTIN_ROLE_ORDER
    }

    selections = (
        _project_custom_selections(matched) if include_project_custom_roles else []
    )
    selected = select_custom_start(
        project,
        selections,
        selection_source="project-config",
    )
    custom_roles = sorted(
        [
            {
                "role": role,
                "contract": item["custom_role"]["contract"],
                "provider": item["provider"],
                "model": item["model"],
                "thinking": item["thinking"],
            }
            for role, item in selected["roles"].items()
        ],
        key=lambda item: item["role"],
    )
    policy = {
        "version": PLANNER_TOPOLOGY_VERSION,
        "builtins": builtins,
        "optional_roles": optional_roles,
        "custom_roles": custom_roles,
    }
    binding_digest = metadata_digest(
        {
            "project": str(project),
            "model_config": configured,
            "profile": profile,
            "policy": policy,
            "custom_bindings": selected,
        }
    )
    return {"policy": policy, "binding_digest": binding_digest}


def planner_topology_policy(
    project: Path,
    *,
    profile_name: str | None = None,
    include_project_custom_roles: bool = True,
) -> dict[str, Any]:
    return planner_topology_projection(
        project,
        profile_name=profile_name,
        include_project_custom_roles=include_project_custom_roles,
    )["policy"]


def planner_topology_command(args: argparse.Namespace) -> CommandResult:
    project = Path(args.project).expanduser()
    projection = planner_topology_projection(
        project,
        profile_name=getattr(args, "profile", None),
        include_project_custom_roles=not getattr(
            args, "no_project_custom_roles", False
        ),
    )
    policy = projection["policy"]
    path = model_config_path(project.resolve(strict=True))
    human_print(
        f"Planner topology: {len(policy['optional_roles'])} optional built-ins; "
        f"{len(policy['custom_roles'])} trusted custom roles"
    )
    return CommandResult(
        data={
            "config_path": str(path),
            "binding_digest": projection["binding_digest"],
            "policy": policy,
        }
    )
