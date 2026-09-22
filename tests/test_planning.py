"""Dynamic-planning provenance and launch-binding tests."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
from unittest import mock

from json_cli_support import JsonCliFixture
from pi_tmux_orchestrator import terminal_planning
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.planning import (
    metadata_digest,
    validate_planning_record,
)
from tests.support import ORCHESTRATOR

ROOT = Path(__file__).resolve().parents[1]


def planning_record(roles):
    retained_roles = [
        {
            "id": role["name"],
            "contract": role.get("specialist_contract", role["name"]),
            "provider": role["provider"],
            "model": role["model"],
            "thinking": role["thinking"],
        }
        for role in roles
    ]
    return {
        "version": 1,
        "mode": "dynamic",
        "request_id": "a" * 32,
        "status": "accepted",
        "created_at_ms": 1_800_000_000_000,
        "accepted_at_ms": 1_800_000_000_001,
        "decision_schema_version": 1,
        "decision_model": {
            "provider": "decision-provider",
            "model": "decision-model",
            "thinking": "medium",
            "source": "configured-fallback",
        },
        "roles": retained_roles,
        "bindings": {
            "input": None,
            "start_config": None,
            "planner_policy": "b" * 64,
            "topology_policy": "c" * 64,
            "candidate_set": "d" * 64,
            "decision": metadata_digest({"version": 1, "roles": retained_roles}),
        },
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 15,
            "cost_total": 0.01,
        },
    }


class PlanningAdmissionTests(JsonCliFixture):
    def start(self, task, *extra):
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
                    task,
                    "--skip-model-check",
                    *extra,
                ]
            )

    def test_preview_binds_private_inputs_and_resolved_start_without_bodies(self):
        code, static, raw, stderr = self.start("PRIVATE_PLAN_TASK", "--dry-run")
        self.assertEqual((code, stderr), (0, ""), raw)
        record = planning_record(static["data"]["roles"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "planning.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            code, envelope, raw, stderr = self.start(
                "PRIVATE_PLAN_TASK",
                "--planning-record-file",
                str(path),
                "--dry-run",
            )
        self.assertEqual((code, stderr), (0, ""), raw)
        planning = envelope["data"]["planning"]
        self.assertRegex(planning["bindings"]["input"], r"^[a-f0-9]{64}$")
        self.assertRegex(planning["bindings"]["start_config"], r"^[a-f0-9]{64}$")
        self.assertEqual(planning["status"], "accepted")
        self.assertNotIn("PRIVATE_PLAN_TASK", raw)
        validate_planning_record(planning)

    def test_typesafe_jev_provenance_is_body_free_and_strict(self):
        roles = [
            {
                "name": "implementer",
                "provider": "worker-provider",
                "model": "worker-model",
                "thinking": "medium",
            },
            {
                "name": "reviewer",
                "provider": "worker-provider",
                "model": "worker-model",
                "thinking": "low",
            },
        ]
        record = planning_record(roles)
        record["decision_model"] = {
            "provider": "typesafe",
            "model": "jev-1.13.0",
            "thinking": "off",
            "source": "typesafe-environment",
        }
        record["usage"]["cost_total"] = None
        validated = validate_planning_record(record, allow_unbound=True)
        self.assertEqual(validated["decision_model"], record["decision_model"])
        self.assertNotIn("api_key", json.dumps(validated))

    def test_changed_task_rejects_bound_plan_before_state_creation(self):
        code, static, raw, _ = self.start("ORIGINAL_PRIVATE_TASK", "--dry-run")
        self.assertEqual(code, 0, raw)
        record = planning_record(static["data"]["roles"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "planning.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            code, preview, raw, _ = self.start(
                "ORIGINAL_PRIVATE_TASK",
                "--planning-record-file",
                str(path),
                "--dry-run",
            )
            self.assertEqual(code, 0, raw)
            path.write_text(json.dumps(preview["data"]["planning"]), encoding="utf-8")
            code, failed, raw, stderr = self.start(
                "CHANGED_PRIVATE_TASK",
                "--planning-record-file",
                str(path),
            )
        self.assertEqual((code, stderr), (2, ""), raw)
        self.assertEqual(failed["error"]["code"], "stale_planning_binding")
        self.assertNotIn("PRIVATE_TASK", raw)

    def test_accepted_launch_uses_manifest_v8_with_body_free_provenance(self):
        code, static, raw, _ = self.start("RETAINED_PRIVATE_TASK", "--dry-run")
        self.assertEqual(code, 0, raw)
        record = planning_record(static["data"]["roles"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_path = root / "planning.json"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            code, preview, raw, _ = self.start(
                "RETAINED_PRIVATE_TASK",
                "--planning-record-file",
                str(record_path),
                "--dry-run",
            )
            self.assertEqual(code, 0, raw)
            record_path.write_text(
                json.dumps(preview["data"]["planning"]), encoding="utf-8"
            )
            with (
                mock.patch.object(ORCHESTRATOR, "STATE_ROOT", root / "state"),
                mock.patch.object(
                    ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
                ),
                mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
                mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as create_grid,
            ):
                code, envelope, raw, stderr = self.run_main(
                    [
                        "--json",
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "RETAINED_PRIVATE_TASK",
                        "--session",
                        "pi-planning-provenance",
                        "--skip-model-check",
                        "--planning-record-file",
                        str(record_path),
                    ]
                )
                manifest = create_grid.call_args.args[4]
                manifest["monitor_pane_id"] = "%1"
                for index, role in enumerate(manifest["roles"].values(), start=2):
                    role["pane_id"] = f"%{index}"
                coordination = Path(envelope["data"]["paths"]["coordination"])
                ORCHESTRATOR.save_manifest(coordination, manifest)
                loaded = ORCHESTRATOR.load_manifest(
                    coordination, expected_session="pi-planning-provenance"
                )
        self.assertEqual((code, stderr), (0, ""), raw)
        self.assertEqual(manifest["version"], 8)
        self.assertEqual(manifest["planning"], envelope["data"]["planning"])
        self.assertEqual(loaded["planning"], manifest["planning"])
        supervisor = ORCHESTRATOR.public_supervisor_run(coordination, loaded)
        self.assertEqual(supervisor["planning"], manifest["planning"])
        self.assertEqual(manifest["planning"]["status"], "accepted")
        self.assertNotIn("RETAINED_PRIVATE_TASK", json.dumps(manifest))
        self.assertNotIn("RETAINED_PRIVATE_TASK", raw)

    def test_terminal_dynamic_mode_requires_both_explicit_gates(self):
        code, envelope, raw, _ = self.run_main(
            [
                "--json",
                "start",
                "--project",
                str(ROOT),
                "--task",
                "Synthetic",
                "--dynamic-plan",
            ]
        )
        self.assertEqual(code, 2, raw)
        self.assertEqual(envelope["error"]["code"], "planning_confirmation_required")

        code, envelope, raw, _ = self.run_main(
            [
                "--json",
                "start",
                "--project",
                str(ROOT),
                "--task",
                "Synthetic",
                "--dynamic-plan",
                "--authorize-planning",
            ]
        )
        self.assertEqual(code, 2, raw)
        self.assertEqual(envelope["error"]["code"], "start_confirmation_required")

    def test_terminal_dynamic_preview_uses_shared_envelope(self):
        expected = {
            "project": str(ROOT),
            "dry_run": True,
            "planning": {"mode": "dynamic"},
        }
        with mock.patch(
            "pi_tmux_orchestrator.terminal_planning._run_terminal_planner",
            return_value={
                "version": 1,
                "success": True,
                "envelope": {
                    "schema_version": "1",
                    "command": "start",
                    "success": True,
                    "data": expected,
                    "error": None,
                },
                "error": None,
            },
        ) as run:
            code, envelope, raw, stderr = self.run_main(
                [
                    "--json",
                    "start",
                    "--project",
                    str(ROOT),
                    "--task",
                    "PRIVATE_TERMINAL_TASK",
                    "--dynamic-plan",
                    "--authorize-planning",
                    "--dry-run",
                ]
            )
        self.assertEqual((code, stderr), (0, ""), raw)
        self.assertEqual(envelope["data"], expected)
        request = run.call_args.args[2]
        self.assertTrue(request["input"]["dynamicPlan"])
        self.assertTrue(request["previewOnly"])
        self.assertIn("PRIVATE_TERMINAL_TASK", request["input"]["task"])
        self.assertNotIn("PRIVATE_TERMINAL_TASK", raw)

    def test_terminal_rpc_timeout_fails_before_start_delivery(self):
        process = mock.MagicMock()
        process.stdout = io.StringIO()
        process.poll.return_value = None

        class EmptySelector:
            def register(self, *_args):
                pass

            def select(self, *, timeout):
                self.timeout = timeout
                return []

            def close(self):
                pass

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(terminal_planning, "TERMINAL_TIMEOUT_SECONDS", 0),
            mock.patch.object(
                terminal_planning.selectors,
                "DefaultSelector",
                return_value=EmptySelector(),
            ),
        ):
            output = Path(directory) / "result.json"
            output.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(OrchestrationError, "did not complete safely"):
                terminal_planning._read_terminal_result(
                    process, output, mock.MagicMock()
                )

    def test_terminal_rpc_adapter_keeps_private_input_out_of_process_arguments(self):
        private_task = "PRIVATE_RPC_ADAPTER_TASK_91e2"
        request = {
            "version": 1,
            "previewOnly": True,
            "input": {
                "action": "start",
                "dynamicPlan": True,
                "task": private_task,
            },
        }
        process = mock.MagicMock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.poll.return_value = 0
        observed = {}

        def read_result(_process, output_path, _args):
            environment = popen.call_args.kwargs["env"]
            request_path = Path(environment["PI_TMUX_ORCHESTRATOR_TERMINAL_REQUEST"])
            observed["request"] = json.loads(request_path.read_text(encoding="utf-8"))
            observed["request_mode"] = request_path.stat().st_mode & 0o777
            observed["output_mode"] = output_path.stat().st_mode & 0o777
            return {"version": 1, "success": True}

        with (
            mock.patch(
                "pi_tmux_orchestrator.terminal_planning.command_path",
                return_value="/usr/bin/pi",
            ),
            mock.patch(
                "pi_tmux_orchestrator.terminal_planning.subprocess.Popen",
                return_value=process,
            ) as popen,
            mock.patch(
                "pi_tmux_orchestrator.terminal_planning._read_terminal_result",
                side_effect=read_result,
            ),
        ):
            result = terminal_planning._run_terminal_planner(object(), ROOT, request)

        self.assertTrue(result["success"])
        command = popen.call_args.args[0]
        self.assertNotIn(private_task, command)
        self.assertEqual(observed["request"], request)
        self.assertEqual(observed["request_mode"], 0o600)
        self.assertEqual(observed["output_mode"], 0o600)
        self.assertIn("--no-tools", command)
        self.assertIn("--no-context-files", command)
