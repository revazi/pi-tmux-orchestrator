"""Model-free JS/Python evidence contract and real CLI admission/persistence."""

from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from json_cli_support import JsonCliFixture
from pi_tmux_orchestrator import commands, runtime, supervisor_api
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.planner_evidence import (
    MAX_EVIDENCE_BYTES,
    evidence_digest,
    planner_evidence_lines,
)
from pi_tmux_orchestrator.planning import (
    load_planning_record,
    metadata_digest,
    retained_planning,
    validate_planning_record,
)
from tests.support import ORCHESTRATOR

ROOT = Path(__file__).resolve().parents[1]


def rebind(record):
    record["bindings"]["decision"] = metadata_digest(
        {
            "version": 1,
            **{
                field: record[field]
                for field in ("roles", "scopes", "locks", "task_intent")
            },
            "evidence": evidence_digest(record["evidence"]),
        }
    )


class PlannerEvidenceTests(JsonCliFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        result = subprocess.run(
            ["node", str(ROOT / "tests/fixtures/planner-evidence.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
            env={**os.environ, "TYPESAFE_API_KEY": ""},
        )
        cls.fixtures = json.loads(result.stdout)

    def test_cross_language_all_scopes_fixed_pi_mixed_custom_and_redaction(self):
        scopes = [
            list(combo)
            for count in (1, 2, 3)
            for combo in itertools.combinations(
                ("topology", "models", "thinking"), count
            )
        ]
        self.assertEqual([record["scopes"] for record in self.fixtures[:7]], scopes)
        for record in self.fixtures:
            with self.subTest(
                source=record["evidence"]["source"], scopes=record["scopes"]
            ):
                self.assertEqual(
                    validate_planning_record(record, allow_unbound=True), record
                )
                serialized = json.dumps(record)
                for body in (
                    "PRIVATE_TASK_BODY",
                    "PRIVATE_CONTEXT_BODY",
                    "PRIVATE_GUIDANCE_BODY",
                    "PRIVATE_CATALOG_BODY",
                    "PRIVATE_CATALOG_ENDPOINT",
                    "PRIVATE_CREDENTIAL",
                    "SYNTHETIC_KEY",
                    "instructions",
                    "probabilities",
                ):
                    self.assertNotIn(body, serialized)
                for item in record["evidence"]["decisions"]:
                    if (
                        item["authority"] == "fixed"
                        or record["evidence"]["source"] == "pi_selection"
                    ):
                        self.assertIsNone(item["confidence"])
                        self.assertIsNone(item["selected_probability"])
                        self.assertEqual(item["alternatives"], [])
                    else:
                        self.assertEqual(item["confidence"], 0.77)
                        self.assertEqual(item["selected_probability"], 0.4)
                        self.assertLessEqual(len(item["alternatives"]), 3)
                self.assertIn(
                    "not joint confidence or reasoning",
                    planner_evidence_lines(record)[0],
                )
                self.assertIn(
                    "rationale_unavailable", "\n".join(planner_evidence_lines(record))
                )
        self.assertEqual(
            self.fixtures[8]["evidence"]["provider_comparison"]["state"], "mixed"
        )
        self.assertEqual(
            self.fixtures[9]["evidence"]["decisions"][-1]["axis"], "composition"
        )

    def test_rejects_malformed_duplicate_non_normalized_inconsistent_and_fact_mismatched(
        self,
    ):
        base = self.fixtures[6]
        mutations = [
            lambda e: e.update(version=True),
            lambda e: e.update(source="invented_reasoning"),
            lambda e: e.update(raw_response="PRIVATE_RESPONSE"),
            lambda e: e["catalog"][0]["capabilities"]["input"].update(text="missing"),
            lambda e: e["catalog"][0]["capabilities"].update(
                endpoint="PRIVATE_ENDPOINT"
            ),
            lambda e: e["catalog"][0]["capabilities"]["declared_cost"].update(input=-1),
            lambda e: e["catalog"][0]["capabilities"]["context_window"].update(
                tokens=True
            ),
            lambda e: e["catalog"].append(copy.deepcopy(e["catalog"][0])),
            lambda e: e["catalog"].reverse(),
            lambda e: e["catalog"][0]["thinking_levels"].append("low"),
            lambda e: e["eligibility"][0]["identities"].append(
                copy.deepcopy(e["eligibility"][0]["identities"][0])
            ),
            lambda e: e["decisions"].append(copy.deepcopy(e["decisions"][0])),
            lambda e: e["decisions"][0].update(confidence=1),
            lambda e: e["decisions"][1]["options"][0]["identity"].update(
                facts="a" * 64
            ),
            lambda e: e["decisions"][1]["options"].reverse(),
            lambda e: e["decisions"][1]["options"].pop(),
            lambda e: e["decisions"][1]["options"][0].update(probability=0.9),
            lambda e: e["decisions"][1].update(confidence=True),
            lambda e: e["decisions"][1].update(selected_probability=True),
            lambda e: e["decisions"][1]["alternatives"].reverse(),
            lambda e: e["decisions"][1]["alternatives"].append(
                e["decisions"][1]["selected"]
            ),
            lambda e: e["decisions"][1].update(selected="f" * 64),
            lambda e: e["decisions"][2]["options"][0]["identity"]["models"].pop(),
            lambda e: e["provider_comparison"].update(state="mixed"),
            lambda e: e["provider_comparison"].update(
                rationale="Jev explained quality"
            ),
            lambda e: e.update(catalog=[{}] * 101),
            lambda e: e.update(raw_response="x" * (MAX_EVIDENCE_BYTES + 1)),
        ]
        # Every rejection is tested even after recomputing the outer decision digest.
        for index, mutate in enumerate(mutations):
            record = copy.deepcopy(base)
            mutate(record["evidence"])
            rebind(record)
            with self.subTest(mutation=index), self.assertRaises(OrchestrationError):
                validate_planning_record(record, allow_unbound=True)
        changed = copy.deepcopy(base)
        changed["evidence"]["catalog"][0]["capabilities"]["declared_cost"]["input"] = 99
        rebind(changed)
        with self.assertRaises(OrchestrationError):
            validate_planning_record(changed, allow_unbound=True)

    def test_strict_file_duplicate_and_oversize_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "planning.json"
            text = json.dumps(self.fixtures[6])
            for raw in (
                text.replace(
                    '"confidence": 0.77', '"confidence": 0.77, "confidence": 0.77', 1
                ),
                " " * (256 * 1024 + 1),
                "[" * 1200,
            ):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises(OrchestrationError):
                    load_planning_record(str(path), allow_unbound=True)

    def start(self, path, record, *extra):
        args = [
            "--json",
            "start",
            "--project",
            str(ROOT),
            "--task",
            "PRIVATE_TASK_BODY",
            "--task-intent",
            "change",
            "--skip-model-check",
            "--session",
            "pi-evidence",
            "--planning-record-file",
            str(path),
        ]
        for role in record["roles"]:
            for field in ("provider", "model", "thinking"):
                args.extend([f"--{role['id']}-{field}", role[field]])
        with (
            mock.patch.object(
                ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
            ),
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
        ):
            return self.run_main([*args, *extra])

    def test_cli_preview_stale_evidence_launch_reopen_status_list_supervisor_dashboard(
        self,
    ):
        from pi_tmux_orchestrator.dashboard import render_dashboard

        record = copy.deepcopy(self.fixtures[6])
        pool = [
            {"provider": item["provider"], "model": item["model"]}
            for item in record["evidence"]["catalog"]
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "planning.json"
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {"version": 5, "workerCandidates": {"version": 1, "all": pool}}
                ),
                encoding="utf-8",
            )
            path.write_text(json.dumps(record), encoding="utf-8")
            with (
                mock.patch.dict(
                    os.environ, {"PI_TMUX_ORCHESTRATOR_CONFIG": str(config)}
                ),
                mock.patch.object(ORCHESTRATOR, "STATE_ROOT", root / "state"),
            ):
                code, preview, raw, _ = self.start(path, record, "--dry-run")
                self.assertEqual(code, 0, raw[:300])
                bound = preview["data"]["planning"]
                config.write_text(
                    json.dumps(
                        {
                            "version": 5,
                            "workerCandidates": {"version": 1, "all": pool[:-1]},
                        }
                    ),
                    encoding="utf-8",
                )
                with mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as launch:
                    code, rejected, _, _ = self.start(path, record, "--dry-run")
                self.assertEqual(code, 2)
                self.assertEqual(rejected["error"]["code"], "stale_planning_binding")
                launch.assert_not_called()
                config.write_text(
                    json.dumps(
                        {"version": 5, "workerCandidates": {"version": 1, "all": pool}}
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(bound["evidence"], record["evidence"])
                tampered = copy.deepcopy(bound)
                tampered["evidence"]["decisions"][-1]["confidence"] = 0.5
                rebind(tampered)
                path.write_text(json.dumps(tampered), encoding="utf-8")
                with mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as launch:
                    code, rejected, _, _ = self.start(path, record)
                self.assertEqual(code, 2)
                self.assertEqual(rejected["error"]["code"], "stale_planning_binding")
                launch.assert_not_called()
                path.write_text(json.dumps(bound), encoding="utf-8")
                with (
                    mock.patch.object(ORCHESTRATOR, "STATE_ROOT", root / "state"),
                    mock.patch.object(ORCHESTRATOR, "create_tmux_grid") as grid,
                ):
                    code, launched, raw, _ = self.start(path, record)
                    manifest = grid.call_args.args[4]
                    manifest["monitor_pane_id"] = "%0"
                    for index, role in enumerate(manifest["roles"].values(), start=1):
                        role["pane_id"] = f"%{index}"
                    coordination = Path(launched["data"]["paths"]["coordination"])
                    ORCHESTRATOR.save_manifest(coordination, manifest)
                self.assertEqual(code, 0, raw[:300])
                coordination = Path(launched["data"]["paths"]["coordination"])
                manifest = ORCHESTRATOR.load_manifest(
                    coordination, expected_session="pi-evidence"
                )
                self.assertEqual(manifest["planning"]["evidence"], record["evidence"])
                with mock.patch.object(
                    supervisor_api,
                    "resolve_supervisor_target",
                    return_value=(coordination, manifest),
                ):
                    snapshot = supervisor_api.supervisor_snapshot("pi-evidence", None)
                self.assertEqual(snapshot["planning"]["evidence"], record["evidence"])
                with (
                    mock.patch.object(
                        commands,
                        "resolve_session",
                        return_value=("pi-evidence", coordination),
                    ),
                    mock.patch.object(
                        commands, "tmux", return_value=mock.Mock(stdout="")
                    ),
                    mock.patch.object(
                        commands, "try_public_broker_snapshot", return_value=None
                    ),
                    mock.patch.object(runtime, "JSON_MODE", False),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    status = commands.status_command(mock.Mock(session="pi-evidence"))
                self.assertEqual(
                    status.data["planning"]["evidence"], record["evidence"]
                )
                self.assertIn("alternatives:", output.getvalue())
                with (
                    mock.patch.object(
                        commands,
                        "orchestrated_sessions",
                        return_value=[("pi-evidence", coordination)],
                    ),
                    mock.patch.object(
                        commands, "orchestration_dashboard_summary", return_value=None
                    ),
                ):
                    listed = commands.list_command(mock.Mock())
                self.assertEqual(
                    listed.data["sessions"][0]["planning"]["evidence"]["projection"],
                    "summary",
                )
                self.assertEqual(
                    supervisor_api.public_supervisor_run(
                        coordination, manifest, summary=True
                    )["planning"],
                    listed.data["sessions"][0]["planning"],
                )
                retained_sessions = supervisor_api.retained_sessions()
                self.assertEqual(
                    retained_sessions["sessions"][0]["planning"]["evidence"][
                        "projection"
                    ],
                    "summary",
                )
                summary = retained_planning(manifest, summary=True)["evidence"]
                self.assertEqual(summary["projection"], "summary")
                self.assertNotIn("catalog", summary)
                self.assertEqual(
                    summary["decision_binding"], bound["bindings"]["decision"]
                )
                self.assertNotIn("PRIVATE_TASK_BODY", json.dumps(manifest))
                # Retained records never replace immutable launch assignment identity.
                corrupted = copy.deepcopy(manifest)
                corrupted["roles"]["implementer"]["model"] = "other"
                with self.assertRaises(OrchestrationError):
                    ORCHESTRATOR.validate_manifest(
                        corrupted, coordination, expected_session="pi-evidence"
                    )
                rendered = render_dashboard(
                    manifest, {}, [], width=160, height=40, color=False
                )
                self.assertIn("Planner evidence v1 typesafe_choice", rendered)
                self.assertIn("rationale_unavailable", rendered)

    def test_legacy_v4_and_static_explicit_unavailable(self):
        record = copy.deepcopy(self.fixtures[6])
        record["version"] = 4
        del record["evidence"]
        record["bindings"]["decision"] = metadata_digest(
            {
                "version": 1,
                **{
                    field: record[field]
                    for field in ("roles", "scopes", "locks", "task_intent")
                },
            }
        )
        record["bindings"].update(input="a" * 64, start_config="b" * 64)
        validate_planning_record(record)
        retained = retained_planning({"version": 10, "planning": record})
        self.assertEqual(retained["evidence"], {"version": 1, "status": "unavailable"})
        self.assertEqual(retained_planning({})["evidence"], retained["evidence"])
        self.assertIn("unavailable", planner_evidence_lines(retained)[0])
