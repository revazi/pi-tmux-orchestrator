from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR


class SupervisorApiFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.original_state_root = ORCHESTRATOR.STATE_ROOT
        self.addCleanup(self.restore_state_root)
        ORCHESTRATOR.STATE_ROOT = Path(self.temporary.name) / "state"
        self.root = ORCHESTRATOR.canonical_state_root(create=True)
        self.project = Path(self.temporary.name).resolve() / "project"
        self.project.mkdir()
        self.session = "pi-supervisor-api"
        self.coord, self.manifest = self.create_run("run-1")
        self.registries: dict[str, dict[str, object]] = {}
        for index, role in enumerate(("implementer", "reviewer"), start=1):
            paths = ORCHESTRATOR.rpc_role_paths(self.coord, role, create=True)
            self.registries[role] = ORCHESTRATOR.initialize_rpc_registry(
                self.coord,
                paths,
                role,
                400 + index,
            )

    def restore_state_root(self) -> None:
        ORCHESTRATOR.STATE_ROOT = self.original_state_root

    def create_run(
        self, run_id: str, *, transport: str = "rpc"
    ) -> tuple[Path, dict[str, object]]:
        session_root = ORCHESTRATOR.ensure_private_directory(self.root / self.session)
        coord = ORCHESTRATOR.ensure_private_directory(session_root / run_id)
        roles: dict[str, dict[str, object]] = {}
        for index, role in enumerate(("implementer", "reviewer"), start=1):
            prompt = coord / f"{role}.prompt.md"
            ORCHESTRATOR.secure_write(prompt, f"Synthetic {role} prompt.\n")
            session_dir = ORCHESTRATOR.ensure_private_directory(
                coord / "sessions" / role,
                parents=True,
            )
            roles[role] = {
                "provider": "synthetic",
                "model": role,
                "thinking": "high",
                "tools": None
                if role == "implementer"
                else ORCHESTRATOR.READ_ONLY_TOOLS,
                "pane_id": f"%{index}",
                "prompt_path": str(prompt),
                "session_dir": str(session_dir),
            }
        manifest = {
            "version": 2,
            "created_at": f"2026-08-10T00:00:0{1 if run_id == 'run-1' else 2}+00:00",
            "session": self.session,
            "window": ORCHESTRATOR.WINDOW,
            "project": str(self.project),
            "coord": str(coord),
            "approve_project": False,
            "transport": transport,
            "monitor_pane_id": "%99",
            "roles": roles,
        }
        ORCHESTRATOR.save_manifest(coord, manifest)
        return coord, manifest

    def record_command(
        self,
        role: str,
        command_id: str,
        statuses: tuple[str, ...],
    ) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, role, create=False)
        registry = self.registries[role]
        ORCHESTRATOR.record_rpc_event(
            paths,
            registry,
            role,
            "command_received",
            command_id=command_id,
            command="prompt",
            delivery="steer",
        )
        for status in statuses:
            ORCHESTRATOR.transition_rpc_command(
                paths,
                registry,
                role,
                command_id,
                status,
            )


class SupervisorApiTests(SupervisorApiFixture):
    def test_capabilities_define_versioned_tmux_independent_read_surface(self) -> None:
        capabilities = ORCHESTRATOR.supervisor_capabilities()
        self.assertEqual(capabilities["api_version"], "2")
        self.assertEqual(
            capabilities["read_operations"],
            [
                "capabilities",
                "sessions",
                "runs",
                "snapshot",
                "usage",
                "events",
                "command",
            ],
        )
        self.assertFalse(capabilities["host_adapter"]["runtime_observed_by_read_api"])
        self.assertEqual(
            capabilities["control_commands"]["send"]["exact_run_option"],
            "--run",
        )
        self.assertEqual(
            capabilities["control_semantics"]["acknowledgement"],
            "acceptance-or-queueing",
        )
        self.assertFalse(capabilities["control_semantics"]["exactly_once"])
        self.assertTrue(capabilities["usage_accounting"]["latest_assignment_usage"])
        self.assertEqual(
            capabilities["usage_accounting"]["legacy_assignment_usage"],
            "unavailable",
        )
        self.assertTrue(capabilities["metadata_only"])

    def test_sessions_runs_and_snapshot_use_only_retained_state(self) -> None:
        second_coord, _ = self.create_run("run-2")
        for role in ("implementer", "reviewer"):
            ORCHESTRATOR.rpc_role_paths(second_coord, role, create=True)
        with mock.patch.object(
            ORCHESTRATOR,
            "tmux",
            side_effect=AssertionError("supervisor read API queried tmux"),
        ):
            sessions = ORCHESTRATOR.retained_sessions()
            runs, issues, truncated = ORCHESTRATOR.retained_runs(self.session, limit=10)
            snapshot = ORCHESTRATOR.supervisor_snapshot(self.session, "run-1")

        self.assertEqual(
            [value["session"] for value in sessions["sessions"]], [self.session]
        )
        self.assertEqual(sessions["sessions"][0]["run_id"], "run-2")
        self.assertEqual([coord.name for coord, _ in runs], ["run-2", "run-1"])
        self.assertEqual(issues, [])
        self.assertFalse(truncated)
        self.assertEqual(snapshot["run_id"], "run-1")
        self.assertEqual(snapshot["host_adapter"]["runtime_status"], "not_observed")
        self.assertEqual(snapshot["roles"][0]["worker"]["last_event_sequence"], 1)
        self.assertEqual(snapshot["roles"][0]["runtime"]["liveness"], "not-observed")
        self.assertIsNone(snapshot["roles"][0]["runtime"]["state"])

    def test_legacy_usage_capability_is_explicitly_unavailable(self) -> None:
        usage = ORCHESTRATOR.supervisor_usage(self.session, "run-1", limit=10)
        self.assertFalse(usage["available"])
        self.assertEqual(usage["availability"], "unavailable_legacy_coordination")
        self.assertIsNone(usage["cumulative"])
        self.assertEqual(usage["roles"], [])
        self.assertFalse(usage["semantics"]["payload_bodies_included"])

    def test_usage_human_output_separates_cache_and_assignment_delta(self) -> None:
        data = {
            "session": self.session,
            "run_id": "run-1",
            "availability": "available",
            "cumulative": {
                "provider_calls": 3,
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 500,
                "cache_write_tokens": 5,
                "reasoning_tokens": None,
                "cost_total": 1.25,
                "operational_tokens": 625,
            },
            "roles": [
                {
                    "role": "implementer",
                    "cumulative": {
                        "provider_calls": 3,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_tokens": 500,
                        "cache_write_tokens": 5,
                        "reasoning_tokens": None,
                        "cost_total": 1.25,
                        "operational_tokens": 625,
                    },
                    "assignments": [
                        {
                            "assignment_id": "a" * 32,
                            "round": 1,
                            "kind": "implementation",
                            "usage": {
                                "provider_calls": 1,
                                "input_tokens": 40,
                                "output_tokens": 10,
                                "cache_read_tokens": 120,
                                "cache_write_tokens": 5,
                                "reasoning_tokens": None,
                                "cost_total": 0.25,
                                "operational_tokens": 175,
                                "context_tokens": 180,
                                "context_window": 1_000,
                                "context_percent": 18.0,
                                "peak_context_tokens": 200,
                            },
                        }
                    ],
                }
            ],
            "truncated": False,
            "limit": 10,
        }
        output = io.StringIO()
        with (
            mock.patch.object(ORCHESTRATOR, "supervisor_usage", return_value=data),
            mock.patch.object(ORCHESTRATOR, "JSON_MODE", False),
            redirect_stdout(output),
        ):
            result = ORCHESTRATOR.supervisor_usage_command(
                argparse.Namespace(session=self.session, run="run-1", limit=10)
            )
        rendered = output.getvalue()
        self.assertIn("cumulative:", rendered)
        self.assertIn("round=1 kind=implementation", rendered)
        self.assertIn("input=40 cache-read=120 cache-write=5 output=10", rendered)
        self.assertIn("reasoning=unavailable", rendered)
        self.assertIn("context=180/1000 context-percent=18.0 peak=200", rendered)
        self.assertEqual(result.data, data)

    def test_state_scans_are_bounded_and_do_not_follow_session_symlinks(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        linked = self.root / "pi-linked-state"
        linked.symlink_to(outside, target_is_directory=True)
        page = ORCHESTRATOR.retained_sessions()
        self.assertNotIn(linked.name, {item["session"] for item in page["sessions"]})
        self.assertIn(linked.name, {item["session"] for item in page["issues"]})

        for index in range(3):
            (self.root / f"extra-{index}").write_text("invalid", encoding="utf-8")
        with mock.patch.object(ORCHESTRATOR, "MAX_SUPERVISOR_SCAN_ENTRIES", 2):
            children, truncated = ORCHESTRATOR.bounded_children(self.root)
        self.assertEqual(len(children), 2)
        self.assertTrue(truncated)

    def test_empty_state_root_returns_an_empty_bounded_session_page(self) -> None:
        ORCHESTRATOR.STATE_ROOT = Path(self.temporary.name) / "missing-state"
        self.assertEqual(
            ORCHESTRATOR.retained_sessions(),
            {
                "api_version": "2",
                "sessions": [],
                "issues": [],
                "truncated": False,
            },
        )
        ORCHESTRATOR.STATE_ROOT.symlink_to(
            Path(self.temporary.name) / "missing-target",
            target_is_directory=True,
        )
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.retained_sessions()

    def test_event_pages_keep_independent_role_cursors(self) -> None:
        self.record_command("implementer", "1" * 32, ("accepted", "completed"))
        self.record_command("reviewer", "2" * 32, ("rejected",))
        batch = ORCHESTRATOR.supervisor_event_batch(
            self.session,
            "run-1",
            requested_roles=None,
            cursors={"implementer": 1, "reviewer": 0},
            limit=1,
        )
        roles = {value["role"]: value for value in batch["roles"]}
        self.assertEqual(roles["implementer"]["events"][0]["sequence"], 2)
        self.assertEqual(roles["implementer"]["cursor"]["next"], 2)
        self.assertTrue(roles["implementer"]["cursor"]["truncated"])
        self.assertTrue(roles["implementer"]["cursor"]["synchronized"])
        self.assertEqual(roles["reviewer"]["events"][0]["sequence"], 1)
        self.assertEqual(roles["reviewer"]["cursor"]["after"], 0)

    def test_all_read_views_omit_task_prompt_and_report_bodies(self) -> None:
        canary = "PRIVATE_SUPERVISOR_VIEW_CANARY_72ce"
        ORCHESTRATOR.secure_write(self.coord / "task.md", canary)
        ORCHESTRATOR.secure_write(self.coord / "handoff-1.md", canary)
        ORCHESTRATOR.secure_write(
            self.coord / "implementer.prompt.md", f"Synthetic prompt {canary}.\n"
        )
        views = {
            "sessions": ORCHESTRATOR.retained_sessions(),
            "snapshot": ORCHESTRATOR.supervisor_snapshot(self.session, "run-1"),
            "usage": ORCHESTRATOR.supervisor_usage(self.session, "run-1", limit=10),
            "events": ORCHESTRATOR.supervisor_event_batch(
                self.session,
                "run-1",
                requested_roles=None,
                cursors={},
                limit=10,
            ),
        }
        self.assertNotIn(canary, json.dumps(views))

    def test_command_lookup_returns_metadata_without_payload_bodies(self) -> None:
        command_id = "3" * 32
        canary = "PRIVATE_SUPERVISOR_COMMAND_CANARY_9c2e"
        ORCHESTRATOR.secure_write(self.coord / "task.md", canary)
        self.record_command(
            "implementer", command_id, ("accepted", "started", "failed")
        )
        result = ORCHESTRATOR.supervisor_command_status(
            self.session,
            "run-1",
            role="implementer",
            command_id=command_id,
        )
        self.assertEqual(result["command"]["status"], "failed")
        self.assertTrue(result["command"]["terminal"])
        self.assertNotIn(canary, json.dumps(result))
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as raised:
            ORCHESTRATOR.supervisor_command_status(
                self.session,
                "run-1",
                role="implementer",
                command_id="4" * 32,
            )
        self.assertEqual(raised.exception.code, "rpc_command_not_found")

    def test_initial_cursor_reports_a_rotated_retention_gap(self) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "implementer", create=False)
        with mock.patch.object(ORCHESTRATOR, "MAX_RPC_EVENT_SEGMENT_BYTES", 700):
            for index in range(8):
                self.record_command(
                    "implementer",
                    f"{index + 10:032x}",
                    ("rejected",),
                )
        events = ORCHESTRATOR.load_rpc_events(self.coord, "implementer")
        self.assertGreater(events[0]["sequence"], 1)
        _, cursor = ORCHESTRATOR.rpc_event_page(events, after=0, limit=1)
        self.assertTrue(cursor["gap"])
        self.assertEqual(cursor["earliest_retained"], events[0]["sequence"])
        self.assertTrue(paths["events_archive"].is_file())

    def test_service_methods_validate_limits_cursors_and_command_ids(self) -> None:
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.retained_runs(self.session, limit=101)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.supervisor_usage(self.session, "run-1", limit=0)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.rpc_event_page([], after=-1, limit=1)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.rpc_event_page([], after=0, limit=101)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as invalid_id:
            ORCHESTRATOR.supervisor_command_status(
                self.session,
                "run-1",
                role="implementer",
                command_id="A" * 32,
            )
        self.assertEqual(invalid_id.exception.code, "invalid_arguments")

    def test_cursor_arguments_reject_duplicates_and_unselected_roles(self) -> None:
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as duplicate:
            ORCHESTRATOR.supervisor_cursor_arguments([("reviewer", 1), ("reviewer", 2)])
        self.assertEqual(duplicate.exception.code, "invalid_arguments")
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as unselected:
            ORCHESTRATOR.supervisor_event_batch(
                self.session,
                "run-1",
                requested_roles=["implementer"],
                cursors={"reviewer": 1},
                limit=10,
            )
        self.assertEqual(unselected.exception.code, "invalid_arguments")

    def test_tui_snapshot_is_visible_but_durable_events_are_rejected(self) -> None:
        tui_coord, _ = self.create_run("run-tui", transport="tui")
        snapshot = ORCHESTRATOR.supervisor_snapshot(self.session, tui_coord.name)
        self.assertFalse(snapshot["durable_workers"])
        self.assertIsNone(snapshot["roles"][0]["worker"])
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as raised:
            ORCHESTRATOR.supervisor_event_batch(
                self.session,
                tui_coord.name,
                requested_roles=None,
                cursors={},
                limit=10,
            )
        self.assertEqual(raised.exception.code, "supervisor_requires_rpc")

    def test_exact_run_send_and_abort_do_not_resolve_a_tmux_session(self) -> None:
        prompt_ack = {
            "version": 2,
            "id": "5" * 32,
            "command": "prompt",
            "success": True,
            "status": "accepted",
            "duplicate": False,
            "event_sequence": 2,
        }
        abort_ack = {
            "version": 2,
            "id": "6" * 32,
            "command": "abort",
            "success": True,
            "status": "completed",
            "duplicate": False,
            "event_sequence": 3,
        }
        with (
            mock.patch.object(
                ORCHESTRATOR,
                "resolve_session",
                side_effect=AssertionError("exact-run control queried tmux"),
            ),
            mock.patch.object(
                ORCHESTRATOR,
                "rpc_control_request",
                side_effect=[prompt_ack, abort_ack],
            ) as request,
        ):
            sent = ORCHESTRATOR.send_command(
                argparse.Namespace(
                    session=self.session,
                    run="run-1",
                    role="implementer",
                    message="Synthetic private instruction",
                    message_file=None,
                    command_id="5" * 32,
                    delivery="steer",
                )
            )
            aborted = ORCHESTRATOR.abort_command(
                argparse.Namespace(
                    session=self.session,
                    run="run-1",
                    role="implementer",
                    command_id="6" * 32,
                )
            )
        self.assertTrue(sent.data["acknowledged"])
        self.assertTrue(aborted.data["acknowledged"])
        self.assertEqual(sent.data["run_id"], "run-1")
        self.assertEqual(aborted.data["run_id"], "run-1")
        self.assertEqual(request.call_count, 2)

    def test_parser_exposes_all_supervisor_actions_and_strict_cursors(self) -> None:
        parser = ORCHESTRATOR.build_parser()
        cases = (
            ["supervisor", "capabilities"],
            ["supervisor", "sessions"],
            ["supervisor", "runs", self.session],
            ["supervisor", "snapshot", self.session, "--run", "run-1"],
            [
                "supervisor",
                "usage",
                self.session,
                "--run",
                "run-1",
                "--limit",
                "25",
            ],
            [
                "supervisor",
                "events",
                self.session,
                "--role",
                "reviewer",
                "--cursor",
                "reviewer=2",
            ],
            [
                "supervisor",
                "command",
                self.session,
                "--role",
                "reviewer",
                "--command-id",
                "7" * 32,
            ],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                parsed = parser.parse_args(["--json", *arguments])
                self.assertEqual(parsed.command, "supervisor")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                ["supervisor", "events", self.session, "--cursor", "reviewer=-1"]
            )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["supervisor", "usage", self.session, "--limit", "0"])


if __name__ == "__main__":
    unittest.main()
