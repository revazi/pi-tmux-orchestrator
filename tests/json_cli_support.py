from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATOR

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "pi-tmux-agents"


class JsonCliFixture(unittest.TestCase):
    def tearDown(self) -> None:
        ORCHESTRATOR.JSON_MODE = False

    def run_main(self, argv: list[str]) -> tuple[int, dict[str, object], str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT), *argv]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = ORCHESTRATOR.main()
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1, stdout.getvalue())
        return code, json.loads(lines[0]), stdout.getvalue(), stderr.getvalue()

    def assert_envelope(
        self, envelope: dict[str, object], command: str, success: bool
    ) -> None:
        self.assertEqual(
            set(envelope),
            {"schema_version", "command", "success", "data", "error"},
        )
        self.assertEqual(envelope["schema_version"], "1")
        self.assertEqual(envelope["command"], command)
        self.assertIs(envelope["success"], success)
        if success:
            self.assertIsNone(envelope["error"])
        else:
            self.assertIsInstance(envelope["error"], dict)
            self.assertLessEqual(
                len(envelope["error"]["message"]), ORCHESTRATOR.MAX_ERROR_CHARS
            )
