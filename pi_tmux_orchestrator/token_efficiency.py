"""Model-free, metadata-only retained usage analysis for development benchmarks."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from . import runtime
from .broker_store import public_broker_snapshot
from .constants import MAX_JSON_ITEMS
from .models import OrchestrationError
from .supervisor_api import retained_runs, retained_sessions

ANALYSIS_SCHEMA_VERSION = 1


def _empty_role_usage() -> dict[str, int | float]:
    return {
        "run_count": 0,
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


def analyze_retained_usage(state_root: Path, *, max_runs: int = 100) -> dict[str, Any]:
    """Aggregate only public broker metadata from a bounded retained-state scan."""

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
        sessions_analyzed: set[str] = set()
        runs_analyzed = 0
        runs_with_usage = 0
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
                except (OSError, OrchestrationError):
                    issue_count += 1
                    continue
                runs_analyzed += 1
                sessions_analyzed.add(session["session"])
                workflow_states[snapshot["workflow"]["state"]] += 1
                if snapshot["usage"]["total_tokens"] > 0:
                    runs_with_usage += 1
                for role in snapshot["roles"]:
                    target = role_totals.setdefault(role["role"], _empty_role_usage())
                    _add_role_usage(target, role)
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
            "sessions_analyzed": len(sessions_analyzed),
            "legacy_runs_skipped": legacy_runs_skipped,
            "issue_count": issue_count,
            "truncated": truncated,
            "workflow_states": dict(sorted(workflow_states.items())),
            "roles": roles,
            "total": _total_usage(roles),
            "semantics": {
                "operational_tokens": "input + output + cache read + cache write; not a billing unit",
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
