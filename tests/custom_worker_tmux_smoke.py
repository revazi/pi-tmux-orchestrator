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
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pi_tmux_orchestrator import broker_store, commands, constants, runtime  # noqa: E402
from pi_tmux_orchestrator.configuration import public_project_config  # noqa: E402
from pi_tmux_orchestrator.custom_role_resources import select_custom_start  # noqa: E402
from pi_tmux_orchestrator.models import OrchestrationError  # noqa: E402
from pi_tmux_orchestrator.storage import (  # noqa: E402
    ensure_private_directory,
    secure_write,
)

FAKE_PI = r"""#!/usr/bin/env node
import { appendFileSync, existsSync, unlinkSync } from "node:fs";
import { pathToFileURL } from "node:url";
import readline from "node:readline";

const argv = process.argv.slice(2);
const role = process.env.PI_TMUX_ORCHESTRATOR_ROLE;
const mode = argv.includes("--mode") ? "rpc" : "tui";
const extensionIndex = argv.indexOf("--extension");
if (extensionIndex < 0 || !argv[extensionIndex + 1]) throw new Error("missing_worker_extension");
const extension = await import(pathToFileURL(argv[extensionIndex + 1]).href);
const handlers = new Map();
const entries = [];
let reportTool;
let pendingReportKind;
let reporting = false;
const holdMarker = process.env.CUSTOM_WORKER_HOLD_MARKER;
const releaseMarker = process.env.CUSTOM_WORKER_RELEASE_MARKER;
const exitBeforeAckMarker = process.env.CUSTOM_WORKER_EXIT_BEFORE_ACK_MARKER;
const eventRecord = process.env.CUSTOM_WORKER_EVENT_RECORD;
let activeTools = argv.includes("--tools")
  ? argv[argv.indexOf("--tools") + 1].split(",")
  : [];
const context = {
  sessionManager: { getEntries: () => entries },
  getContextUsage: () => undefined,
  isIdle: () => true,
  abort: () => {},
};

function fail(error) {
  console.error(error?.stack || String(error));
  process.exitCode = 2;
  setTimeout(() => process.exit(2), 10);
}

function recordEvent(value) {
  if (!eventRecord) return;
  appendFileSync(eventRecord, JSON.stringify({ role, generation: Number(process.env.PI_TMUX_ORCHESTRATOR_GENERATION), ...value }) + "\n");
}

async function submitReport(kind, context) {
  if (reporting || !kind) return;
  reporting = true;
  pendingReportKind = undefined;
  try {
    const report = { kind, summary: "SYNTHETIC_CUSTOM_WORKER_REPORT" };
    if (kind === "review") report.verdict = "approved";
    else if (kind === "playwright") report.verdict = "pass";
    else if (kind === "django") report.verdict = "advisory_approved";
    await handlers.get("turn_start")?.({}, context);
    await reportTool.execute("synthetic-report", report, undefined, undefined, context);
    await handlers.get("agent_settled")?.({}, context);
    recordEvent({ event: "report", kind });
  } catch (error) {
    fail(error);
  } finally {
    reporting = false;
  }
}

const pi = {
  registerTool(tool) { reportTool = tool; },
  on(name, handler) { handlers.set(name, handler); },
  getActiveTools() { return [...activeTools]; },
  setActiveTools(names) { activeTools = [...names]; },
  appendEntry(customType, data) {
    entries.push({ type: "custom", customType, data });
  },
  sendMessage(message) {
    if (message?.details?.kind !== "assignment") return;
    const kind = message.details.assignment_kind;
    recordEvent({
      event: "assignment",
      kind,
      assignment_id: message.details.assignment_id,
      delivery_id: message.details.delivery_id,
    });
    if (role.startsWith("custom-") && exitBeforeAckMarker && existsSync(exitBeforeAckMarker)) {
      unlinkSync(exitBeforeAckMarker);
      process.exit(0);
    }
    if (role.startsWith("custom-") && holdMarker && existsSync(holdMarker)) {
      pendingReportKind = kind;
      return;
    }
    setImmediate(() => submitReport(kind, context));
  },
};

extension.default(pi);
handlers.get("session_start")?.({}, context);
setInterval(() => {
  if (!pendingReportKind || !releaseMarker || !existsSync(releaseMarker)) return;
  unlinkSync(releaseMarker);
  if (holdMarker && existsSync(holdMarker)) unlinkSync(holdMarker);
  submitReport(pendingReportKind, context);
}, 25);
appendFileSync(
  process.env.CUSTOM_WORKER_RECORD,
  JSON.stringify({
    role,
    mode,
    argv,
    contract: process.env.PI_TMUX_ORCHESTRATOR_SPECIALIST_CONTRACT,
    generation: Number(process.env.PI_TMUX_ORCHESTRATOR_GENERATION),
    activeTools,
  }) + "\n",
  { encoding: "utf8" },
);

let stopping = false;
function shutdown() {
  if (stopping) return;
  stopping = true;
  handlers.get("session_shutdown")?.();
  process.exit(process.exitCode || 0);
}
process.on("SIGHUP", shutdown);
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

if (mode === "rpc") {
  const input = readline.createInterface({ input: process.stdin });
  input.on("line", (line) => {
    const value = JSON.parse(line);
    const response = {
      type: "response",
      command: value.type,
      success: true,
      ...(value.id === undefined ? {} : { id: value.id }),
      ...(value.type === "get_state"
        ? { data: { sessionId: "synthetic-custom-rpc", isStreaming: false } }
        : {}),
    };
    process.stdout.write(JSON.stringify(response) + "\n");
  });
}
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


def broker_recovery_state(coord: Path) -> tuple[int, int, int]:
    with broker_store.connect_broker_database(coord, readonly=True) as database:
        starts = database.execute(
            "SELECT COUNT(*) FROM events WHERE event='broker_started'"
        ).fetchone()[0]
        connected = database.execute(
            "SELECT COUNT(*) FROM roles WHERE connected=1"
        ).fetchone()[0]
        connection_events = database.execute(
            "SELECT COUNT(*) FROM events WHERE event='worker_connected'"
        ).fetchone()[0]
    return starts, connected, connection_events


def active_assignment_state(coord: Path, role: str) -> dict[str, object] | None:
    with broker_store.connect_broker_database(coord, readonly=True) as database:
        row = database.execute(
            "SELECT assignments.id,assignments.delivery_id,assignments.state "
            "FROM roles JOIN assignments ON assignments.id=roles.active_assignment_id "
            "WHERE roles.role=?",
            (role,),
        ).fetchone()
    return dict(row) if row is not None else None


def report_counts(coord: Path) -> tuple[int, int]:
    with broker_store.connect_broker_database(coord, readonly=True) as database:
        reports = database.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        reviewer = database.execute(
            "SELECT COUNT(*) FROM assignments WHERE role='reviewer'"
        ).fetchone()[0]
    return reports, reviewer


def recorded_events(path: Path, role: str, event: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        value
        for value in (json.loads(line) for line in path.read_text().splitlines())
        if value["role"] == role and value["event"] == event
    ]


def respawn_worker(
    pane_id: str, coord: Path, state_root: Path, wrapper: Path, role: str
) -> None:
    command = shlex.join(
        [
            str(wrapper),
            "_run-agent",
            "--state-root",
            str(state_root),
            "--coord",
            str(coord),
            "--role",
            role,
        ]
    )
    subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", pane_id, command],
        check=True,
        text=True,
        capture_output=True,
    )


def respawn_broker(manifest: dict[str, object], coord: Path, wrapper: Path) -> None:
    pane_id = manifest["monitor_pane_id"]
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
        # The acceptance target includes abrupt broker loss. Bound graceful
        # shutdown, then terminate only the exact process hosted by this pane.
        os.kill(pane_pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
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
            raise AssertionError("previous broker process did not terminate")
    broker_command = shlex.join(
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
        ["tmux", "respawn-pane", "-t", pane_id, broker_command],
        check=True,
        text=True,
        capture_output=True,
    )


async def run_transport(
    root: Path,
    transport: str,
    *,
    interrupted: bool = False,
    startup_failure: bool = False,
) -> None:
    scenario = (
        "startup-failure"
        if startup_failure
        else ("uncertain" if interrupted else "accepted")
    )
    session = f"pi-custom-{transport}-{scenario}-{os.getpid()}"
    project = ensure_private_directory(root / f"project-{transport}-{scenario}")
    policy = ensure_private_directory(root / f"policy-{transport}-{scenario}")
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
    record_path = root / f"launch-{transport}-{scenario}.jsonl"
    event_path = root / f"events-{transport}-{scenario}.jsonl"
    hold_marker = root / f"hold-{transport}-{scenario}"
    release_marker = root / f"release-{transport}-{scenario}"
    exit_marker = root / f"exit-before-ack-{transport}-{scenario}"
    hold_marker.touch(mode=0o600)
    wrapper = root / f"runtime-{transport}-{scenario}.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'if [[ "$1" == "_run-agent" ]]; then\n'
        f" export PATH={shlex.quote(str(root))}:$PATH\n"
        f" export CUSTOM_WORKER_RECORD={shlex.quote(str(record_path))}\n"
        f" export CUSTOM_WORKER_EVENT_RECORD={shlex.quote(str(event_path))}\n"
        f" export CUSTOM_WORKER_HOLD_MARKER={shlex.quote(str(hold_marker))}\n"
        f" export CUSTOM_WORKER_RELEASE_MARKER={shlex.quote(str(release_marker))}\n"
        f" export CUSTOM_WORKER_EXIT_BEFORE_ACK_MARKER={shlex.quote(str(exit_marker))}\n"
        f' exec {shlex.quote(str(ROOT / "bin" / "pi-tmux-agents"))} "$@"\n'
        "fi\n"
        'if [[ "$1" == "_broker" ]]; then\n'
        f" export CUSTOM_GATED_BROKER_ROLE={shlex.quote(role_name)}\n"
        f' exec {shlex.quote(sys.executable)} {shlex.quote(str(ROOT / "tests" / "gated_custom_broker.py"))} "$@"\n'
        "fi\nexec sleep 60\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    runtime.SCRIPT_PATH = wrapper
    collision_session = f"{session}-keep" if startup_failure else None
    try:
        if collision_session is not None:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", collision_session, "sleep 60"],
                check=True,
                text=True,
                capture_output=True,
            )
        secure_write(coord / "startup-state", "STARTING\n")
        if startup_failure:
            Path(prompt["path"]).write_text("REVOKED BEFORE LAUNCH\n", encoding="utf-8")
        commands.create_tmux_grid(session, project, coord, roles, manifest)
        if startup_failure:
            try:
                await asyncio.to_thread(
                    commands.wait_for_custom_startup,
                    session,
                    coord,
                    roles,
                    manifest,
                    timeout=2,
                )
            except OrchestrationError as error:
                if error.code != "startup_failed":
                    raise
            else:
                raise AssertionError("revoked custom startup passed admission")
            await asyncio.to_thread(commands.rollback_partial_start, session, coord)
            secure_write(coord / "startup-state", "FAILED\n")
            await wait_for(
                lambda: (
                    not commands.session_exists(session)
                    and not broker_store.broker_paths(coord)["socket"].exists()
                    and broker_recovery_state(coord)[1] == 0
                ),
                f"failed custom {transport} startup left runtime state",
            )
            if (
                collision_session is None
                or not commands.session_exists(collision_session)
                or (coord / "startup-state").read_text() != "FAILED\n"
            ):
                raise AssertionError(
                    "failed custom startup affected its prefix collision or retained state"
                )
            with broker_store.connect_broker_database(coord, readonly=True) as database:
                dump = "\n".join(database.iterdump())
            for canary in (
                "SYNTHETIC CUSTOM TMUX SMOKE",
                "PRIVATE_CUSTOM_PROMPT_CANARY",
                "PRIVATE_CUSTOM_SKILL_CANARY",
            ):
                if canary in dump:
                    raise AssertionError(
                        "failed custom startup retained a private body"
                    )
            print(f"OK failed custom {transport.upper()} startup rolled back exactly")
            return
        await asyncio.to_thread(
            commands.wait_for_custom_startup,
            session,
            coord,
            roles,
            manifest,
        )
        if manifest["version"] != 6 or manifest["custom_role_registry"] != str(
            registry
        ):
            raise AssertionError("custom launch did not retain manifest v6 binding")
        try:
            await wait_for(
                lambda: (
                    active_assignment_state(coord, role_name) is not None
                    and active_assignment_state(coord, role_name)["state"] == "accepted"
                    and role_state(coord, role_name)
                    == {"state": "active", "connected": 1, "generation": 1}
                ),
                f"{transport} custom assignment was not held after acknowledgement",
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
            events = (
                event_path.read_text(encoding="utf-8") if event_path.exists() else ""
            )
            raise AssertionError(
                f"{error}; snapshot={snapshot!r}; launches={launches!r}; "
                f"events={events!r}; panes={panes!r}"
            ) from error
        initial_assignment = active_assignment_state(coord, role_name)
        if initial_assignment is None or report_counts(coord) != (1, 0):
            raise AssertionError("custom assignment routed reviewer work before report")
        records = [json.loads(line) for line in record_path.read_text().splitlines()]
        custom = [value for value in records if value["role"] == role_name]
        if (
            len(custom) != 1
            or custom[0]["mode"] != transport
            or custom[0]["generation"] != 1
        ):
            raise AssertionError(f"unexpected custom launch records: {custom!r}")
        launch = custom[0]
        if launch["contract"] != "probe":
            raise AssertionError("custom contract was not launch-bound")
        argv = launch["argv"]
        expected_tools = "read,grep,find,ls,orchestrator_report"
        if (
            argv[argv.index("--tools") + 1] != expected_tools
            or launch["activeTools"] != expected_tools.split(",")
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

        pane_id = manifest["roles"][role_name]["pane_id"]
        restart_args = argparse.Namespace(
            yes=True,
            session=session,
            role=role_name,
            provider=None,
            model=None,
            thinking=None,
            skip_model_check=True,
        )
        starts_before, _, connections_before = broker_recovery_state(coord)
        if interrupted:
            await asyncio.to_thread(
                commands.broker_control_request, coord, role_name, "restart"
            )
            await wait_for(
                lambda: role_state(coord, role_name)
                == {"state": "restarting", "connected": 0, "generation": 2},
                f"stale {transport} generation did not disconnect after restart admission",
            )
            await asyncio.sleep(0.5)
            pane_status = subprocess.run(
                ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_dead}"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            if (
                pane_status != "0"
                or role_state(coord, role_name)
                != {"state": "restarting", "connected": 0, "generation": 2}
                or len(recorded_events(event_path, role_name, "assignment")) != 1
            ):
                raise AssertionError(
                    "live stale-generation worker reconnected or received more work"
                )

            exit_marker.touch(mode=0o600)
            respawn_worker(pane_id, coord, state_root, wrapper, role_name)
            try:
                await wait_for(
                    lambda: role_state(coord, role_name)
                    == {"state": "uncertain", "connected": 0, "generation": 2},
                    f"interrupted {transport} replacement delivery was not uncertain",
                )
            except AssertionError as error:
                raise AssertionError(
                    f"{error}; role={role_state(coord, role_name)!r}; "
                    f"assignment={active_assignment_state(coord, role_name)!r}; "
                    f"launches={record_path.read_text(encoding='utf-8')!r}; "
                    f"events={event_path.read_text(encoding='utf-8')!r}; "
                    f"exit_marker={exit_marker.exists()}"
                ) from error
            if workflow_state(coord) != "uncertain" or report_counts(coord) != (1, 0):
                raise AssertionError("interrupted replacement routed or completed work")

            respawn_worker(pane_id, coord, state_root, wrapper, role_name)
            try:
                await wait_for(
                    lambda: (
                        role_state(coord, role_name)
                        == {"state": "uncertain", "connected": 1, "generation": 2}
                        and active_assignment_state(coord, role_name) is not None
                        and active_assignment_state(coord, role_name)["state"]
                        == "uncertain"
                    ),
                    f"current {transport} generation did not reconnect as uncertain",
                )
            except AssertionError as error:
                raise AssertionError(
                    f"{error}; role={role_state(coord, role_name)!r}; "
                    f"assignment={active_assignment_state(coord, role_name)!r}; "
                    f"launches={record_path.read_text(encoding='utf-8')!r}; "
                    f"events={event_path.read_text(encoding='utf-8')!r}"
                ) from error
            assignment_events = recorded_events(event_path, role_name, "assignment")
            if (
                len(assignment_events) != 2
                or assignment_events[0]["assignment_id"]
                != assignment_events[1]["assignment_id"]
                or assignment_events[0]["delivery_id"]
                == assignment_events[1]["delivery_id"]
                or [value["generation"] for value in assignment_events] != [1, 2]
            ):
                raise AssertionError(
                    f"interrupted replacement identity drifted: {assignment_events!r}"
                )

            starts_before, _, connections_before = broker_recovery_state(coord)
            respawn_broker(manifest, coord, wrapper)
            try:
                await wait_for(
                    lambda: broker_recovery_state(coord)
                    == (starts_before + 1, len(roles), connections_before + len(roles)),
                    f"uncertain {transport} workers did not reconnect after broker restart",
                )
            except AssertionError as error:
                raise AssertionError(
                    f"{error}; recovery={broker_recovery_state(coord)!r}; "
                    f"expected={(starts_before + 1, len(roles), connections_before + len(roles))!r}; "
                    f"role={role_state(coord, role_name)!r}"
                ) from error
            release_marker.touch(mode=0o600)
            await asyncio.sleep(0.3)
            if (
                workflow_state(coord) != "uncertain"
                or role_state(coord, role_name)
                != {"state": "uncertain", "connected": 1, "generation": 2}
                or report_counts(coord) != (1, 0)
                or len(recorded_events(event_path, role_name, "assignment")) != 2
            ):
                raise AssertionError(
                    "uncertain custom assignment replayed, reported, or routed review"
                )
            with broker_store.connect_broker_database(coord, readonly=True) as database:
                dump = "\n".join(database.iterdump())
            for canary in (
                "SYNTHETIC CUSTOM TMUX SMOKE",
                "SYNTHETIC_CUSTOM_WORKER_REPORT",
                "PRIVATE_CUSTOM_PROMPT_CANARY",
                "PRIVATE_CUSTOM_SKILL_CANARY",
            ):
                if canary in dump:
                    raise AssertionError("private interrupted payload entered SQLite")
            print(
                f"OK custom {transport.upper()} stale generation and uncertain handover survived broker recovery"
            )
            return

        await asyncio.to_thread(commands.restart_command, restart_args)
        await wait_for(
            lambda: (
                role_state(coord, role_name)
                == {"state": "active", "connected": 1, "generation": 2}
                and active_assignment_state(coord, role_name) is not None
                and active_assignment_state(coord, role_name)["state"] == "accepted"
            ),
            f"accepted {transport} assignment did not survive worker restart",
        )
        replacement_assignment = active_assignment_state(coord, role_name)
        replacement_events = recorded_events(event_path, role_name, "assignment")
        if (
            replacement_assignment is None
            or replacement_assignment["id"] != initial_assignment["id"]
            or replacement_assignment["delivery_id"]
            == initial_assignment["delivery_id"]
            or [value["generation"] for value in replacement_events] != [1, 2]
        ):
            raise AssertionError(
                f"accepted replacement identity drifted: {replacement_events!r}"
            )
        initial_assignment = replacement_assignment
        starts_before, _, connections_before = broker_recovery_state(coord)
        respawn_broker(manifest, coord, wrapper)
        try:
            await wait_for(
                lambda: (
                    broker_recovery_state(coord)
                    == (starts_before + 1, len(roles), connections_before + len(roles))
                    and active_assignment_state(coord, role_name) == initial_assignment
                    and role_state(coord, role_name)
                    == {"state": "active", "connected": 1, "generation": 2}
                ),
                f"accepted {transport} assignment did not reconnect with stable identity",
            )
        except AssertionError as error:
            raise AssertionError(
                f"{error}; recovery={broker_recovery_state(coord)!r}; "
                f"assignment={active_assignment_state(coord, role_name)!r}; "
                f"initial={initial_assignment!r}; role={role_state(coord, role_name)!r}; "
                f"events={recorded_events(event_path, role_name, 'assignment')!r}"
            ) from error
        if len(recorded_events(event_path, role_name, "assignment")) != 2:
            raise AssertionError("accepted custom assignment was redelivered to Pi")
        release_marker.touch(mode=0o600)
        await wait_for(
            lambda: workflow_state(coord) == "ready",
            f"{transport} custom worker workflow did not reach independent approval",
        )

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

        await wait_for(
            lambda: role_state(coord, role_name)["connected"] == 1,
            f"custom {transport} worker disconnected after accepted report",
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

        starts_before, _, connections_before = broker_recovery_state(coord)
        respawn_broker(manifest, coord, wrapper)
        await wait_for(
            lambda: broker_recovery_state(coord)
            == (starts_before + 1, len(roles), connections_before + len(roles)),
            f"custom {transport} workers did not reconnect to the replacement broker",
        )
        if workflow_state(coord) != "ready" or role_state(coord, role_name) != {
            "state": "idle",
            "connected": 1,
            "generation": 2,
        }:
            raise AssertionError(
                "broker recovery changed retained custom authority/state"
            )
        print(
            f"OK custom {transport.upper()} worker reached review, safe restart, and broker recovery"
        )
    finally:
        socket_path = broker_store.broker_paths(coord)["socket"]
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if collision_session is not None:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={collision_session}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        await wait_for(
            lambda: not socket_path.exists() and broker_recovery_state(coord)[1] == 0,
            f"custom {transport} tmux processes did not shut down cleanly",
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
            await run_transport(root, transport, interrupted=True)
            await run_transport(root, transport, startup_failure=True)


def main() -> int:
    asyncio.run(async_main())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
