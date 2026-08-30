#!/usr/bin/env python3
"""Model-free brokered tmux grid smoke test."""

from __future__ import annotations

import argparse
import json
import os
import pty
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support import ORCHESTRATOR  # noqa: E402

SCRIPT = ROOT / "bin" / "pi-tmux-agents"


def frame(value: dict[str, object]) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode()
    return len(payload).to_bytes(4, "big") + payload


def receive(stream: socket.socket) -> dict[str, object]:
    size = int.from_bytes(stream.recv(4), "big")
    payload = b""
    while len(payload) < size:
        payload += stream.recv(size - len(payload))
    return json.loads(payload)


def assert_attach_detach_reuses_invoking_parent() -> None:
    socket_name = f"pi-attach-smoke-{os.getpid()}"
    invoking_session = "invoking-pi"
    worker_session = "orchestration"
    client_pid: int | None = None
    client_fd: int | None = None

    def isolated_tmux(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", socket_name, *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_for_client(expected: str) -> None:
        for _ in range(100):
            clients = isolated_tmux(
                "list-clients", "-F", "#{session_name}", check=False
            )
            if clients.returncode == 0 and clients.stdout.splitlines() == [expected]:
                return
            time.sleep(0.05)
        raise AssertionError(f"tmux client did not switch to {expected}")

    def pane_pid(target: str) -> str:
        return isolated_tmux(
            "display-message", "-p", "-t", target, "#{pane_pid}"
        ).stdout.strip()

    try:
        subprocess.run(
            [
                "tmux",
                "-L",
                socket_name,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                invoking_session,
                "-c",
                str(ROOT),
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        isolated_tmux("new-session", "-d", "-s", worker_session, "-c", str(ROOT))
        sessions = isolated_tmux(
            "list-sessions", "-F", "#{session_name}"
        ).stdout.splitlines()
        if set(sessions) != {invoking_session, worker_session}:
            raise AssertionError(f"unexpected tmux sessions before attach: {sessions}")

        invoking_target = f"={invoking_session}:0.0"
        worker_target = f"={worker_session}:0.0"
        invoking_pid = pane_pid(invoking_target)
        worker_pid = pane_pid(worker_target)
        invoking_pane = isolated_tmux(
            "display-message", "-p", "-t", invoking_target, "#{pane_id}"
        ).stdout.strip()

        client_pid, client_fd = pty.fork()
        if client_pid == 0:
            os.environ.pop("TMUX", None)
            os.environ.pop("TMUX_PANE", None)
            os.environ["TERM"] = "xterm-256color"
            os.execvp(
                "tmux",
                [
                    "tmux",
                    "-L",
                    socket_name,
                    "attach-session",
                    "-t",
                    f"={invoking_session}",
                ],
            )
        wait_for_client(invoking_session)

        code = (
            f"import sys;sys.path.insert(0,{str(ROOT)!r});"
            "from pi_tmux_orchestrator.tmux import attach_session;"
            f"attach_session({worker_session!r})"
        )
        command = shlex.join([sys.executable, "-c", code])
        for _ in range(2):
            isolated_tmux("send-keys", "-t", invoking_pane, "-l", command)
            isolated_tmux("send-keys", "-t", invoking_pane, "Enter")
            wait_for_client(worker_session)
            if pane_pid(invoking_target) != invoking_pid:
                raise AssertionError("attach replaced the invoking parent Pi pane")
            if pane_pid(worker_target) != worker_pid:
                raise AssertionError("attach restarted the orchestration worker pane")

            # Tmux's default prefix-L binding switches this exact client back to
            # its previous session. This is the user-facing detach/return path.
            os.write(client_fd, b"\x02L")
            wait_for_client(invoking_session)
            if pane_pid(invoking_target) != invoking_pid:
                raise AssertionError(
                    "detach did not preserve the invoking parent Pi pane"
                )
            if pane_pid(worker_target) != worker_pid:
                raise AssertionError("detach stopped the running orchestration")

        sessions = isolated_tmux(
            "list-sessions", "-F", "#{session_name}"
        ).stdout.splitlines()
        if set(sessions) != {invoking_session, worker_session}:
            raise AssertionError(
                f"attach/detach created a separate parent session: {sessions}"
            )
    finally:
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if client_fd is not None:
            os.close(client_fd)
        if client_pid not in {None, 0}:
            try:
                os.waitpid(client_pid, 0)
            except ChildProcessError:
                pass


def main() -> int:
    if not shutil.which("tmux"):
        print("tmux is required for the functional smoke", file=sys.stderr)
        return 1

    assert_attach_detach_reuses_invoking_parent()

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
            "agents",
            "sleep",
            "60",
        ],
        check=True,
    )

    temporary_root = Path(tempfile.mkdtemp(prefix="pi-tmux-orchestrator-smoke-"))
    fake_pi = temporary_root / "pi"
    fake_pi.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "mode='rpc' if '--mode' in sys.argv else 'tui'\n"
        "role=os.environ.get('PI_TMUX_ORCHESTRATOR_ROLE','unknown')\n"
        "argv_dir=os.environ.get('SMOKE_PI_ARGV_DIR')\n"
        "if argv_dir:\n"
        "    with open(os.path.join(argv_dir, f'{mode}-{role}.json'), 'w', encoding='utf-8') as handle:\n"
        "        json.dump(sys.argv[1:], handle)\n"
        "if '--mode' not in sys.argv:\n"
        "    time.sleep(60)\n"
        "    raise SystemExit(0)\n"
        "for line in sys.stdin:\n"
        "    value=json.loads(line); rid=value.get('id'); kind=value.get('type')\n"
        "    data={'sessionId':'synthetic-rpc-session','isStreaming':False} if kind=='get_state' else None\n"
        "    response={'type':'response','command':kind,'success':True}\n"
        "    if rid is not None: response['id']=rid\n"
        "    if data is not None: response['data']=data\n"
        "    print(json.dumps(response), flush=True)\n"
        "    if kind=='get_state':\n"
        "        print(json.dumps({'type':'agent_start'}), flush=True)\n"
        "        print(json.dumps({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'Synthetic assistant progress.'}}), flush=True)\n"
        "        print(json.dumps({'type':'tool_execution_start','toolName':'read','args':{'path':'synthetic-visible.txt'}}), flush=True)\n"
        "        print(json.dumps({'type':'tool_execution_end','toolName':'read','isError':False,'result':{'content':[{'type':'text','text':'SYNTHETIC_RPC_VISIBLE_OUTPUT'}]}}), flush=True)\n"
        "        print(json.dumps({'type':'agent_settled'}), flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.chmod(0o700)

    wrapper = temporary_root / "fake-runtime.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "$1" == "_broker" || "$1" == "_run-agent" ]]; then\n'
        f"  export PATH={shlex.quote(str(temporary_root))}:$PATH\n"
        f"  export SMOKE_PI_ARGV_DIR={shlex.quote(str(temporary_root))}\n"
        f'  exec {shlex.quote(str(SCRIPT))} "$@"\n'
        "fi\n"
        "exec sleep 60\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    original_command_path = ORCHESTRATOR.command_path

    def smoke_command_path(name: str) -> str:
        return str(fake_pi) if name == "pi" else original_command_path(name)

    ORCHESTRATOR.command_path = smoke_command_path
    ORCHESTRATOR.STATE_ROOT = temporary_root / "state"
    ORCHESTRATOR.SCRIPT_PATH = wrapper
    ORCHESTRATOR.WORKER_EXTENSION_PATH = ROOT / "extensions" / "orchestrator-worker.js"
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
        task="Synthetic functional smoke. Do not modify project files.",
        task_file=None,
        context_capsule="### Current state\nSYNTHETIC_CONTEXT_CAPSULE_CANARY",
        context_capsule_file=None,
        session=session,
        implementation_flow="phased",
        with_probe=True,
        probe_task="Synthetic probe evidence.",
        probe_task_file=None,
        with_playwright=True,
        playwright_task="Synthetic browser evidence.",
        playwright_task_file=None,
        with_django_expert=True,
        django_task="Synthetic Django evidence.",
        django_task_file=None,
        approve_project=False,
        rpc_workers=True,
        attach=False,
        dry_run=False,
        skip_model_check=True,
        **{
            f"{role}_{field}": None
            for role in ("implementer", "reviewer", "probe", "playwright", "django")
            for field in ("provider", "model", "thinking")
        },
    )

    try:
        controller = ORCHESTRATOR.controller_start_command(argparse.Namespace())
        if not controller.data["running"]:
            raise AssertionError("controller did not start")
        ORCHESTRATOR.controller_stop_command(argparse.Namespace(confirm=True))

        ORCHESTRATOR.start_command(arguments)
        coord = Path(
            subprocess.run(
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
        )
        manifest = ORCHESTRATOR.load_manifest(coord)
        subprocess.run(
            [
                "tmux",
                "resize-window",
                "-t",
                ORCHESTRATOR.exact_window_target(session),
                "-x",
                "240",
                "-y",
                "90",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        roles = {"implementer", "reviewer", "probe", "playwright", "django"}
        if manifest["version"] != 5 or manifest["coordination"] != "broker-v1":
            raise AssertionError("new run did not use manifest v5 broker coordination")
        if manifest["execution_profile"] != {
            "name": "thorough",
            "kind": "packaged",
            "source": "packaged-default",
        }:
            raise AssertionError("manifest did not bind the execution profile")
        if manifest["project_config"]["matched"] is not False:
            raise AssertionError("manifest did not bind the unmatched project policy")
        if manifest["orchestration_config"]["version"] != 3:
            raise AssertionError(
                "manifest did not bind the orchestration config schema"
            )
        if set(manifest["roles"]) != roles:
            raise AssertionError("all roles were not started")
        if "SYNTHETIC_CONTEXT_CAPSULE_CANARY" in json.dumps(manifest):
            raise AssertionError("manifest persisted the context capsule")
        forbidden = [
            path.name
            for path in coord.iterdir()
            if path.name.endswith(".ready")
            or path.name.startswith(
                ("handoff-", "review-", "playwright-", "django-review-")
            )
            or path.name == "task.md"
        ]
        if forbidden:
            raise AssertionError(f"new run created legacy payload files: {forbidden}")

        panes = subprocess.run(
            ["tmux", "list-panes", "-t", f"={session}:=agents", "-F", "#{pane_dead}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        if len(panes) != 6 or any(value != "0" for value in panes):
            raise AssertionError(f"brokered RPC grid is unhealthy: {panes}")

        implementer_pane = manifest["roles"]["implementer"]["pane_id"]
        deadline = time.time() + 4
        rpc_output = ""
        while time.time() < deadline:
            rpc_output = subprocess.run(
                ["tmux", "capture-pane", "-p", "-S", "-200", "-t", implementer_pane],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            if "SYNTHETIC_RPC_VISIBLE_OUTPUT" in rpc_output:
                break
            time.sleep(0.05)
        rpc_argv = json.loads(
            (temporary_root / "rpc-implementer.json").read_text(encoding="utf-8")
        )
        if (
            "--no-skills" not in rpc_argv
            or "--system-prompt" not in rpc_argv
            or "--append-system-prompt" in rpc_argv
        ):
            raise AssertionError(f"RPC worker resource policy drifted: {rpc_argv}")
        if (
            "[assistant]" not in rpc_output
            or "Synthetic assistant progress." not in rpc_output
            or "[tool read input]" not in rpc_output
            or '"path": "synthetic-visible.txt"' not in rpc_output
            or "[tool read output]" not in rpc_output
            or "SYNTHETIC_RPC_VISIBLE_OUTPUT" not in rpc_output
        ):
            raise AssertionError(
                f"RPC pane omitted visible assistant/tool output: {rpc_output!r}"
            )

        deadline = time.time() + 8
        socket_path = ORCHESTRATOR.broker_paths(coord)["socket"]
        while time.time() < deadline and not socket_path.exists():
            time.sleep(0.05)
        if not socket_path.exists():
            pane_ids = subprocess.run(
                ["tmux", "list-panes", "-t", f"={session}:=agents", "-F", "#{pane_id}"],
                check=False,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            pane_output = {
                pane_id: subprocess.run(
                    ["tmux", "capture-pane", "-p", "-S", "-100", "-t", pane_id],
                    check=False,
                    text=True,
                    capture_output=True,
                ).stdout
                for pane_id in pane_ids
            }
            raise AssertionError(f"broker socket did not start: {pane_output!r}")

        clients: dict[str, socket.socket] = {}
        for role in roles:
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stream.connect(str(socket_path))
            token = (coord / f"{role}.token").read_text(encoding="utf-8").strip()
            stream.sendall(
                frame(
                    {
                        "version": 1,
                        "type": "hello",
                        "role": role,
                        "token": token,
                        "id": (str(len(clients) + 1) * 32)[:32],
                        "generation": 1,
                    }
                )
            )
            response = receive(stream)
            if not response["success"]:
                raise AssertionError(f"broker rejected {role}")
            clients[role] = stream

        for role, stream in clients.items():
            stream.settimeout(4)
            baseline = receive(stream)
            if (
                baseline.get("type") != "context"
                or baseline.get("kind") != "baseline"
                or "SYNTHETIC_CONTEXT_CAPSULE_CANARY" not in baseline.get("content", "")
            ):
                raise AssertionError(
                    f"{role} did not receive the bounded context capsule"
                )

        required_dashboard_text = (
            "PI TMUX ORCHESTRATOR",
            f"SESSION {session}",
            "ACTIVE   ROUND 1",
            "TRANSPORT RPC",
            "PROTOCOL BROKER-V1 / V1",
            "implementer",
            "reviewer",
            "RECENT METADATA EVENTS",
            "pi-tmux-agents attach",
        )
        deadline = time.time() + 4
        monitor_output = ""
        while time.time() < deadline:
            snapshot = ORCHESTRATOR.public_broker_snapshot(coord)
            monitor_output = subprocess.run(
                [
                    "tmux",
                    "capture-pane",
                    "-p",
                    "-t",
                    manifest["monitor_pane_id"],
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            implementer = next(
                worker
                for worker in snapshot["roles"]
                if worker["role"] == "implementer"
            )
            state_ready = (
                snapshot["workflow"]["state"] == "active"
                and snapshot["workflow"]["implementation_flow"] == "phased"
                and all(worker["connected"] for worker in snapshot["roles"])
                and (implementer["assignment"] or {}).get("kind") == "plan"
            )
            dashboard_ready = all(
                value in monitor_output for value in required_dashboard_text
            )
            if state_ready and dashboard_ready:
                break
            time.sleep(0.05)
        else:
            missing = [
                value
                for value in required_dashboard_text
                if value not in monitor_output
            ]
            raise AssertionError(
                "broker/dashboard did not become ready: "
                f"state={snapshot['workflow']['state']} missing={missing}"
            )
        if (
            "Synthetic functional smoke" in monitor_output
            or "SYNTHETIC_CONTEXT_CAPSULE_CANARY" in monitor_output
        ):
            raise AssertionError("broker dashboard exposed a workflow body")
        if (coord / "startup.json").exists():
            raise AssertionError("broker retained the startup context capsule")
        with ORCHESTRATOR.connect_broker_database(coord, readonly=True) as database:
            database_dump = "\n".join(database.iterdump())
        if "SYNTHETIC_CONTEXT_CAPSULE_CANARY" in database_dump:
            raise AssertionError("broker persisted the context capsule in SQLite")
        capsule_bytes = b"SYNTHETIC_CONTEXT_CAPSULE_CANARY"
        leaked_metadata_paths = []
        for path in coord.rglob("*"):
            relative = path.relative_to(coord)
            if not path.is_file() or relative.parts[0] == "sessions":
                continue
            if capsule_bytes in path.read_bytes():
                leaked_metadata_paths.append(str(relative))
        if leaked_metadata_paths:
            raise AssertionError(
                "coordination metadata persisted the context capsule: "
                f"{leaked_metadata_paths}"
            )

        # The authenticated operator path is brokered and acknowledged for both presentations.
        send_id = "a" * 32
        sent = ORCHESTRATOR.send_command(
            argparse.Namespace(
                session=session,
                run=None,
                role="reviewer",
                message="Synthetic operator message.",
                message_file=None,
                delivery="follow-up",
                command_id=send_id,
            )
        )
        if not sent.data["acknowledged"] or sent.data["command_id"] != send_id:
            raise AssertionError("broker send was not acknowledged")
        duplicate = ORCHESTRATOR.send_command(
            argparse.Namespace(
                session=session,
                run=None,
                role="reviewer",
                message="Different body; first accepted payload wins.",
                message_file=None,
                delivery="follow-up",
                command_id=send_id,
            )
        )
        if not duplicate.data["duplicate"]:
            raise AssertionError("broker control retry was not deduplicated")
        aborted = ORCHESTRATOR.abort_command(
            argparse.Namespace(
                session=session,
                run=None,
                role="implementer",
                command_id="b" * 32,
            )
        )
        if not aborted.data["acknowledged"]:
            raise AssertionError("broker abort was not acknowledged")

        status = ORCHESTRATOR.status_command(argparse.Namespace(session=session))
        if (
            status.data["broker"]["workflow"]["state"] != "active"
            or status.data["files"]
            or status.data["paths"].get("observer_socket")
            != str(ORCHESTRATOR.broker_paths(coord)["socket"])
        ):
            raise AssertionError("status did not expose broker-only metadata")
        supervisor = ORCHESTRATOR.supervisor_snapshot(session, coord.name)
        batch = ORCHESTRATOR.supervisor_event_batch(
            session,
            coord.name,
            requested_roles=["implementer", "reviewer"],
            cursors={"implementer": 0, "reviewer": 0},
            limit=20,
        )
        if (
            supervisor["host_adapter"]["runtime_status"] != "not_observed"
            or supervisor["coordination"] != "broker-v1"
            or len(batch["roles"]) != 2
        ):
            raise AssertionError("Supervisor API v2 broker reads failed")
        public_metadata = json.dumps(
            {"status": status.data, "supervisor": supervisor, "events": batch}
        )
        if "SYNTHETIC_CONTEXT_CAPSULE_CANARY" in public_metadata:
            raise AssertionError("public metadata exposed the context capsule")

        for stream in clients.values():
            stream.close()
        ORCHESTRATOR.stop_command(argparse.Namespace(session=session, yes=True))
        if not ORCHESTRATOR.session_exists(prefix_collision):
            raise AssertionError("exact stop affected prefix-collision session")

        # Default TUI remains the presentation default and still starts broker v1.
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
        tui_coord = Path(
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
        tui_manifest = ORCHESTRATOR.load_manifest(tui_coord)
        if (
            tui_manifest["transport"] != "tui"
            or tui_manifest["coordination"] != "broker-v1"
        ):
            raise AssertionError("TUI did not share broker protocol")
        deadline = time.time() + 3
        tui_argv_path = temporary_root / "tui-implementer.json"
        while not tui_argv_path.exists() and time.time() < deadline:
            time.sleep(0.05)
        tui_argv = json.loads(tui_argv_path.read_text(encoding="utf-8"))
        if (
            "--no-skills" not in tui_argv
            or "--system-prompt" not in tui_argv
            or "--append-system-prompt" in tui_argv
        ):
            raise AssertionError(f"TUI worker resource policy drifted: {tui_argv}")
        ORCHESTRATOR.stop_command(argparse.Namespace(session=tui_session, yes=True))
        deadline = time.time() + 3
        for path in (
            ORCHESTRATOR.broker_paths(coord)["socket"],
            ORCHESTRATOR.broker_paths(tui_coord)["socket"],
        ):
            while path.exists() and time.time() < deadline:
                time.sleep(0.05)
            if path.exists():
                raise AssertionError(f"broker socket residue remains: {path}")

        print("OK invoking Pi remains the parent across repeated attach/detach")
        print("OK detach returns the exact client without stopping worker panes")
        print("OK controller lifecycle")
        print("OK TUI and RPC presentations share manifest-v5 broker-v1")
        print("OK RPC panes render assistant progress plus tool inputs and outputs")
        print("OK owner-only broker accepted five authenticated role bridges")
        print("OK broker pane rendered the metadata-only adaptive dashboard")
        print(
            "OK new runs created no handoff, readiness, mailbox, or relay payload files"
        )
        print("OK broker send/abort acknowledgement and idempotent retry")
        print("OK metadata-only status and Supervisor API v2 retained reads")
        print("OK exact tmux targeting preserved prefix collision")
        cleanup()
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
