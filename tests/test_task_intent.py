"""Provider-free Stage 1 admission, provenance, redaction and CLI regressions."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from json_cli_support import JsonCliFixture
from test_planning import scope_planning_record
from tests.support import ORCHESTRATOR
from pi_tmux_orchestrator import start_commands, terminal_planning
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.planning import metadata_digest, validate_planning_record
from pi_tmux_orchestrator.task_intent import (
    TASK_INTENTS,
    retained_task_intent,
    task_intent_metadata,
    validate_intent_metadata,
    validate_task_intent,
)

ROOT = Path(__file__).resolve().parents[1]
CANARY = "PRIVATE_INTENT_TASK_83ea"


def intent_record(roles, operator, recommendation):
    record = scope_planning_record(roles, ["topology"])
    record.update(version=4, task_intent=task_intent_metadata(operator, recommendation))
    record["bindings"]["decision"] = metadata_digest(
        {key: record[key] for key in ("roles", "scopes", "locks", "task_intent")}
        | {"version": 1}
    )
    return record


class TaskIntentTests(JsonCliFixture):
    def start(self, *extra):
        with (
            mock.patch.object(
                ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
            ),
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
        ):
            return self.run_main(
                [
                    "--json",
                    "start",
                    "--project",
                    str(ROOT),
                    "--task",
                    CANARY,
                    "--skip-model-check",
                    *extra,
                ]
            )

    def test_every_intent_metadata_is_strict_and_operator_wins(self):
        for operator in (None, *TASK_INTENTS):
            for recommendation in (None, *TASK_INTENTS):
                value = task_intent_metadata(operator, recommendation)
                self.assertEqual(
                    value["effective"], operator or recommendation or "change"
                )
                self.assertEqual(validate_intent_metadata(value), value)
        for value in ("", "Change", " change", CANARY, True, {}, [], 1):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(OrchestrationError),
            ):
                validate_task_intent(value)
        for mutation in (
            {"version": True},
            {"source": "provider-body"},
            {"effective": "review"},
            {"rationale": CANARY},
        ):
            with self.assertRaises(OrchestrationError):
                validate_intent_metadata(task_intent_metadata("change") | mutation)
        with self.assertRaises(OrchestrationError):
            validate_intent_metadata(
                task_intent_metadata("investigation"), launched=True
            )
        self.assertIsNone(retained_task_intent({"version": 9}))

    def test_non_change_exits_before_terminal_provider_and_all_launch_resources(self):
        for intent in TASK_INTENTS[1:]:
            for extra in (
                [],
                ["--dry-run"],
                ["--dynamic-plan"],
                [
                    "--dynamic-plan",
                    "--authorize-planning",
                    "--yes",
                    "--approve-project",
                    "--rpc-workers",
                ],
            ):
                with (
                    self.subTest(intent=intent, flags=extra),
                    mock.patch.object(start_commands, "command_path") as binaries,
                    mock.patch.object(start_commands, "load_model_config") as config,
                    mock.patch.object(start_commands, "load_planning_record") as record,
                    mock.patch.object(start_commands, "canonical_state_root") as state,
                    mock.patch.object(
                        start_commands, "initialize_broker_run"
                    ) as broker,
                    mock.patch.object(start_commands, "create_tmux_grid") as grid,
                    mock.patch.object(
                        terminal_planning, "_run_terminal_planner"
                    ) as planner,
                ):
                    code, envelope, raw, stderr = self.run_main(
                        [
                            "--json",
                            "start",
                            "--task-intent",
                            intent,
                            "--task",
                            CANARY,
                            "--project",
                            "/nonexistent",
                            *extra,
                        ]
                    )
                self.assertEqual(code, 0)
                self.assert_envelope(envelope, "start", True)
                self.assertEqual(envelope["data"]["disposition"], "direct-parent")
                self.assertFalse(envelope["data"]["launched"])
                self.assertNotIn("implementation_flow", envelope["data"])
                self.assertNotIn(CANARY, raw + stderr)
                for boundary in (
                    binaries,
                    config,
                    record,
                    state,
                    broker,
                    grid,
                    planner,
                ):
                    boundary.assert_not_called()

    def test_malformed_cli_intent_is_redacted_and_never_dispatches(self):
        for intent in ("", "CHANGE", " change", CANARY):
            with mock.patch.object(start_commands, "create_tmux_grid") as grid:
                code, envelope, raw, stderr = self.run_main(
                    ["--json", "start", "--task-intent", intent, "--task", CANARY]
                )
            self.assertNotEqual(code, 0)
            self.assertEqual(envelope["error"]["code"], "invalid_arguments")
            self.assertNotIn(CANARY, raw + stderr)
            grid.assert_not_called()

    def test_change_static_and_dynamic_forwarding_preserve_one_writer_and_reviewer(
        self,
    ):
        for flags, operator in (([], None), (["--task-intent", "change"], "change")):
            with mock.patch.object(
                terminal_planning, "_run_terminal_planner"
            ) as planner:
                code, preview, _, _ = self.start("--dry-run", *flags)
            self.assertEqual(code, 0)
            self.assertEqual(
                preview["data"]["task_intent"], task_intent_metadata(operator)
            )
            self.assertEqual(
                [role["name"] for role in preview["data"]["roles"]],
                ["implementer", "reviewer"],
            )
            planner.assert_not_called()
        args = ORCHESTRATOR.build_parser().parse_args(
            ["start", "--task", CANARY, "--task-intent", "change", "--dynamic-plan"]
        )
        self.assertEqual(
            terminal_planning._terminal_input(args, ROOT)["taskIntent"], "change"
        )
        self.assertFalse(
            terminal_planning._confirmation_value(args, "Answer directly in parent?")
        )

    def test_recommendation_binding_tampering_and_stale_operator_fail_before_launch(
        self,
    ):
        _, preview, _, _ = self.start("--dry-run")
        roles = preview["data"]["roles"]
        for recommendation in TASK_INTENTS:
            with tempfile.TemporaryDirectory() as directory:
                record = intent_record(roles, "change", recommendation)
                path = Path(directory) / "planning.json"
                path.write_text(json.dumps(record), encoding="utf-8")
                code, bound, raw, _ = self.start(
                    "--dry-run",
                    "--task-intent",
                    "change",
                    "--planning-record-file",
                    str(path),
                )
                self.assertEqual(code, 0, raw)
                self.assertEqual(bound["data"]["task_intent"], record["task_intent"])
                self.assertNotIn(CANARY, raw)
                bound_record = bound["data"]["planning"]
                validate_planning_record(bound_record)
                tampered = copy.deepcopy(bound_record)
                tampered["task_intent"] = task_intent_metadata(
                    "change", "review" if recommendation != "review" else "advisory"
                )
                with self.assertRaises(OrchestrationError):
                    validate_planning_record(tampered)
                path.write_text(json.dumps(bound_record), encoding="utf-8")
                with mock.patch.object(start_commands, "create_tmux_grid") as grid:
                    code, failure, _, _ = self.start(
                        "--planning-record-file", str(path)
                    )
                self.assertNotEqual(code, 0)
                self.assertEqual(failure["error"]["code"], "stale_planning_binding")
                grid.assert_not_called()
                # Recomputing the decision digest cannot reuse the old accepted start binding.
                tampered["bindings"]["decision"] = metadata_digest(
                    {
                        key: tampered[key]
                        for key in ("roles", "scopes", "locks", "task_intent")
                    }
                    | {"version": 1}
                )
                path.write_text(json.dumps(tampered), encoding="utf-8")
                with mock.patch.object(start_commands, "create_tmux_grid") as grid:
                    code, failure, _, _ = self.start(
                        "--task-intent", "change", "--planning-record-file", str(path)
                    )
                self.assertNotEqual(code, 0)
                self.assertEqual(failure["error"]["code"], "stale_planning_binding")
                grid.assert_not_called()
        record = intent_record(roles, None, "advisory")
        with self.assertRaises(OrchestrationError):
            validate_planning_record(record, allow_unbound=True)

    def test_launch_retains_only_bounded_intent_and_supervisor_projection(self):
        _, preview, _, _ = self.start("--dry-run", "--task-intent", "change")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(ORCHESTRATOR, "STATE_ROOT", Path(directory) / "state"),
        ):
            root = Path(directory)
            path = root / "planning.json"
            record = intent_record(preview["data"]["roles"], "change", "advisory")
            path.write_text(json.dumps(record), encoding="utf-8")
            code, bound, raw, _ = self.start(
                "--dry-run",
                "--task-intent",
                "change",
                "--planning-record-file",
                str(path),
            )
            self.assertEqual(code, 0, raw)
            path.write_text(json.dumps(bound["data"]["planning"]), encoding="utf-8")
            with (
                mock.patch.object(ORCHESTRATOR, "STATE_ROOT", root / "state"),
                mock.patch.object(start_commands, "create_tmux_grid") as grid,
            ):
                code, launched, raw, _ = self.start(
                    "--task-intent", "change", "--planning-record-file", str(path)
                )
            self.assertEqual(code, 0, raw)
            manifest = grid.call_args.args[4]
            coord = Path(launched["data"]["paths"]["coordination"])
            manifest["monitor_pane_id"] = "%1"
            for index, role in enumerate(manifest["roles"].values(), start=2):
                role["pane_id"] = f"%{index}"
            ORCHESTRATOR.save_manifest(coord, manifest)
            retained = ORCHESTRATOR.load_manifest(coord)
            self.assertEqual(retained_task_intent(retained), record["task_intent"])
            projected = ORCHESTRATOR.public_supervisor_run(coord, retained)
            self.assertEqual(projected["task_intent"], record["task_intent"])
            self.assertNotIn(CANARY, json.dumps(retained) + raw)
            self.assertEqual(retained["roles"]["implementer"]["tools"], None)
            self.assertEqual(
                retained["roles"]["reviewer"]["tools"], ORCHESTRATOR.READ_ONLY_TOOLS
            )
            changed = copy.deepcopy(retained)
            changed["task_intent"] = task_intent_metadata("change", "review")
            with self.assertRaises(OrchestrationError):
                ORCHESTRATOR.save_manifest(coord, changed)
            changed = copy.deepcopy(retained)
            changed["task_intent"] = task_intent_metadata("review")
            with self.assertRaises(OrchestrationError):
                ORCHESTRATOR.save_manifest(coord, changed)
            # On-disk tampering is rejected at the read boundary too.
            (coord / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(OrchestrationError):
                ORCHESTRATOR.load_manifest(coord)

    def test_real_cli_non_change_has_no_state_or_binary_calls_in_tui_rpc_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = {
                **os.environ,
                "PI_TMUX_AGENTS_HOME": str(state),
                "PI_TMUX_ORCHESTRATOR_CONFIG": str(Path(directory) / "missing.json"),
            }
            for intent in TASK_INTENTS[1:]:
                for transport in ([], ["--rpc-workers"]):
                    result = subprocess.run(
                        [
                            str(ROOT / "bin/pi-tmux-agents"),
                            "--json",
                            "start",
                            "--task-intent",
                            intent,
                            "--task",
                            CANARY,
                            "--dynamic-plan",
                            "--yes",
                            *transport,
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertFalse(json.loads(result.stdout)["data"]["launched"])
                    self.assertNotIn(CANARY, result.stdout + result.stderr)
                    self.assertFalse(state.exists())
