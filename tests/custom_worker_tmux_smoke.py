#!/usr/bin/env python3
"""Model-free real-tmux launch smoke for gated custom TUI and RPC workers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pi_tmux_orchestrator import broker_store, commands, constants, runtime  # noqa: E402
from pi_tmux_orchestrator.broker import Broker  # noqa: E402
from pi_tmux_orchestrator.configuration import public_project_config  # noqa: E402
from pi_tmux_orchestrator.custom_role_resources import select_custom_start  # noqa: E402
from pi_tmux_orchestrator.models import OrchestrationError  # noqa: E402
from pi_tmux_orchestrator.storage import (  # noqa: E402
    ensure_private_directory,
    secure_write,
)

FAKE_PI = r"""#!/usr/bin/env python3
import json, os, socket, sys, threading, time

def send(sock, value):
    payload=json.dumps(value,separators=(',',':')).encode()
    sock.sendall(len(payload).to_bytes(4,'big')+payload)

def receive(sock):
    prefix=b''
    while len(prefix)<4:
        chunk=sock.recv(4-len(prefix))
        if not chunk: return None
        prefix+=chunk
    size=int.from_bytes(prefix,'big'); payload=b''
    while len(payload)<size:
        chunk=sock.recv(size-len(payload))
        if not chunk: return None
        payload+=chunk
    return json.loads(payload)

def broker_worker():
    role=os.environ['PI_TMUX_ORCHESTRATOR_ROLE']
    token=os.environ['PI_TMUX_ORCHESTRATOR_TOKEN']
    generation=int(os.environ['PI_TMUX_ORCHESTRATOR_GENERATION'])
    path=os.environ['PI_TMUX_ORCHESTRATOR_SOCKET']
    sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    for _ in range(200):
        try: sock.connect(path); break
        except OSError: time.sleep(.025)
    else: raise SystemExit('broker unavailable')
    sequence=1
    send(sock,{'version':1,'type':'hello','role':role,'token':token,'id':f'{sequence:032x}','generation':generation})
    sequence+=1
    while True:
        value=receive(sock)
        if value is None: os._exit(0)
        if value.get('type') not in {'context','assignment'}: continue
        send(sock,{'version':1,'type':'ack','role':role,'token':token,'id':f'{sequence:032x}','delivery_id':value['id'],'status':'accepted'})
        sequence+=1
        if value['type']!='assignment': continue
        kind=value['kind']; report={'kind':kind,'summary':'SYNTHETIC_CUSTOM_WORKER_REPORT'}
        if kind=='review': report['verdict']='approved'
        elif kind=='playwright': report['verdict']='pass'
        elif kind=='django': report['verdict']='advisory_approved'
        send(sock,{'version':1,'type':'report','role':role,'token':token,'id':f'{sequence:032x}','assignment_id':value['assignment_id'],'report':report})
        sequence+=1

def record():
    path=os.environ['CUSTOM_WORKER_RECORD']
    value={'role':os.environ['PI_TMUX_ORCHESTRATOR_ROLE'],'mode':'rpc' if '--mode' in sys.argv else 'tui','argv':sys.argv[1:],'contract':os.environ.get('PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT')}
    with open(path,'a',encoding='utf-8') as handle:
        handle.write(json.dumps(value,separators=(',',':'))+'\n')

record()
if '--mode' not in sys.argv:
    broker_worker()
else:
    threading.Thread(target=broker_worker,daemon=True).start()
    for line in sys.stdin:
        value=json.loads(line); response={'type':'response','command':value.get('type'),'success':True}
        if 'id' in value: response['id']=value['id']
        if value.get('type')=='get_state': response['data']={'sessionId':'synthetic-custom-rpc','isStreaming':False}
        print(json.dumps(response),flush=True)
"""


def resource(path: Path, body: str) -> dict[str, str]:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(body.encode()).hexdigest()}


async def wait_for(predicate, message: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


def workflow_state(coord: Path) -> str:
    with broker_store.connect_broker_database(coord, readonly=True) as database:
        return database.execute(
            "SELECT value FROM meta WHERE key='workflow_state'"
        ).fetchone()[0]


def role_state(coord: Path, role: str) -> dict[str, object]:
    with broker_store.connect_broker_database(coord, readonly=True) as database:
        return dict(
            database.execute(
                "SELECT state,connected,generation FROM roles WHERE role=?", (role,)
            ).fetchone()
        )


async def run_transport(root: Path, transport: str) -> None:
    session = f"pi-custom-{transport}-{os.getpid()}"
    project = ensure_private_directory(root / f"project-{transport}")
    policy = ensure_private_directory(root / f"policy-{transport}")
    prompt_body = "PRIVATE_CUSTOM_PROMPT_CANARY\n"
    skill_body = (
        "---\nname: custom-smoke\ndescription: PRIVATE_CUSTOM_SKILL_CANARY\n---\n"
    )
    prompt = resource(policy / "prompt.md", prompt_body)
    skill = resource(policy / "skill.md", skill_body)
    role_name = "custom-security"
    registry = policy / "roles.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "roles": [
                    {
                        "id": role_name,
                        "contract": "probe",
                        "prompt": prompt,
                        "skills": [skill],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry.chmod(0o600)
    selected = select_custom_start(
        project,
        [[role_name, "fixture", "model", "off"]],
        str(registry),
    )
    roles = ["implementer", "reviewer", role_name]
    configs = {
        "implementer": {
            "provider": "fixture",
            "model": "model",
            "thinking": "off",
            "tools": None,
            "pane_id": None,
            "skills": [],
        },
        "reviewer": {
            "provider": "fixture",
            "model": "model",
            "thinking": "off",
            "tools": constants.READ_ONLY_TOOLS,
            "pane_id": None,
            "skills": [],
        },
        **selected["roles"],
    }
    state_root = ensure_private_directory(root / "state")
    original_state = runtime.STATE_ROOT
    original_script = runtime.SCRIPT_PATH
    runtime.STATE_ROOT = state_root
    coord = ensure_private_directory(state_root / session / "run-1", parents=True)
    manifest = commands.construct_start_manifest(
        coord,
        project,
        session,
        transport,
        approve_project=False,
        execution_profile={
            "name": "balanced",
            "kind": "packaged",
            "source": "packaged-default",
        },
        project_config=public_project_config(None),
        orchestration_config={"path": str(root / "models.json"), "version": 3},
        roles=roles,
        configs=configs,
        custom_role_registry=selected["registry_path"],
    )
    ensure_private_directory(coord / "sessions")
    for role in roles:
        ensure_private_directory(Path(manifest["roles"][role]["session_dir"]))
    if transport == constants.RPC_TRANSPORT:
        for role in roles:
            commands.rpc_role_paths(coord, role, create=True)
    tokens = {role: secrets.token_hex(16) for role in roles}
    control_token = secrets.token_hex(16)
    broker_store.initialize_broker_database(
        coord,
        manifest,
        tokens,
        control_token,
        soft_role_tokens=0,
        soft_total_tokens=0,
    )
    secure_write(
        coord / "startup.json",
        json.dumps({"task": "SYNTHETIC CUSTOM TMUX SMOKE", "role_tasks": {}}),
    )
    for role, token in tokens.items():
        secure_write(coord / f"{role}.token", token + "\n")
    secure_write(coord / "control.token", control_token + "\n")
    record_path = root / f"launch-{transport}.jsonl"
    wrapper = root / f"runtime-{transport}.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'if [[ "$1" == "_run-agent" ]]; then\n'
        f" export PATH={shlex.quote(str(root))}:$PATH\n"
        f" export CUSTOM_WORKER_RECORD={shlex.quote(str(record_path))}\n"
        f' exec {shlex.quote(str(ROOT / "bin" / "pi-tmux-agents"))} "$@"\n'
        "fi\nexec sleep 60\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    runtime.SCRIPT_PATH = wrapper
    broker = None
    broker_task = None
    handler_tasks: set[asyncio.Task[None]] = set()
    try:
        commands.create_tmux_grid(session, project, coord, roles, manifest)
        if manifest["version"] != 6 or manifest["custom_role_registry"] != str(
            registry
        ):
            raise AssertionError("custom launch did not retain manifest v6 binding")
        with mock.patch.object(
            constants, "KNOWN_ROLES", constants.KNOWN_ROLES | {role_name}
        ):
            broker = Broker(coord, manifest)
        handle_client = broker.handle_client

        async def tracked_client(reader, writer):
            task = asyncio.current_task()
            handler_tasks.add(task)
            try:
                await handle_client(reader, writer)
            finally:
                handler_tasks.discard(task)

        broker.handle_client = tracked_client
        broker_task = asyncio.create_task(broker._run())
        try:
            await wait_for(
                lambda: workflow_state(coord) == "ready",
                f"{transport} custom worker workflow did not reach independent approval",
            )
        except AssertionError as error:
            snapshot = broker_store.public_broker_snapshot(coord)
            panes = {
                role: subprocess.run(
                    [
                        "tmux",
                        "capture-pane",
                        "-p",
                        "-S",
                        "-100",
                        "-t",
                        manifest["roles"][role]["pane_id"],
                    ],
                    check=False,
                    text=True,
                    capture_output=True,
                ).stdout
                for role in roles
            }
            launches = (
                record_path.read_text(encoding="utf-8") if record_path.exists() else ""
            )
            raise AssertionError(
                f"{error}; snapshot={snapshot!r}; launches={launches!r}; panes={panes!r}"
            ) from error
        records = [json.loads(line) for line in record_path.read_text().splitlines()]
        custom = [value for value in records if value["role"] == role_name]
        if len(custom) != 1 or custom[0]["mode"] != transport:
            raise AssertionError(f"unexpected custom launch records: {custom!r}")
        launch = custom[0]
        if launch["contract"] != "probe":
            raise AssertionError("custom contract was not launch-bound")
        argv = launch["argv"]
        expected_tools = "read,grep,find,ls,orchestrator_report"
        if (
            argv[argv.index("--tools") + 1] != expected_tools
            or "--no-extensions" not in argv
            or "--no-prompt-templates" not in argv
            or "--no-skills" not in argv
            or argv.count("--extension") != 1
            or argv.count("--skill") != 1
        ):
            raise AssertionError(
                f"custom {transport} resource policy drifted: {argv!r}"
            )
        system_prompt = Path(argv[argv.index("--system-prompt") + 1])
        snapshot_skill = Path(argv[argv.index("--skill") + 1])
        if prompt_body.strip() not in system_prompt.read_text(encoding="utf-8"):
            raise AssertionError("custom prompt snapshot was not launched")
        if snapshot_skill.read_text(encoding="utf-8") != skill_body:
            raise AssertionError("custom skill snapshot was not launched")
        with broker_store.connect_broker_database(coord, readonly=True) as database:
            dump = "\n".join(database.iterdump())
            report_roles = {
                row[0]
                for row in database.execute("SELECT role FROM reports ORDER BY role")
            }
        if report_roles != {"implementer", "reviewer", role_name}:
            raise AssertionError(
                f"custom workflow reports are incomplete: {report_roles}"
            )
        for canary in (
            "SYNTHETIC CUSTOM TMUX SMOKE",
            "SYNTHETIC_CUSTOM_WORKER_REPORT",
            "PRIVATE_CUSTOM_PROMPT_CANARY",
            "PRIVATE_CUSTOM_SKILL_CANARY",
        ):
            if canary in dump:
                raise AssertionError("private custom payload entered SQLite")

        restart_args = argparse.Namespace(
            yes=True,
            session=session,
            role=role_name,
            provider=None,
            model=None,
            thinking=None,
            skip_model_check=True,
        )
        await asyncio.to_thread(commands.restart_command, restart_args)
        await wait_for(
            lambda: role_state(coord, role_name)
            == {"state": "idle", "connected": 1, "generation": 2},
            f"custom {transport} restart did not complete its authenticated handover",
        )
        await wait_for(
            lambda: record_path.read_text(encoding="utf-8").count("\n") == 4,
            f"custom {transport} replacement worker did not launch",
        )
        records = [json.loads(line) for line in record_path.read_text().splitlines()]
        replacements = [value for value in records if value["role"] == role_name]
        if len(replacements) != 2 or any(
            value["contract"] != "probe" or value["mode"] != transport
            for value in replacements
        ):
            raise AssertionError(
                f"custom {transport} restart binding drifted: {replacements!r}"
            )

        pane_id = manifest["roles"][role_name]["pane_id"]
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        Path(prompt["path"]).write_text("REVOKED\n", encoding="utf-8")
        try:
            await asyncio.to_thread(commands.restart_command, restart_args)
        except OrchestrationError:
            pass
        else:
            raise AssertionError("revoked custom resources reached tmux respawn")
        if role_state(coord, role_name) != {
            "state": "idle",
            "connected": 1,
            "generation": 2,
        }:
            raise AssertionError("revoked restart mutated broker generation/state")
        current_pid = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        if current_pid != pane_pid:
            raise AssertionError("revoked restart replaced the healthy worker")
        print(
            f"OK custom {transport.upper()} worker reached mandatory review and safe restart"
        )
    finally:
        if broker is not None:
            broker.stopping.set()
        if broker_task is not None:
            await asyncio.wait_for(broker_task, 10)
        if handler_tasks:
            await asyncio.wait_for(asyncio.gather(*handler_tasks), 10)
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        runtime.STATE_ROOT = original_state
        runtime.SCRIPT_PATH = original_script


async def async_main() -> None:
    if not shutil.which("tmux"):
        raise SystemExit("tmux is required for the custom worker smoke")
    with tempfile.TemporaryDirectory(prefix="pi-custom-worker-smoke-") as temporary:
        root = Path(temporary).resolve()
        fake_pi = root / "pi"
        fake_pi.write_text(FAKE_PI, encoding="utf-8")
        fake_pi.chmod(0o700)
        for transport in ("tui", "rpc"):
            await run_transport(root, transport)


def main() -> int:
    asyncio.run(async_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
