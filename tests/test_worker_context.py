"""Model-free policy, migration, and worker-launch context regressions."""

from __future__ import annotations

import argparse
import json
import os
from unittest import mock

from pi_tmux_orchestrator import broker_store, commands, runtime
from pi_tmux_orchestrator.broker import initialize_broker_run
from pi_tmux_orchestrator.cli import build_parser
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.worker_context import (
    context_policy,
    resolve_context_policy,
    retained_context_policy,
)
from test_broker import BrokerFixture


class WorkerContextTests(BrokerFixture):
    def test_cli_selections_are_explicit_strict_and_role_scoped(self):
        self.enterContext(mock.patch.object(runtime, "JSON_MODE", True))
        parser = build_parser()
        self.assertIsNone(parser.parse_args(["start"]).worker_context)
        selected = parser.parse_args(
            [
                "start",
                "--worker-context",
                "reviewer=retain",
                "--worker-context",
                "implementer=prune",
            ]
        ).worker_context
        policy = resolve_context_policy(selected, set(self.manifest["roles"]))
        self.assertEqual(policy, {"version": 1, "overrides": dict(selected)})
        for invalid in (
            "retain",
            "all=retain",
            "reviewer=auto",
            "reviewer=fresh",
            "reviewer=retain=prune",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(OrchestrationError):
                parser.parse_args(["start", "--worker-context", invalid])
        for selections in (
            [("probe", "retain")],
            [("reviewer", "retain"), ("reviewer", "prune")],
        ):
            with self.assertRaises(OrchestrationError):
                resolve_context_policy(selections, set(self.manifest["roles"]))
        for overrides in (None, [], {"reviewer": True}, {"reviewer": "compact"}):
            with self.assertRaises(OrchestrationError):
                context_policy(overrides, set(self.manifest["roles"]))

    def test_policy_survives_reopen_without_resetting_usage_or_repair_limit(self):
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_CONTEXT_POLICY_TASK",
            {},
            max_repair_rounds=2,
            worker_context_overrides={"reviewer": "retain"},
        )
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("UPDATE roles SET provider_calls=12 WHERE role='reviewer'")
        for _ in range(2):
            broker_store.prepare_broker_database(self.coord)
            self.assertEqual(
                broker_store.worker_context_mode(self.coord, "reviewer"), "retain"
            )
            self.assertEqual(
                broker_store.worker_context_mode(self.coord, "implementer"), "prune"
            )
            snapshot = broker_store.public_broker_snapshot(self.coord)
            self.assertEqual(
                snapshot["workflow"]["worker_context_policy"],
                {"version": 1, "overrides": {"reviewer": "retain"}},
            )
            self.assertEqual(
                snapshot["workflow"]["continuation"]["max_repair_rounds"], 2
            )
            self.assertEqual(snapshot["usage"]["provider_calls"], 12)
            self.assertNotIn("PRIVATE_CONTEXT_POLICY_TASK", json.dumps(snapshot))
        with self.assertRaises(OrchestrationError):
            broker_store.worker_context_mode(self.coord, "probe")

    def test_schema_nine_defaults_to_prune_but_new_missing_policy_fails_closed(self):
        initialize_broker_run(self.coord, self.manifest, "synthetic", {})
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("DELETE FROM meta WHERE key='worker_context_policy'")
        with self.assertRaisesRegex(OrchestrationError, "policy is missing"):
            broker_store.prepare_broker_database(self.coord)
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "schema_version", "9")
            self.assertEqual(
                retained_context_policy(database), {"version": 1, "overrides": {}}
            )
        broker_store.prepare_broker_database(self.coord)
        self.assertEqual(
            broker_store.worker_context_mode(self.coord, "reviewer"), "prune"
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                "10",
            )

    def test_corrupt_or_body_bearing_policy_cannot_reach_worker_launch(self):
        initialize_broker_run(self.coord, self.manifest, "synthetic", {})
        for value in (
            "invalid",
            "null",
            "[]",
            " " * 513,
            '{"version":true,"overrides":{}}',
            '{"version":2,"overrides":{}}',
            '{"version":1,"overrides":{"reviewer":"fresh"}}',
            '{"version":1,"overrides":{"probe":"retain"}}',
            '{"version":1,"overrides":{"reviewer":"retain","reviewer":"prune"}}',
            '{"version":1,"overrides":{},"body":"PRIVATE"}',
        ):
            with self.subTest(value=value):
                with broker_store.connect_broker_database(self.coord) as database:
                    broker_store.set_meta(database, "worker_context_policy", value)
                with self.assertRaises(OrchestrationError):
                    broker_store.worker_context_mode(self.coord, "reviewer")
                with self.assertRaises(OrchestrationError):
                    broker_store.prepare_broker_database(self.coord)

    def test_native_launch_overwrites_ambient_mode_from_durable_role_policy(self):
        initialize_broker_run(
            self.coord,
            self.manifest,
            "synthetic",
            {},
            worker_context_overrides={"reviewer": "retain"},
        )
        for role, expected in (("reviewer", "retain"), ("implementer", "prune")):
            with (
                mock.patch.dict(
                    os.environ, {"PI_TMUX_ORCHESTRATOR_CONTEXT_MODE": "fresh"}
                ),
                mock.patch.object(commands, "command_path", return_value="/usr/bin/pi"),
                mock.patch.object(commands.os, "chdir"),
                mock.patch.object(commands.os, "execvpe") as execute,
            ):
                commands.run_agent_command(
                    argparse.Namespace(
                        state_root=str(runtime.STATE_ROOT),
                        coord=str(self.coord),
                        role=role,
                    )
                )
            self.assertEqual(
                execute.call_args.args[2]["PI_TMUX_ORCHESTRATOR_CONTEXT_MODE"], expected
            )
