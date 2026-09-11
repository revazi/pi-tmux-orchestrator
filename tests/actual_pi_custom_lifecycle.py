#!/usr/bin/env python3
"""Provider-free connected lifecycle acceptance using staged package + actual Pi."""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


FAKE_BUILTIN_PI = r"""import { pathToFileURL } from "node:url";
import readline from "node:readline";

const argv = process.argv.slice(2);
const extensionIndex = argv.indexOf("--extension");
if (extensionIndex < 0 || !argv[extensionIndex + 1]) throw new Error("missing_worker_extension");
const extension = await import(pathToFileURL(argv[extensionIndex + 1]).href);
const handlers = new Map();
const entries = [];
let activeTools = argv.includes("--tools") ? argv[argv.indexOf("--tools") + 1].split(",") : [];
const context = {
  sessionManager: { getEntries: () => entries },
  getContextUsage: () => undefined,
  isIdle: () => true,
  abort: () => {},
};
const pi = {
  registerTool() {},
  on(name, handler) { handlers.set(name, handler); },
  getActiveTools() { return [...activeTools]; },
  setActiveTools(names) { activeTools = [...names]; },
  appendEntry(customType, data) { entries.push({ type: "custom", customType, data }); },
  sendMessage() {},
};
extension.default(pi);
handlers.get("session_start")?.({}, context);
let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true;
  handlers.get("session_shutdown")?.();
  process.exit(0);
}
process.on("SIGHUP", shutdown);
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
if (argv.includes("--mode")) {
  const input = readline.createInterface({ input: process.stdin });
  input.on("line", (line) => {
    const value = JSON.parse(line);
    process.stdout.write(JSON.stringify({
      type: "response", command: value.type, success: true,
      ...(value.id === undefined ? {} : { id: value.id }),
      ...(value.type === "get_state" ? { data: { sessionId: "fixture-builtin", isStreaming: false } } : {}),
    }) + "\n");
  });
}
"""

PI_WRAPPER = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

role = os.environ.get("PI_TMUX_ORCHESTRATOR_ROLE", "")
if role.startswith("custom-"):
    record = {
        "argv": sys.argv[1:],
        "contract": os.environ.get("PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT"),
        "generation": int(os.environ["PI_TMUX_ORCHESTRATOR_GENERATION"]),
        "mode": "rpc" if "--mode" in sys.argv else "tui",
        "pid": os.getpid(),
        "pi": str(Path(os.environ["ACTUAL_PI_EXECUTABLE"]).resolve()),
    }
    with open(os.environ["ACTUAL_PI_LAUNCH_RECORD"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    actual = os.environ["ACTUAL_PI_EXECUTABLE"]
    os.execv(actual, [actual, "--offline", *sys.argv[1:]])
node = os.environ["ACTUAL_PI_NODE_EXECUTABLE"]
os.execv(node, [node, os.environ["ACTUAL_PI_FAKE_BUILTIN"], *sys.argv[1:]])
"""

BROKER_SHIM = r"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys

sys.path.insert(0, os.environ["ACTUAL_PI_PACKAGE_ROOT"])
from pi_tmux_orchestrator import constants
from pi_tmux_orchestrator.cli import main
from pi_tmux_orchestrator.role_registry import valid_custom_role_id

role = os.environ.get("ACTUAL_PI_CUSTOM_ROLE", "")
if not valid_custom_role_id(role):
    raise SystemExit("test-only custom broker role is invalid")
constants.KNOWN_ROLES = constants.KNOWN_ROLES | {role}
raise SystemExit(main())
"""


class RequestSentinel(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), SentinelHandler)
        self.requests = 0


class SentinelHandler(http.server.BaseHTTPRequestHandler):
    def _reject(self) -> None:
        self.server.requests += 1  # type: ignore[attr-defined]
        self.send_response(503)
        self.end_headers()

    do_GET = _reject
    do_POST = _reject
    do_PUT = _reject
    do_DELETE = _reject

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def resource(path: Path, content: str) -> dict[str, str]:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def wait_for(predicate, message: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def role_connection(coord: Path, role: str) -> tuple[int, int, str]:
    from pi_tmux_orchestrator import broker_store

    with broker_store.connect_broker_database(coord, readonly=True) as database:
        row = database.execute(
            "SELECT connected,generation,state FROM roles WHERE role=?", (role,)
        ).fetchone()
    return int(row["connected"]), int(row["generation"]), str(row["state"])


def recovery_counts(coord: Path) -> tuple[int, int, int]:
    from pi_tmux_orchestrator import broker_store

    with broker_store.connect_broker_database(coord, readonly=True) as database:
        starts = database.execute(
            "SELECT COUNT(*) FROM events WHERE event='broker_started'"
        ).fetchone()[0]
        connected = database.execute(
            "SELECT COUNT(*) FROM roles WHERE connected=1"
        ).fetchone()[0]
        connections = database.execute(
            "SELECT COUNT(*) FROM events WHERE event='worker_connected'"
        ).fetchone()[0]
    return starts, connected, connections


def respawn_broker(manifest: dict[str, object], coord: Path, wrapper: Path) -> None:
    pane_id = str(manifest["monitor_pane_id"])
    pane_pid = int(
        subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    )
    os.kill(pane_pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        dead = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_dead}"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        if dead == "1":
            break
        time.sleep(0.05)
    else:
        os.kill(pane_pid, signal.SIGKILL)
        wait_for(
            lambda: subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_dead}"],
                check=False,
                text=True,
                capture_output=True,
            ).stdout.strip()
            == "1",
            "previous broker process did not terminate",
            timeout=3,
        )
    command = shlex.join(
        [
            str(wrapper),
            "_broker",
            "--state-root",
            str(coord.parent.parent),
            "--coord",
            str(coord),
        ]
    )
    subprocess.run(
        ["tmux", "respawn-pane", "-t", pane_id, command],
        check=True,
        text=True,
        capture_output=True,
    )


def assert_launch(
    launch: dict[str, object], transport: str, package_root: Path, snapshot_skill: str
) -> None:
    argv = launch["argv"]
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise AssertionError("actual Pi launch arguments were not recorded safely")
    expected_tools = "read,grep,find,ls,orchestrator_report"
    expected_extension = str(package_root / "extensions" / "orchestrator-worker.js")
    if (
        launch["mode"] != transport
        or launch["contract"] != "probe"
        or argv[argv.index("--tools") + 1] != expected_tools
        or "--no-extensions" not in argv
        or "--no-prompt-templates" not in argv
        or "--no-skills" not in argv
        or argv.count("--extension") != 1
        or argv[argv.index("--extension") + 1] != expected_extension
        or argv.count("--skill") != 1
        or argv[argv.index("--skill") + 1] != snapshot_skill
        or ("--mode" in argv) != (transport == "rpc")
    ):
        raise AssertionError(f"actual Pi custom bootstrap policy drifted: {launch!r}")


def run_transport(
    root: Path,
    package_root: Path,
    transport: str,
    sentinel: RequestSentinel,
) -> None:
    from pi_tmux_orchestrator import broker_store, commands, constants, runtime
    from pi_tmux_orchestrator.configuration import public_project_config
    from pi_tmux_orchestrator.custom_role_resources import select_custom_start
    from pi_tmux_orchestrator.models import OrchestrationError
    from pi_tmux_orchestrator.storage import ensure_private_directory, secure_write

    role_name = "custom-security"
    session = f"pi-actual-custom-{transport}-{os.getpid()}"
    project = ensure_private_directory(root / f"project-{transport}")
    policy = ensure_private_directory(root / f"policy-{transport}")
    prompt_body = f"PRIVATE_ACTUAL_PI_PROMPT_{transport.upper()}\n"
    skill_body = (
        "---\nname: actual-pi-custom\n"
        f"description: PRIVATE_ACTUAL_PI_SKILL_{transport.upper()}\n"
        "allowed-tools: write edit bash\n---\nRead-only fixture.\n"
    )
    prompt = resource(policy / "prompt.md", prompt_body)
    skill = resource(policy / "skill.md", skill_body)
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
        [[role_name, "actual-pi-fixture", "no-inference", "off"]],
        str(registry),
    )
    roles = ["implementer", "reviewer", role_name]
    configs = {
        "implementer": {
            "provider": "actual-pi-fixture",
            "model": "no-inference",
            "thinking": "off",
            "tools": None,
            "pane_id": None,
            "skills": [],
        },
        "reviewer": {
            "provider": "actual-pi-fixture",
            "model": "no-inference",
            "thinking": "off",
            "tools": constants.READ_ONLY_TOOLS,
            "pane_id": None,
            "skills": [],
        },
        **selected["roles"],
    }
    state_root = ensure_private_directory(root / "state")
    coord = ensure_private_directory(state_root / session / "run-1", parents=True)
    manifest = commands.construct_start_manifest(
        coord,
        project,
        session,
        transport,
        approve_project=True,
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
        json.dumps({"task": "PRIVATE_ACTUAL_PI_TASK", "role_tasks": {}}),
    )
    for role, token in tokens.items():
        secure_write(coord / f"{role}.token", token + "\n")
    secure_write(coord / "control.token", control_token + "\n")

    launch_record = Path(os.environ["ACTUAL_PI_LAUNCH_RECORD"])
    launches_before = len(launch_records(launch_record))
    unsafe_marker = root / f"unexpected-discovered-extension-{transport}"
    unsafe_source = (
        'import {writeFileSync} from "node:fs"; export default function(){'
        f'writeFileSync({json.dumps(str(unsafe_marker))}, "unexpected");}}\n'
    )
    for extension_dir in (
        Path(os.environ["PI_CODING_AGENT_DIR"]) / "extensions",
        project / ".pi" / "extensions",
    ):
        extension_dir.mkdir(parents=True, exist_ok=True)
        (extension_dir / f"unsafe-{transport}.mjs").write_text(
            unsafe_source, encoding="utf-8"
        )

    socket_path = broker_store.broker_paths(coord)["socket"]
    actual_pids: list[int] = []
    original_script = runtime.SCRIPT_PATH
    runtime.SCRIPT_PATH = Path(os.environ["ACTUAL_PI_RUNTIME_WRAPPER"])
    try:
        secure_write(coord / "startup-state", "STARTING\n")
        commands.create_tmux_grid(session, project, coord, roles, manifest)
        try:
            commands.wait_for_custom_startup(session, coord, roles, manifest)
        except OrchestrationError as error:
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
            raise AssertionError(
                f"actual Pi {transport} startup failed: {error}; "
                f"snapshot={broker_store.public_broker_snapshot(coord)!r}; "
                f"launches={launch_records(launch_record)!r}; panes={panes!r}"
            ) from error
        secure_write(coord / "startup-state", "RUNNING\n")
        wait_for(
            lambda: len(launch_records(launch_record)) == launches_before + 1,
            f"actual Pi {transport} launch was not recorded",
        )
        first = launch_records(launch_record)[-1]
        actual_pids.append(int(first["pid"]))
        snapshot_skill = str(coord / f"{role_name}.skill-0.md")
        assert_launch(first, transport, package_root, snapshot_skill)
        if first["generation"] != 1 or not process_exists(actual_pids[-1]):
            raise AssertionError("actual Pi initial generation did not stay live")
        if (
            prompt_body
            not in (coord / f"{role_name}.system.md").read_text(encoding="utf-8")
            or (coord / f"{role_name}.skill-0.md").read_text(encoding="utf-8")
            != skill_body
        ):
            raise AssertionError("actual Pi did not receive exact verified snapshots")
        if unsafe_marker.exists():
            raise AssertionError(
                "actual Pi executed an unapproved discovered extension"
            )
        connected, generation, state = role_connection(coord, role_name)
        if connected != 1 or generation != 1 or state == "uncertain":
            raise AssertionError(
                "actual Pi custom identity did not authenticate stably"
            )
        if transport == constants.RPC_TRANSPORT:
            rpc_state = commands.rpc_role_paths(coord, role_name, create=False)["state"]
            wait_for(
                lambda: rpc_state.exists()
                and json.loads(rpc_state.read_text(encoding="utf-8")).get("session_id"),
                "actual Pi RPC supervisor did not complete get_state",
            )

        restart_args = argparse.Namespace(
            yes=True,
            session=session,
            role=role_name,
            provider=None,
            model=None,
            thinking=None,
            skip_model_check=True,
        )
        commands.restart_command(restart_args)
        wait_for(
            lambda: len(launch_records(launch_record)) == launches_before + 2
            and role_connection(coord, role_name)[:2] == (1, 2),
            f"actual Pi {transport} replacement did not authenticate",
        )
        second = launch_records(launch_record)[-1]
        actual_pids.append(int(second["pid"]))
        assert_launch(second, transport, package_root, snapshot_skill)
        if (
            second["generation"] != 2
            or second["pid"] == first["pid"]
            or not process_exists(actual_pids[-1])
        ):
            raise AssertionError("actual Pi restart did not advance exact generation")
        wait_for(
            lambda: not process_exists(int(first["pid"])),
            "previous actual Pi process survived confirmed restart",
        )
        if unsafe_marker.exists():
            raise AssertionError("restarted actual Pi executed discovered extension")

        starts, _, connections = recovery_counts(coord)
        stable_pid = actual_pids[-1]
        respawn_broker(manifest, coord, runtime.SCRIPT_PATH)
        wait_for(
            lambda: recovery_counts(coord)
            == (starts + 1, len(roles), connections + len(roles)),
            f"actual Pi {transport} workers did not reconnect to broker",
        )
        if (
            not process_exists(stable_pid)
            or role_connection(coord, role_name)[:2] != (1, 2)
            or len(launch_records(launch_record)) != launches_before + 2
        ):
            raise AssertionError("broker reconnect replaced or rebound actual Pi")

        Path(prompt["path"]).write_text(
            "REVOKED_ACTUAL_PI_RESOURCE\n", encoding="utf-8"
        )
        healthy_pid = actual_pids[-1]
        try:
            commands.restart_command(restart_args)
        except OrchestrationError:
            pass
        else:
            raise AssertionError("revoked custom resource reached actual Pi respawn")
        time.sleep(0.2)
        if (
            len(launch_records(launch_record)) != launches_before + 2
            or not process_exists(healthy_pid)
            or role_connection(coord, role_name)[:2] != (1, 2)
        ):
            raise AssertionError("revoked restart changed the healthy actual Pi worker")

        with broker_store.connect_broker_database(coord, readonly=True) as database:
            dump = "\n".join(database.iterdump())
            provider_calls = database.execute(
                "SELECT COALESCE(SUM(provider_calls),0) FROM roles"
            ).fetchone()[0]
        if provider_calls != 0:
            raise AssertionError("actual Pi lifecycle recorded provider calls")
        for canary in (
            "PRIVATE_ACTUAL_PI_TASK",
            prompt_body.strip(),
            f"PRIVATE_ACTUAL_PI_SKILL_{transport.upper()}",
        ):
            if canary in dump:
                raise AssertionError("private actual Pi body entered SQLite")
        print(
            f"OK staged actual Pi {transport.upper()} startup, broker reconnect, "
            "restart, and revocation boundary"
        )
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if socket_path.exists() or commands.session_exists(session):
            wait_for(
                lambda: not socket_path.exists()
                and not commands.session_exists(session)
                and recovery_counts(coord)[1] == 0,
                f"actual Pi {transport} cleanup left tmux/socket/connection state",
            )
        for pid in actual_pids:
            wait_for(
                lambda pid=pid: not process_exists(pid),
                f"actual Pi process {pid} survived exact session cleanup",
            )
        runtime.SCRIPT_PATH = original_script


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package_root")
    parser.add_argument("--pi")
    args = parser.parse_args()
    package_root = Path(args.package_root).resolve(strict=True)
    actual_pi = Path(args.pi or shutil.which("pi") or "")
    tmux = shutil.which("tmux")
    node = shutil.which("node")
    python = shutil.which("python3")
    if not actual_pi.is_file() or tmux is None or node is None or python is None:
        print(
            "SKIP connected actual-Pi custom lifecycle (pi/tmux/node/python3 unavailable)."
        )
        return 0
    if not (package_root / "pi_tmux_orchestrator" / "__init__.py").is_file():
        raise SystemExit("staged package root is incomplete")

    # Keep the isolated tmux socket below the Unix-domain path limit on macOS.
    with tempfile.TemporaryDirectory(prefix="pi-actual-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        directories = {
            name: root / name
            for name in (
                "home",
                "pi-home",
                "xdg-config",
                "xdg-cache",
                "xdg-data",
                "npm-cache",
                "npm-tmp",
                "tmux",
                "bin",
            )
        }
        for path in directories.values():
            path.mkdir(mode=0o700)
        user_npmrc = root / "user-npmrc"
        global_npmrc = root / "global-npmrc"
        user_npmrc.touch(mode=0o600)
        global_npmrc.touch(mode=0o600)
        fake_builtin = root / "fake-builtin.mjs"
        fake_builtin.write_text(FAKE_BUILTIN_PI, encoding="utf-8")
        pi_wrapper = directories["bin"] / "pi"
        pi_wrapper.write_text(PI_WRAPPER, encoding="utf-8")
        pi_wrapper.chmod(0o700)
        broker_shim = root / "gated-broker.py"
        broker_shim.write_text(BROKER_SHIM, encoding="utf-8")
        broker_shim.chmod(0o700)
        launch_record = root / "actual-pi-launches.jsonl"
        wrapper = root / "runtime.sh"

        sentinel = RequestSentinel()
        sentinel_thread = threading.Thread(target=sentinel.serve_forever, daemon=True)
        sentinel_thread.start()
        port = sentinel.server_address[1]
        models = {
            "providers": {
                "actual-pi-fixture": {
                    "baseUrl": f"http://127.0.0.1:{port}/v1",
                    "api": "openai-completions",
                    "apiKey": "nonsecret-lifecycle-fixture",
                    "models": [{"id": "no-inference", "reasoning": False}],
                }
            }
        }
        (directories["pi-home"] / "models.json").write_text(
            json.dumps(models), encoding="utf-8"
        )
        (directories["pi-home"] / "models.json").chmod(0o600)

        path_parts = [
            str(directories["bin"]),
            str(Path(node).parent),
            str(Path(python).parent),
            str(Path(tmux).parent),
            "/usr/bin",
            "/bin",
        ]
        safe_path = ":".join(dict.fromkeys(path_parts))
        exact_environment = {
            "PATH": safe_path,
            "HOME": str(directories["home"]),
            "PI_CODING_AGENT_DIR": str(directories["pi-home"]),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "PI_TMUX_AGENTS_HOME": str(root / "state"),
            "XDG_CONFIG_HOME": str(directories["xdg-config"]),
            "XDG_CACHE_HOME": str(directories["xdg-cache"]),
            "XDG_DATA_HOME": str(directories["xdg-data"]),
            "NPM_CONFIG_USERCONFIG": str(user_npmrc),
            "NPM_CONFIG_GLOBALCONFIG": str(global_npmrc),
            "NPM_CONFIG_CACHE": str(directories["npm-cache"]),
            "NPM_CONFIG_OFFLINE": "true",
            "TMPDIR": str(directories["npm-tmp"]),
            "TMUX_TMPDIR": str(directories["tmux"]),
            "TERM": "xterm-256color",
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1",
            "ACTUAL_PI_EXECUTABLE": str(actual_pi.resolve()),
            "ACTUAL_PI_NODE_EXECUTABLE": str(Path(node).resolve()),
            "ACTUAL_PI_FAKE_BUILTIN": str(fake_builtin),
            "ACTUAL_PI_LAUNCH_RECORD": str(launch_record),
            "ACTUAL_PI_PACKAGE_ROOT": str(package_root),
            "ACTUAL_PI_CUSTOM_ROLE": "custom-security",
            "ACTUAL_PI_RUNTIME_WRAPPER": str(wrapper),
        }
        env_args = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in exact_environment.items()
        )
        package_cli = package_root / "bin" / "pi-tmux-agents"
        wrapper.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            'if [[ "$1" == "_run-agent" ]]; then\n'
            f' exec env -i {env_args} {shlex.quote(str(package_cli))} "$@"\n'
            "fi\n"
            'if [[ "$1" == "_broker" ]]; then\n'
            f" exec env -i {env_args} {shlex.quote(str(Path(python).resolve()))} "
            f'{shlex.quote(str(broker_shim))} "$@"\n'
            "fi\nexit 2\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)

        original_environment = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(exact_environment)
            sys.path.insert(0, str(package_root))
            from pi_tmux_orchestrator import runtime

            runtime.STATE_ROOT = root / "state"
            runtime.SCRIPT_PATH = wrapper
            for transport in ("tui", "rpc"):
                run_transport(root, package_root, transport, sentinel)
        finally:
            os.environ.clear()
            os.environ.update(original_environment)
            sentinel.shutdown()
            sentinel.server_close()
            sentinel_thread.join(timeout=3)
        if sentinel.requests:
            raise AssertionError(
                f"actual Pi attempted {sentinel.requests} provider request(s)"
            )
    print(
        "Connected staged-package actual-Pi TUI/RPC custom startup, broker reconnect, "
        "fresh restart, revoked-resource rejection, and exact cleanup passed without "
        "a prompt, credential access, or provider request; report inference was not exercised."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
