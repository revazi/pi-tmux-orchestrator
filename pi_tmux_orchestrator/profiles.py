"""Deterministic execution-profile policy."""

from __future__ import annotations

import re
from typing import Any

from .constants import DEFAULT_MODELS, THINKING_LEVELS
from .models import OrchestrationError

DEFAULT_EXECUTION_PROFILE = "thorough"
MAX_CUSTOM_PROFILES = 16
MAX_PROFILE_NAME_CHARS = 32
PROFILE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,31}")
PROFILE_KINDS = frozenset({"packaged", "custom"})
PROFILE_SOURCES = frozenset({"per-run", "user-global", "packaged-default"})
EXECUTION_PROFILE_FIELDS = frozenset({"name", "kind", "source"})

# The compatibility default preserves the pre-profile worker thinking levels.
# Comparative provider usage and quality remain unavailable until measured runs exist.
PACKAGED_EXECUTION_PROFILES: dict[str, dict[str, str]] = {
    "economy": {
        "implementer": "medium",
        "reviewer": "medium",
        "probe": "low",
        "playwright": "medium",
        "django": "medium",
    },
    "balanced": {
        "implementer": "high",
        "reviewer": "high",
        "probe": "medium",
        "playwright": "medium",
        "django": "medium",
    },
    "thorough": {role: config["thinking"] for role, config in DEFAULT_MODELS.items()},
}


def profile_name(value: str) -> str:
    if not isinstance(value, str) or not PROFILE_NAME_PATTERN.fullmatch(value):
        raise OrchestrationError(
            "Execution profile name must match [a-z][a-z0-9-]{0,31}"
        )
    return value


def validate_custom_profiles(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise OrchestrationError("Model configuration profiles must be an object")
    if len(value) > MAX_CUSTOM_PROFILES:
        raise OrchestrationError(
            f"Model configuration supports at most {MAX_CUSTOM_PROFILES} custom profiles"
        )
    expected_roles = set(DEFAULT_MODELS)
    profiles: dict[str, dict[str, str]] = {}
    for raw_name, raw_mapping in value.items():
        name = profile_name(raw_name)
        if name in PACKAGED_EXECUTION_PROFILES:
            raise OrchestrationError(
                f"Custom profile {name} cannot replace a packaged execution profile"
            )
        if not isinstance(raw_mapping, dict) or set(raw_mapping) != expected_roles:
            raise OrchestrationError(
                f"Custom profile {name} must map every known role exactly once"
            )
        mapping: dict[str, str] = {}
        for role in DEFAULT_MODELS:
            thinking = raw_mapping[role]
            if not isinstance(thinking, str) or thinking not in THINKING_LEVELS:
                raise OrchestrationError(
                    f"Custom profile {name}.{role} must be a supported thinking level"
                )
            mapping[role] = thinking
        profiles[name] = mapping
    return profiles


def resolve_execution_profile(
    config: dict[str, Any], requested: str | None = None
) -> dict[str, Any]:
    if requested is not None:
        name = profile_name(requested)
        source = "per-run"
    elif config.get("default_profile") is not None:
        name = profile_name(config["default_profile"])
        source = "user-global"
    else:
        name = DEFAULT_EXECUTION_PROFILE
        source = "packaged-default"

    custom_profiles = config.get("profiles", {})
    if name in custom_profiles:
        kind = "custom"
        thinking = custom_profiles[name]
    elif name in PACKAGED_EXECUTION_PROFILES:
        kind = "packaged"
        thinking = PACKAGED_EXECUTION_PROFILES[name]
    else:
        raise OrchestrationError(f"Unknown execution profile: {name}")
    return {
        "name": name,
        "kind": kind,
        "source": source,
        "thinking": dict(thinking),
    }


def public_execution_profile(profile: dict[str, Any]) -> dict[str, str]:
    return {field: profile[field] for field in ("name", "kind", "source")}


def validate_manifest_execution_profile(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != EXECUTION_PROFILE_FIELDS:
        raise OrchestrationError("Manifest execution profile has invalid fields")
    name = profile_name(value.get("name"))
    kind = value.get("kind")
    source = value.get("source")
    if kind not in PROFILE_KINDS or not isinstance(kind, str):
        raise OrchestrationError("Manifest execution profile kind is invalid")
    if source not in PROFILE_SOURCES or not isinstance(source, str):
        raise OrchestrationError("Manifest execution profile source is invalid")
    if kind == "packaged" and name not in PACKAGED_EXECUTION_PROFILES:
        raise OrchestrationError("Manifest packaged execution profile is unknown")
    if kind == "custom" and name in PACKAGED_EXECUTION_PROFILES:
        raise OrchestrationError("Manifest custom execution profile name is reserved")
    return {"name": name, "kind": kind, "source": source}


def retained_execution_profile(manifest: dict[str, Any]) -> dict[str, str | None]:
    if manifest.get("version", 0) < 4:
        return {"name": None, "kind": "unavailable", "source": "legacy"}
    return dict(manifest["execution_profile"])
