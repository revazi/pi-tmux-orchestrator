"""Strict user-global and per-run provider-usage budget policy."""

from __future__ import annotations

import json
import math
import os
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any

from . import runtime
from .models import OrchestrationError

BUDGET_CONFIG_VERSION = 1
MAX_BUDGET_CONFIG_BYTES = 64 * 1024
BUDGET_CONFIG_ENV = "PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG"
BUDGET_ENFORCEMENT = ("warn-only", "hard")
BUDGET_LEVELS = ("warning", "hard")
BUDGET_SCOPES = ("run", "role", "assignment")
BUDGET_INTEGER_METRICS = frozenset(
    {
        "provider_calls",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "operational_tokens",
        "context_tokens",
    }
)
BUDGET_NUMBER_METRICS = frozenset({"cost_total", "context_percent"})
BUDGET_METRICS = BUDGET_INTEGER_METRICS | BUDGET_NUMBER_METRICS
BUDGET_CONFIG_FIELDS = frozenset({"version", "enforcement", *BUDGET_LEVELS})
MAX_BUDGET_INTEGER = 10**12
MAX_BUDGET_COST = 10**9
MAX_BUDGET_OVERRIDES = len(BUDGET_LEVELS) * len(BUDGET_SCOPES) * len(BUDGET_METRICS)

_PACKAGED_BUDGET_POLICY = {
    "version": BUDGET_CONFIG_VERSION,
    "enforcement": "warn-only",
    "warning": {
        "run": {"operational_tokens": 600_000},
        "role": {"operational_tokens": 200_000},
        "assignment": {},
    },
    "hard": {scope: {} for scope in BUDGET_SCOPES},
}


def packaged_budget_policy() -> dict[str, Any]:
    return deepcopy(_PACKAGED_BUDGET_POLICY)


def budget_config_path(project: Path | None = None) -> Path:
    configured = os.environ.get(BUDGET_CONFIG_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise OrchestrationError(f"{BUDGET_CONFIG_ENV} must be an absolute path")
    else:
        if not runtime.PI_HOME.is_absolute():
            raise OrchestrationError(
                "Pi configuration directory must be an absolute path"
            )
        path = runtime.PI_HOME / "tmux-orchestrator-budgets.json"
    return _validate_budget_config_path(path, project)


def _validate_budget_config_path(path: Path, project: Path | None) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    if project is not None:
        project = project.resolve(strict=True)
        resolved = path.resolve(strict=False)
        if resolved == project or project in resolved.parents:
            raise OrchestrationError(
                "Budget configuration must remain outside the target project"
            )
    return path


def load_budget_config(
    path: Path | None = None, *, project: Path | None = None
) -> dict[str, Any]:
    config_path = (
        _validate_budget_config_path(path, project)
        if path is not None
        else budget_config_path(project)
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(config_path, flags)
    except FileNotFoundError:
        return packaged_budget_policy()
    except OSError as error:
        raise OrchestrationError(
            "Budget configuration cannot be opened safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OrchestrationError(
                "Budget configuration must be a regular non-symlink file"
            )
        if metadata.st_size > MAX_BUDGET_CONFIG_BYTES:
            raise OrchestrationError("Budget configuration exceeds the 64 KiB limit")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = None
            raw = handle.read(MAX_BUDGET_CONFIG_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_BUDGET_CONFIG_BYTES:
            raise OrchestrationError("Budget configuration exceeds the 64 KiB limit")
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise OrchestrationError(
            "Budget configuration is not valid UTF-8 JSON"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return validate_budget_config(value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def validate_budget_config(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - BUDGET_CONFIG_FIELDS:
        raise OrchestrationError(
            "Budget configuration has unsupported top-level fields"
        )
    if value.get("version") != BUDGET_CONFIG_VERSION:
        raise OrchestrationError(
            f"Budget configuration version must be {BUDGET_CONFIG_VERSION}"
        )
    result = packaged_budget_policy()
    if "enforcement" in value:
        enforcement = value["enforcement"]
        if enforcement not in BUDGET_ENFORCEMENT or not isinstance(enforcement, str):
            raise OrchestrationError(
                "Budget configuration enforcement must be warn-only or hard"
            )
        result["enforcement"] = enforcement
    for level in BUDGET_LEVELS:
        if level in value:
            _merge_budget_level(result[level], value[level], level)
    _validate_budget_order(result)
    return result


def _merge_budget_level(
    destination: dict[str, dict[str, int | float]], value: object, level: str
) -> None:
    if not isinstance(value, dict) or set(value) - set(BUDGET_SCOPES):
        raise OrchestrationError(f"Budget configuration {level} has unknown scopes")
    for scope, thresholds in value.items():
        if not isinstance(thresholds, dict) or set(thresholds) - BUDGET_METRICS:
            raise OrchestrationError(
                f"Budget configuration {level}.{scope} has unknown metrics"
            )
        for metric, raw in thresholds.items():
            if raw is None:
                destination[scope].pop(metric, None)
            else:
                destination[scope][metric] = validate_budget_value(
                    metric, raw, f"{level}.{scope}.{metric}"
                )


def validate_budget_value(metric: str, value: object, label: str) -> int | float:
    if metric in BUDGET_INTEGER_METRICS:
        if type(value) is not int or not 1 <= value <= MAX_BUDGET_INTEGER:
            raise OrchestrationError(
                f"Budget {label} must be an integer from 1 to {MAX_BUDGET_INTEGER}"
            )
        return value
    if metric not in BUDGET_NUMBER_METRICS:
        raise OrchestrationError(f"Budget {label} uses an unknown metric")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise OrchestrationError(f"Budget {label} must be a finite number")
    number = float(value)
    maximum = 100.0 if metric == "context_percent" else float(MAX_BUDGET_COST)
    if not 0 < number <= maximum:
        raise OrchestrationError(
            f"Budget {label} must be greater than zero and at most {maximum:g}"
        )
    return number


def _validate_budget_order(policy: dict[str, Any]) -> None:
    for scope in BUDGET_SCOPES:
        warning = policy["warning"][scope]
        hard = policy["hard"][scope]
        for metric in warning.keys() & hard.keys():
            if warning[metric] > hard[metric]:
                raise OrchestrationError(
                    f"Budget warning.{scope}.{metric} cannot exceed its hard threshold"
                )


def parse_budget_override(value: str) -> tuple[str, str, str, int | float | None]:
    key, separator, raw = value.partition("=")
    parts = key.split(".")
    if separator != "=" or len(parts) != 3 or not raw:
        raise OrchestrationError(
            "Budget override must use LEVEL.SCOPE.METRIC=VALUE or =off",
            "invalid_arguments",
        )
    level, scope, metric = parts
    if (
        level not in BUDGET_LEVELS
        or scope not in BUDGET_SCOPES
        or metric not in BUDGET_METRICS
    ):
        raise OrchestrationError(
            "Budget override uses an unknown level, scope, or metric",
            "invalid_arguments",
        )
    if raw == "off":
        return level, scope, metric, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise OrchestrationError(
            "Budget override value must be numeric or off", "invalid_arguments"
        ) from error
    return (
        level,
        scope,
        metric,
        validate_budget_value(metric, parsed, f"override {level}.{scope}.{metric}"),
    )


def _validated_effective_override(
    override: object,
) -> tuple[str, str, str, int | float | None]:
    if not isinstance(override, (list, tuple)) or len(override) != 4:
        raise OrchestrationError("Budget override is invalid", "invalid_arguments")
    level, scope, metric, value = override
    if (
        level not in BUDGET_LEVELS
        or scope not in BUDGET_SCOPES
        or metric not in BUDGET_METRICS
    ):
        raise OrchestrationError("Budget override is invalid", "invalid_arguments")
    if value is None:
        return level, scope, metric, None
    try:
        validated = validate_budget_value(
            metric, value, f"override {level}.{scope}.{metric}"
        )
    except OrchestrationError as error:
        raise OrchestrationError(str(error), "invalid_arguments") from error
    return level, scope, metric, validated


def effective_budget_policy(
    configured: dict[str, Any],
    *,
    enforcement: str | None = None,
    overrides: list[tuple[str, str, str, int | float | None]] | None = None,
) -> dict[str, Any]:
    policy = deepcopy(configured)
    if enforcement is not None:
        if enforcement not in BUDGET_ENFORCEMENT:
            raise OrchestrationError("Budget enforcement override is invalid")
        policy["enforcement"] = enforcement
    selected = overrides or []
    if len(selected) > MAX_BUDGET_OVERRIDES:
        raise OrchestrationError(
            f"At most {MAX_BUDGET_OVERRIDES} budget overrides are allowed",
            "invalid_arguments",
        )
    seen: set[tuple[str, str, str]] = set()
    for override in selected:
        level, scope, metric, value = _validated_effective_override(override)
        key = (level, scope, metric)
        if key in seen:
            raise OrchestrationError(
                "Budget overrides cannot repeat a threshold", "invalid_arguments"
            )
        seen.add(key)
        if value is None:
            policy[level][scope].pop(metric, None)
        else:
            policy[level][scope][metric] = value
    try:
        _validate_budget_order(policy)
    except OrchestrationError as error:
        raise OrchestrationError(str(error), "invalid_arguments") from error
    return policy
