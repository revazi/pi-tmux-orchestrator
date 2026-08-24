"""Authoritative report-time hard-budget evaluation for broker routing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .budgeting import BUDGET_METRICS, packaged_budget_policy, validate_budget_config
from .models import OrchestrationError

_USAGE_COLUMNS = {
    "provider_calls": "provider_calls",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "cost_total": "cost_total",
    "context_tokens": "context_tokens",
    "context_percent": "context_percent",
}


def retained_budget_policy(database: Any) -> dict[str, Any]:
    row = database.execute(
        "SELECT value FROM meta WHERE key='budget_policy'"
    ).fetchone()
    if row is None:
        return packaged_budget_policy()
    try:
        value = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError) as error:
        raise OrchestrationError("Retained budget policy is invalid") from error
    return validate_budget_config(value)


def budget_fingerprint(finding: dict[str, Any]) -> str:
    identity = [
        finding["scope"],
        None if finding["scope"] == "run" else finding["role"],
        finding["assignment_id"],
        finding["metric"],
        finding["threshold"],
    ]
    encoded = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()[:32]


def _operational_tokens(value: dict[str, int | float | None]) -> int | None:
    fields = (
        value["input_tokens"],
        value["output_tokens"],
        value["cache_read_tokens"],
        value["cache_write_tokens"],
    )
    if any(item is None for item in fields):
        return None
    return sum(int(item) for item in fields if item is not None)


def _row_usage(row: Any) -> dict[str, int | float | None]:
    usage = {metric: row[column] for metric, column in _USAGE_COLUMNS.items()}
    usage["operational_tokens"] = _operational_tokens(usage)
    return usage


def _known_sum(values: list[int | float | None]) -> int | float | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _run_usage(role_usage: list[dict[str, int | float | None]]) -> dict[str, Any]:
    result = {
        metric: _known_sum([usage[metric] for usage in role_usage])
        for metric in BUDGET_METRICS - {"context_percent"}
    }
    known_percent = [
        usage["context_percent"]
        for usage in role_usage
        if usage["context_percent"] is not None
    ]
    result["context_percent"] = max(known_percent) if known_percent else None
    return result


def _is_overridden(database: Any, fingerprint: str) -> bool:
    row = database.execute(
        "SELECT status FROM budget_exhaustions WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    return row is not None and row["status"] == "overridden"


def _finding(
    database: Any,
    *,
    scope: str,
    role: str,
    assignment_id: str | None,
    usage: dict[str, Any],
    thresholds: dict[str, int | float],
) -> dict[str, Any] | None:
    for metric in sorted(thresholds):
        observed = usage.get(metric)
        threshold = thresholds[metric]
        if observed is None or observed < threshold:
            continue
        finding = {
            "scope": scope,
            "role": role,
            "assignment_id": assignment_id,
            "metric": metric,
            "observed": observed,
            "threshold": threshold,
        }
        finding["fingerprint"] = budget_fingerprint(finding)
        if not _is_overridden(database, finding["fingerprint"]):
            return finding
    return None


def _round_assignments(database: Any, role: str, round_number: int) -> list[Any]:
    return list(
        database.execute(
            "SELECT assignment_id,role,provider_calls,input_tokens,output_tokens,"
            "cache_read_tokens,cache_write_tokens,reasoning_tokens,cost_total,"
            "context_tokens,context_percent FROM reports WHERE round=? "
            "ORDER BY CASE WHEN role=? THEN 0 ELSE 1 END,created_at DESC,rowid DESC",
            (round_number, role),
        )
    )


def first_hard_budget_exhaustion(
    database: Any, *, trigger_role: str, round_number: int
) -> dict[str, Any] | None:
    """Return the first proven, non-overridden hard limit in stable scope order."""

    policy = retained_budget_policy(database)
    if policy["enforcement"] != "hard":
        return None
    hard = policy["hard"]
    for assignment in _round_assignments(database, trigger_role, round_number):
        finding = _finding(
            database,
            scope="assignment",
            role=assignment["role"],
            assignment_id=assignment["assignment_id"],
            usage=_row_usage(assignment),
            thresholds=hard["assignment"],
        )
        if finding is not None:
            return finding

    role_rows = list(
        database.execute(
            "SELECT role,provider_calls,input_tokens,output_tokens,cache_read_tokens,"
            "cache_write_tokens,reasoning_tokens,cost_total,context_tokens,"
            "context_percent FROM roles ORDER BY CASE WHEN role=? THEN 0 ELSE 1 END,role",
            (trigger_role,),
        )
    )
    usages = [(row["role"], _row_usage(row)) for row in role_rows]
    for role, usage in usages:
        finding = _finding(
            database,
            scope="role",
            role=role,
            assignment_id=None,
            usage=usage,
            thresholds=hard["role"],
        )
        if finding is not None:
            return finding

    return _finding(
        database,
        scope="run",
        role=trigger_role,
        assignment_id=None,
        usage=_run_usage([usage for _, usage in usages]),
        thresholds=hard["run"],
    )
