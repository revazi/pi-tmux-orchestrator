from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-tmux-agents.py"
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("pi_tmux_orchestrator", SCRIPT)
assert SPEC and SPEC.loader
ORCHESTRATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ORCHESTRATOR)


class SkillMetadataTests(unittest.TestCase):
    def test_skill_frontmatter_is_valid(self) -> None:
        content = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(content.startswith("---\n"))
        frontmatter = content.split("---\n", 2)[1]
        values = {
            line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in frontmatter.splitlines()
            if ":" in line
        }
        name = values["name"]
        description = values["description"]
        self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertLessEqual(len(name), 64)
        self.assertGreater(len(description), 0)
        self.assertLessEqual(len(description), 1024)
        self.assertIn("tmux_orchestrator", content)
        self.assertIn("standalone `pi-tmux-agents` CLI fallback", content)


class PromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path("/tmp/example-project")
        self.coord = Path("/tmp/example-coordination")
        self.task = "First criterion.\nSecond criterion.\n"

    def assert_normalized(self, prompt: str) -> None:
        self.assertTrue(prompt.startswith("# Role:"))
        self.assertFalse(prompt.startswith(" "))
        self.assertIn("First criterion.\nSecond criterion.", prompt)
        self.assertIn(str(self.coord), prompt)
        self.assertTrue(prompt.endswith("\n"))

    def test_implementer_prompt(self) -> None:
        prompt = ORCHESTRATOR.implementer_prompt(self.project, self.coord, self.task)
        self.assert_normalized(prompt)
        self.assertIn("sole agent permitted", prompt)
        self.assertIn("handoff-N.ready", prompt)

    def test_reviewer_prompt(self) -> None:
        prompt = ORCHESTRATOR.reviewer_prompt(self.project, self.coord, self.task)
        self.assert_normalized(prompt)
        self.assertIn("read-only reviewer", prompt)
        self.assertIn("CHANGES_REQUESTED", prompt)

    def test_probe_prompt(self) -> None:
        prompt = ORCHESTRATOR.probe_prompt(
            self.project,
            self.coord,
            self.task,
            "Inspect the synthetic API boundary.",
        )
        self.assert_normalized(prompt)
        self.assertIn("production wire acceptance", prompt)
        self.assertIn("probe.ready", prompt)

    def test_playwright_prompt_contract(self) -> None:
        prompt = ORCHESTRATOR.playwright_prompt(
            self.project,
            self.coord,
            self.task,
            "Exercise the synthetic local test application.",
        )
        self.assert_normalized(prompt)
        self.assertIn("read-only Playwright test agent", prompt)
        self.assertIn("real test application through a browser", prompt)
        self.assertIn("relevant failure path", prompt)
        self.assertIn("playwright-N.md", prompt)
        self.assertIn("exactly `PASS` or `FAIL`", prompt)
        self.assertIn("process cleanup", prompt)

    def test_django_expert_prompt_contract(self) -> None:
        prompt = ORCHESTRATOR.django_expert_prompt(
            self.project,
            self.coord,
            self.task,
            "Review synthetic Django behavior.",
        )
        self.assert_normalized(prompt)
        self.assertIn("read-only senior Django expert", prompt)
        self.assertIn("settings/app lifecycle", prompt)
        self.assertIn("transaction/test-database behavior", prompt)
        self.assertIn("django-review-N.md", prompt)
        self.assertIn("`ADVISORY_APPROVED` or `ISSUES_FOUND`", prompt)


class UtilityTests(unittest.TestCase):
    def test_slugify_is_tmux_safe_and_bounded(self) -> None:
        self.assertEqual(ORCHESTRATOR.slugify("My Project!"), "my-project")
        self.assertLessEqual(len(ORCHESTRATOR.slugify("x" * 200)), 48)

    def test_session_name_validation(self) -> None:
        self.assertEqual(
            ORCHESTRATOR.validate_session_name("pi-project_agents.1"),
            "pi-project_agents.1",
        )
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.validate_session_name("bad session")

    def test_session_existence_uses_exact_tmux_target(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(ORCHESTRATOR, "tmux", return_value=completed) as tmux:
            self.assertTrue(ORCHESTRATOR.session_exists("pi-project-agents"))
        tmux.assert_called_once_with(
            ["has-session", "-t", "=pi-project-agents"],
            check=False,
            capture=True,
        )

    def test_exact_target_helpers_and_session_option(self) -> None:
        self.assertEqual(
            ORCHESTRATOR.exact_session_target("pi-project-agents"),
            "=pi-project-agents",
        )
        self.assertEqual(
            ORCHESTRATOR.exact_window_target("pi-project-agents"),
            "=pi-project-agents:=agents",
        )
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.exact_window_target("pi-project-agents", "other")

        completed = subprocess.CompletedProcess([], 0, "/private/run\n", "")
        with mock.patch.object(ORCHESTRATOR, "tmux", return_value=completed) as tmux:
            self.assertEqual(
                ORCHESTRATOR.session_option("pi-project-agents", "@pi_agents_coord"),
                "/private/run",
            )
        tmux.assert_called_once_with(
            [
                "show-options",
                "-qv",
                "-t",
                "=pi-project-agents:=agents",
                "@pi_agents_coord",
            ],
            check=False,
            capture=True,
        )

    def test_attach_and_stop_use_exact_session_targets(self) -> None:
        with (
            mock.patch.dict(os.environ, {"TMUX": "active"}),
            mock.patch.object(ORCHESTRATOR, "tmux") as tmux,
        ):
            ORCHESTRATOR.attach_session("pi-project-agents")
        tmux.assert_called_once_with(
            ["switch-client", "-t", "=pi-project-agents"]
        )

        with (
            mock.patch.object(
                ORCHESTRATOR,
                "resolve_session",
                return_value=("pi-project-agents", Path("/private/run")),
            ),
            mock.patch.object(ORCHESTRATOR, "load_manifest"),
            mock.patch.object(ORCHESTRATOR, "tmux") as tmux,
        ):
            ORCHESTRATOR.stop_command(
                argparse.Namespace(session="pi-project-agents", yes=True)
            )
        tmux.assert_called_once_with(
            ["kill-session", "-t", "=pi-project-agents"]
        )

    def test_external_attach_exec_uses_exact_session_target(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/tmux"),
            mock.patch.object(ORCHESTRATOR.os, "execvp") as execvp,
        ):
            ORCHESTRATOR.attach_session("pi-project-agents")
        execvp.assert_called_once_with(
            "/usr/bin/tmux",
            ["tmux", "attach", "-t", "=pi-project-agents"],
        )

    def test_secure_write_uses_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "message.md"
            ORCHESTRATOR.secure_write(path, "bounded status\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "bounded status\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_task_file_and_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "task.md"
            task.write_text("Focused task\n", encoding="utf-8")
            self.assertEqual(
                ORCHESTRATOR.read_text_argument(None, str(task), "task"),
                "Focused task\n",
            )
        oversized = "x" * (ORCHESTRATOR.MAX_TASK_BYTES + 1)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.read_text_argument(oversized, None, "task")

    def test_public_parser_hides_internal_commands(self) -> None:
        help_text = ORCHESTRATOR.build_parser().format_help()
        self.assertNotIn("_run-agent", help_text)
        self.assertNotIn("_relay", help_text)
        self.assertIn("controller", help_text)
        self.assertIn("restart", help_text)

    def test_controller_parser_has_the_exact_confirmed_lifecycle_surface(self) -> None:
        parser = ORCHESTRATOR.build_parser()
        for action in ("start", "status", "attach"):
            parsed = parser.parse_args(["controller", action])
            self.assertEqual(parsed.command, "controller")
            self.assertEqual(parsed.controller_action, action)
        stopped = parser.parse_args(["controller", "stop", "--confirm"])
        self.assertTrue(stopped.confirm)

    def test_version(self) -> None:
        self.assertEqual(ORCHESTRATOR.VERSION, "0.4.0")

    def test_default_model_contract_for_all_roles(self) -> None:
        self.assertEqual(
            ORCHESTRATOR.DEFAULT_MODELS,
            {
                "implementer": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "thinking": "xhigh",
                },
                "reviewer": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4",
                    "thinking": "high",
                },
                "probe": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4-mini",
                    "thinking": "high",
                },
                "playwright": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4",
                    "thinking": "high",
                },
                "django": {
                    "provider": "openai-codex",
                    "model": "gpt-5.4",
                    "thinking": "high",
                },
            },
        )

    def test_parser_exposes_specialist_tasks_models_and_role_commands(self) -> None:
        parser = ORCHESTRATOR.build_parser()
        start = parser.parse_args(
            [
                "start",
                "--task",
                "Synthetic task",
                "--with-playwright",
                "--playwright-task",
                "Browser check",
                "--playwright-provider",
                "synthetic-browser-provider",
                "--playwright-model",
                "synthetic-browser-model",
                "--playwright-thinking",
                "low",
                "--with-django-expert",
                "--django-task",
                "Django check",
                "--django-provider",
                "synthetic-django-provider",
                "--django-model",
                "synthetic-django-model",
                "--django-thinking",
                "max",
            ]
        )
        self.assertTrue(start.with_playwright)
        self.assertEqual(start.playwright_task, "Browser check")
        self.assertEqual(start.playwright_provider, "synthetic-browser-provider")
        self.assertEqual(start.playwright_model, "synthetic-browser-model")
        self.assertEqual(start.playwright_thinking, "low")
        self.assertTrue(start.with_django_expert)
        self.assertEqual(start.django_task, "Django check")
        self.assertEqual(start.django_provider, "synthetic-django-provider")
        self.assertEqual(start.django_model, "synthetic-django-model")
        self.assertEqual(start.django_thinking, "max")
        for command in ("send", "restart"):
            arguments = [command, "session", "--role", "playwright"]
            if command == "restart":
                arguments.append("--yes")
            self.assertEqual(parser.parse_args(arguments).role, "playwright")
            arguments[arguments.index("playwright")] = "django"
            self.assertEqual(parser.parse_args(arguments).role, "django")

    def test_role_config_keeps_specialists_read_only(self) -> None:
        arguments = argparse.Namespace(
            **{
                f"{role}_{field}": None
                for role in ORCHESTRATOR.DEFAULT_MODELS
                for field in ("provider", "model", "thinking")
            }
        )
        for role, defaults in ORCHESTRATOR.DEFAULT_MODELS.items():
            config = ORCHESTRATOR.role_config(arguments, role)
            self.assertEqual(
                {key: config[key] for key in ("provider", "model", "thinking")},
                defaults,
            )
            expected_tools = None if role == "implementer" else ORCHESTRATOR.READ_ONLY_TOOLS
            self.assertEqual(config["tools"], expected_tools)

    def test_relay_send_reports_transport_result(self) -> None:
        manifest = {"roles": {"reviewer": {"pane_id": "%2"}}}
        with mock.patch.object(ORCHESTRATOR, "send_keys") as send_keys:
            self.assertTrue(ORCHESTRATOR.relay_send(manifest, "reviewer", "notice"))
        send_keys.assert_called_once_with("%2", "notice")
        with mock.patch.object(
            ORCHESTRATOR,
            "send_keys",
            side_effect=subprocess.CalledProcessError(1, ["tmux"]),
        ):
            self.assertFalse(ORCHESTRATOR.relay_send(manifest, "reviewer", "notice"))
        self.assertFalse(ORCHESTRATOR.relay_send(manifest, "probe", "notice"))


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.original_state_root = ORCHESTRATOR.STATE_ROOT
        ORCHESTRATOR.STATE_ROOT = Path(self.temporary.name) / "orchestrations"
        self.addCleanup(self.restore_state_root)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PI_TMUX_CONTROLLER_HOME": str(Path(self.temporary.name) / "controller"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def restore_state_root(self) -> None:
        ORCHESTRATOR.STATE_ROOT = self.original_state_root

    def valid_state(self) -> tuple[Path, dict[str, object]]:
        root = ORCHESTRATOR.controller_state_root(create=True)
        workspace = ORCHESTRATOR.ensure_private_directory(root / "workspace")
        sessions = ORCHESTRATOR.ensure_private_directory(root / "sessions")
        state: dict[str, object] = {
            "version": ORCHESTRATOR.CONTROLLER_STATE_VERSION,
            "created_at": "2026-08-07T12:00:00+00:00",
            "last_started_at": "2026-08-07T12:01:00+00:00",
            "session": ORCHESTRATOR.CONTROLLER_TMUX_SESSION,
            "window": ORCHESTRATOR.CONTROLLER_WINDOW,
            "pi_session_id": ORCHESTRATOR.CONTROLLER_PI_SESSION_ID,
            "root": str(root),
            "workspace": str(workspace),
            "session_dir": str(sessions),
        }
        ORCHESTRATOR.save_controller_state(root, state)
        return root, state

    def test_controller_state_is_private_strict_and_symlink_safe(self) -> None:
        root, state = self.valid_state()
        state_path = ORCHESTRATOR.controller_state_path(root)
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(ORCHESTRATOR.load_controller_state(root), state)

        tampered = dict(state)
        tampered["unknown"] = True
        ORCHESTRATOR.secure_write(state_path, json.dumps(tampered))
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_controller_state(root)

        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text(json.dumps(state), encoding="utf-8")
        state_path.unlink()
        state_path.symlink_to(outside)
        with self.assertRaises(ORCHESTRATOR.OrchestrationError):
            ORCHESTRATOR.load_controller_state(root)

    def test_controller_start_launches_one_persistent_project_neutral_pi_session(self) -> None:
        root_path = Path(self.temporary.name).resolve() / "controller"
        orchestration_root = Path(self.temporary.name).resolve() / "orchestrations"
        session_checks = iter((False, True))

        def fake_tmux(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            stdout = ""
            if arguments[0] == "list-panes" and arguments[-1] == "#{pane_id}":
                stdout = "%71\n"
            elif arguments[0] == "list-panes":
                stdout = "%71\t4321\tnode\t0\tPI ORCHESTRATOR CONTROLLER\n"
            elif arguments[0] == "show-options":
                values = {
                    ORCHESTRATOR.CONTROLLER_OPTION_VERSION: str(
                        ORCHESTRATOR.CONTROLLER_STATE_VERSION
                    ),
                    ORCHESTRATOR.CONTROLLER_OPTION_ROOT: str(root_path),
                    ORCHESTRATOR.CONTROLLER_OPTION_SESSION_ID: (
                        ORCHESTRATOR.CONTROLLER_PI_SESSION_ID
                    ),
                }
                stdout = values.get(arguments[-1], "") + "\n"
            return subprocess.CompletedProcess(arguments, 0, stdout, "")

        with (
            mock.patch.object(
                ORCHESTRATOR,
                "command_path",
                side_effect=lambda name: f"/usr/bin/{name}",
            ),
            mock.patch.object(
                ORCHESTRATOR,
                "session_exists",
                side_effect=lambda _session: next(session_checks),
            ),
            mock.patch.object(ORCHESTRATOR, "tmux", side_effect=fake_tmux) as tmux,
        ):
            result = ORCHESTRATOR.controller_start_command(argparse.Namespace())

        self.assertTrue(result.data["running"])
        self.assertEqual(result.data["pane"]["id"], "%71")
        self.assertEqual(result.data["paths"]["root"], str(root_path))
        state = ORCHESTRATOR.load_controller_state(root_path)
        self.assertEqual(state["pi_session_id"], ORCHESTRATOR.CONTROLLER_PI_SESSION_ID)
        prompt = (root_path / "controller.prompt.md").read_text(encoding="utf-8")
        self.assertIn("project-neutral control session", prompt)
        self.assertIn(str(orchestration_root), prompt)

        calls = [call.args[0] for call in tmux.call_args_list]
        new_session = next(arguments for arguments in calls if arguments[0] == "new-session")
        self.assertEqual(new_session[new_session.index("-c") + 1], str(root_path / "workspace"))
        respawn = next(arguments for arguments in calls if arguments[0] == "respawn-pane")
        shell_command = respawn[-1]
        self.assertIn("umask 077; exec /usr/bin/pi", shell_command)
        self.assertIn(f"--session-id {ORCHESTRATOR.CONTROLLER_PI_SESSION_ID}", shell_command)
        self.assertIn(f"--session-dir {root_path / 'sessions'}", shell_command)
        self.assertIn("--no-context-files", shell_command)
        self.assertIn("--no-approve", shell_command)
        self.assertNotIn(" --approve ", shell_command)
        environment_names = {
            arguments[-2]
            for arguments in calls
            if arguments[0] == "set-environment"
        }
        self.assertTrue(
            {"PI_TMUX_CONTROLLER", "PI_TMUX_CONTROLLER_HOME", "PI_TMUX_AGENTS_HOME"}
            .issubset(environment_names)
        )

    def test_controller_identity_is_not_inherited_by_worker_pi_processes(self) -> None:
        role = {
            "provider": "synthetic-provider",
            "model": "synthetic-model",
            "thinking": "low",
            "tools": None,
            "prompt_path": str(Path(self.temporary.name) / "implementer.prompt.md"),
            "session_dir": str(Path(self.temporary.name) / "sessions"),
        }
        manifest = {
            "project": self.temporary.name,
            "approve_project": False,
            "roles": {"implementer": role},
        }
        args = argparse.Namespace(
            state_root=str(ORCHESTRATOR.STATE_ROOT),
            coord=str(Path(self.temporary.name) / "coord"),
            role="implementer",
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PI_TMUX_CONTROLLER": "1",
                    "PI_TMUX_CONTROLLER_HOME": "/private/controller",
                },
            ),
            mock.patch.object(ORCHESTRATOR, "load_manifest", return_value=manifest),
            mock.patch.object(ORCHESTRATOR, "require_regular_file"),
            mock.patch.object(ORCHESTRATOR, "ensure_private_directory"),
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/pi"),
            mock.patch.object(ORCHESTRATOR.os, "chdir"),
            mock.patch.object(ORCHESTRATOR.os, "execvpe") as execvpe,
        ):
            self.assertEqual(ORCHESTRATOR.run_agent_command(args), 0)
        environment = execvpe.call_args.args[2]
        self.assertNotIn("PI_TMUX_CONTROLLER", environment)
        self.assertNotIn("PI_TMUX_CONTROLLER_HOME", environment)
        self.assertEqual(environment["PI_TELEMETRY"], "0")

    def test_controller_launch_failure_kills_only_its_exact_partial_session(self) -> None:
        root, _ = self.valid_state()
        orchestration_root = ORCHESTRATOR.canonical_state_root(create=True)
        prompt = root / "controller.prompt.md"
        ORCHESTRATOR.secure_write(prompt, "Synthetic controller prompt.\n")
        calls: list[list[str]] = []

        def fake_tmux(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            if arguments[0] == "list-panes":
                return subprocess.CompletedProcess(arguments, 0, "%9\n", "")
            if arguments[0] == "respawn-pane":
                raise ORCHESTRATOR.OrchestrationError("synthetic launch failure")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with (
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/pi"),
            mock.patch.object(ORCHESTRATOR, "tmux", side_effect=fake_tmux),
        ):
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.create_controller_tmux(root, orchestration_root, prompt)
        self.assertIn(
            [
                "kill-session",
                "-t",
                f"={ORCHESTRATOR.CONTROLLER_TMUX_SESSION}",
            ],
            calls,
        )

    def test_controller_refuses_duplicates_and_unmanaged_name_collisions(self) -> None:
        with (
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/true"),
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=True),
            mock.patch.object(ORCHESTRATOR, "controller_is_marked", return_value=True),
        ):
            with self.assertRaises(ORCHESTRATOR.OrchestrationError) as raised:
                ORCHESTRATOR.controller_start_command(argparse.Namespace())
        self.assertEqual(raised.exception.code, "already_running")

        with (
            mock.patch.object(ORCHESTRATOR, "command_path", return_value="/usr/bin/true"),
            mock.patch.object(ORCHESTRATOR, "session_exists", return_value=True),
            mock.patch.object(ORCHESTRATOR, "controller_is_marked", return_value=False),
        ):
            with self.assertRaises(ORCHESTRATOR.OrchestrationError) as raised:
                ORCHESTRATOR.controller_start_command(argparse.Namespace())
        self.assertEqual(raised.exception.code, "session_collision")

    def test_controller_status_is_noncreating_and_stop_is_exact_and_confirmed(self) -> None:
        with mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False):
            result = ORCHESTRATOR.controller_status_command(argparse.Namespace())
        self.assertFalse(result.data["running"])
        self.assertFalse(result.data["state_retained"])
        self.assertFalse(Path(result.data["paths"]["root"]).exists())

        _, state = self.valid_state()
        with mock.patch.object(ORCHESTRATOR, "session_exists", return_value=False):
            retained = ORCHESTRATOR.controller_status_command(argparse.Namespace())
        self.assertFalse(retained.data["running"])
        self.assertTrue(retained.data["state_retained"])
        self.assertEqual(retained.data["created_at"], state["created_at"])

        details = ORCHESTRATOR.controller_public_data(
            state,
            {
                "id": "%8",
                "pid": 88,
                "command": "node",
                "dead": False,
                "title": "controller",
            },
        )
        with mock.patch.object(ORCHESTRATOR, "tmux") as tmux:
            with self.assertRaises(ORCHESTRATOR.OrchestrationError):
                ORCHESTRATOR.controller_stop_command(argparse.Namespace(confirm=False))
            tmux.assert_not_called()

        with (
            mock.patch.object(ORCHESTRATOR, "controller_details", return_value=details),
            mock.patch.object(ORCHESTRATOR, "tmux") as tmux,
        ):
            result = ORCHESTRATOR.controller_stop_command(argparse.Namespace(confirm=True))
        tmux.assert_called_once_with(
            ["kill-session", "-t", f"={ORCHESTRATOR.CONTROLLER_TMUX_SESSION}"]
        )
        self.assertTrue(result.data["stopped"])
        self.assertFalse(result.data["running"])


if __name__ == "__main__":
    unittest.main()
