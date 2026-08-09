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
    controller_session = f"{session}-controller"
    tui_session = f"{session}-tui"
    ORCHESTRATOR.CONTROLLER_TMUX_SESSION = controller_session
    ORCHESTRATOR.CONTROLLER_PI_SESSION_ID = f"smoke-controller-{os.getpid()}"
    for candidate in (session, prefix_collision, controller_session, tui_session):
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
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "if '--mode' not in sys.argv or sys.argv[sys.argv.index('--mode') + 1] != 'rpc':\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        "for line in sys.stdin:\n"
        "    value = json.loads(line)\n"
        "    request_id = value.get('id')\n"
        "    command = value.get('type')\n"
        "    response = {'type': 'response', 'command': command, 'success': True}\n"
        "    if request_id is not None:\n"
        "        response['id'] = request_id\n"
        "    if command == 'get_state':\n"
        "        response['data'] = {'sessionId': 'synthetic-rpc-session', 'isStreaming': False}\n"
        "    print(json.dumps(response), flush=True)\n"
        "    if command == 'prompt':\n"
        "        print(json.dumps({'type': 'agent_start'}), flush=True)\n"
        "        print(json.dumps({'type': 'message_update', 'assistantMessageEvent': "
        "{'type': 'text_delta', 'delta': 'Synthetic RPC response.'}}), flush=True)\n"
        "        print(json.dumps({'type': 'agent_settled'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o700)

    wrapper = temporary_root / "fake-runtime.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$1" == "_relay" || "$1" == "_run-agent" ]]; then\n'
        f"  export PATH={shlex.quote(str(temporary_root))}:$PATH\n"
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
    os.environ["PI_TMUX_CONTROLLER_HOME"] = str(temporary_root / "controller")

    def cleanup() -> None:
        for candidate in (session, prefix_collision, controller_session, tui_session):
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
        rpc_workers=True,
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
        controller = ORCHESTRATOR.controller_start_command(argparse.Namespace())
        if not controller.data["running"] or controller.data["pane"]["dead"]:
            raise AssertionError(f"controller did not stay healthy: {controller.data}")
        controller_pid = controller.data["pane"]["pid"]
        controller_status = ORCHESTRATOR.controller_status_command(argparse.Namespace())
        if controller_status.data["pi_session_id"] != ORCHESTRATOR.CONTROLLER_PI_SESSION_ID:
            raise AssertionError("controller status lost the stable Pi session identity")
        try:
            ORCHESTRATOR.controller_start_command(argparse.Namespace())
        except ORCHESTRATOR.OrchestrationError as error:
            if error.code != "already_running":
                raise
        else:
            raise AssertionError("duplicate controller start was not refused")
        ORCHESTRATOR.controller_stop_command(argparse.Namespace(confirm=True))
        if ORCHESTRATOR.session_exists(controller_session):
            raise AssertionError("controller tmux session survived confirmed stop")

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
        if ORCHESTRATOR.manifest_transport(manifest) != ORCHESTRATOR.RPC_TRANSPORT:
            raise AssertionError("functional grid did not use RPC worker transport")
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
        pane_pids = [controller_pid, *[int(line.split()[-2]) for line in panes]]

        deadline = time.time() + 8
        while time.time() < deadline:
            states = {
                role: ORCHESTRATOR.load_rpc_state(coord, role)
                for role in expected_roles
            }
            if all(state and state["session_id"] == "synthetic-rpc-session" for state in states.values()):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"RPC supervisors did not publish session state: {states}")
        pane_pids.extend(state["pid"] for state in states.values() if state)
        status = ORCHESTRATOR.status_command(argparse.Namespace(session=session))
        if not all(role["rpc_state"] for role in status.data["roles"]):
            raise AssertionError("status did not expose bounded RPC role metadata")

        sent = ORCHESTRATOR.send_command(
            argparse.Namespace(
                session=session,
                role="implementer",
                message="Synthetic acknowledged steering message.",
                message_file=None,
                delivery="follow-up",
            )
        )
        if not sent.data["acknowledged"] or sent.data["transport"] != "rpc":
            raise AssertionError(f"RPC send was not acknowledged: {sent.data}")
        aborted = ORCHESTRATOR.abort_command(
            argparse.Namespace(session=session, role="implementer")
        )
        if not aborted.data["acknowledged"]:
            raise AssertionError("RPC abort was not acknowledged")

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

        tui_arguments = argparse.Namespace(**vars(arguments))
        tui_arguments.session = tui_session
        tui_arguments.rpc_workers = False
        tui_arguments.with_probe = False
        tui_arguments.probe_task = None
        tui_arguments.with_playwright = False
        tui_arguments.playwright_task = None
        tui_arguments.with_django_expert = False
        tui_arguments.django_task = None
        ORCHESTRATOR.start_command(tui_arguments)
        tui_manifest_coord = Path(
            subprocess.run(
                [
                    "tmux",
                    "show-options",
                    "-qv",
                    "-t",
                    ORCHESTRATOR.exact_window_target(tui_session),
                    "@pi_agents_coord",
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
        )
        tui_manifest = ORCHESTRATOR.load_manifest(tui_manifest_coord)
        if ORCHESTRATOR.manifest_transport(tui_manifest) != ORCHESTRATOR.TUI_TRANSPORT:
            raise AssertionError("default TUI transport was not preserved")
        tui_panes = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                f"{tui_session}:agents",
                "-F",
                "#{pane_pid} #{pane_dead}",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        if len(tui_panes) != 3 or any(line.rsplit(" ", 1)[-1] != "0" for line in tui_panes):
            raise AssertionError(f"default TUI grid is unhealthy: {tui_panes}")
        pane_pids.extend(int(line.split()[0]) for line in tui_panes)
        tui_send = ORCHESTRATOR.send_command(
            argparse.Namespace(
                session=tui_session,
                role="implementer",
                message="Synthetic TUI transport message.",
                message_file=None,
                delivery="steer",
            )
        )
        if tui_send.data["acknowledged"] or tui_send.data["transport"] != "tui":
            raise AssertionError("default TUI send semantics changed")
        ORCHESTRATOR.stop_command(argparse.Namespace(session=tui_session, yes=True))

        print("OK persistent project-neutral controller lifecycle and duplicate refusal")
        print("OK default interactive TUI grid and transport remain compatible")
        print("OK functional grid: all five RPC roles plus monitor are healthy")
        print("OK RPC steer/follow-up mailbox delivery and abort are acknowledged")
        print("OK exact targeting preserves a prefix session after the target disappears")
        print("OK private manifest, session options, and read-only specialist tools")
        print(
            "OK relay consumed all handoff, specialist, probe, review, and "
            "implementation-ready markers"
        )
        print("OK no Pi provider process was launched")
        cleanup()
        for candidate in (session, prefix_collision, controller_session, tui_session):
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