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
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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

    arguments = argparse.Namespace(
        project=str(ROOT),
        task="Synthetic functional smoke. Do not read or modify project files.",
        task_file=None,
        session=session,
        with_probe=True,
        probe_task="Exercise synthetic probe marker routing only.",
        probe_task_file=None,
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
    )

    try:
        ORCHESTRATOR.start_command(arguments)
        coord_value = subprocess.run(
            ["tmux", "show-options", "-qv", "-t", session, "@pi_agents_coord"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        coord = Path(coord_value)
        manifest = ORCHESTRATOR.load_manifest(coord)
        if set(manifest["roles"]) != {"implementer", "reviewer", "probe"}:
            raise AssertionError(f"unexpected roles: {manifest['roles'].keys()}")

        panes = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{session}:agents",
                "-F",
                "#{pane_id} #{pane_current_command} #{pane_dead}",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        if len(panes) != 4 or any(line.rsplit(" ", 1)[-1] != "0" for line in panes):
            raise AssertionError(f"unexpected pane state: {panes}")

        (coord / "probe.md").write_text("Synthetic probe complete.\n", encoding="utf-8")
        (coord / "probe.ready").touch()
        (coord / "handoff-1.md").write_text("Synthetic handoff.\n", encoding="utf-8")
        (coord / "handoff-1.ready").touch()

        deadline = time.time() + 8
        probe_seen = coord / ".relay-seen" / "probe.ready"
        handoff_seen = coord / ".relay-seen" / "handoff-1.ready"
        while time.time() < deadline and not (probe_seen.exists() and handoff_seen.exists()):
            time.sleep(0.25)
        if not probe_seen.exists() or not handoff_seen.exists():
            raise AssertionError("relay did not consume probe and handoff markers")

        print("OK functional grid: 4 healthy panes")
        print("OK private manifest and session options")
        print("OK relay consumed probe and handoff markers")
        print("OK no Pi provider process was launched")
        return 0
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(temporary_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())