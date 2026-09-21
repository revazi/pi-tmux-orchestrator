"""Bounded dynamic worker-topology policy projection tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pi_tmux_orchestrator.planner_topology import planner_topology_policy


class PlannerTopologyTests(unittest.TestCase):
    def test_missing_configuration_projects_only_packaged_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            with mock.patch.dict(
                os.environ,
                {"PI_TMUX_ORCHESTRATOR_CONFIG": str(root / "absent.json")},
            ):
                policy = planner_topology_policy(project)
        self.assertEqual(policy["version"], 1)
        self.assertEqual(policy["optional_roles"], ["probe", "playwright", "django"])
        self.assertEqual(policy["custom_roles"], [])
        self.assertEqual(
            {role: item["constraint"] for role, item in policy["builtins"].items()},
            {
                "implementer": {},
                "reviewer": {},
                "probe": {},
                "playwright": {},
                "django": {},
            },
        )

    def test_exact_project_constraints_override_user_global_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "project"
            project.mkdir()
            config_path = root / "models.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "defaultProfile": "economy",
                        "profiles": {
                            "project-careful": {
                                "implementer": "medium",
                                "reviewer": "low",
                                "probe": "off",
                                "playwright": "minimal",
                                "django": "medium",
                            }
                        },
                        "defaults": {
                            "provider": "global-provider",
                            "model": "global-model",
                        },
                        "roles": {"reviewer": {"thinking": "minimal"}},
                        "projects": [
                            {
                                "directory": str(project),
                                "profile": "project-careful",
                                "defaults": {
                                    "provider": "project-provider",
                                    "model": "project-model",
                                },
                                "roles": {
                                    "reviewer": {
                                        "provider": "project-provider",
                                        "model": "review-model",
                                    }
                                },
                                "specialists": ["django", "probe"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"PI_TMUX_ORCHESTRATOR_CONFIG": str(config_path)}
            ):
                policy = planner_topology_policy(project)
        self.assertEqual(policy["optional_roles"], ["probe", "django"])
        self.assertEqual(
            policy["builtins"]["implementer"]["constraint"],
            {
                "provider": "project-provider",
                "model": "project-model",
                "thinking": "medium",
            },
        )
        self.assertEqual(
            policy["builtins"]["reviewer"]["constraint"],
            {
                "provider": "project-provider",
                "model": "review-model",
                "thinking": "minimal",
            },
        )


if __name__ == "__main__":
    unittest.main()
