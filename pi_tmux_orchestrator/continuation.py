"""Opt-in repair-round admission policy, independent of observational budgets."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .models import OrchestrationError

MAX_REPAIR_ROUNDS = 1_000_000


def validate_repair_limit(value: object) -> int | None:
    if value is not None and (
        type(value) is not int or not 0 <= value <= MAX_REPAIR_ROUNDS
    ):
        raise OrchestrationError(
            f"max repair rounds must be an integer from 0 to {MAX_REPAIR_ROUNDS}"
        )
    return value


def repair_policy(limit: object) -> dict[str, Any]:
    return {"version": 1, "max_repair_rounds": validate_repair_limit(limit)}


def _unique_policy_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OrchestrationError(
                "Retained continuation policy has duplicate fields"
            )
        result[key] = value
    return result


def retained_repair_policy(database: sqlite3.Connection) -> dict[str, Any]:
    row = database.execute(
        "SELECT value FROM meta WHERE key='continuation_policy'"
    ).fetchone()
    if row is None:
        schema = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if schema is not None and int(schema["value"]) >= 9:
            raise OrchestrationError("Retained continuation policy is missing")
        return repair_policy(None)
    if len(row["value"]) > 256:
        raise OrchestrationError("Retained continuation policy is oversized")
    try:
        value = json.loads(row["value"], object_pairs_hook=_unique_policy_fields)
    except (ValueError, TypeError) as error:
        raise OrchestrationError("Retained continuation policy is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "max_repair_rounds"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise OrchestrationError("Retained continuation policy is invalid")
    return repair_policy(value["max_repair_rounds"])


def continuation_status(database: sqlite3.Connection) -> dict[str, Any]:
    policy = retained_repair_policy(database)
    used = database.execute(
        "SELECT COUNT(DISTINCT round) FROM assignments "
        "WHERE role='implementer' AND kind='implementation' AND round>1"
    ).fetchone()[0]
    pending = database.execute(
        "SELECT value FROM meta WHERE key='pending_repair_round'"
    ).fetchone()
    pending_round = None
    if pending is not None:
        try:
            pending_round = int(pending["value"])
        except (ValueError, TypeError) as error:
            raise OrchestrationError(
                "Retained repair continuation is invalid"
            ) from error
        if pending_round < 2:
            raise OrchestrationError("Retained repair continuation is invalid")
    return {
        **policy,
        "repair_rounds_admitted": used,
        "pending_repair_round": pending_round,
        "pause_reason": "repair_round_limit" if pending_round is not None else None,
    }


def repair_limit_reached(
    database: sqlite3.Connection, role: str, kind: str, round_number: int
) -> bool:
    # Initial implementation and same-round phased plans do not consume repairs.
    if role != "implementer" or kind != "implementation" or round_number <= 1:
        return False
    status = continuation_status(database)
    limit = status["max_repair_rounds"]
    return limit is not None and status["repair_rounds_admitted"] >= limit


def approved_repair_extension(
    database: sqlite3.Connection,
) -> tuple[int, dict[str, Any]]:
    """Validate an explicit one-round approval; caller commits it with command ID."""
    status = continuation_status(database)
    state = database.execute(
        "SELECT value FROM meta WHERE key='workflow_state'"
    ).fetchone()[0]
    active = database.execute(
        "SELECT 1 FROM roles WHERE active_assignment_id IS NOT NULL "
        "OR state IN ('active','waiting','restarting','recovering','uncertain')"
    ).fetchone()
    limit = status["max_repair_rounds"]
    pending = status["pending_repair_round"]
    if (
        state != "needs_attention"
        or pending is None
        or limit is None
        or active is not None
    ):
        raise OrchestrationError(
            "No safely paused repair continuation is available", "conflict"
        )
    # Approve one additional admission, not a fresh allowance for the same work.
    return pending, repair_policy(max(limit, status["repair_rounds_admitted"]) + 1)
