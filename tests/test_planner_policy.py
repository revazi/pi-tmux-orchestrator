from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from pi_tmux_orchestrator import configuration, planner_policy, runtime
from pi_tmux_orchestrator.models import OrchestrationError


class PlannerPolicyTests(TestCase):
    def test_missing_policy_is_versioned_cancel_without_an_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            self.assertEqual(
                planner_policy.load_planner_policy(path),
                {
                    "version": 1,
                    "preferred": None,
                    "fallbacks": [],
                    "no_eligible": "cancel",
                    "dynamic_guidance": None,
                },
            )

    def test_preferred_and_ordered_cross_provider_fallbacks_are_exact(self) -> None:
        policy = planner_policy.validate_planner_policy(
            {
                "version": 1,
                "preferred": {
                    "provider": "canonical-provider",
                    "model": "canonical-model",
                    "thinking": "max",
                },
                "fallbacks": [
                    {"provider": "xai", "model": "grok-exact", "thinking": "low"},
                    {
                        "provider": "openai",
                        "model": "gpt-exact",
                        "thinking": "medium",
                    },
                ],
                "noEligible": "static",
            }
        )
        self.assertEqual(policy["preferred"]["model"], "canonical-model")
        self.assertEqual(policy["preferred"]["thinking"], "max")
        self.assertEqual(
            [(item["provider"], item["model"]) for item in policy["fallbacks"]],
            [("xai", "grok-exact"), ("openai", "gpt-exact")],
        )
        self.assertEqual(policy["no_eligible"], "static")

    def test_malformed_duplicate_and_unsupported_thinking_policies_fail_closed(
        self,
    ) -> None:
        base = {
            "version": 1,
            "preferred": None,
            "fallbacks": [],
            "noEligible": "cancel",
        }
        invalid = (
            {**base, "unknown": True},
            {**base, "version": 2},
            {**base, "noEligible": "guess"},
            {**base, "fallbacks": [{"provider": "xai", "model": "grok"}]},
            {
                **base,
                "fallbacks": [
                    {"provider": "xai", "model": "grok", "thinking": "ultra"}
                ],
            },
            {
                **base,
                "preferred": {"provider": "xai", "model": "grok", "thinking": "low"},
                "fallbacks": [
                    {"provider": "xai", "model": "grok", "thinking": "medium"}
                ],
            },
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                planner_policy.validate_planner_policy(value)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(
                '{"version":1,"preferred":null,"fallbacks":[],"noEligible":"cancel",'
                '"noEligible":"static"}',
                encoding="utf-8",
            )
            with self.assertRaises(OrchestrationError):
                planner_policy.load_planner_policy(path)

    def test_policy_path_is_external_regular_bounded_and_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            inside = project / "planner.json"
            inside.write_text("{}", encoding="utf-8")
            with self.assertRaises(OrchestrationError):
                planner_policy.load_planner_policy(inside, project=project)

            target = root / "target.json"
            target.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "preferred": None,
                        "fallbacks": [],
                        "noEligible": "cancel",
                    }
                ),
                encoding="utf-8",
            )
            linked = root / "linked.json"
            linked.symlink_to(target)
            with self.assertRaises(OrchestrationError):
                planner_policy.load_planner_policy(linked)

            oversized = root / "oversized.json"
            oversized.write_text(" " * (planner_policy.MAX_PLANNER_POLICY_BYTES + 1))
            with self.assertRaises(OrchestrationError):
                planner_policy.load_planner_policy(oversized)

            with mock.patch.dict(
                os.environ,
                {planner_policy.LEGACY_PLANNER_POLICY_ENV: "relative.json"},
            ):
                with self.assertRaisesRegex(OrchestrationError, "retired"):
                    planner_policy.load_planner_policy(project=project)

    def test_dynamic_guidance_is_bounded_with_legacy_compatibility(self) -> None:
        base = {
            "version": 1,
            "preferred": None,
            "fallbacks": [],
            "noEligible": "cancel",
        }
        text = "Prefer a smaller worker roster and deeper thinking for risky work."
        configured = planner_policy.validate_planner_policy(
            {**base, "dynamicGuidance": text}
        )
        self.assertEqual(configured["dynamic_guidance"], text)
        legacy = planner_policy.validate_planner_policy({**base, "jevGuidance": text})
        self.assertEqual(legacy["dynamic_guidance"], text)
        with self.assertRaisesRegex(OrchestrationError, "both"):
            planner_policy.validate_planner_policy(
                {**base, "dynamicGuidance": text, "jevGuidance": text}
            )
        self.assertEqual(
            planner_policy.validate_planner_policy(
                {
                    **base,
                    "dynamicGuidance": "x" * planner_policy.MAX_DYNAMIC_GUIDANCE_BYTES,
                }
            )["dynamic_guidance"],
            "x" * planner_policy.MAX_DYNAMIC_GUIDANCE_BYTES,
        )
        self.assertIsNone(
            planner_policy.validate_planner_policy(base)["dynamic_guidance"]
        )
        self.assertIsNone(
            planner_policy.validate_planner_policy({**base, "dynamicGuidance": None})[
                "dynamic_guidance"
            ]
        )
        for invalid in (
            "",
            " surrounding whitespace ",
            "bad\x00guidance",
            "bad\tguidance",
            "bad\nguidance",
            "bad\x85guidance",
            "bad\ud800guidance",
            "x" * (planner_policy.MAX_DYNAMIC_GUIDANCE_BYTES + 1),
        ):
            with (
                self.subTest(value=invalid[:20]),
                self.assertRaises(OrchestrationError),
            ):
                planner_policy.validate_planner_policy(
                    {**base, "dynamicGuidance": invalid}
                )

    def test_unified_planner_member_requires_schema_four_and_an_object(self) -> None:
        base = {"defaults": {}, "roles": {}, "projects": []}
        configured = configuration.validate_model_config(
            {"version": 4, **base, "planner": {}}
        )
        self.assertIsNone(configured["planner"]["dynamic_guidance"])
        for invalid in (
            {"version": 3, **base, "planner": {}},
            {"version": 4, **base, "planner": None},
            {"version": 4, **base, "planner": "invalid"},
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(OrchestrationError),
            ):
                configuration.validate_model_config(invalid)

    def test_cli_projection_contains_only_path_flag_and_strict_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            unified_path = root / "tmux-orchestrator.json"
            unified_path.write_text(
                json.dumps(
                    {
                        "version": 4,
                        "defaults": {},
                        "roles": {},
                        "planner": {
                            "preferred": {
                                "provider": "xai",
                                "model": "grok-exact",
                                "thinking": "medium",
                            },
                            "fallbacks": [],
                            "noEligible": "cancel",
                            "dynamicGuidance": (
                                "Prefer the smallest sufficient roster and explain "
                                "uncertainty."
                            ),
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {planner_policy.MODEL_CONFIG_ENV: str(unified_path)},
                ),
                mock.patch.object(planner_policy.runtime, "PI_HOME", root),
            ):
                args = type("Args", (), {"project": str(project)})()
                result = planner_policy.planner_policy_command(args)
        self.assertEqual(result.code, 0)
        self.assertEqual(
            set(result.data or {}),
            {
                "config_path",
                "configured",
                "binding_digest",
                "policy",
                "dynamic_guidance",
            },
        )
        self.assertTrue(result.data["configured"])
        self.assertRegex(result.data["binding_digest"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            result.data["dynamic_guidance"]["text"],
            "Prefer the smallest sufficient roster and explain uncertainty.",
        )
        serialized = json.dumps(result.data)
        for forbidden in ("apiKey", "endpoint", "task", "response"):
            self.assertNotIn(forbidden, serialized)

    def test_legacy_separate_file_is_rejected_until_explicit_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            unified = root / "tmux-orchestrator.json"
            legacy = root / "tmux-orchestrator-planner.json"
            config = {
                "version": 4,
                "defaults": {},
                "roles": {},
                "planner": {
                    "preferred": None,
                    "fallbacks": [],
                    "noEligible": "cancel",
                    "dynamicGuidance": "Unified choice.",
                },
            }
            unified.write_text(json.dumps(config), encoding="utf-8")
            legacy.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "preferred": None,
                        "fallbacks": [],
                        "noEligible": "cancel",
                        "jevGuidance": "Legacy choice.",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {planner_policy.MODEL_CONFIG_ENV: str(unified)},
                ),
                mock.patch.object(planner_policy.runtime, "PI_HOME", root),
            ):
                with self.assertRaisesRegex(OrchestrationError, "migrate"):
                    planner_policy.load_planner_policy(project=project)
                with self.assertRaisesRegex(OrchestrationError, "migrate"):
                    planner_policy.planner_policy_command(
                        type("Args", (), {"project": str(project)})()
                    )
                legacy.unlink()
                self.assertEqual(
                    planner_policy.load_planner_policy(project=project)[
                        "dynamic_guidance"
                    ],
                    "Unified choice.",
                )
                # The legacy field is accepted after explicit migration into
                # the authoritative file; simultaneous names remain rejected.
                unified.write_text(
                    json.dumps(
                        {
                            **config,
                            "planner": {
                                "preferred": None,
                                "fallbacks": [],
                                "noEligible": "cancel",
                                "jevGuidance": "Legacy choice.",
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    planner_policy.load_planner_policy(project=project)[
                        "dynamic_guidance"
                    ],
                    "Legacy choice.",
                )

    def test_default_path_uses_unified_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = runtime.PI_HOME
            runtime.PI_HOME = Path(directory).resolve()
            self.addCleanup(setattr, runtime, "PI_HOME", previous)
            with mock.patch.dict(
                os.environ,
                {
                    planner_policy.MODEL_CONFIG_ENV: "",
                    planner_policy.LEGACY_PLANNER_POLICY_ENV: "",
                },
            ):
                self.assertEqual(
                    planner_policy.planner_policy_path(),
                    runtime.PI_HOME / "tmux-orchestrator.json",
                )
                self.assertIsNone(
                    planner_policy.load_planner_policy()["dynamic_guidance"]
                )
