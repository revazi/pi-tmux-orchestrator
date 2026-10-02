"""Model-free approved worker policy bounds, projection, and compatibility."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pi_tmux_orchestrator.configuration import (
    validate_manifest_orchestration_config,
    validate_model_config,
)
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.planner_topology import planner_topology_projection
from pi_tmux_orchestrator.worker_candidates import validate_worker_candidates


def identity(index=0):
    return {"provider": "synthetic", "model": f"exact-{index}"}


def pool():
    return {"version": 1, "all": [identity()], "roles": {"reviewer": [identity(1)]}}


class WorkerCandidatePolicyTests(unittest.TestCase):
    def test_schema_is_strict_exact_and_bounded(self):
        valid = pool()
        self.assertEqual(validate_worker_candidates(valid), valid)
        validate_worker_candidates(
            {"version": 1, "all": [identity(i) for i in range(32)]}
        )
        invalid = [
            None,
            {},
            {**valid, "version": True},
            {**valid, "version": 2},
            {**valid, "extra": "PRIVATE_CONFIG_BODY"},
            {**valid, "all": []},
            {**valid, "all": [identity()] * 2},
            {**valid, "all": [identity(i) for i in range(33)]},
            {**valid, "all": [{"provider": "synthetic"}]},
            {**valid, "all": [{**identity(), "thinking": "high"}]},
            {**valid, "all": [{**identity(), "model": "bad name"}]},
            {**valid, "all": [{**identity(), "model": "bad\x7f"}]},
            {**valid, "all": [{**identity(), "provider": "x" * 257}]},
            {**valid, "roles": {"custom-security": [identity()]}},
            {**valid, "roles": {"implementer": []}},
            {**valid, "roles": None},
            {"version": 1, "roles": {}},
        ]
        for value in invalid:
            with (
                self.subTest(value=value),
                self.assertRaises(OrchestrationError) as raised,
            ):
                validate_worker_candidates(value)
            self.assertNotIn("PRIVATE_CONFIG_BODY", str(raised.exception))

    def test_distinct_union_limit_and_cross_pool_reuse(self):
        roles = ("implementer", "reviewer", "probe", "playwright", "django")
        valid = {
            "version": 1,
            "all": [identity(i) for i in range(32)],
            "roles": {role: [identity(i) for i in range(32)] for role in roles},
        }
        validate_worker_candidates(valid)
        maximum = {
            "version": 1,
            "all": [identity(i) for i in range(32)],
            "roles": {
                "implementer": [identity(i) for i in range(32, 64)],
                "reviewer": [identity(i) for i in range(64, 96)],
                "probe": [identity(i) for i in range(96, 100)],
            },
        }
        validate_worker_candidates(maximum)
        maximum["roles"]["probe"].append(identity(100))
        with self.assertRaisesRegex(OrchestrationError, "100 distinct"):
            validate_worker_candidates(maximum)
        valid["roles"] = {
            role: [identity(32 + r * 32 + i) for i in range(32)]
            for r, role in enumerate(roles)
        }
        with self.assertRaisesRegex(OrchestrationError, "100 distinct"):
            validate_worker_candidates(valid)

    def test_root_migration_and_retained_versions(self):
        for version in (1, 2, 3, 4, 5):
            self.assertEqual(validate_model_config({"version": version})["version"], 5)
        config = validate_model_config({"version": 5, "workerCandidates": pool()})
        self.assertEqual(config["worker_candidates"], pool())
        with self.assertRaises(OrchestrationError):
            validate_model_config({"version": 4, "workerCandidates": pool()})
        for version in (3, 4, 5):
            validate_manifest_orchestration_config(
                {"path": "/external/config.json", "version": version}
            )

    def test_projection_precedence_and_pool_binding_without_body_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            path = root / "policy.json"
            config = {
                "version": 5,
                "workerCandidates": pool(),
                "defaults": identity(2),
                "roles": {"reviewer": identity(3)},
                "projects": [
                    {
                        "directory": str(project),
                        "defaults": identity(4),
                        "roles": {"reviewer": identity(5)},
                    }
                ],
            }
            with mock.patch.dict(
                os.environ, {"PI_TMUX_ORCHESTRATOR_CONFIG": str(path)}
            ):
                path.write_text(json.dumps(config), encoding="utf-8")
                original = planner_topology_projection(project)
                self.assertEqual(
                    original["policy"]["builtins"]["implementer"]["constraint"],
                    identity(4),
                )
                self.assertEqual(
                    original["policy"]["builtins"]["reviewer"]["constraint"],
                    identity(5),
                )
                self.assertEqual(original["policy"]["worker_candidates"], pool())
                changed = copy.deepcopy(config)
                changed["workerCandidates"]["all"].append(identity(6))
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertNotEqual(
                    original["binding_digest"],
                    planner_topology_projection(project)["binding_digest"],
                )
                # v4 custom-role selection remains supported after v5 migration.
                config = {
                    "version": 4,
                    "projects": [
                        {
                            "directory": str(project),
                            "customRoles": [
                                {
                                    "id": "custom-security",
                                    **identity(),
                                    "thinking": "low",
                                }
                            ],
                        }
                    ],
                }
                validated = validate_model_config(config)
                self.assertEqual(
                    validated["projects"][0]["custom_roles"][0]["id"], "custom-security"
                )
