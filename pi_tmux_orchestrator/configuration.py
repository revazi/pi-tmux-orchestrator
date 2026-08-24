"""User-owned model policy configuration."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .constants import DEFAULT_MODELS, KNOWN_ROLES, THINKING_LEVELS
from . import runtime
from .models import OrchestrationError
from .profiles import (
    PACKAGED_EXECUTION_PROFILES,
    profile_name,
    validate_custom_profiles,
)

MODEL_CONFIG_VERSION = 2
LEGACY_MODEL_CONFIG_VERSION = 1
MAX_MODEL_CONFIG_BYTES = 64 * 1024
MODEL_FIELDS = frozenset({"provider", "model", "thinking"})
LEGACY_MODEL_CONFIG_FIELDS = frozenset({"version", "defaults", "roles"})
MODEL_CONFIG_FIELDS = LEGACY_MODEL_CONFIG_FIELDS | {"defaultProfile", "profiles"}
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
    }


def validate_model_config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationError("Model configuration must be an object")
    version = value.get("version")
    if version == LEGACY_MODEL_CONFIG_VERSION:
        allowed_fields = LEGACY_MODEL_CONFIG_FIELDS
    elif version == MODEL_CONFIG_VERSION:
        allowed_fields = MODEL_CONFIG_FIELDS
    else:
        raise OrchestrationError(
            f"Model configuration version must be {LEGACY_MODEL_CONFIG_VERSION} or {MODEL_CONFIG_VERSION}"
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
    if version == MODEL_CONFIG_VERSION:
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
    return {
        "version": MODEL_CONFIG_VERSION,
        "default_profile": default_profile,
        "profiles": profiles,
        "defaults": defaults,
        "roles": roles,
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


def effective_model_config(
    role: str,
    config: dict[str, Any],
    execution_profile: dict[str, Any] | None = None,
) -> dict[str, str]:
    effective = dict(DEFAULT_MODELS[role])
    if execution_profile is not None:
        effective["thinking"] = execution_profile["thinking"][role]
    effective.update(config["defaults"])
    effective.update(config["roles"].get(role, {}))
    return effective
