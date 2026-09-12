from __future__ import annotations

import json
import runpy
import unittest
from pathlib import Path

from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.specialist_activation import (
    decide_custom_specialist,
    decide_initial_probe,
    decide_specialist,
    validate_forced_specialists,
)


ROOT = Path(__file__).resolve().parents[1]


class SpecialistActivationPolicyTests(unittest.TestCase):
    def test_probe_task_policy_is_conservative_and_force_wins(self) -> None:
        self.assertEqual(
            decide_initial_probe("Update README documentation for a typo."),
            {
                "role": "probe",
                "decision": "skipped",
                "rule_id": "probe-docs-only-task-v1",
                "forced": False,
            },
        )
        self.assertEqual(
            decide_initial_probe("Update authentication documentation."),
            {
                "role": "probe",
                "decision": "run",
                "rule_id": "probe-high-risk-task-v1",
                "forced": False,
            },
        )
        self.assertEqual(
            decide_initial_probe(None),
            {
                "role": "probe",
                "decision": "run",
                "rule_id": "probe-ambiguous-task-v1",
                "forced": False,
            },
        )
        self.assertEqual(
            decide_initial_probe("Update README.", forced=True)["rule_id"],
            "probe-forced-v1",
        )

    def test_playwright_runs_for_browser_or_ambiguous_paths(self) -> None:
        self.assertEqual(
            decide_specialist("playwright", ["web/app.tsx"])["rule_id"],
            "playwright-browser-path-v1",
        )
        self.assertEqual(
            decide_specialist("playwright", ["README.md"])["decision"],
            "skipped",
        )
        for value in ([], None, ["server/service.py"]):
            with self.subTest(value=value):
                self.assertEqual(
                    decide_specialist("playwright", value)["decision"], "run"
                )

    def test_django_skips_only_clear_docs_or_frontend_only_changes(self) -> None:
        self.assertEqual(
            decide_specialist("django", ["app/models.py"])["rule_id"],
            "django-framework-path-v1",
        )
        self.assertEqual(
            decide_specialist("django", ["frontend/app.ts", "styles/app.css"])[
                "rule_id"
            ],
            "django-frontend-only-paths-v1",
        )
        self.assertEqual(
            decide_specialist("django", ["docs/usage.md"])["decision"],
            "skipped",
        )
        self.assertEqual(
            decide_specialist("django", ["service/domain.py"])["decision"], "run"
        )
        self.assertEqual(
            decide_specialist("django", ["README.md"], forced=True)["rule_id"],
            "django-forced-v1",
        )

    def test_repair_probe_skips_docs_and_runs_ambiguous_paths(self) -> None:
        self.assertEqual(
            decide_specialist("probe", ["CHANGELOG.md"])["decision"], "skipped"
        )
        self.assertEqual(
            decide_specialist("probe", ["src/runtime.py"])["decision"], "run"
        )

    def test_fixed_assignment_proxy_keeps_provider_and_quality_claims_unavailable(
        self,
    ) -> None:
        module = runpy.run_path(str(ROOT / "scripts/specialist-activation-baseline.py"))
        baseline = module["build_baseline"]()
        expected = json.loads(
            (ROOT / "tests/fixtures/specialist-activation-baseline.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(baseline, expected)
        self.assertEqual(baseline["totals"]["assignments_avoided"], 5)
        self.assertTrue(
            all(
                any(
                    decision["role"] == "custom-security"
                    for decision in case["decisions"]
                )
                for case in baseline["cases"]
            )
        )
        self.assertEqual(
            baseline["authoritative_evidence"]["provider_usage"]["availability"],
            "unavailable",
        )
        self.assertEqual(
            baseline["authoritative_evidence"]["quality"]["availability"],
            "unavailable",
        )
        self.assertFalse(any(baseline["claims"].values()))

    def test_custom_contract_rules_are_identity_bound_and_conservative(self) -> None:
        skipped = decide_custom_specialist(
            "custom-security", "probe", ["docs/guide.md"]
        )
        self.assertEqual(
            skipped,
            {
                "role": "custom-security",
                "decision": "skipped",
                "rule_id": "custom-security-docs-only-paths-v1",
                "forced": False,
            },
        )
        ambiguous = decide_custom_specialist(
            "custom-browser", "playwright", [], forced=False
        )
        self.assertEqual(ambiguous["decision"], "run")
        self.assertEqual(ambiguous["rule_id"], "custom-browser-ambiguous-paths-v1")
        forced = decide_custom_specialist(
            "custom-framework", "django", ["docs/guide.md"], forced=True
        )
        self.assertEqual(
            forced,
            {
                "role": "custom-framework",
                "decision": "run",
                "rule_id": "custom-framework-forced-v1",
                "forced": True,
            },
        )
        for role, contract, paths in (
            ("custom-Bad", "probe", ["src/app.py"]),
            ("custom-security", "reviewer", ["src/app.py"]),
            ("custom-security", "probe", [1]),
        ):
            with self.subTest(role=role, contract=contract, paths=paths):
                if paths == [1]:
                    self.assertEqual(
                        decide_custom_specialist(role, contract, paths)["decision"],
                        "run",
                    )
                else:
                    with self.assertRaises(OrchestrationError):
                        decide_custom_specialist(role, contract, paths)

    def test_forced_specialists_are_unique_enabled_and_canonical(self) -> None:
        configured = ["implementer", "reviewer", "probe", "playwright", "django"]
        self.assertEqual(
            validate_forced_specialists(["django", "probe"], configured),
            ("probe", "django"),
        )
        configured.extend(["custom-zeta", "custom-alpha"])
        self.assertEqual(
            validate_forced_specialists(
                ["custom-zeta", "probe", "custom-alpha"], configured
            ),
            ("probe", "custom-alpha", "custom-zeta"),
        )
        for value in (["reviewer"], ["probe", "probe"], ["playwright"]):
            enabled = configured if value != ["playwright"] else ["implementer"]
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                validate_forced_specialists(value, enabled)


if __name__ == "__main__":
    unittest.main()
