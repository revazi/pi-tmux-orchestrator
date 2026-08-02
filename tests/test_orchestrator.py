from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(ORCHESTRATOR.VERSION, "0.1.0")

    def test_default_model_contract(self) -> None:
        self.assertEqual(
            ORCHESTRATOR.DEFAULT_MODELS["implementer"],
            {
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "thinking": "xhigh",
            },
        )


if __name__ == "__main__":
    unittest.main()