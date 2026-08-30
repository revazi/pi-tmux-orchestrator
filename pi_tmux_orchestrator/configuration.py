"""User-owned model policy configuration."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_MODELS,
    IMPLEMENTATION_FLOWS,
    KNOWN_ROLES,
    THINKING_LEVELS,
)
from . import runtime
from .models import OrchestrationError
from .profiles import (
    PACKAGED_EXECUTION_PROFILES,
    profile_name,
    validate_custom_profiles,
)
from .specialist_activation import SPECIALIST_ROLES

MODEL_CONFIG_VERSION = 3
PROFILE_MODEL_CONFIG_VERSION = 2
LEGACY_MODEL_CONFIG_VERSION = 1
MAX_MODEL_CONFIG_BYTES = 64 * 1024
MAX_PROJECT_CONFIGS = 64
MODEL_FIELDS = frozenset({"provider", "model", "thinking"})
LEGACY_MODEL_CONFIG_FIELDS = frozenset({"version", "defaults", "roles"})
PROFILE_MODEL_CONFIG_FIELDS = LEGACY_MODEL_CONFIG_FIELDS | {
    "defaultProfile",
    "profiles",
}
MODEL_CONFIG_FIELDS = PROFILE_MODEL_CONFIG_FIELDS | {"projects"}
PROJECT_CONFIG_FIELDS = frozenset(
    {
        "directory",
        "profile",
        "defaults",
        "roles",
        "implementationFlow",
        "specialists",
        "workspaceCapsule",
    }
)
MODEL_CONFIG_ENV = "PI_TMUX_ORCHESTRATOR_CONFIG"


def model_config_path(project: Path | None = None) -> Path:
    configured = os.environ.get(MODEL_CONFIG_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise OrchestrationError(f"{MODEL_CONFIG_ENV} must be an absolute path")
        return _validate_model_config_path(path, project)
    pi_home = runtime.PI_HOME
    if not pi_home.is_absolute():
        raise OrchestrationError("Pi configuration directory must be an absolute path")
    return _validate_model_config_path(pi_home / "tmux-orchestrator.json", project)


def _validate_model_config_path(path: Path, project: Path | None) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    if project is not None:
        project = project.resolve(strict=True)
        resolved = path.resolve(strict=False)
        if resolved == project or project in resolved.parents:
            raise OrchestrationError(
                "Model and profile configuration must remain outside the target project"
            )
    return path


def load_model_config(
    path: Path | None = None, *, project: Path | None = None
) -> dict[str, Any]:
    config_path = (
        _validate_model_config_path(path, project)
        if path is not None
        else model_config_path(project)
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(config_path, flags)
    except FileNotFoundError:
        return empty_model_config()
    except OSError as error:
        raise OrchestrationError(
            "Model configuration cannot be opened safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OrchestrationError(
                "Model configuration must be a regular non-symlink file"
            )
        if metadata.st_size > MAX_MODEL_CONFIG_BYTES:
            raise OrchestrationError("Model configuration exceeds the 64 KiB limit")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = None
            raw = handle.read(MAX_MODEL_CONFIG_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_MODEL_CONFIG_BYTES:
            raise OrchestrationError("Model configuration exceeds the 64 KiB limit")
        value = json.loads(raw, object_pairs_hook=unique_json_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise OrchestrationError(
            "Model configuration is not valid UTF-8 JSON"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return validate_model_config(value)


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def empty_model_config() -> dict[str, Any]:
    return {
        "version": MODEL_CONFIG_VERSION,
        "default_profile": None,
        "profiles": {},
        "defaults": {},
        "roles": {},
        "projects": [],
    }


def validate_model_config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationError("Model configuration must be an object")
    version = value.get("version")
    if version == LEGACY_MODEL_CONFIG_VERSION:
        allowed_fields = LEGACY_MODEL_CONFIG_FIELDS
    elif version == PROFILE_MODEL_CONFIG_VERSION:
        allowed_fields = PROFILE_MODEL_CONFIG_FIELDS
    elif version == MODEL_CONFIG_VERSION:
        allowed_fields = MODEL_CONFIG_FIELDS
    else:
        raise OrchestrationError(
            "Model configuration version must be "
            f"{LEGACY_MODEL_CONFIG_VERSION}, {PROFILE_MODEL_CONFIG_VERSION}, or {MODEL_CONFIG_VERSION}"
        )
    if set(value) - allowed_fields:
        raise OrchestrationError("Model configuration has unsupported top-level fields")
    defaults = validate_model_fields(value.get("defaults", {}), "defaults")
    raw_roles = value.get("roles", {})
    if not isinstance(raw_roles, dict) or set(raw_roles) - KNOWN_ROLES:
        raise OrchestrationError("Model configuration roles must use known role names")
    roles = {
        role: validate_model_fields(config, f"roles.{role}")
        for role, config in raw_roles.items()
    }
    default_profile: str | None = None
    profiles: dict[str, dict[str, str]] = {}
    if version >= PROFILE_MODEL_CONFIG_VERSION:
        raw_default = value.get("defaultProfile")
        if raw_default is not None:
            default_profile = profile_name(raw_default)
        profiles = validate_custom_profiles(value.get("profiles", {}))
        if (
            default_profile is not None
            and default_profile not in PACKAGED_EXECUTION_PROFILES
            and default_profile not in profiles
        ):
            raise OrchestrationError(
                "Model configuration defaultProfile must name a packaged or configured profile"
            )
    projects = (
        validate_project_configs(value.get("projects", []), profiles)
        if version == MODEL_CONFIG_VERSION
        else []
    )
    return {
        "version": MODEL_CONFIG_VERSION,
        "default_profile": default_profile,
        "profiles": profiles,
        "defaults": defaults,
        "roles": roles,
        "projects": projects,
    }


def validate_model_fields(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - MODEL_FIELDS:
        raise OrchestrationError(f"Model configuration {label} has unsupported fields")
    result: dict[str, str] = {}
    for field in ("provider", "model"):
        if field not in value:
            continue
        candidate = value[field]
        if (
            not isinstance(candidate, str)
            or not candidate
            or len(candidate) > 256
            or any(
                character.isspace() or ord(character) < 32 for character in candidate
            )
        ):
            raise OrchestrationError(
                f"Model configuration {label}.{field} must be a bounded identifier"
            )
        result[field] = candidate
    if ("provider" in result) != ("model" in result):
        raise OrchestrationError(
            f"Model configuration {label} must set provider and model together"
        )
    if "thinking" in value:
        thinking = value["thinking"]
        if not isinstance(thinking, str) or thinking not in THINKING_LEVELS:
            raise OrchestrationError(
                f"Model configuration {label}.thinking must be a supported level"
            )
        result["thinking"] = thinking
    return result


def _validated_profile_reference(
    value: object,
    profiles: dict[str, dict[str, str]],
    label: str,
) -> str:
    name = profile_name(value)
    if name not in PACKAGED_EXECUTION_PROFILES and name not in profiles:
        raise OrchestrationError(
            f"Model configuration {label} must name a packaged or configured profile"
        )
    return name


def _canonical_project_directory(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise OrchestrationError(
            "Model configuration project directory must be a bounded absolute path"
        )
    supplied = Path(value)
    if not supplied.is_absolute():
        raise OrchestrationError(
            "Model configuration project directory must be an absolute path"
        )
    lexical = Path(os.path.abspath(os.fspath(supplied)))
    try:
        canonical = supplied.resolve(strict=True)
    except OSError as error:
        raise OrchestrationError(
            "Model configuration project directory must exist"
        ) from error
    if not canonical.is_dir() or lexical != canonical:
        raise OrchestrationError(
            "Model configuration project directory must be canonical and contain no symlink components"
        )
    return str(canonical)


def validate_project_configs(
    value: object,
    profiles: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise OrchestrationError("Model configuration projects must be an array")
    if len(value) > MAX_PROJECT_CONFIGS:
        raise OrchestrationError(
            f"Model configuration supports at most {MAX_PROJECT_CONFIGS} projects"
        )
    projects: list[dict[str, Any]] = []
    directories: set[str] = set()
    for index, raw in enumerate(value):
        label = f"projects[{index}]"
        if not isinstance(raw, dict) or set(raw) - PROJECT_CONFIG_FIELDS:
            raise OrchestrationError(
                f"Model configuration {label} has unsupported fields"
            )
        if "directory" not in raw:
            raise OrchestrationError(
                f"Model configuration {label}.directory is required"
            )
        directory = _canonical_project_directory(raw["directory"])
        if directory in directories:
            raise OrchestrationError(
                "Model configuration project directories must be unique"
            )
        directories.add(directory)
        profile = (
            _validated_profile_reference(raw["profile"], profiles, f"{label}.profile")
            if "profile" in raw
            else None
        )
        defaults = validate_model_fields(raw.get("defaults", {}), f"{label}.defaults")
        raw_roles = raw.get("roles", {})
        if not isinstance(raw_roles, dict) or set(raw_roles) - KNOWN_ROLES:
            raise OrchestrationError(
                f"Model configuration {label}.roles must use known role names"
            )
        roles = {
            role: validate_model_fields(config, f"{label}.roles.{role}")
            for role, config in raw_roles.items()
        }
        implementation_flow = raw.get("implementationFlow")
        if (
            implementation_flow is not None
            and implementation_flow not in IMPLEMENTATION_FLOWS
        ):
            raise OrchestrationError(
                f"Model configuration {label}.implementationFlow is invalid"
            )
        raw_specialists = raw.get("specialists")
        specialists: list[str] | None = None
        if raw_specialists is not None:
            if (
                not isinstance(raw_specialists, list)
                or len(raw_specialists) > len(SPECIALIST_ROLES)
                or any(
                    not isinstance(role, str) or role not in SPECIALIST_ROLES
                    for role in raw_specialists
                )
                or len(set(raw_specialists)) != len(raw_specialists)
            ):
                raise OrchestrationError(
                    f"Model configuration {label}.specialists must contain unique built-in specialist roles"
                )
            specialists = [role for role in SPECIALIST_ROLES if role in raw_specialists]
        workspace_capsule = raw.get("workspaceCapsule")
        if workspace_capsule is not None and type(workspace_capsule) is not bool:
            raise OrchestrationError(
                f"Model configuration {label}.workspaceCapsule must be boolean"
            )
        projects.append(
            {
                "directory": directory,
                "profile": profile,
                "defaults": defaults,
                "roles": roles,
                "implementation_flow": implementation_flow,
                "specialists": specialists,
                "workspace_capsule": workspace_capsule,
            }
        )
    return projects


def project_model_config(
    config: dict[str, Any], project: Path
) -> dict[str, Any] | None:
    canonical = str(project.resolve(strict=True))
    return next(
        (item for item in config.get("projects", []) if item["directory"] == canonical),
        None,
    )


def public_project_config(project: dict[str, Any] | None) -> dict[str, Any]:
    if project is None:
        return {
            "matched": False,
            "directory": None,
            "profile": None,
            "implementation_flow": None,
            "specialists": None,
            "workspace_capsule": None,
            "model_defaults": False,
            "role_overrides": [],
        }
    return {
        "matched": True,
        "directory": project["directory"],
        "profile": project["profile"],
        "implementation_flow": project["implementation_flow"],
        "specialists": (
            list(project["specialists"]) if project["specialists"] is not None else None
        ),
        "workspace_capsule": project["workspace_capsule"],
        "model_defaults": bool(project["defaults"]),
        "role_overrides": sorted(project["roles"]),
    }


def validate_manifest_project_config(value: object) -> dict[str, Any]:
    fields = {
        "matched",
        "directory",
        "profile",
        "implementation_flow",
        "specialists",
        "workspace_capsule",
        "model_defaults",
        "role_overrides",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise OrchestrationError("Manifest project configuration has invalid fields")
    matched = value["matched"]
    if type(matched) is not bool or type(value["model_defaults"]) is not bool:
        raise OrchestrationError("Manifest project configuration flags are invalid")
    directory = value["directory"]
    if matched:
        if (
            not isinstance(directory, str)
            or not directory
            or len(directory) > 4096
            or not Path(directory).is_absolute()
            or Path(os.path.abspath(directory)) != Path(directory)
            or any(ord(character) < 32 for character in directory)
        ):
            raise OrchestrationError(
                "Manifest project configuration directory is invalid"
            )
    elif directory is not None:
        raise OrchestrationError(
            "Manifest unmatched project configuration cannot name a directory"
        )
    profile = value["profile"]
    if profile is not None:
        profile_name(profile)
    flow = value["implementation_flow"]
    if flow is not None and flow not in IMPLEMENTATION_FLOWS:
        raise OrchestrationError("Manifest project implementation flow is invalid")
    specialists = value["specialists"]
    if specialists is not None and (
        not isinstance(specialists, list)
        or any(role not in SPECIALIST_ROLES for role in specialists)
        or len(set(specialists)) != len(specialists)
    ):
        raise OrchestrationError("Manifest project specialists are invalid")
    capsule = value["workspace_capsule"]
    if capsule is not None and type(capsule) is not bool:
        raise OrchestrationError("Manifest project workspace capsule is invalid")
    role_overrides = value["role_overrides"]
    if (
        not isinstance(role_overrides, list)
        or any(role not in KNOWN_ROLES for role in role_overrides)
        or len(set(role_overrides)) != len(role_overrides)
    ):
        raise OrchestrationError("Manifest project role overrides are invalid")
    if not matched and any(
        item is not None for item in (profile, flow, specialists, capsule)
    ):
        raise OrchestrationError(
            "Manifest unmatched project configuration cannot contain defaults"
        )
    if not matched and (value["model_defaults"] or role_overrides):
        raise OrchestrationError(
            "Manifest unmatched project configuration cannot contain model overrides"
        )
    return dict(value)


def validate_manifest_orchestration_config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "version"}:
        raise OrchestrationError("Manifest orchestration configuration is invalid")
    path = value["path"]
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or not Path(path).is_absolute()
        or Path(os.path.abspath(path)) != Path(path)
        or any(ord(character) < 32 for character in path)
    ):
        raise OrchestrationError("Manifest orchestration configuration path is invalid")
    if value["version"] != MODEL_CONFIG_VERSION:
        raise OrchestrationError(
            "Manifest orchestration configuration version is invalid"
        )
    return dict(value)


def retained_orchestration_config(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("version", 0) < 5:
        return {"path": None, "version": None}
    return dict(manifest["orchestration_config"])


def retained_project_config(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("version", 0) < 5:
        return {
            "matched": None,
            "directory": None,
            "profile": None,
            "implementation_flow": None,
            "specialists": None,
            "workspace_capsule": None,
            "model_defaults": None,
            "role_overrides": [],
        }
    return dict(manifest["project_config"])


def effective_model_config(
    role: str,
    config: dict[str, Any],
    execution_profile: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, str]:
    effective = dict(DEFAULT_MODELS[role])
    if execution_profile is not None:
        effective["thinking"] = execution_profile["thinking"][role]
    effective.update(config["defaults"])
    effective.update(config["roles"].get(role, {}))
    if project is not None:
        effective.update(project["defaults"])
        effective.update(project["roles"].get(role, {}))
    return effective
