"""Dynamic-planning provenance and launch-binding tests."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from json_cli_support import JsonCliFixture
from pi_tmux_orchestrator import (
    commands,
    supervisor_api,
    supervisor_commands,
    terminal_planning,
)
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
        "version": 2,
        "mode": "dynamic",
        "worker_candidates": {
            "version": 1,
            "source": "authoritative-locks",
            "count": len(
                set((role["provider"], role["model"]) for role in retained_roles)
            ),
            "roles": [
                {"role": role["id"], "source": "exact-lock", "count": 1}
                for role in retained_roles
            ],
        },
        "request_id": "a" * 32,
        "status": "accepted",
        "created_at_ms": 1_800_000_000_000,
        "accepted_at_ms": 1_800_000_000_001,
        "decision_schema_version": 1,
        "decision_model": {
            "provider": "decision-provider",
            "model": "decision-model",
            "thinking": "max",
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


def scope_planning_record(roles, scopes):
    record = planning_record(roles)
    record.update(
        version=3,
        scopes=list(scopes),
        locks=[
            {
                "role": role["id"],
                "inclusion": True,
                "provider": role["provider"],
                "model": role["model"],
                "thinking": role["thinking"],
            }
            for role in record["roles"]
        ],
    )
    record["bindings"]["decision"] = metadata_digest(
        {
            "version": 1,
            "roles": record["roles"],
            "scopes": record["scopes"],
            "locks": record["locks"],
        }
    )
    return record


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

    def test_all_scope_records_bind_and_reject_stale_or_conflicting_axes(self):
        import itertools

        code, static, _, _ = self.start("Synthetic scopes", "--dry-run")
        self.assertEqual(code, 0)
        for count in (1, 2, 3):
            for scopes in itertools.combinations(
                ("topology", "models", "thinking"), count
            ):
                with (
                    self.subTest(scopes=scopes),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    record = scope_planning_record(static["data"]["roles"], scopes)
                    path = Path(directory) / "planning.json"
                    path.write_text(json.dumps(record), encoding="utf-8")
                    code, preview, raw, _ = self.start(
                        "Synthetic scopes",
                        "--planning-record-file",
                        str(path),
                        "--dry-run",
                    )
                    self.assertEqual(code, 0, raw)
                    bound = preview["data"]["planning"]
                    self.assertEqual(bound["scopes"], list(scopes))
                    self.assertEqual(bound["locks"], record["locks"])
                    validate_planning_record(bound)
                    changed = json.loads(json.dumps(bound))
                    changed["locks"][0]["thinking"] = (
                        "off" if changed["roles"][0]["thinking"] != "off" else "low"
                    )
                    with self.assertRaises(OrchestrationError):
                        validate_planning_record(changed)
                    changed = json.loads(json.dumps(bound))
                    changed["scopes"] = (
                        ["thinking"] if list(scopes) != ["thinking"] else ["models"]
                    )
                    with self.assertRaises(OrchestrationError):
                        validate_planning_record(changed)
                    # Even a valid re-bound decision cannot reuse a confirmed preview of different scopes.
                    changed["bindings"]["decision"] = metadata_digest(
                        {
                            "version": 1,
                            "roles": changed["roles"],
                            "scopes": changed["scopes"],
                            "locks": changed["locks"],
                        }
                    )
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with mock.patch.object(ORCHESTRATOR, "tmux") as launch:
                        code, rejected, _, _ = self.start(
                            "Synthetic scopes", "--planning-record-file", str(path)
                        )
                    self.assertNotEqual(code, 0)
                    self.assertEqual(
                        rejected["error"]["code"], "stale_planning_binding"
                    )
                    launch.assert_not_called()

    def test_terminal_forwards_each_scope_combination_and_rejects_duplicates(self):
        import itertools

        for count in (1, 2, 3):
            for scopes in itertools.combinations(
                ("topology", "models", "thinking"), count
            ):
                args = ORCHESTRATOR.build_parser().parse_args(
                    [
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "Synthetic",
                        "--dynamic-plan",
                        "--authorize-planning",
                        "--dry-run",
                        *[
                            arg
                            for scope in scopes
                            for arg in ("--planning-scope", scope)
                        ],
                    ]
                )
                projected = terminal_planning._terminal_input(args, ROOT)
                self.assertEqual(projected["planningScopes"], list(scopes))
                self.assertIs(projected["dynamicPlan"], True)
        with mock.patch.object(terminal_planning, "_run_terminal_planner") as provider:
            code, _, _, _ = self.run_main(
                [
                    "--json",
                    "start",
                    "--task",
                    "Synthetic",
                    "--dynamic-plan",
                    "--authorize-planning",
                    "--dry-run",
                    "--planning-scope",
                    "topology",
                    "--planning-scope",
                    "topology",
                ]
            )
        self.assertNotEqual(code, 0)
        provider.assert_not_called()
        code, _, _, _ = self.start(
            "Synthetic", "--planning-scope", "models", "--dry-run"
        )
        self.assertNotEqual(code, 0)

    def test_terminal_and_supervisor_human_reads_show_scope_authority(self):
        import itertools

        roles = [
            {"name": role, "provider": "p", "model": "a", "thinking": "low"}
            for role in ("implementer", "reviewer")
        ]
        for count in (1, 2, 3):
            for scopes in itertools.combinations(
                ("topology", "models", "thinking"), count
            ):
                record = scope_planning_record(roles, scopes)
                for lock in record["locks"]:
                    if "models" in scopes:
                        lock.update(provider=None, model=None)
                    if "thinking" in scopes:
                        lock["thinking"] = None
                record["locks"].append(
                    {
                        "role": "probe",
                        "inclusion": False,
                        "provider": None,
                        "model": None,
                        "thinking": None,
                    }
                )
                args = ORCHESTRATOR.build_parser().parse_args(
                    [
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "PRIVATE_HUMAN_READ_TASK",
                        "--dynamic-plan",
                        "--authorize-planning",
                        "--dry-run",
                    ]
                )
                output = io.StringIO()
                ORCHESTRATOR.JSON_MODE = False
                with (
                    redirect_stdout(output),
                    mock.patch.object(
                        terminal_planning,
                        "_run_terminal_planner",
                        return_value={
                            "version": 1,
                            "success": True,
                            "envelope": {
                                "command": "start",
                                "success": True,
                                "data": {"planning": record},
                            },
                        },
                    ),
                    mock.patch.object(
                        supervisor_commands,
                        "supervisor_snapshot",
                        return_value={
                            "session": "pi-synthetic",
                            "run_id": "synthetic",
                            "transport": "rpc",
                            "planning": record,
                            "roles": [],
                        },
                    ),
                ):
                    terminal_planning.terminal_dynamic_start(args)
                    supervisor_commands.supervisor_snapshot_command(
                        mock.Mock(session="pi-synthetic", run=None)
                    )
                text = output.getvalue()
                self.assertEqual(text.count(f"Planning scopes: {','.join(scopes)}"), 2)
                self.assertEqual(text.count("decision source=configured-fallback"), 2)
                model = "planner" if "models" in scopes else "locked p/a"
                thinking = "planner" if "thinking" in scopes else "locked low"
                self.assertEqual(
                    text.count(
                        f"implementer: inclusion=locked include; model={model}; thinking={thinking}"
                    ),
                    2,
                )
                self.assertEqual(
                    text.count(
                        "probe: inclusion=locked omit; model=not applicable; thinking=not applicable"
                    ),
                    2,
                )
                self.assertNotIn("PRIVATE_HUMAN_READ_TASK", text)

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
        for source in ("typesafe-auth", "typesafe-environment"):
            with self.subTest(source=source):
                record = planning_record(roles)
                record["decision_model"] = {
                    "provider": "typesafe",
                    "model": "jev-1.13.0",
                    "thinking": "off",
                    "source": source,
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

    def test_accepted_launch_uses_manifest_v10_with_body_free_provenance(self):
        code, static, raw, _ = self.start("RETAINED_PRIVATE_TASK", "--dry-run")
        self.assertEqual(code, 0, raw)
        record = scope_planning_record(static["data"]["roles"], ["topology"])
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
                terminal_args = ORCHESTRATOR.build_parser().parse_args(
                    [
                        "start",
                        "--project",
                        str(ROOT),
                        "--task",
                        "RETAINED_PRIVATE_TASK",
                        "--dynamic-plan",
                        "--authorize-planning",
                        "--dry-run",
                    ]
                )
                ORCHESTRATOR.JSON_MODE = False
                for version in (1, 2, 3):
                    with self.subTest(retained_planning_version=version):
                        retained = json.loads(json.dumps(manifest))
                        plan = retained["planning"]
                        if version != 3:
                            plan["version"] = version
                            del plan["scopes"], plan["locks"]
                            plan["bindings"]["decision"] = metadata_digest(
                                {"version": 1, "roles": plan["roles"]}
                            )
                            if version == 1:
                                del plan["worker_candidates"]
                        ORCHESTRATOR.save_manifest(coordination, retained)
                        loaded = ORCHESTRATOR.load_manifest(
                            coordination, expected_session="pi-planning-provenance"
                        )
                        with mock.patch.object(
                            supervisor_api,
                            "resolve_supervisor_target",
                            return_value=(coordination, loaded),
                        ):
                            snapshot = supervisor_api.supervisor_snapshot(
                                "pi-planning-provenance", None
                            )
                        self.assertEqual(snapshot["planning"], plan)
                        output = io.StringIO()
                        with (
                            redirect_stdout(output),
                            mock.patch.object(
                                commands,
                                "resolve_session",
                                return_value=("pi-planning-provenance", coordination),
                            ),
                            mock.patch.object(
                                commands, "tmux", return_value=mock.Mock(stdout="")
                            ),
                            mock.patch.object(
                                commands,
                                "try_public_broker_snapshot",
                                return_value=None,
                            ),
                            mock.patch.object(
                                supervisor_commands,
                                "supervisor_snapshot",
                                return_value=snapshot,
                            ),
                            mock.patch.object(
                                terminal_planning,
                                "_run_terminal_planner",
                                return_value={
                                    "version": 1,
                                    "success": True,
                                    "envelope": {
                                        "command": "start",
                                        "success": True,
                                        "data": {"planning": plan},
                                    },
                                },
                            ),
                        ):
                            status = commands.status_command(
                                mock.Mock(session="pi-planning-provenance")
                            )
                            supervisor_commands.supervisor_snapshot_command(
                                mock.Mock(session="pi-planning-provenance", run=None)
                            )
                            terminal_planning.terminal_dynamic_start(terminal_args)
                        self.assertEqual(status.data["planning"], plan)
                        text = output.getvalue()
                        if version == 3:
                            self.assertIn("scopes=topology", text)
                            self.assertEqual(text.count("Planning scopes: topology"), 2)
                            self.assertEqual(
                                text.count(
                                    "implementer: inclusion=locked include; model=locked "
                                ),
                                3,
                            )
                        else:
                            self.assertIn("scopes=unavailable (legacy record)", text)
                            self.assertEqual(
                                text.count(
                                    "Planning scopes: unavailable (legacy record)"
                                ),
                                2,
                            )
                            self.assertEqual(
                                text.count("Locks: unavailable (legacy record)"), 3
                            )
                            self.assertNotIn("topology", text)
                            self.assertNotIn("inclusion=", text)
                            self.assertNotIn("model=planner", text)
                            self.assertNotIn("thinking=planner", text)
                            self.assertNotIn("scopes", status.data["planning"])
                            self.assertNotIn("locks", snapshot["planning"])
                        self.assertEqual(text.count("source=configured-fallback"), 3)
                        self.assertNotIn("RETAINED_PRIVATE_TASK", text)
        self.assertEqual((code, stderr), (0, ""), raw)
        self.assertEqual(manifest["version"], 10)
        self.assertEqual(manifest["planning"], envelope["data"]["planning"])
        self.assertEqual(loaded["planning"], manifest["planning"])
        supervisor = ORCHESTRATOR.public_supervisor_run(coordination, loaded)
        self.assertEqual(supervisor["planning"], manifest["planning"])
        self.assertEqual(supervisor["planning"]["scopes"], ["topology"])
        self.assertEqual(supervisor["planning"]["locks"], record["locks"])
        self.assertEqual(manifest["planning"]["status"], "accepted")
        self.assertNotIn("RETAINED_PRIVATE_TASK", json.dumps(manifest))
        self.assertNotIn("RETAINED_PRIVATE_TASK", raw)

    def test_legacy_planning_remains_readable_but_cannot_admit_a_new_start(self):
        from pi_tmux_orchestrator.planning import retained_planning

        code, static, raw, _ = self.start("Synthetic", "--dry-run")
        self.assertEqual(code, 0, raw)
        record = planning_record(static["data"]["roles"])
        record["version"] = 1
        del record["worker_candidates"]
        record["bindings"]["input"] = "a" * 64
        record["bindings"]["start_config"] = "b" * 64
        self.assertEqual(retained_planning({"version": 8, "planning": record}), record)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            code, failed, raw, _ = self.start(
                "Synthetic", "--planning-record-file", str(path), "--dry-run"
            )
        self.assertEqual(code, 2, raw)
        self.assertIn("fresh approved-pool preview", failed["error"]["message"])

    def test_candidate_metadata_changes_reject_launch_without_state(self):
        code, static, raw, _ = self.start("Synthetic", "--dry-run")
        self.assertEqual(code, 0, raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "planning.json"
            path.write_text(
                json.dumps(planning_record(static["data"]["roles"])), encoding="utf-8"
            )
            code, preview, raw, _ = self.start(
                "Synthetic", "--planning-record-file", str(path), "--dry-run"
            )
            self.assertEqual(code, 0, raw)
            record = preview["data"]["planning"]
            record["bindings"]["candidate_set"] = "e" * 64
            path.write_text(json.dumps(record), encoding="utf-8")
            with mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as launch:
                code, failed, raw, _ = self.start(
                    "Synthetic", "--planning-record-file", str(path)
                )
            launch.assert_not_called()
        self.assertEqual(code, 2, raw)
        self.assertEqual(failed["error"]["code"], "stale_planning_binding")

    def test_configured_pool_membership_and_changed_pool_are_launch_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            path = root / "planning.json"
            with mock.patch.dict(
                os.environ, {"PI_TMUX_ORCHESTRATOR_CONFIG": str(config_path)}
            ):
                code, static, raw, _ = self.start("Synthetic", "--dry-run")
                self.assertEqual(code, 0, raw)
                roles = static["data"]["roles"]
                identities = [
                    {"provider": role["provider"], "model": role["model"]}
                    for role in roles
                ]
                config = {
                    "version": 5,
                    "workerCandidates": {"version": 1, "all": identities},
                }
                config_path.write_text(json.dumps(config), encoding="utf-8")
                record = planning_record(roles)
                record["worker_candidates"] = {
                    "version": 1,
                    "source": "configured",
                    "count": len(identities),
                    "roles": [
                        {
                            "role": role["name"],
                            "source": "all-pool",
                            "count": len(identities),
                        }
                        for role in roles
                    ],
                }
                path.write_text(json.dumps(record), encoding="utf-8")
                code, preview, raw, _ = self.start(
                    "Synthetic", "--planning-record-file", str(path), "--dry-run"
                )
                self.assertEqual(code, 0, raw)
                path.write_text(
                    json.dumps(preview["data"]["planning"]), encoding="utf-8"
                )
                config["workerCandidates"]["all"].append(
                    {"provider": "synthetic", "model": "new-approved"}
                )
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as launch:
                    code, failed, raw, _ = self.start(
                        "Synthetic", "--planning-record-file", str(path)
                    )
                launch.assert_not_called()
                self.assertEqual(code, 2, raw)
                self.assertEqual(failed["error"]["code"], "stale_planning_binding")
                record["roles"][0]["model"] = "unapproved"
                record["bindings"]["decision"] = metadata_digest(
                    {"version": 1, "roles": record["roles"]}
                )
                path.write_text(json.dumps(record), encoding="utf-8")
                code, failed, raw, _ = self.start(
                    "Synthetic", "--planning-record-file", str(path), "--dry-run"
                )
                self.assertEqual(code, 2, raw)
                self.assertIn("unapproved worker model", failed["error"]["message"])

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
            output = io.StringIO()
            ORCHESTRATOR.JSON_MODE = False
            with redirect_stdout(output):
                terminal_planning.terminal_dynamic_start(run.call_args.args[0])
        text = output.getvalue()
        self.assertIn("Planning scopes: unavailable (legacy record)", text)
        self.assertIn("Locks: unavailable (legacy record)", text)
        self.assertIn("decision source=unavailable", text)
        self.assertNotIn("decision source=legacy", text)
        self.assertNotIn("topology", text)
        self.assertNotIn("PRIVATE_TERMINAL_TASK", text)
        self.assertEqual((code, stderr), (0, ""), raw)
        self.assertEqual(envelope["data"], expected)
        request = run.call_args.args[2]
        self.assertTrue(request["input"]["dynamicPlan"])
        self.assertTrue(request["previewOnly"])
        self.assertIn("PRIVATE_TERMINAL_TASK", request["input"]["task"])
        self.assertNotIn("PRIVATE_TERMINAL_TASK", raw)

    def test_terminal_missing_pool_preserves_actionable_policy_error(self):
        message = (
            "Dynamic planning requires approved worker models. Configure version 5 "
            "workerCandidates in the external tmux-orchestrator.json, or supply exact "
            "provider/model overrides for every planner-eligible role (including "
            "optional roles)."
        )
        with (
            mock.patch.object(
                terminal_planning,
                "_run_terminal_planner",
                return_value={
                    "version": 1,
                    "success": False,
                    "envelope": None,
                    "error": {
                        "code": "approved_worker_pool_required",
                        "message": message,
                    },
                },
            ),
            mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as create_grid,
        ):
            code, envelope, raw, stderr = self.run_main(
                [
                    "--json",
                    "start",
                    "--project",
                    str(ROOT),
                    "--task",
                    "PRIVATE_TERMINAL_POOL_TASK",
                    "--dynamic-plan",
                    "--authorize-planning",
                    "--yes",
                ]
            )
        self.assertEqual((code, stderr), (2, ""), raw)
        self.assertEqual(envelope["error"]["code"], "approved_worker_pool_required")
        self.assertEqual(envelope["error"]["message"], message)
        self.assertNotIn("PRIVATE_TERMINAL_POOL_TASK", raw)
        create_grid.assert_not_called()

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
