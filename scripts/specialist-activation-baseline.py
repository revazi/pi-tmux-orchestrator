#!/usr/bin/env python3
"""Deterministic specialist activation assignment-count proxy fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pi_tmux_orchestrator.specialist_activation import (  # noqa: E402
    decide_initial_probe,
    decide_specialist,
)

FIXTURE = ROOT / "tests" / "fixtures" / "specialist-activation-baseline.json"
CASES = (
    ("docs-only", "Update README documentation for a typo.", ["README.md"]),
    ("frontend", "Implement the browser interface.", ["web/app.tsx"]),
    ("django-risk", "Implement a database migration.", ["app/models.py"]),
    ("empty-paths", "Implement the requested code change.", []),
)


def build_baseline() -> dict[str, object]:
    cases = []
    before = 0
    after = 0
    for name, task, paths in CASES:
        decisions = [
            decide_initial_probe(task),
            decide_specialist("playwright", paths),
            decide_specialist("django", paths),
        ]
        selected = sum(value["decision"] == "run" for value in decisions)
        before += len(decisions)
        after += selected
        cases.append(
            {
                "name": name,
                "decisions": [
                    {
                        "role": value["role"],
                        "decision": value["decision"],
                        "rule_id": value["rule_id"],
                    }
                    for value in decisions
                ],
                "configured_assignments_before": len(decisions),
                "selected_assignments_after": selected,
            }
        )
    return {
        "schema_version": 1,
        "benchmark_kind": "deterministic-specialist-assignment-count-proxy",
        "cases": cases,
        "totals": {
            "configured_assignments_before": before,
            "selected_assignments_after": after,
            "assignments_avoided": before - after,
        },
        "authoritative_evidence": {
            "provider_usage": {
                "availability": "unavailable",
                "required_metrics": [
                    "provider_calls",
                    "input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cost_total",
                ],
            },
            "quality": {
                "availability": "unavailable",
                "required_metrics": [
                    "required_checks",
                    "reviewer_findings",
                    "missed_findings",
                    "revision_rounds",
                ],
            },
        },
        "claims": {
            "provider_call_savings": False,
            "provider_token_savings": False,
            "billing_savings": False,
            "quality_equivalence": False,
        },
    }


def main() -> int:
    baseline = build_baseline()
    if "--write" in sys.argv:
        FIXTURE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {FIXTURE}")
        return 0
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if expected != baseline:
        raise RuntimeError(
            "Specialist activation baseline changed; inspect and recapture with --write"
        )
    print(
        "Verified specialist activation assignment proxy: "
        f"{baseline['totals']['assignments_avoided']} avoided."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
