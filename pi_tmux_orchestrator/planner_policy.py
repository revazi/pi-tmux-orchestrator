"""Strict user-global preflight planner decision-model policy."""

from __future__ import annotations

import argparse
import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Any

from . import runtime
from .models import CommandResult, OrchestrationError
from .output import human_print
from .planning import metadata_digest

PLANNER_POLICY_VERSION = 1
MAX_PLANNER_POLICY_BYTES = 32 * 1024
MAX_PLANNER_FALLBACKS = 16
JEV_GUIDANCE_VERSION = 1
MAX_JEV_GUIDANCE_BYTES = 16 * 1024
PLANNER_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)
PLANNER_NO_ELIGIBLE = frozenset({"cancel", "static"})
PLANNER_POLICY_FIELDS = frozenset({"version", "preferred", "fallbacks", "noEligible"})
PLANNER_POLICY_OPTIONAL_FIELDS = frozenset({"jevGuidance"})
PLANNER_MODEL_FIELDS = frozenset({"provider", "model", "thinking"})
PLANNER_POLICY_ENV = "PI_TMUX_ORCHESTRATOR_PLANNER_CONFIG"


def planner_policy_path(project: Path | None = None) -> Path:
    configured = os.environ.get(PLANNER_POLICY_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise OrchestrationError(f"{PLANNER_POLICY_ENV} must be an absolute path")
        return _validate_planner_policy_path(path, project)
    pi_home = runtime.PI_HOME
    if not pi_home.is_absolute():
        raise OrchestrationError("Pi configuration directory must be an absolute path")
    return _validate_planner_policy_path(
        pi_home / "tmux-orchestrator-planner.json", project
    )


def _validate_planner_policy_path(path: Path, project: Path | None) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    if project is not None:
        project = project.resolve(strict=True)
        resolved = path.resolve(strict=False)
        if resolved == project or project in resolved.parents:
            raise OrchestrationError(
                "Planner policy configuration must remain outside the target project"
            )
    return path


def empty_planner_policy() -> dict[str, Any]:
    return {
        "version": PLANNER_POLICY_VERSION,
        "preferred": None,
        "fallbacks": [],
        "no_eligible": "cancel",
        "jev_guidance": None,
    }


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _bounded_identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise OrchestrationError(
            f"Planner policy {label} must be a bounded canonical identifier"
        )
    return value


def _decision_model(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != PLANNER_MODEL_FIELDS:
        raise OrchestrationError(
            f"Planner policy {label} must contain exactly provider, model, and thinking"
        )
    thinking = value.get("thinking")
    if not isinstance(thinking, str) or thinking not in PLANNER_THINKING_LEVELS:
        raise OrchestrationError(
            f"Planner policy {label}.thinking must be off, minimal, low, medium, high, xhigh, or max"
        )
    return {
        "provider": _bounded_identifier(value.get("provider"), f"{label}.provider"),
        "model": _bounded_identifier(value.get("model"), f"{label}.model"),
        "thinking": thinking,
    }


def _jev_guidance(value: object) -> str | None:
    if value is None:
        return None
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError as error:
        raise OrchestrationError(
            "Planner policy jevGuidance must be bounded natural-language text or null"
        ) from error
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(encoded) > MAX_JEV_GUIDANCE_BYTES
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
    ):
        raise OrchestrationError(
            "Planner policy jevGuidance must be bounded natural-language text or null"
        )
    return value


def validate_planner_policy(value: object) -> dict[str, Any]:
    fields = set(value) if isinstance(value, dict) else set()
    if (
        not isinstance(value, dict)
        or not PLANNER_POLICY_FIELDS.issubset(fields)
        or not fields.issubset(PLANNER_POLICY_FIELDS | PLANNER_POLICY_OPTIONAL_FIELDS)
    ):
        raise OrchestrationError(
            "Planner policy must contain version, preferred, fallbacks, and noEligible, with optional jevGuidance"
        )
    if value.get("version") != PLANNER_POLICY_VERSION:
        raise OrchestrationError(
            f"Planner policy version must be {PLANNER_POLICY_VERSION}"
        )
    preferred_value = value.get("preferred")
    preferred = (
        None
        if preferred_value is None
        else _decision_model(preferred_value, "preferred")
    )
    fallback_values = value.get("fallbacks")
    if (
        not isinstance(fallback_values, list)
        or len(fallback_values) > MAX_PLANNER_FALLBACKS
    ):
        raise OrchestrationError(
            f"Planner policy fallbacks must contain at most {MAX_PLANNER_FALLBACKS} entries"
        )
    fallbacks = [
        _decision_model(item, f"fallbacks[{index}]")
        for index, item in enumerate(fallback_values)
    ]
    candidates = ([preferred] if preferred is not None else []) + fallbacks
    identities = [(item["provider"], item["model"]) for item in candidates]
    if len(set(identities)) != len(identities):
        raise OrchestrationError(
            "Planner policy preferred and fallbacks must use unique provider/model identities"
        )
    no_eligible = value.get("noEligible")
    if not isinstance(no_eligible, str) or no_eligible not in PLANNER_NO_ELIGIBLE:
        raise OrchestrationError("Planner policy noEligible must be cancel or static")
    return {
        "version": PLANNER_POLICY_VERSION,
        "preferred": preferred,
        "fallbacks": fallbacks,
        "no_eligible": no_eligible,
        "jev_guidance": _jev_guidance(value.get("jevGuidance")),
    }


def load_planner_policy(
    path: Path | None = None, *, project: Path | None = None
) -> dict[str, Any]:
    policy_path = (
        _validate_planner_policy_path(path, project)
        if path is not None
        else planner_policy_path(project)
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(policy_path, flags)
    except FileNotFoundError:
        return empty_planner_policy()
    except OSError as error:
        raise OrchestrationError(
            "Planner policy configuration cannot be opened safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OrchestrationError(
                "Planner policy configuration must be a regular non-symlink file"
            )
        if metadata.st_size > MAX_PLANNER_POLICY_BYTES:
            raise OrchestrationError("Planner policy exceeds the 32 KiB limit")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = None
            raw = handle.read(MAX_PLANNER_POLICY_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_PLANNER_POLICY_BYTES:
            raise OrchestrationError("Planner policy exceeds the 32 KiB limit")
        value = json.loads(raw, object_pairs_hook=unique_json_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise OrchestrationError("Planner policy is not valid UTF-8 JSON") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return validate_planner_policy(value)


def planner_policy_command(args: argparse.Namespace) -> CommandResult:
    try:
        project = Path(args.project).expanduser().resolve(strict=True)
    except OSError as error:
        raise OrchestrationError(
            f"Project directory does not exist: {args.project}"
        ) from error
    if not project.is_dir():
        raise OrchestrationError(f"Project directory does not exist: {project}")
    path = planner_policy_path(project)
    configured = path.exists()
    loaded_policy = load_planner_policy(path, project=project)
    guidance_text = loaded_policy["jev_guidance"]
    policy = {
        key: value for key, value in loaded_policy.items() if key != "jev_guidance"
    }
    guidance = {
        "version": JEV_GUIDANCE_VERSION,
        "config_path": str(path),
        "configured": guidance_text is not None,
        "digest": metadata_digest(
            {"version": JEV_GUIDANCE_VERSION, "text": guidance_text}
        ),
        "text": guidance_text,
    }
    human_print(
        f"Planner policy: {path} "
        f"({'configured' if configured else 'default cancel'}; "
        f"Jev guidance {'configured' if guidance['configured'] else 'default'})"
    )
    return CommandResult(
        data={
            "config_path": str(path),
            "configured": configured,
            "binding_digest": metadata_digest(
                {
                    "config_path": str(path),
                    "configured": configured,
                    "policy": policy,
                    "jev_guidance": {
                        "version": guidance["version"],
                        "config_path": guidance["config_path"],
                        "configured": guidance["configured"],
                        "digest": guidance["digest"],
                    },
                }
            ),
            "policy": policy,
            "jev_guidance": guidance,
        }
    )
