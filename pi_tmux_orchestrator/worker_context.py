"""Explicit metadata-only per-role provider-context projection policy."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .constants import KNOWN_ROLES
from .models import OrchestrationError

CONTEXT_MODES = frozenset({"prune", "retain"})


def worker_context_argument(value: str) -> tuple[str, str]:
    role, separator, mode = value.partition("=")
    if not separator or role not in KNOWN_ROLES or mode not in CONTEXT_MODES:
        raise OrchestrationError("worker context must be ROLE=prune or ROLE=retain")
    return role, mode


def context_policy(overrides: object, roles: set[str]) -> dict[str, Any]:
    if not isinstance(overrides, dict) or set(overrides) - roles:
        raise OrchestrationError("Worker context overrides require enabled roles")
    if any(
        not isinstance(mode, str) or mode not in CONTEXT_MODES
        for mode in overrides.values()
    ):
        raise OrchestrationError("Worker context mode must be prune or retain")
    return {"version": 1, "overrides": dict(overrides)}


def resolve_context_policy(
    selections: list[tuple[str, str]] | None, roles: set[str]
) -> dict[str, Any]:
    overrides: dict[str, str] = {}
    for role, mode in selections or []:
        if role in overrides:
            raise OrchestrationError("Duplicate worker context role selection")
        overrides[role] = mode
    return context_policy(overrides, roles)


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OrchestrationError("Worker context policy has duplicate fields")
        result[key] = value
    return result


def retained_context_policy(database: sqlite3.Connection) -> dict[str, Any]:
    row = database.execute(
        "SELECT value FROM meta WHERE key='worker_context_policy'"
    ).fetchone()
    roles = {row["role"] for row in database.execute("SELECT role FROM roles")}
    if row is None:
        schema = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if schema is not None and int(schema["value"]) >= 10:
            raise OrchestrationError("Retained worker context policy is missing")
        return context_policy({}, roles)
    if len(row["value"]) > 512:
        raise OrchestrationError("Retained worker context policy is oversized")
    try:
        value = json.loads(row["value"], object_pairs_hook=_unique_fields)
    except (ValueError, TypeError) as error:
        raise OrchestrationError("Retained worker context policy is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "overrides"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise OrchestrationError("Retained worker context policy is invalid")
    return context_policy(value["overrides"], roles)
