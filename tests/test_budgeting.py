from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ORCHESTRATOR


class BudgetPolicyTests(unittest.TestCase):
    def test_packaged_policy_is_warn_only_and_keeps_categories_explicit(self) -> None:
        policy = ORCHESTRATOR.packaged_budget_policy()
        self.assertEqual(policy["version"], 1)
        self.assertEqual(policy["enforcement"], "warn-only")
        self.assertEqual(policy["warning"]["run"], {"operational_tokens": 600_000})
        self.assertEqual(policy["warning"]["role"], {"operational_tokens": 200_000})
        self.assertEqual(policy["warning"]["assignment"], {})
        self.assertTrue(all(not policy["hard"][scope] for scope in policy["hard"]))

    def test_missing_user_file_migrates_to_packaged_warn_only_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            self.assertEqual(
                ORCHESTRATOR.load_budget_config(missing),
                ORCHESTRATOR.packaged_budget_policy(),
            )

    def test_user_global_policy_merges_defaults_and_per_run_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budgets.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "enforcement": "hard",
                        "warning": {
                            "run": {
                                "operational_tokens": None,
                                "provider_calls": 10,
                                "cache_read_tokens": 5_000,
                            }
                        },
                        "hard": {"run": {"provider_calls": 20}},
                    }
                ),
                encoding="utf-8",
            )
            configured = ORCHESTRATOR.load_budget_config(path)

        self.assertNotIn("operational_tokens", configured["warning"]["run"])
        self.assertEqual(configured["warning"]["run"]["provider_calls"], 10)
        self.assertEqual(configured["warning"]["role"]["operational_tokens"], 200_000)
        effective = ORCHESTRATOR.effective_budget_policy(
            configured,
            enforcement="warn-only",
            overrides=[
                ("warning", "run", "provider_calls", 12),
                ("hard", "run", "provider_calls", 25),
                ("warning", "role", "operational_tokens", None),
            ],
        )
        self.assertEqual(effective["enforcement"], "warn-only")
        self.assertEqual(effective["warning"]["run"]["provider_calls"], 12)
        self.assertEqual(effective["hard"]["run"]["provider_calls"], 25)
        self.assertNotIn("operational_tokens", effective["warning"]["role"])

    def test_policy_rejects_unknown_secret_fields_unsafe_values_and_order(self) -> None:
        invalid = [
            {"version": 1, "apiKey": "forbidden"},
            {"version": 1, "warning": {"run": {"endpoint": 1}}},
            {"version": 1, "warning": {"run": {"provider_calls": True}}},
            {"version": 1, "hard": {"run": {"context_percent": 101}}},
            {
                "version": 1,
                "warning": {"run": {"provider_calls": 30}},
                "hard": {"run": {"provider_calls": 20}},
            },
        ]
        for value in invalid:
            with (
                self.subTest(value=value),
                self.assertRaises(ORCHESTRATOR.OrchestrationError),
            ):
                ORCHESTRATOR.validate_budget_config(value)

    def test_policy_file_is_bounded_non_symlink_and_outside_target_project(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            local = project / "budgets.json"
            local.write_text('{"version":1}', encoding="utf-8")
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.load_budget_config(local, project=project)

            target = root / "target.json"
            target.write_text('{"version":1}', encoding="utf-8")
            linked = root / "linked.json"
            linked.symlink_to(target)
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.load_budget_config(linked)

            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"version":1,"warning":{},"warning":{}}', encoding="utf-8"
            )
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.load_budget_config(duplicate)

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * (ORCHESTRATOR.MAX_BUDGET_CONFIG_BYTES + 1))
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.load_budget_config(oversized)

            with (
                mock.patch.dict(
                    os.environ,
                    {"PI_TMUX_ORCHESTRATOR_BUDGET_CONFIG": "relative.json"},
                ),
                self.assertRaises(ORCHESTRATOR.OrchestrationError),
            ):
                ORCHESTRATOR.budget_config_path(project)

    def test_override_parser_is_bounded_numeric_and_can_disable(self) -> None:
        self.assertEqual(
            ORCHESTRATOR.parse_budget_override(
                "warning.assignment.cache_read_tokens=1200"
            ),
            ("warning", "assignment", "cache_read_tokens", 1200),
        )
        self.assertEqual(
            ORCHESTRATOR.parse_budget_override("hard.run.cost_total=2.5"),
            ("hard", "run", "cost_total", 2.5),
        )
        self.assertEqual(
            ORCHESTRATOR.parse_budget_override("hard.role.context_percent=off"),
            ("hard", "role", "context_percent", None),
        )
        for value in (
            "hard.run.provider_calls=0",
            "hard.run.provider_calls=true",
            "hard.run.credential=1",
            "hard.project.provider_calls=1",
            "hard.run.provider_calls=NaN",
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(ORCHESTRATOR.OrchestrationError),
            ):
                ORCHESTRATOR.parse_budget_override(value)

        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as duplicate:
            ORCHESTRATOR.effective_budget_policy(
                ORCHESTRATOR.packaged_budget_policy(),
                overrides=[
                    ("hard", "run", "provider_calls", 10),
                    ("hard", "run", "provider_calls", 20),
                ],
            )
        self.assertEqual(duplicate.exception.code, "invalid_arguments")
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as ordering:
            ORCHESTRATOR.effective_budget_policy(
                ORCHESTRATOR.packaged_budget_policy(),
                overrides=[("hard", "run", "operational_tokens", 500_000)],
            )
        self.assertEqual(ordering.exception.code, "invalid_arguments")

    def test_start_parser_exposes_explicit_per_run_overrides(self) -> None:
        args = ORCHESTRATOR.build_parser().parse_args(
            [
                "start",
                "--task",
                "synthetic",
                "--budget-enforcement",
                "hard",
                "--budget-override",
                "warning.run.provider_calls=10",
                "--budget-override",
                "hard.assignment.cost_total=3.5",
            ]
        )
        self.assertEqual(args.budget_enforcement, "hard")
        self.assertEqual(
            args.budget_override,
            [
                ("warning", "run", "provider_calls", 10),
                ("hard", "assignment", "cost_total", 3.5),
            ],
        )


if __name__ == "__main__":
    unittest.main()
