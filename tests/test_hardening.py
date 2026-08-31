from __future__ import annotations

import argparse
import copy
import io
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR

ROOT = Path(__file__).resolve().parents[1]


class PrivateStateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.original_state_root = ORCHESTRATOR.STATE_ROOT
        self.addCleanup(self.restore_state_root)
        ORCHESTRATOR.STATE_ROOT = Path(self.temporary.name) / "state"
        self.root = ORCHESTRATOR.canonical_state_root(create=True)
        self.session = "pi-fixture-agents"
        self.session_root = ORCHESTRATOR.ensure_private_directory(
            self.root / self.session
        )
        self.coord = ORCHESTRATOR.ensure_private_directory(self.session_root / "run-1")
        self.project = Path(self.temporary.name).resolve() / "project"
        self.project.mkdir()

    def restore_state_root(self) -> None:
        ORCHESTRATOR.STATE_ROOT = self.original_state_root

    def manifest(
        self, roles: tuple[str, ...] = ("implementer", "reviewer")
    ) -> dict[str, object]:
        role_values: dict[str, object] = {}
        for index, role in enumerate(roles, start=1):
            prompt = self.coord / f"{role}.prompt.md"
            ORCHESTRATOR.secure_write(prompt, f"Synthetic {role} prompt.\n")
            defaults = ORCHESTRATOR.DEFAULT_MODELS[role]
            role_values[role] = {
                "provider": defaults["provider"],
                "model": defaults["model"],
                "thinking": defaults["thinking"],
                "tools": None
                if role == "implementer"
                else ORCHESTRATOR.READ_ONLY_TOOLS,
                "pane_id": f"%{index}",
                "prompt_path": str(prompt),
                "session_dir": str(self.coord / "sessions" / role),
            }
        return {
            "version": 1,
            "created_at": "2026-08-06T10:00:00+00:00",
            "session": self.session,
            "window": ORCHESTRATOR.WINDOW,
            "project": str(self.project),
            "coord": str(self.coord),
            "approve_project": False,
            "monitor_pane_id": "%99",
            "roles": role_values,
        }

    def write_raw_manifest(self, manifest: object) -> None:
        ORCHESTRATOR.secure_write(
            self.coord / "manifest.json",
            json.dumps(manifest) + "\n",
        )


class PrivateStateSafetyTests(PrivateStateFixture):
    def test_recursive_creation_does_not_chmod_preexisting_parent(self) -> None:
        public_parent = Path(self.temporary.name).resolve() / "preexisting-parent"
        public_parent.mkdir(mode=0o755)
        public_parent.chmod(0o755)

        requested = public_parent / "new-root" / "nested"
        created = ORCHESTRATOR.ensure_private_directory(requested, parents=True)

        self.assertEqual(public_parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual((public_parent / "new-root").stat().st_mode & 0o777, 0o700)
        self.assertEqual(created.stat().st_mode & 0o777, 0o700)

    def test_secure_write_rejects_symlink_file_and_parent(self) -> None:
        outside = Path(self.temporary.name).resolve() / "outside.txt"
        outside.write_text("unchanged\n", encoding="utf-8")
        destination = self.coord / "linked.md"
        destination.symlink_to(outside)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.secure_write(destination, "redirected\n")
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")

        real_parent = Path(self.temporary.name).resolve() / "real-parent"
        real_parent.mkdir()
        linked_parent = self.coord / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.secure_write(linked_parent / "state.md", "redirected\n")
        self.assertFalse((real_parent / "state.md").exists())

        non_regular = self.coord / "directory-as-state"
        non_regular.mkdir()
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.secure_write(non_regular, "not a regular file\n")

    def test_root_session_run_symlinks_and_outside_coord_are_rejected(self) -> None:
        outside = Path(self.temporary.name).resolve() / "outside"
        outside.mkdir()

        linked_root = Path(self.temporary.name).resolve() / "linked-root"
        linked_root.symlink_to(outside, target_is_directory=True)
        ORCHESTRATOR.STATE_ROOT = linked_root
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.canonical_state_root(create=False)

        ORCHESTRATOR.STATE_ROOT = self.root
        linked_session = self.root / "linked-session"
        linked_session.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.ensure_private_directory(linked_session)

        linked_run = self.session_root / "linked-run"
        linked_run.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.validate_coordination_directory(linked_run)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.validate_coordination_directory(outside)

    def test_manifest_write_is_atomic_and_cleans_unique_temporary_on_failure(
        self,
    ) -> None:
        manifest = self.manifest()
        ORCHESTRATOR.save_manifest(self.coord, manifest)
        original = (self.coord / "manifest.json").read_bytes()
        with mock.patch.object(
            ORCHESTRATOR.os, "replace", side_effect=OSError("synthetic")
        ):
            with self.assertRaises(OSError):
                ORCHESTRATOR.save_manifest(self.coord, manifest)
        self.assertEqual((self.coord / "manifest.json").read_bytes(), original)
        self.assertEqual(list(self.coord.glob(".manifest.json.*.tmp")), [])


class ManifestValidationTests(PrivateStateFixture):
    def test_valid_v1_manifest_round_trip(self) -> None:
        manifest = self.manifest()
        ORCHESTRATOR.save_manifest(self.coord, manifest)
        self.assertEqual(
            ORCHESTRATOR.load_manifest(self.coord, expected_session=self.session),
            manifest,
        )

    def test_valid_v4_manifest_binds_execution_profile_metadata(self) -> None:
        manifest = self.manifest()
        manifest["version"] = 4
        manifest["transport"] = ORCHESTRATOR.TUI_TRANSPORT
        manifest["coordination"] = ORCHESTRATOR.BROKER_COORDINATION
        manifest["protocol_version"] = ORCHESTRATOR.BROKER_PROTOCOL_VERSION
        manifest["execution_profile"] = {
            "name": "balanced",
            "kind": "packaged",
            "source": "per-run",
        }
        for role, value in manifest["roles"].items():
            del value["prompt_path"]
            value["session_id"] = f"run-1-{role}"
        ORCHESTRATOR.save_manifest(self.coord, manifest)
        loaded = ORCHESTRATOR.load_manifest(self.coord, expected_session=self.session)
        self.assertEqual(loaded["execution_profile"], manifest["execution_profile"])

        for invalid_profile in (
            {"name": "balanced", "kind": "custom", "source": "per-run"},
            {"name": "unknown", "kind": "packaged", "source": "per-run"},
            {"name": "balanced", "kind": "packaged", "source": "project"},
        ):
            with self.subTest(profile=invalid_profile):
                invalid = copy.deepcopy(manifest)
                invalid["execution_profile"] = invalid_profile
                self.write_raw_manifest(invalid)
                with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                    ORCHESTRATOR.load_manifest(
                        self.coord, expected_session=self.session
                    )

    def test_valid_v5_manifest_binds_project_configuration_metadata(self) -> None:
        manifest = self.manifest()
        manifest["version"] = 5
        manifest["transport"] = ORCHESTRATOR.TUI_TRANSPORT
        manifest["coordination"] = ORCHESTRATOR.BROKER_COORDINATION
        manifest["protocol_version"] = ORCHESTRATOR.BROKER_PROTOCOL_VERSION
        manifest["execution_profile"] = {
            "name": "balanced",
            "kind": "packaged",
            "source": "project",
        }
        manifest["project_config"] = {
            "matched": True,
            "directory": manifest["project"],
            "profile": "balanced",
            "implementation_flow": "phased",
            "specialists": ["probe"],
            "workspace_capsule": False,
            "model_defaults": True,
            "role_overrides": ["reviewer"],
        }
        manifest["orchestration_config"] = {
            "path": "/tmp/tmux-orchestrator.json",
            "version": 3,
        }
        for role, value in manifest["roles"].items():
            del value["prompt_path"]
            value["session_id"] = f"run-1-{role}"
        ORCHESTRATOR.save_manifest(self.coord, manifest)
        loaded = ORCHESTRATOR.load_manifest(self.coord, expected_session=self.session)
        self.assertEqual(loaded["project_config"], manifest["project_config"])

        invalid_values = (
            {**manifest["project_config"], "matched": False},
            {**manifest["project_config"], "specialists": ["reviewer"]},
            {**manifest["project_config"], "workspace_capsule": "no"},
            {**manifest["project_config"], "directory": "/other/project"},
            {**manifest["project_config"], "extra": True},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                invalid = copy.deepcopy(manifest)
                invalid["project_config"] = value
                self.write_raw_manifest(invalid)
                with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                    ORCHESTRATOR.load_manifest(
                        self.coord, expected_session=self.session
                    )

        for orchestration_config in (
            {"path": "relative.json", "version": 3},
            {"path": "/tmp/config.json", "version": 2},
            {"path": "/tmp/config.json", "version": 3, "extra": True},
        ):
            with self.subTest(orchestration_config=orchestration_config):
                invalid = copy.deepcopy(manifest)
                invalid["orchestration_config"] = orchestration_config
                self.write_raw_manifest(invalid)
                with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                    ORCHESTRATOR.load_manifest(
                        self.coord, expected_session=self.session
                    )

    def test_malformed_unknown_role_and_path_tampering_are_rejected(self) -> None:
        base = self.manifest()
        cases: list[tuple[str, object]] = []

        missing = copy.deepcopy(base)
        del missing["window"]
        cases.append(("missing field", missing))

        wrong_trust = copy.deepcopy(base)
        wrong_trust["approve_project"] = 1
        cases.append(("non-boolean trust", wrong_trust))

        wrong_coord = copy.deepcopy(base)
        wrong_coord["coord"] = str(self.root)
        cases.append(("coord tampering", wrong_coord))

        wrong_project = copy.deepcopy(base)
        wrong_project["project"] = "relative/project"
        cases.append(("relative project", wrong_project))

        wrong_window = copy.deepcopy(base)
        wrong_window["window"] = "other"
        cases.append(("wrong window", wrong_window))

        wrong_pane = copy.deepcopy(base)
        wrong_pane["roles"]["implementer"]["pane_id"] = "not-a-pane"
        cases.append(("wrong pane", wrong_pane))

        wrong_prompt = copy.deepcopy(base)
        wrong_prompt["roles"]["reviewer"]["prompt_path"] = str(self.coord / "task.md")
        cases.append(("prompt tampering", wrong_prompt))

        wrong_session_dir = copy.deepcopy(base)
        wrong_session_dir["roles"]["reviewer"]["session_dir"] = str(
            self.root / "elsewhere"
        )
        cases.append(("session path tampering", wrong_session_dir))

        unknown_role = copy.deepcopy(base)
        unknown_role["roles"]["intruder"] = copy.deepcopy(
            unknown_role["roles"]["reviewer"]
        )
        cases.append(("unknown role", unknown_role))

        for label, manifest in cases:
            with self.subTest(label=label):
                self.write_raw_manifest(manifest)
                with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                    ORCHESTRATOR.load_manifest(
                        self.coord, expected_session=self.session
                    )

    def test_symlinked_or_non_regular_manifest_and_prompt_are_rejected(self) -> None:
        manifest = self.manifest()
        self.write_raw_manifest(manifest)
        manifest_path = self.coord / "manifest.json"
        manifest_path.unlink()
        outside = Path(self.temporary.name).resolve() / "outside.json"
        outside.write_text(json.dumps(manifest), encoding="utf-8")
        manifest_path.symlink_to(outside)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_manifest(self.coord)

        manifest_path.unlink()
        manifest_path.mkdir()
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_manifest(self.coord)
        manifest_path.rmdir()
        self.write_raw_manifest(manifest)
        prompt = self.coord / "reviewer.prompt.md"
        prompt.unlink()
        prompt.symlink_to(outside)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_manifest(self.coord)


class RpcTransportHardeningTests(PrivateStateFixture):
    def rpc_manifest(self) -> dict[str, object]:
        manifest = self.manifest()
        manifest["version"] = 2
        manifest["transport"] = ORCHESTRATOR.RPC_TRANSPORT
        return manifest

    def test_v2_rpc_manifest_round_trip_and_transport_validation(self) -> None:
        manifest = self.rpc_manifest()
        ORCHESTRATOR.save_manifest(self.coord, manifest)
        loaded = ORCHESTRATOR.load_manifest(self.coord, expected_session=self.session)
        self.assertEqual(ORCHESTRATOR.manifest_transport(loaded), "rpc")

        invalid = copy.deepcopy(manifest)
        invalid["transport"] = "raw-tmux"
        self.write_raw_manifest(invalid)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_manifest(self.coord, expected_session=self.session)

    def test_rpc_state_is_private_strict_and_metadata_only(self) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "implementer", create=True)
        state = {
            "version": 1,
            "role": "implementer",
            "pid": 123,
            "status": "streaming",
            "is_streaming": True,
            "steering_count": 1,
            "follow_up_count": 2,
            "session_id": "synthetic-session",
            "updated_at": "2026-08-09T12:00:00+00:00",
        }
        ORCHESTRATOR.save_rpc_state(paths["state"], state, "implementer")
        self.assertEqual(paths["state"].stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            ORCHESTRATOR.load_rpc_state(self.coord, "implementer"),
            state,
        )
        public = ORCHESTRATOR.public_rpc_state(state)
        self.assertNotIn("message", public)
        self.assertNotIn("prompt", public)

        tampered = dict(state)
        tampered["steering_count"] = -1
        ORCHESTRATOR.secure_write(paths["state"], json.dumps(tampered))
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_rpc_state(self.coord, "implementer")

    def test_rpc_mailbox_acknowledges_without_leaving_private_payload_files(
        self,
    ) -> None:
        manifest = self.rpc_manifest()
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "implementer", create=True)
        canary = "PRIVATE_RPC_MESSAGE_CANARY_f81c"
        observed: list[dict[str, object]] = []

        def acknowledge() -> None:
            deadline = time.time() + 2
            while time.time() < deadline:
                requests = list(paths["inbox"].glob("*.json"))
                if requests:
                    request_path = requests[0]
                    token = request_path.stem
                    request = ORCHESTRATOR.read_rpc_mailbox_request(request_path, token)
                    observed.append(request)
                    ORCHESTRATOR.unlink_private_regular(request_path, "RPC request")
                    ORCHESTRATOR.rpc_acknowledge(
                        paths["acks"] / f"{token}.json",
                        token,
                        "prompt",
                        True,
                    )
                    return
                time.sleep(0.01)
            raise AssertionError("RPC request did not appear")

        worker = threading.Thread(target=acknowledge)
        worker.start()
        ack = ORCHESTRATOR.rpc_control_request(
            self.coord,
            manifest,
            "implementer",
            "prompt",
            message=canary,
            delivery="follow-up",
            timeout=2,
        )
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(ack["success"])
        self.assertEqual(observed[0]["message"], canary)
        self.assertEqual(observed[0]["delivery"], "follow-up")
        self.assertEqual(list(paths["inbox"].iterdir()), [])
        self.assertEqual(list(paths["acks"].iterdir()), [])

    def test_durable_rpc_registry_and_event_journal_are_metadata_only(self) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "implementer", create=True)
        registry = ORCHESTRATOR.initialize_rpc_registry(
            self.coord,
            paths,
            "implementer",
            123,
        )
        worker_id = registry["worker_id"]
        command_id = "a" * 32
        canary = "PRIVATE_EVENT_PAYLOAD_CANARY_7b2e"
        ORCHESTRATOR.record_rpc_event(
            paths,
            registry,
            "implementer",
            "command_received",
            command_id=command_id,
            command="prompt",
            delivery="follow-up",
        )
        for status in ("accepted", "started", "completed"):
            ORCHESTRATOR.transition_rpc_command(
                paths,
                registry,
                "implementer",
                command_id,
                status,
            )
        ORCHESTRATOR.record_rpc_event(
            paths,
            registry,
            "implementer",
            "agent_completed",
        )

        loaded = ORCHESTRATOR.load_rpc_registry(self.coord, "implementer")
        self.assertEqual(loaded["worker_id"], worker_id)
        self.assertEqual(loaded["commands"][0]["status"], "completed")
        self.assertEqual(loaded["active_command_ids"], [])
        public = ORCHESTRATOR.public_rpc_registry(loaded)
        self.assertEqual(public["command_status_counts"]["completed"], 1)
        events = ORCHESTRATOR.load_rpc_events(self.coord, "implementer")
        self.assertEqual([event["sequence"] for event in events], list(range(1, 7)))
        self.assertNotIn(canary, json.dumps(events))
        for path in (paths["registry"], paths["events"], paths["event_lock"]):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rpc_supervisor_recovery_marks_inflight_command_uncertain(self) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "reviewer", create=True)
        registry = ORCHESTRATOR.initialize_rpc_registry(
            self.coord,
            paths,
            "reviewer",
            111,
        )
        worker_id = registry["worker_id"]
        command_id = "b" * 32
        ORCHESTRATOR.record_rpc_event(
            paths,
            registry,
            "reviewer",
            "command_received",
            command_id=command_id,
            command="prompt",
            delivery="steer",
        )
        ORCHESTRATOR.transition_rpc_command(
            paths,
            registry,
            "reviewer",
            command_id,
            "accepted",
        )

        recovered = ORCHESTRATOR.initialize_rpc_registry(
            self.coord,
            paths,
            "reviewer",
            222,
        )
        self.assertEqual(recovered["worker_id"], worker_id)
        self.assertEqual(recovered["generation"], 2)
        self.assertEqual(recovered["pid"], 222)
        self.assertEqual(recovered["commands"][0]["status"], "uncertain")
        self.assertEqual(recovered["active_command_ids"], [])
        self.assertEqual(
            ORCHESTRATOR.load_rpc_events(self.coord, "reviewer")[-1]["event"],
            "command_uncertain",
        )

    def test_rpc_event_journal_rotates_with_a_contiguous_retained_window(self) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "django", create=True)
        with mock.patch.object(ORCHESTRATOR, "MAX_RPC_EVENT_SEGMENT_BYTES", 700):
            registry = ORCHESTRATOR.initialize_rpc_registry(
                self.coord,
                paths,
                "django",
                654,
            )
            for index in range(6):
                command_id = f"{index:032x}"
                ORCHESTRATOR.record_rpc_event(
                    paths,
                    registry,
                    "django",
                    "command_received",
                    command_id=command_id,
                    command="prompt",
                    delivery="steer",
                )
                ORCHESTRATOR.transition_rpc_command(
                    paths,
                    registry,
                    "django",
                    command_id,
                    "rejected",
                )
            events = ORCHESTRATOR.load_rpc_events(self.coord, "django")
        sequences = [event["sequence"] for event in events]
        self.assertTrue(paths["events_archive"].is_file())
        self.assertGreater(sequences[0], 1)
        self.assertEqual(
            sequences,
            list(range(sequences[0], registry["last_event_sequence"] + 1)),
        )

    def test_rpc_event_journal_rejects_payload_fields_and_sequence_tampering(
        self,
    ) -> None:
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "probe", create=True)
        registry = ORCHESTRATOR.initialize_rpc_registry(
            self.coord,
            paths,
            "probe",
            321,
        )
        event = ORCHESTRATOR.load_rpc_events(self.coord, "probe")[0]
        event["private_payload"] = "must not be accepted"
        ORCHESTRATOR.secure_write(paths["events"], json.dumps(event) + "\n")
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_rpc_events(self.coord, "probe")
        self.assertEqual(registry["last_event_sequence"], 1)

    def test_rpc_reader_uses_lf_only_and_preserves_unicode_separators(self) -> None:
        read_fd, write_fd = os.pipe()
        records: queue.Queue[tuple[str, object]] = queue.Queue()
        with os.fdopen(read_fd, "rb", closefd=True) as reader:
            worker = threading.Thread(
                target=ORCHESTRATOR.strict_rpc_reader,
                args=(reader, "stdout", records),
            )
            worker.start()
            os.write(
                write_fd,
                '{"type":"message_update","delta":"left\u2028right"}\n'.encode("utf-8"),
            )
            os.close(write_fd)
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        channel, payload = records.get_nowait()
        self.assertEqual(channel, "stdout")
        self.assertIn("left\u2028right".encode("utf-8"), payload)

    def test_rpc_uncertain_acknowledgement_fails_closed_with_stable_command_id(
        self,
    ) -> None:
        manifest = self.rpc_manifest()
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "reviewer", create=True)
        command_id = "d" * 32

        def acknowledge_uncertain() -> None:
            deadline = time.time() + 2
            request_path = paths["inbox"] / f"{command_id}.json"
            while time.time() < deadline and not request_path.exists():
                time.sleep(0.01)
            ORCHESTRATOR.unlink_private_regular(request_path, "RPC request")
            ORCHESTRATOR.rpc_acknowledge(
                paths["acks"] / f"{command_id}.json",
                command_id,
                "prompt",
                False,
                status="uncertain",
                duplicate=True,
                event_sequence=9,
            )

        worker = threading.Thread(target=acknowledge_uncertain)
        worker.start()
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as raised:
            ORCHESTRATOR.rpc_control_request(
                self.coord,
                manifest,
                "reviewer",
                "prompt",
                message="Synthetic uncertain delivery",
                command_id=command_id,
                timeout=2,
            )
        worker.join(timeout=2)
        self.assertEqual(raised.exception.code, "rpc_uncertain")
        self.assertIn(command_id, str(raised.exception))
        self.assertEqual(list(paths["inbox"].iterdir()), [])
        self.assertEqual(list(paths["acks"].iterdir()), [])

    def test_rpc_timeout_removes_undelivered_message_file(self) -> None:
        manifest = self.rpc_manifest()
        paths = ORCHESTRATOR.rpc_role_paths(self.coord, "reviewer", create=True)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError) as raised:
            ORCHESTRATOR.rpc_control_request(
                self.coord,
                manifest,
                "reviewer",
                "prompt",
                message="Synthetic timeout message",
                timeout=0.05,
            )
        self.assertEqual(raised.exception.code, "rpc_timeout")
        self.assertEqual(list(paths["inbox"].iterdir()), [])
        self.assertEqual(list(paths["acks"].iterdir()), [])


class RelayHardeningTests(PrivateStateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.roles = ("implementer", "reviewer", "probe", "playwright", "django")
        self.relay_manifest = self.manifest(self.roles)
        self.seen = self.coord / ".relay-seen"

    def write_report_and_marker(self, report: str, content: str, marker: str) -> None:
        (self.coord / report).write_text(content, encoding="utf-8")
        (self.coord / marker).touch()

    def test_relay_routes_only_valid_report_marker_pairs(self) -> None:
        pairs = {
            "probe.ready": ("probe.md", "Synthetic probe complete.\n"),
            "handoff-1.ready": ("handoff-1.md", "Synthetic handoff.\n"),
            "playwright-1.ready": (
                "playwright-1.md",
                "PASS\nSynthetic browser report.\n",
            ),
            "django-review-1.ready": (
                "django-review-1.md",
                "ADVISORY_APPROVED\nSynthetic Django report.\n",
            ),
            "review-1.ready": ("review-1.md", "APPROVED\nSynthetic review.\n"),
        }
        for marker, (report, content) in pairs.items():
            self.write_report_and_marker(report, content, marker)
        (self.coord / "implementation-ready.md").write_text(
            "Synthetic implementation readiness.\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            ORCHESTRATOR, "relay_send", return_value=True
        ) as relay_send:
            ORCHESTRATOR.relay_once(self.coord, self.relay_manifest, self.seen)

        destinations = [call.args[1] for call in relay_send.call_args_list]
        self.assertEqual(destinations.count("implementer"), 4)
        self.assertEqual(destinations.count("reviewer"), 5)
        self.assertEqual(destinations.count("playwright"), 1)
        self.assertEqual(destinations.count("django"), 1)
        messages = "\n".join(call.args[2] for call in relay_send.call_args_list)
        for report, _ in pairs.values():
            self.assertIn(report, messages)
        for marker in (*pairs, "implementation-ready.md"):
            self.assertTrue((self.seen / marker).exists())

    def test_report_before_marker_race_stays_pending_until_report_exists(self) -> None:
        marker = self.coord / "handoff-2.ready"
        marker.touch()
        with mock.patch.object(
            ORCHESTRATOR, "relay_send", return_value=True
        ) as relay_send:
            ORCHESTRATOR.relay_once(self.coord, self.relay_manifest, self.seen)
            relay_send.assert_not_called()
            self.assertFalse((self.seen / marker.name).exists())
            (self.coord / "handoff-2.md").write_text("Ready now.\n", encoding="utf-8")
            ORCHESTRATOR.relay_once(self.coord, self.relay_manifest, self.seen)
            self.assertEqual(relay_send.call_count, 3)
        self.assertTrue((self.seen / marker.name).exists())

    def test_invalid_or_empty_first_line_reports_stay_pending(self) -> None:
        invalid_reports = (
            ("playwright-3.md", "MAYBE\n", "playwright-3.ready"),
            ("django-review-3.md", "PASS\n", "django-review-3.ready"),
            ("review-3.md", "\nAPPROVED\n", "review-3.ready"),
        )
        for report, content, marker in invalid_reports:
            self.write_report_and_marker(report, content, marker)
        (self.coord / "handoff-3.md").touch()
        (self.coord / "handoff-3.ready").touch()
        with mock.patch.object(
            ORCHESTRATOR, "relay_send", return_value=True
        ) as relay_send:
            ORCHESTRATOR.relay_once(self.coord, self.relay_manifest, self.seen)
        relay_send.assert_not_called()
        for _, _, marker in invalid_reports:
            self.assertFalse((self.seen / marker).exists())
        self.assertFalse((self.seen / "handoff-3.ready").exists())

    def test_failed_recipient_remains_pending_without_duplicate_success(self) -> None:
        manifest = {
            "roles": {
                role: self.relay_manifest["roles"][role]
                for role in ("implementer", "reviewer")
            }
        }
        self.write_report_and_marker(
            "playwright-4.md",
            "FAIL\nSynthetic failure evidence.\n",
            "playwright-4.ready",
        )
        first_attempts: list[str] = []

        def first_delivery(_manifest: object, role: str, _message: str) -> bool:
            first_attempts.append(role)
            return role == "implementer"

        with mock.patch.object(ORCHESTRATOR, "relay_send", side_effect=first_delivery):
            ORCHESTRATOR.relay_once(self.coord, manifest, self.seen)
        self.assertEqual(first_attempts, ["implementer", "reviewer"])
        self.assertTrue((self.seen / "playwright-4.ready--implementer").exists())
        self.assertFalse((self.seen / "playwright-4.ready").exists())

        with mock.patch.object(
            ORCHESTRATOR, "relay_send", return_value=True
        ) as relay_send:
            ORCHESTRATOR.relay_once(self.coord, manifest, self.seen)
        self.assertEqual(
            [call.args[1] for call in relay_send.call_args_list], ["reviewer"]
        )
        self.assertTrue((self.seen / "playwright-4.ready").exists())


class MetadataOutputTests(PrivateStateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.manifest_value = self.manifest()
        ORCHESTRATOR.save_manifest(self.coord, self.manifest_value)
        self.canary = "SECRET_CANARY_REPORT_BODY_9f67"
        (self.coord / "handoff-9.md").write_text(
            f"{self.canary}\nprivate body\n",
            encoding="utf-8",
        )
        self.tmux_result = subprocess.CompletedProcess(
            [],
            0,
            "0\t%1\t123\tpython3\t0\tIMPLEMENTER\n",
            "",
        )

    def test_status_prints_only_file_metadata(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                ORCHESTRATOR,
                "resolve_session",
                return_value=(self.session, self.coord),
            ),
            mock.patch.object(
                ORCHESTRATOR,
                "tmux",
                return_value=self.tmux_result,
            ) as tmux,
            redirect_stdout(output),
        ):
            ORCHESTRATOR.status_command(argparse.Namespace(session=self.session))
        self.assertEqual(
            tmux.call_args.args[0][2],
            f"={self.session}:={ORCHESTRATOR.WINDOW}",
        )
        rendered = output.getvalue()
        self.assertNotIn(self.canary, rendered)
        self.assertIn("handoff-9.md:", rendered)
        self.assertIn("bytes", rendered)

    def test_monitor_prints_only_file_metadata(self) -> None:
        output = io.StringIO()
        with (
            mock.patch.object(
                ORCHESTRATOR,
                "tmux",
                return_value=self.tmux_result,
            ) as tmux,
            redirect_stdout(output),
        ):
            ORCHESTRATOR.render_monitor(self.coord, self.manifest_value)
        self.assertEqual(
            tmux.call_args.args[0][2],
            f"={self.session}:={ORCHESTRATOR.WINDOW}",
        )
        rendered = output.getvalue()
        self.assertNotIn(self.canary, rendered)
        self.assertIn("handoff-9.md", rendered)
        self.assertIn("bytes", rendered)


class ModelAvailabilityTests(unittest.TestCase):
    def test_catalog_is_queried_once_per_provider_within_an_operation(self) -> None:
        results = {
            "provider-a": "Provider Model\nprovider-a model-1\nprovider-a model-2\n",
            "provider-b": "Provider Model\nprovider-b model-3\n",
        }

        def listed(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess:
            provider = command[-1]
            return subprocess.CompletedProcess(command, 0, results[provider], "")

        catalogs = {}
        with (
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/bin/pi"),
            mock.patch.object(ORCHESTRATOR, "run", side_effect=listed) as run,
        ):
            self.assertEqual(
                ORCHESTRATOR.model_available("provider-a", "model-1", catalogs),
                (True, "available"),
            )
            self.assertEqual(
                ORCHESTRATOR.model_available("provider-a", "missing", catalogs),
                (False, "provider-a/missing is not listed as available"),
            )
            self.assertEqual(
                ORCHESTRATOR.model_available("provider-b", "model-3", catalogs),
                (True, "available"),
            )

        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            ["provider-a", "provider-b"],
        )


class StartupRecoveryTests(PrivateStateFixture):
    def test_failed_start_kills_partial_session_and_marks_run_failed(self) -> None:
        session = "pi-startup-failure"
        values = {
            f"{role}_{field}": None
            for role in ORCHESTRATOR.DEFAULT_MODELS
            for field in ("provider", "model", "thinking")
        }
        args = argparse.Namespace(
            project=str(self.project),
            task="Synthetic startup failure.",
            task_file=None,
            session=session,
            with_probe=False,
            probe_task=None,
            probe_task_file=None,
            with_playwright=False,
            playwright_task=None,
            playwright_task_file=None,
            with_django_expert=False,
            django_task=None,
            django_task_file=None,
            approve_project=False,
            attach=False,
            dry_run=False,
            skip_model_check=True,
            **values,
        )
        with (
            mock.patch.object(
                ORCHESTRATOR, "command_path", return_value="/usr/bin/true"
            ),
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False),
            mock.patch.object(
                ORCHESTRATOR,
                "create_tmux_grid",
                side_effect=ORCHESTRATOR.OrchestrationError(
                    "synthetic startup failure"
                ),
            ),
            mock.patch.object(ORCHESTRATOR, "tmux") as tmux,
        ):
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.start_command(args)
        tmux.assert_called_once_with(
            ["kill-session", "-t", f"={session}"],
            check=False,
            capture=True,
        )
        runs = list((self.root / session).iterdir())
        self.assertEqual(len(runs), 1)
        self.assertEqual(
            (runs[0] / "startup-state").read_text(encoding="utf-8"), "FAILED\n"
        )


if __name__ == "__main__":
    unittest.main()
