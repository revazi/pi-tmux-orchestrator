#!/usr/bin/env python3
"""Model-free tmux grid and relay smoke test."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-tmux-agents.py"
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("pi_tmux_orchestrator_smoke", SCRIPT)
assert SPEC and SPEC.loader
ORCHESTRATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ORCHESTRATOR)


def main() -> int:
    if not shutil.which("tmux"):
        print("tmux is required for the functional smoke", file=sys.stderr)
        return 1

    session = f"pi-orchestrator-smoke-{os.getpid()}"
    prefix_collision = f"{session}-prefix-collision"
    for candidate in (session, prefix_collision):
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={candidate}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            prefix_collision,
            "-n",
            ORCHESTRATOR.WINDOW,
            "sleep",
            "60",
        ],
        check=True,
    )
    subprocess.run(
        [
            "tmux",
            "set-option",
            "-t",
            ORCHESTRATOR.exact_window_target(prefix_collision),
            "@pi_agents_coord",
            "/prefix-collision-canary",
        ],
        check=True,
    )

    temporary_root = Path(tempfile.mkdtemp(prefix="pi-tmux-orchestrator-smoke-"))
    fake_pi = temporary_root / "pi"
    fake_pi.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_pi.chmod(0o700)

    wrapper = temporary_root / "fake-runtime.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$1" == "_relay" ]]; then\n'
        f"  exec {shlex.quote(str(SCRIPT))} \"$@\"\n"
        "fi\n"
        "exec sleep 60\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    original_command_path = ORCHESTRATOR.command_path

    def smoke_command_path(name: str) -> str:
        if name == "pi":
            return str(fake_pi)
        return original_command_path(name)

    ORCHESTRATOR.command_path = smoke_command_path
    ORCHESTRATOR.STATE_ROOT = temporary_root / "state"
    ORCHESTRATOR.SCRIPT_PATH = wrapper

    def cleanup() -> None:
        for candidate in (session, prefix_collision):
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={candidate}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        shutil.rmtree(temporary_root, ignore_errors=True)

    arguments = argparse.Namespace(
        project=str(ROOT),
        task="Synthetic functional smoke. Do not read or modify project files.",
        task_file=None,
        session=session,
        with_probe=True,
        probe_task="Exercise synthetic probe marker routing only.",
        probe_task_file=None,
        with_playwright=True,
        playwright_task="Exercise synthetic Playwright marker routing only.",
        playwright_task_file=None,
        with_django_expert=True,
        django_task="Exercise synthetic Django marker routing only.",
        django_task_file=None,
        approve_project=False,
        attach=False,
        dry_run=False,
        skip_model_check=True,
        implementer_provider=None,
        implementer_model=None,
        implementer_thinking=None,
        reviewer_provider=None,
        reviewer_model=None,
        reviewer_thinking=None,
        probe_provider=None,
        probe_model=None,
        probe_thinking=None,
        playwright_provider=None,
        playwright_model=None,
        playwright_thinking=None,
        django_provider=None,
        django_model=None,
        django_thinking=None,
    )

    try:
        ORCHESTRATOR.start_command(arguments)
        coord_value = subprocess.run(
            [
                "tmux",
                "show-options",
                "-qv",
                "-t",
                ORCHESTRATOR.exact_window_target(session),
                "@pi_agents_coord",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        coord = Path(coord_value)
        manifest = ORCHESTRATOR.load_manifest(coord)
        expected_roles = {"implementer", "reviewer", "probe", "playwright", "django"}
        if set(manifest["roles"]) != expected_roles:
            raise AssertionError(f"unexpected roles: {manifest['roles'].keys()}")
        for role in expected_roles - {"implementer"}:
            if manifest["roles"][role]["tools"] != ORCHESTRATOR.READ_ONLY_TOOLS:
                raise AssertionError(f"{role} did not receive the read-only tool set")

        panes = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{session}:agents",
                "-F",
                "#{pane_id} #{pane_current_command} #{pane_pid} #{pane_dead}",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        if len(panes) != 6 or any(line.rsplit(" ", 1)[-1] != "0" for line in panes):
            raise AssertionError(f"unexpected pane state: {panes}")
        pane_pids = [int(line.split()[-2]) for line in panes]

        reports = {
            "probe.md": "Synthetic probe complete.\n",
            "handoff-1.md": "Synthetic handoff.\n",
            "playwright-1.md": "PASS\nSynthetic browser report.\n",
            "django-review-1.md": "ADVISORY_APPROVED\nSynthetic Django report.\n",
            "review-1.md": "APPROVED\nSynthetic review.\n",
        }
        markers = {
            "probe.ready",
            "handoff-1.ready",
            "playwright-1.ready",
            "django-review-1.ready",
            "review-1.ready",
            "implementation-ready.md",
        }
        for name, content in reports.items():
            (coord / name).write_text(content, encoding="utf-8")
        for name in markers - {"implementation-ready.md"}:
            (coord / name).touch()
        (coord / "implementation-ready.md").write_text(
            "Synthetic implementation readiness.\n",
            encoding="utf-8",
        )

        deadline = time.time() + 8
        seen = coord / ".relay-seen"
        while time.time() < deadline and not all((seen / marker).exists() for marker in markers):
            time.sleep(0.25)
        missing = sorted(marker for marker in markers if not (seen / marker).exists())
        if missing:
            raise AssertionError(f"relay did not consume markers: {missing}")

        if not ORCHESTRATOR.session_exists(prefix_collision):
            raise AssertionError("prefix-collision control session was unexpectedly replaced")

        subprocess.run(
            ["tmux", "kill-session", "-t", ORCHESTRATOR.exact_session_target(session)],
            check=True,
        )
        vanished_target = subprocess.run(
            ["tmux", "kill-session", "-t", ORCHESTRATOR.exact_session_target(session)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if vanished_target.returncode == 0:
            raise AssertionError("an absent exact target unexpectedly matched another session")
        if ORCHESTRATOR.session_option(session, "@pi_agents_coord") is not None:
            raise AssertionError("an absent option target matched its prefix-collision control")
        if not ORCHESTRATOR.session_exists(prefix_collision):
            raise AssertionError("vanished exact target affected its prefix-collision control")

        print("OK functional grid: all five roles plus monitor are healthy")
        print("OK exact targeting preserves a prefix session after the target disappears")
        print("OK private manifest, session options, and read-only specialist tools")
        print(
            "OK relay consumed all handoff, specialist, probe, review, and "
            "implementation-ready markers"
        )
        print("OK no Pi provider process was launched")
        cleanup()
        for candidate in (session, prefix_collision):
            residue = subprocess.run(
                ["tmux", "has-session", "-t", f"={candidate}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if residue.returncode == 0:
                raise AssertionError(f"tmux session residue remains: {candidate}")
        if temporary_root.exists():
            raise AssertionError(f"temporary state residue remains: {temporary_root}")
        deadline = time.time() + 3
        live_pids = pane_pids
        while live_pids and time.time() < deadline:
            remaining = []
            for pid in live_pids:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                remaining.append(pid)
            live_pids = remaining
            if live_pids:
                time.sleep(0.05)
        if live_pids:
            raise AssertionError(f"pane process residue remains: {live_pids}")
        print("OK no tmux, process, or temporary-state residue remains")
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())