"""Pure, versioned assignment-boundary advice with no context or workflow authority."""

from __future__ import annotations

import json
from typing import Any

from .role_registry import CONTRACTS

MAX_CONTEXT_ADVICE_BYTES = 512
HIGH_CONTEXT_PERCENT = 80
_FACTS = {
    "relationship": ("same", "related", "different", "unknown"),
    "evidence": ("unchanged", "stale", "unknown"),
    "pressure": ("low", "high", "unknown"),
}
_ASSIGNMENT_KINDS = {*CONTRACTS, "plan", "implementation", "review"}
_TRADEOFFS = {
    "retain": ("keeps_prior_exchanges", "may_increase_context"),
    "prune": ("drops_prior_assignment_context", "may_reduce_cache_prefix_reuse"),
    None: (),
}


def context_pressure(percent: object) -> str:
    """Band a prior assignment's Pi occupancy sample, not a next-request estimate."""
    if type(percent) not in {int, float} or not 0 <= percent < float("inf"):
        return "unknown"
    return "high" if percent >= HIGH_CONTEXT_PERCENT else "low"


def context_advice(facts: object) -> dict[str, Any]:
    if type(facts) is not dict or set(facts) != set(_FACTS):
        raise ValueError("Context advice facts are invalid")
    if any(
        type(facts[key]) is not str or facts[key] not in allowed
        for key, allowed in _FACTS.items()
    ):
        raise ValueError("Context advice facts are invalid")
    relationship, evidence, pressure = (facts[key] for key in _FACTS)
    unknown = [key for key in _FACTS if facts[key] == "unknown"]
    recommendation = None
    reasons = [f"{relationship}_work"]
    if relationship == "different":
        recommendation = "prune"
    elif relationship in {"same", "related"}:
        if evidence == "stale":
            reasons.append("stale_evidence")
        if pressure == "high":
            reasons.append("high_pressure")
        if len(reasons) > 1:
            recommendation = "prune"
        elif evidence == "unchanged" and pressure == "low":
            recommendation = "retain"
            reasons.extend(("unchanged_evidence", "low_pressure"))
        else:
            reasons.append("insufficient_inputs")
    return {
        "version": 1,
        "source": "deterministic-assignment-boundary",
        "recommendation": recommendation,
        "facts": {key: facts[key] for key in _FACTS},
        "reasons": reasons,
        "unknown_inputs": unknown,
        "tradeoffs": list(_TRADEOFFS[recommendation]),
    }


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Context advice has duplicate fields")
    return fields


def read_context_advice(value: object) -> dict[str, Any] | None:
    """Fail closed on optional legacy, unsupported, or malformed retained advice."""
    if (
        type(value) is not str
        or len(value) > MAX_CONTEXT_ADVICE_BYTES
        or not value.isascii()
    ):
        return None
    try:
        record = json.loads(value, object_pairs_hook=_unique_fields)
        if type(record) is not dict:
            return None
        version, round_number, kind = (
            record.get(key) for key in ("version", "round", "kind")
        )
        if (
            type(version) is not int
            or version != 1
            or type(round_number) is not int
            or not 1 <= round_number <= 2**63 - 1
            or type(kind) is not str
            or kind not in _ASSIGNMENT_KINDS
        ):
            return None
        expected = {
            "round": round_number,
            "kind": kind,
            **context_advice(record.get("facts")),
        }
        return expected if record == expected else None
    except ValueError:
        return None
