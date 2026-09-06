"""Model-free, metadata-only retained usage analysis for development benchmarks."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from math import ceil
from pathlib import Path
from typing import Any

from . import runtime
from .broker_store import (
    connect_broker_database,
    public_assignment_usage,
    public_broker_snapshot,
)
from .constants import KNOWN_ROLES, MAX_JSON_ITEMS
from .models import OrchestrationError
from .protocol import REPORT_KINDS
from .specialist_activation import ACTIVATION_DECISIONS, SPECIALIST_ROLES
from .supervisor_api import retained_runs, retained_sessions

ANALYSIS_SCHEMA_VERSION = 2


def _empty_role_usage() -> dict[str, int | float]:
    return {
        "run_count": 0,
        "provider_calls": 0,
        "provider_calls_unavailable_runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "reasoning_unavailable_runs": 0,
        "operational_tokens": 0,
        "provider_cost": 0.0,
    }


def _add_role_usage(target: dict[str, int | float], role: dict[str, Any]) -> None:
    target["run_count"] += 1
    provider_calls = role.get("provider_calls")
    if provider_calls is None:
        target["provider_calls_unavailable_runs"] += 1
    else:
        target["provider_calls"] += provider_calls
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        target[field] += role[field]
    reasoning = role.get("reasoning_tokens")
    if reasoning is None:
        target["reasoning_unavailable_runs"] += 1
    else:
        target["reasoning_tokens"] += reasoning
    target["operational_tokens"] += role["total_tokens"]
    target["provider_cost"] += role["cost_total"]


def _total_usage(roles: list[dict[str, Any]]) -> dict[str, int | float]:
    total = _empty_role_usage()
    total.pop("run_count")
    for role in roles:
        for field, value in role.items():
            if field in total:
                total[field] += value
    total["provider_cost"] = round(float(total["provider_cost"]), 12)
    return total


def _distribution(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "median": None,
            "p95": None,
            "maximum": None,
            "sum": 0,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "median": ordered[(len(ordered) - 1) // 2],
        "p95": ordered[ceil(len(ordered) * 0.95) - 1],
        "maximum": ordered[-1],
        "sum": sum(ordered),
    }


def _empty_assignment_bucket() -> dict[str, Any]:
    return {
        "assignment_count": 0,
        "usage_available": 0,
        "provider_calls": [],
        "peak_context_tokens": [],
    }


def _add_assignment_usage(bucket: dict[str, Any], assignment: dict[str, Any]) -> None:
    bucket["assignment_count"] += 1
    usage = assignment["usage"]
    if usage is None:
        return
    bucket["usage_available"] += 1
    bucket["provider_calls"].append(usage["provider_calls"])
    if usage["peak_context_tokens"] is not None:
        bucket["peak_context_tokens"].append(usage["peak_context_tokens"])


def _public_assignment_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "assignment_count": bucket["assignment_count"],
        "usage_available": bucket["usage_available"],
        "usage_unavailable": bucket["assignment_count"] - bucket["usage_available"],
        "provider_calls": _distribution(bucket["provider_calls"]),
        "peak_context_tokens": _distribution(bucket["peak_context_tokens"]),
    }


def _grouped_assignment_buckets(
    buckets: dict[str, dict[str, Any]], field: str
) -> list[dict[str, Any]]:
    return [
        {field: value, **_public_assignment_bucket(bucket)}
        for value, bucket in sorted(buckets.items())
    ]


_ASSIGNMENT_STATES = frozenset({"delivering", "accepted", "uncertain", "completed"})
_WORKFLOW_STATES = frozenset(
    {
        "starting",
        "connecting",
        "routing",
        "initializing",
        "active",
        "ready",
        "needs_attention",
        "uncertain",
    }
)
_GROUPED_FIELDS = {
    "assignments": {"state", "role", "kind"},
    "specialist_activations": {"decision", "role"},
}


def _grouped_counts(
    database: sqlite3.Connection, table: str, field: str
) -> dict[str, int]:
    if field not in _GROUPED_FIELDS.get(table, set()):
        raise OrchestrationError("Workflow-shape grouping field is invalid")
    rows = list(
        database.execute(
            f"SELECT {field} AS value,COUNT(*) AS count FROM {table} "
            f"GROUP BY {field} ORDER BY {field} LIMIT ?",
            (MAX_JSON_ITEMS + 1,),
        )
    )
    if len(rows) > MAX_JSON_ITEMS:
        raise OrchestrationError(
            "Retained workflow-shape groups exceed the analysis limit"
        )
    return {row["value"]: row["count"] for row in rows}


def _specialist_activation_summary(
    database: sqlite3.Connection, schema_version: int
) -> dict[str, Any] | None:
    if schema_version < 7:
        return None
    totals = database.execute(
        "SELECT COUNT(*) AS count,COALESCE(SUM(forced),0) AS forced_count,"
        "SUM(CASE WHEN forced NOT IN (0,1) THEN 1 ELSE 0 END) AS invalid_forced_count "
        "FROM specialist_activations"
    ).fetchone()
    decisions = _grouped_counts(database, "specialist_activations", "decision")
    roles = _grouped_counts(database, "specialist_activations", "role")
    if (
        totals["invalid_forced_count"]
        or not set(decisions).issubset(ACTIVATION_DECISIONS)
        or not set(roles).issubset(SPECIALIST_ROLES)
    ):
        raise OrchestrationError("Retained specialist activation is invalid")
    return {
        "count": totals["count"],
        "forced_count": totals["forced_count"],
        "decisions": decisions,
        "roles": roles,
    }


def _validate_assignment_usage(page: dict[str, Any]) -> None:
    for assignment in page["assignments"]:
        valid = (
            assignment.get("role") in KNOWN_ROLES
            and assignment.get("kind") in REPORT_KINDS
            and type(assignment.get("round")) is int
            and assignment["round"] > 0
        )
        if not valid:
            raise OrchestrationError("Retained assignment usage metadata is invalid")
        usage = assignment.get("usage")
        if usage is None:
            continue
        provider_calls = usage.get("provider_calls")
        peak_context_tokens = usage.get("peak_context_tokens")
        if (
            type(provider_calls) is not int
            or provider_calls < 0
            or (
                peak_context_tokens is not None
                and (type(peak_context_tokens) is not int or peak_context_tokens < 0)
            )
        ):
            raise OrchestrationError("Retained assignment usage values are invalid")


def _validate_broker_snapshot(snapshot: dict[str, Any]) -> None:
    workflow = snapshot["workflow"]
    roles = snapshot["roles"]
    if (
        workflow["state"] not in _WORKFLOW_STATES
        or type(workflow["round"]) is not int
        or workflow["round"] < 1
        or len(roles) > len(KNOWN_ROLES)
        or any(role.get("role") not in KNOWN_ROLES for role in roles)
    ):
        raise OrchestrationError("Retained broker snapshot metadata is invalid")


def _run_workflow_metadata(coord: Path) -> dict[str, Any]:
    """Read only body-free workflow-shape metadata for one retained broker run."""

    with connect_broker_database(coord, readonly=True) as database:
        schema_row = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if schema_row is None:
            raise OrchestrationError("Broker database schema is unavailable")
        schema_version = int(schema_row["value"])
        assignment_summary = {
            "count": database.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
            "repair_count": database.execute(
                "SELECT COUNT(*) FROM assignments WHERE round > 1"
            ).fetchone()[0],
            "specialist_count": database.execute(
                "SELECT COUNT(*) FROM assignments WHERE role IN (?,?,?)",
                SPECIALIST_ROLES,
            ).fetchone()[0],
            "states": _grouped_counts(database, "assignments", "state"),
            "roles": _grouped_counts(database, "assignments", "role"),
            "kinds": _grouped_counts(database, "assignments", "kind"),
        }
        if (
            not set(assignment_summary["states"]).issubset(_ASSIGNMENT_STATES)
            or not set(assignment_summary["roles"]).issubset(KNOWN_ROLES)
            or not set(assignment_summary["kinds"]).issubset(REPORT_KINDS)
        ):
            raise OrchestrationError("Retained assignment metadata is invalid")
        activation_summary = _specialist_activation_summary(database, schema_version)
    assignment_usage = public_assignment_usage(coord, limit=MAX_JSON_ITEMS)
    _validate_assignment_usage(assignment_usage)
    return {
        "schema_version": schema_version,
        "assignment_summary": assignment_summary,
        "assignment_usage": assignment_usage,
        "specialist_activations": activation_summary,
    }


def analyze_retained_usage(state_root: Path, *, max_runs: int = 100) -> dict[str, Any]:
    """Aggregate only body-free broker metadata from a bounded retained-state scan."""

    if type(max_runs) is not int or not 1 <= max_runs <= MAX_JSON_ITEMS:
        raise OrchestrationError(
            f"Retained usage run limit must be between 1 and {MAX_JSON_ITEMS}",
            "invalid_arguments",
        )
    root = state_root.expanduser()
    if not root.is_absolute():
        raise OrchestrationError("Retained usage state root must be absolute")

    previous_root = runtime.STATE_ROOT
    runtime.STATE_ROOT = root
    try:
        session_page = retained_sessions()
        role_totals: dict[str, dict[str, int | float]] = {}
        workflow_states: Counter[str] = Counter()
        implementation_flows: Counter[str] = Counter()
        run_rounds: Counter[int] = Counter()
        assignment_states: Counter[str] = Counter()
        assignment_roles: Counter[str] = Counter()
        assignment_kinds: Counter[str] = Counter()
        activation_decisions: Counter[str] = Counter()
        activation_roles: Counter[str] = Counter()
        assignment_buckets = _empty_assignment_bucket()
        assignment_by_role: dict[str, dict[str, Any]] = defaultdict(
            _empty_assignment_bucket
        )
        assignment_by_kind: dict[str, dict[str, Any]] = defaultdict(
            _empty_assignment_bucket
        )
        assignment_by_stage: dict[str, dict[str, Any]] = defaultdict(
            _empty_assignment_bucket
        )
        run_provider_calls: list[int] = []
        run_assignment_counts: list[int] = []
        sessions_analyzed: set[str] = set()
        runs_analyzed = 0
        runs_with_usage = 0
        runs_provider_calls_unavailable = 0
        assignment_usage_truncated_runs = 0
        activation_runs_available = 0
        activation_runs_unavailable = 0
        activation_count = 0
        forced_activations = 0
        repair_assignments = 0
        specialist_assignments = 0
        legacy_runs_skipped = 0
        issue_count = len(session_page["issues"])
        truncated = bool(session_page["truncated"])

        for session_index, session in enumerate(session_page["sessions"]):
            remaining = max_runs - runs_analyzed
            if remaining <= 0:
                truncated = True
                break
            runs, issues, runs_truncated = retained_runs(
                session["session"], limit=min(remaining, MAX_JSON_ITEMS)
            )
            issue_count += len(issues)
            truncated = truncated or runs_truncated
            for coord, manifest in runs:
                if manifest.get("version", 0) < 3:
                    legacy_runs_skipped += 1
                    continue
                try:
                    snapshot = public_broker_snapshot(coord)
                    _validate_broker_snapshot(snapshot)
                    workflow = _run_workflow_metadata(coord)
                except (
                    OSError,
                    OrchestrationError,
                    sqlite3.Error,
                    TypeError,
                    ValueError,
                ):
                    issue_count += 1
                    continue
                runs_analyzed += 1
                sessions_analyzed.add(session["session"])
                workflow_states[snapshot["workflow"]["state"]] += 1
                implementation_flows[snapshot["workflow"]["implementation_flow"]] += 1
                run_rounds[snapshot["workflow"]["round"]] += 1
                provider_calls = snapshot["usage"]["provider_calls"]
                if provider_calls is None:
                    runs_provider_calls_unavailable += 1
                else:
                    run_provider_calls.append(provider_calls)
                if snapshot["usage"]["total_tokens"] > 0:
                    runs_with_usage += 1
                for role in snapshot["roles"]:
                    target = role_totals.setdefault(role["role"], _empty_role_usage())
                    _add_role_usage(target, role)

                assignment_summary = workflow["assignment_summary"]
                run_assignment_counts.append(assignment_summary["count"])
                assignment_states.update(assignment_summary["states"])
                assignment_roles.update(assignment_summary["roles"])
                assignment_kinds.update(assignment_summary["kinds"])
                repair_assignments += assignment_summary["repair_count"]
                specialist_assignments += assignment_summary["specialist_count"]

                activation_summary = workflow["specialist_activations"]
                if activation_summary is None:
                    activation_runs_unavailable += 1
                else:
                    activation_runs_available += 1
                    activation_count += activation_summary["count"]
                    forced_activations += activation_summary["forced_count"]
                    activation_decisions.update(activation_summary["decisions"])
                    activation_roles.update(activation_summary["roles"])

                usage_page = workflow["assignment_usage"]
                if usage_page["truncated"]:
                    assignment_usage_truncated_runs += 1
                for assignment in usage_page["assignments"]:
                    _add_assignment_usage(assignment_buckets, assignment)
                    _add_assignment_usage(
                        assignment_by_role[assignment["role"]], assignment
                    )
                    _add_assignment_usage(
                        assignment_by_kind[assignment["kind"]], assignment
                    )
                    stage = "initial" if assignment["round"] == 1 else "repair"
                    _add_assignment_usage(assignment_by_stage[stage], assignment)

                if runs_analyzed >= max_runs:
                    if session_index + 1 < len(session_page["sessions"]):
                        truncated = True
                    break

        roles = []
        for role, values in sorted(role_totals.items()):
            normalized = dict(values)
            normalized["provider_cost"] = round(float(values["provider_cost"]), 12)
            roles.append({"role": role, **normalized})
        return {
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "source": "retained-metadata-only",
            "runs_analyzed": runs_analyzed,
            "runs_with_usage": runs_with_usage,
            "runs_provider_calls_unavailable": runs_provider_calls_unavailable,
            "sessions_analyzed": len(sessions_analyzed),
            "legacy_runs_skipped": legacy_runs_skipped,
            "issue_count": issue_count,
            "truncated": truncated,
            "workflow_states": dict(sorted(workflow_states.items())),
            "implementation_flows": dict(sorted(implementation_flows.items())),
            "run_rounds": {
                str(round_number): count
                for round_number, count in sorted(run_rounds.items())
            },
            "run_provider_calls": _distribution(run_provider_calls),
            "workflow_shape": {
                "run_assignment_count": _distribution(run_assignment_counts),
                "assignments": {
                    "count": sum(assignment_states.values()),
                    "repair_count": repair_assignments,
                    "specialist_count": specialist_assignments,
                    "states": dict(sorted(assignment_states.items())),
                    "roles": dict(sorted(assignment_roles.items())),
                    "kinds": dict(sorted(assignment_kinds.items())),
                },
                "specialist_activations": {
                    "runs_available": activation_runs_available,
                    "runs_unavailable": activation_runs_unavailable,
                    "count": activation_count,
                    "forced_count": forced_activations,
                    "decisions": dict(sorted(activation_decisions.items())),
                    "roles": dict(sorted(activation_roles.items())),
                },
            },
            "assignment_usage": {
                **_public_assignment_bucket(assignment_buckets),
                "truncated_runs": assignment_usage_truncated_runs,
                "by_role": _grouped_assignment_buckets(assignment_by_role, "role"),
                "by_kind": _grouped_assignment_buckets(assignment_by_kind, "kind"),
                "by_stage": _grouped_assignment_buckets(assignment_by_stage, "stage"),
            },
            "roles": roles,
            "total": _total_usage(roles),
            "semantics": {
                "operational_tokens": "input + output + cache read + cache write; not a billing unit",
                "provider_calls": "summed only when retained; unavailable run counts are separate",
                "assignment_usage": "accepted report deltas only; incomplete assignments remain in workflow shape",
                "provider_cost": "provider-reported cost only; unavailable values are not estimated",
                "reasoning_tokens": "summed only when exposed; unavailable run counts are separate",
                "payload_bodies_read": False,
                "production_wire_acceptance": False,
            },
        }
    finally:
        runtime.STATE_ROOT = previous_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate bounded metadata-only usage from retained orchestrator runs."
    )
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--max-runs", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        result = analyze_retained_usage(args.state_root, max_runs=args.max_runs)
    except OrchestrationError as error:
        parser.error(str(error))
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
