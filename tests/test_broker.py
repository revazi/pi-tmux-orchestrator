from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from pi_tmux_orchestrator import broker_store, runtime
from pi_tmux_orchestrator.broker import initialize_broker_run
from pi_tmux_orchestrator.constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    READ_ONLY_TOOLS,
    WINDOW,
)
from pi_tmux_orchestrator.protocol import decode_frame, encode_frame, validate_report
from pi_tmux_orchestrator.storage import ensure_private_directory, save_manifest


class BrokerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.old_root = runtime.STATE_ROOT
        self.addCleanup(setattr, runtime, "STATE_ROOT", self.old_root)
        runtime.STATE_ROOT = Path(self.temporary.name) / "state"
        root = ensure_private_directory(runtime.STATE_ROOT, parents=True)
        session = "pi-broker-test"
        self.coord = ensure_private_directory(root / session / "run-1", parents=True)
        project = ensure_private_directory(Path(self.temporary.name) / "project")
        roles = {}
        for index, role in enumerate(("implementer", "reviewer"), start=1):
            session_dir = ensure_private_directory(
                self.coord / "sessions" / role, parents=True
            )
            roles[role] = {
                "provider": "test",
                "model": "model",
                "thinking": "off",
                "tools": None if role == "implementer" else READ_ONLY_TOOLS,
                "pane_id": f"%{index}",
                "session_dir": str(session_dir),
                "session_id": f"run-1-{role}",
            }
        self.manifest = {
            "version": 3,
            "created_at": "2026-08-01T00:00:00+00:00",
            "session": session,
            "window": WINDOW,
            "project": str(project),
            "coord": str(self.coord),
            "approve_project": False,
            "transport": "tui",
            "coordination": BROKER_COORDINATION,
            "protocol_version": BROKER_PROTOCOL_VERSION,
            "monitor_pane_id": "%3",
            "roles": roles,
        }
        save_manifest(self.coord, self.manifest)


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip_is_strict_and_bounded(self) -> None:
        value = {"version": 1, "type": "response", "success": True}
        encoded = encode_frame(value)
        self.assertEqual(int.from_bytes(encoded[:4], "big"), len(encoded) - 4)
        self.assertEqual(decode_frame(encoded[4:]), value)

    def test_report_acl_and_bounds(self) -> None:
        report = validate_report(
            {
                "kind": "review",
                "summary": "A focused review.",
                "verdict": "approved",
                "checks": [{"name": "unit", "status": "passed"}],
            },
            "reviewer",
        )
        self.assertEqual(report["verdict"], "approved")
        with self.assertRaisesRegex(Exception, "cannot report changed paths"):
            validate_report(
                {
                    "kind": "review",
                    "summary": "Invalid.",
                    "verdict": "approved",
                    "changed_paths": ["src/file.py"],
                },
                "reviewer",
            )


class BrokerStoreTests(BrokerFixture):
    def test_new_run_has_metadata_only_sqlite_and_no_coordination_payload_files(
        self,
    ) -> None:
        initialize_broker_run(
            self.coord,
            self.manifest,
            "PRIVATE_TASK_CANARY",
            {"reviewer": "PRIVATE_ROLE_CANARY"},
        )
        names = {path.name for path in self.coord.iterdir()}
        self.assertIn("broker.sqlite3", names)
        self.assertIn("startup.json", names)
        self.assertIn("control.token", names)
        self.assertFalse(
            any(
                name.startswith(
                    ("handoff-", "review-", "playwright-", "django-review-")
                )
                or name.endswith(".ready")
                or name == "task.md"
                for name in names
            )
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            for row in database.iterdump():
                self.assertNotIn("PRIVATE_TASK_CANARY", row)
                self.assertNotIn("PRIVATE_ROLE_CANARY", row)
        mode = os.stat(self.coord / "broker.sqlite3").st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_snapshot_reports_actual_usage_fields_and_workflow_state(self) -> None:
        initialize_broker_run(self.coord, self.manifest, "task", {})
        socket_path = broker_store.broker_paths(self.coord)["socket"]
        self.assertLess(len(os.fsencode(socket_path)), 100)
        snapshot = broker_store.public_broker_snapshot(self.coord)
        self.assertEqual(snapshot["workflow"]["state"], "starting")
        self.assertEqual(snapshot["usage"]["total_tokens"], 0)
        self.assertTrue(snapshot["usage"]["actual_provider_usage_only"])
        self.assertFalse(snapshot["usage"]["soft_total_budget_exceeded"])
        self.assertEqual({role["total_tokens"] for role in snapshot["roles"]}, {0})
        self.assertEqual(
            {role["state"] for role in snapshot["roles"]}, {"disconnected"}
        )

    def test_supervisor_command_status_reads_broker_metadata(self) -> None:
        from pi_tmux_orchestrator.supervisor_api import supervisor_command_status

        initialize_broker_run(self.coord, self.manifest, "task", {})
        command_id = "a" * 32
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO control_commands(id,action,role,delivery,status,received_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    command_id,
                    "send",
                    "reviewer",
                    "follow-up",
                    "accepted",
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T00:00:01+00:00",
                ),
            )
        result = supervisor_command_status(
            self.manifest["session"],
            self.coord.name,
            role="reviewer",
            command_id=command_id,
        )
        self.assertEqual(result["command"]["action"], "send")
        self.assertEqual(result["command"]["status"], "accepted")
        self.assertNotIn("message", result["command"])


class PromptAndExtensionContractTests(unittest.TestCase):
    def test_production_code_has_no_lifecycle_polling_sleep(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production = [
            path
            for path in (root / "pi_tmux_orchestrator").glob("*.py")
            if path.name not in {"relay.py", "rpc_protocol.py"}
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in production)
        self.assertNotIn("time.sleep(", combined)
        bridge = (root / "extensions" / "orchestrator-worker.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("orchestrator_report", bridge)
        self.assertIn("terminate: true", bridge)
        self.assertNotIn("handoff-N.md", bridge)
        self.assertNotIn(".ready", bridge)

    def test_worker_prompts_end_turn_instead_of_waiting(self) -> None:
        from pi_tmux_orchestrator.prompts import role_system_prompt

        for role in ("implementer", "reviewer", "probe", "playwright", "django"):
            prompt = role_system_prompt(Path("/tmp/project"), role)
            self.assertIn("End your turn", prompt)
            self.assertIn("Never run sleep commands", prompt)
            self.assertIn("orchestrator_report", prompt)
            self.assertNotIn("handoff-N", prompt)
