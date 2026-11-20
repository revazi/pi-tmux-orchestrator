from pathlib import Path
import json
import signal
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import call, patch

from tests import custom_worker_tmux_smoke as smoke


class RecordedWorkerCleanupTests(unittest.IsolatedAsyncioTestCase):
    pid = 12345
    fake_pi = Path("/tmp/synthetic smoke/pi")
    argv = ["--mode", "rpc", "--name", "synthetic worker"]
    command = " ".join(["node", str(fake_pi), *argv])

    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.record = Path(directory) / "launch.jsonl"
        self.record.write_text(json.dumps({"pid": self.pid, "argv": self.argv}) + "\n")
        self.ps = self.enterContext(patch.object(smoke.subprocess, "run"))
        self.kill = self.enterContext(patch.object(smoke.os, "kill"))

    def live(self, *, prefix=""):
        return CompletedProcess([], 0, f"S+    {prefix}{self.command}\n", "")

    def at_cleanup_deadline(self):
        clock = self.enterContext(patch.object(smoke, "time"))
        clock.monotonic.side_effect = [0, 3]

    async def cleanup_workers(self):
        await smoke.stop_recorded_workers(self.record, self.fake_pi)

    async def test_exact_worker_exits_between_inspection_and_term(self):
        self.ps.return_value = self.live()
        self.kill.side_effect = ProcessLookupError

        await self.cleanup_workers()

        self.kill.assert_called_once_with(self.pid, signal.SIGTERM)

    async def test_defunct_worker_is_not_signalled(self):
        self.ps.return_value = CompletedProcess([], 0, "Z    node <defunct>\n", "")

        await self.cleanup_workers()

        self.kill.assert_not_called()

    async def test_terminated_worker_need_not_be_reaped(self):
        self.ps.side_effect = [
            self.live(prefix="/usr/bin/env "),
            CompletedProcess([], 0, "Z+   <defunct>\n", ""),
        ]

        await self.cleanup_workers()

        self.kill.assert_called_once_with(self.pid, signal.SIGTERM)

    async def test_absent_pid_requires_corroboration(self):
        self.ps.return_value = CompletedProcess([], 1, "", "")
        with self.assertRaisesRegex(AssertionError, "process state"):
            await self.cleanup_workers()
        self.kill.assert_called_once_with(self.pid, 0)

        self.kill.reset_mock(side_effect=True)
        self.kill.side_effect = ProcessLookupError
        await self.cleanup_workers()
        self.kill.assert_called_once_with(self.pid, 0)

    async def test_live_mismatched_pid_is_not_signalled(self):
        for command in (
            "sleep 60",
            f"cat {self.fake_pi}",
            f"node {self.fake_pi}-collision {' '.join(self.argv)}",
            f"{self.command} --different-launch",
        ):
            with self.subTest(command=command):
                self.ps.return_value = CompletedProcess([], 0, f"S {command}\n", "")
                with self.assertRaisesRegex(AssertionError, "exact test command"):
                    await self.cleanup_workers()
                self.kill.assert_not_called()

    async def test_unreliable_inspection_is_not_exit_or_signal_authority(self):
        for result in (
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 1, "", "synthetic ps failure"),
            CompletedProcess([], 2, f"S {self.command}\n", ""),
            CompletedProcess([], 0, f"unknown {self.command}\n", ""),
            CompletedProcess([], 0, f"Zbogus {self.command}\n", ""),
            CompletedProcess([], 0, f"S {self.command}\nZ <defunct>\n", ""),
        ):
            with self.subTest(returncode=result.returncode, stdout=result.stdout):
                self.ps.return_value = result
                with self.assertRaisesRegex(AssertionError, "process state"):
                    await self.cleanup_workers()
                self.kill.assert_not_called()

    async def test_non_lookup_signal_error_is_not_suppressed(self):
        self.ps.return_value = self.live()
        self.kill.side_effect = PermissionError

        with self.assertRaises(PermissionError):
            await self.cleanup_workers()

        self.kill.assert_called_once_with(self.pid, signal.SIGTERM)

    async def test_pid_reused_before_forced_cleanup_is_not_signalled(self):
        self.at_cleanup_deadline()
        self.ps.side_effect = [
            self.live(),
            CompletedProcess([], 0, f"S cat {self.fake_pi}\n", ""),
        ]

        with self.assertRaisesRegex(AssertionError, "exact test command"):
            await self.cleanup_workers()

        self.kill.assert_called_once_with(self.pid, signal.SIGTERM)

    async def test_exit_between_reinspection_and_kill_is_not_forced_cleanup(self):
        self.at_cleanup_deadline()
        self.ps.side_effect = [self.live(), self.live()]
        self.kill.side_effect = [None, ProcessLookupError()]

        await self.cleanup_workers()

        self.assertEqual(
            self.kill.call_args_list,
            [call(self.pid, signal.SIGTERM), call(self.pid, signal.SIGKILL)],
        )

    async def test_forcing_a_still_live_exact_worker_fails_the_smoke(self):
        self.at_cleanup_deadline()
        self.ps.side_effect = [self.live(), self.live()]

        with self.assertRaisesRegex(AssertionError, "required forced cleanup"):
            await self.cleanup_workers()

        self.assertEqual(
            self.kill.call_args_list,
            [call(self.pid, signal.SIGTERM), call(self.pid, signal.SIGKILL)],
        )
