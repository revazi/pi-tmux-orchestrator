from __future__ import annotations

import argparse
import importlib.util
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
        self.assertIn("restart", help_text)

    def test_version(self) -> None:
        self.assertEqual(ORCHESTRATOR.VERSION, "0.3.0")

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

    def test_relay_routes_every_marker_without_provider_process(self) -> None:
        roles = {
            role: {"pane_id": f"%{index}"}
            for index, role in enumerate(
                ("implementer", "reviewer", "probe", "playwright", "django"),
                start=1,
            )
        }
        manifest = {"roles": roles}
        with tempfile.TemporaryDirectory() as directory:
            coord = Path(directory)
            seen = coord / ".relay-seen"
            for marker in (
                "handoff-1.ready",
                "probe.ready",
                "playwright-1.ready",
                "django-review-1.ready",
                "review-1.ready",
            ):
                (coord / marker).touch()
            with mock.patch.object(ORCHESTRATOR, "relay_send") as relay_send:
                ORCHESTRATOR.relay_once(coord, manifest, seen)

            destinations = [call.args[1] for call in relay_send.call_args_list]
            self.assertEqual(destinations.count("implementer"), 4)
            self.assertEqual(destinations.count("reviewer"), 4)
            self.assertEqual(destinations.count("playwright"), 1)
            self.assertEqual(destinations.count("django"), 1)
            messages = "\n".join(call.args[2] for call in relay_send.call_args_list)
            for report in (
                "handoff-1.md",
                "probe.md",
                "playwright-1.md",
                "django-review-1.md",
                "review-1.md",
            ):
                self.assertIn(report, messages)
            for marker in (
                "handoff-1.ready",
                "probe.ready",
                "playwright-1.ready",
                "django-review-1.ready",
                "review-1.ready",
            ):
                self.assertTrue((seen / marker).exists())


if __name__ == "__main__":
    unittest.main()