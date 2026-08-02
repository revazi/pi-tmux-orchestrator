#!/usr/bin/env python3
"""Reusable Pi agent orchestration in tmux.

Uses only the Python standard library. Project changes are made only by the
implementer Pi process; coordination state lives outside project repositories.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Iterable

SCRIPT_PATH = Path(__file__).resolve()
STATE_ROOT = Path(
    os.environ.get(
        "PI_TMUX_AGENTS_HOME",
        str(Path.home() / ".pi" / "agent" / "orchestrations"),
    )
).expanduser()
VERSION = "0.1.0"
WINDOW = "agents"
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
DEFAULT_MODELS = {
    "implementer": {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "thinking": "xhigh",
    },
    "reviewer": {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "thinking": "high",
    },
    "probe": {
        "provider": "openai-codex",
        "model": "gpt-5.4-mini",
        "thinking": "high",
    },
}
READ_ONLY_TOOLS = "read,bash,grep,find,ls"
MAX_TASK_BYTES = 64 * 1024


class OrchestrationError(RuntimeError):
    pass


def eprint(*values: object) -> None:
    print(*values, file=sys.stderr)


def command_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise OrchestrationError(f"Required command is not available: {name}")
    return path


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def tmux(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run([command_path("tmux"), *args], check=check, capture=capture)


def secure_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    path.chmod(mode)


def save_manifest(coord: Path, manifest: dict[str, Any]) -> None:
    destination = coord / "manifest.json"
    temporary = coord / ".manifest.json.tmp"
    secure_write(temporary, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    temporary.replace(destination)
    destination.chmod(0o600)


def load_manifest(coord: Path) -> dict[str, Any]:
    try:
        with (coord / "manifest.json").open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise OrchestrationError(f"Cannot read orchestration manifest in {coord}: {error}") from error
    if value.get("version") != 1:
        raise OrchestrationError(f"Unsupported orchestration manifest version in {coord}")
    return value


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "project")[:48].rstrip("-")


def validate_session_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise OrchestrationError(
            "Session names may contain only letters, digits, underscores, dots, and hyphens"
        )
    return value


def session_exists(session: str) -> bool:
    result = tmux(["has-session", "-t", session], check=False, capture=True)
    return result.returncode == 0


def list_tmux_sessions() -> list[str]:
    result = tmux(["list-sessions", "-F", "#{session_name}"], check=False, capture=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def session_option(session: str, option: str) -> str | None:
    result = tmux(
        ["show-options", "-qv", "-t", session, option],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def orchestrated_sessions() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for session in list_tmux_sessions():
        coord = session_option(session, "@pi_agents_coord")
        if coord:
            found.append((session, Path(coord)))
    return found


def resolve_session(requested: str | None) -> tuple[str, Path]:
    if requested:
        if not session_exists(requested):
            raise OrchestrationError(f"tmux session does not exist: {requested}")
        coord = session_option(requested, "@pi_agents_coord")
        if not coord:
            raise OrchestrationError(f"tmux session was not created by pi-tmux-agents: {requested}")
        return requested, Path(coord)

    conventional = f"pi-{slugify(Path.cwd().name)}-agents"
    if session_exists(conventional):
        coord = session_option(conventional, "@pi_agents_coord")
        if coord:
            return conventional, Path(coord)

    sessions = orchestrated_sessions()
    if len(sessions) == 1:
        return sessions[0]
    if not sessions:
        raise OrchestrationError("No running pi-tmux-agents sessions were found")
    names = ", ".join(session for session, _ in sessions)
    raise OrchestrationError(f"Multiple orchestrations are running; specify one: {names}")


def read_text_argument(text: str | None, file_name: str | None, label: str) -> str:
    if text is not None and file_name is not None:
        raise OrchestrationError(f"Use either --{label} or --{label}-file, not both")
    if file_name is not None:
        source = Path(file_name).expanduser().resolve()
        try:
            value = source.read_text(encoding="utf-8")
        except OSError as error:
            raise OrchestrationError(f"Cannot read {label} file {source}: {error}") from error
    elif text is not None:
        value = text
    else:
        raise OrchestrationError(f"Provide --{label} or --{label}-file")
    if not value.strip():
        raise OrchestrationError(f"{label.capitalize()} cannot be empty")
    if len(value.encode("utf-8")) > MAX_TASK_BYTES:
        raise OrchestrationError(
            f"{label.capitalize()} exceeds the {MAX_TASK_BYTES // 1024} KiB safety limit"
        )
    return value.strip() + "\n"


def model_available(provider: str, model: str) -> tuple[bool, str]:
    pi = command_path("pi")
    result = run([pi, "--list-models", provider], check=False, capture=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or "pi --list-models failed"
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 2 and columns[0] == provider and columns[1] == model:
            return True, "available"
    return False, f"{provider}/{model} is not listed as available"


def validate_model(role: str, config: dict[str, str]) -> None:
    available, detail = model_available(config["provider"], config["model"])
    if not available:
        raise OrchestrationError(f"{role} model unavailable: {detail}")


def common_project_guidance(project: Path) -> str:
    return textwrap.dedent(
        f"""
        Project: `{project}`

        Before acting, discover and read all governing project instructions such as
        `AGENTS.md`, `CONTRIBUTING.md`, scoped instruction files, current-phase docs,
        and referenced design/workflow documents. Follow the closest applicable
        instructions. Work only on the task below and preserve intentional existing
        worktree changes; do not reset, stash, or discard them wholesale.
        """
    ).strip()


def join_prompt_sections(*sections: str) -> str:
    return "\n\n".join(section.strip() for section in sections if section.strip()) + "\n"


def implementer_prompt(project: Path, coord: Path, task: str) -> str:
    rules = textwrap.dedent(
        """
        ## Working rules

        - Start with a short plan before editing.
        - Make the smallest complete change that satisfies the task and project rules.
        - Keep behavior, tests, documentation, migrations, and public contracts aligned.
        - Use synthetic/non-secret fixtures unless the user explicitly authorized other data.
        - Do not expose credentials, private payloads, prompts, provider responses, or raw errors.
        - The reviewer and optional probe are read-only; do not ask them to edit source.
        - Do not push, merge, publish, or deploy unless the task explicitly requests it and
          repository workflow permits it. Never merge without explicit user approval.
        """
    )
    coordination = textwrap.dedent(
        f"""
        ## Coordination

        Coordination directory: `{coord}`

        1. Write `implementer.started.md` when you begin.
        2. If `probe.ready` appears, read `probe.md` and incorporate only valid findings.
        3. When implementation and required verification are ready, choose the next integer N,
           write `handoff-N.md`, then create `handoff-N.ready`.
        4. The handoff must list scope, changed files, exact commands/results, current git status,
           residual limitations, and decisions/tradeoffs without private payloads.
        5. Wait for `review-N.ready` and read `review-N.md`.
        6. If its first line is `CHANGES_REQUESTED`, address every valid finding, rerun checks,
           and submit round N+1.
        7. If its first line is `APPROVED`, write `implementation-ready.md` and stop before push,
           PR, or merge unless those actions were explicitly included in the approved task.
        8. Do not edit reviewer or probe reports.
        """
    )
    return join_prompt_sections(
        "# Role: primary implementer",
        "You are the sole agent permitted to modify tracked project files.",
        common_project_guidance(project),
        "## Task",
        task,
        rules,
        coordination,
        "Begin now and remain focused on this task.",
    )


def reviewer_prompt(project: Path, coord: Path, task: str) -> str:
    introduction = textwrap.dedent(
        """
        You are a read-only reviewer. Do not edit tracked files, commit, push, merge,
        publish, deploy, or access credentials/private project data. You may inspect files
        and run verification commands; generated output under ignored build/test paths is allowed.
        """
    )
    standard = textwrap.dedent(
        """
        ## Review standard

        - Treat tests as necessary but not sufficient; inspect actual behavior and boundaries.
        - Prioritize correctness, regressions, security/privacy, contract drift, missing tests,
          false acceptance claims, and violations of project instructions.
        - Confirm scope remains focused and existing intentional changes are preserved.
        - If a probe exists, evaluate its evidence and limitations rather than accepting it blindly.
        - Record concrete file/line references and acceptance conditions for every blocking finding.
        """
    )
    coordination = textwrap.dedent(
        f"""
        ## Coordination

        Coordination directory: `{coord}`

        1. Write `reviewer.started.md`, then wait for `handoff-1.ready` or a relay notification.
        2. For each round N, read `handoff-N.md`, inspect the current worktree diff, and run
           appropriate read-only verification.
        3. Write `review-N.md`. The first line must be exactly `APPROVED` or
           `CHANGES_REQUESTED`, then create `review-N.ready`.
        4. For changes requested, list findings in severity order and wait for round N+1.
        5. For approval, include verification evidence and residual limitations, create
           `reviewer.approved`, and remain available.
        6. Do not modify implementer/probe files or tracked project files.
        7. Never copy credentials, private payloads, prompts, or provider responses into reports.
        """
    )
    return join_prompt_sections(
        "# Role: independent reviewer",
        introduction,
        common_project_guidance(project),
        "## Task and acceptance target",
        task,
        standard,
        coordination,
        "Start by reading governing instructions and waiting for the first handoff.",
    )


def probe_prompt(project: Path, coord: Path, task: str, probe_task: str) -> str:
    introduction = textwrap.dedent(
        """
        You are a read-only investigation agent. Do not edit tracked files, commit, push,
        merge, publish, deploy, or access credentials/private project data. Use synthetic,
        inert inputs only unless the user explicitly authorized otherwise.
        """
    )
    rules = textwrap.dedent(
        """
        ## Probe rules

        - Independently inspect the relevant implementation, contracts, tests, and runtime boundary.
        - You may run local read-only tests or synthetic model/tool probes when explicitly allowed
          by the task, but never extract or forward Pi/provider credentials.
        - Distinguish semantic simulation, local validation, and exact production wire acceptance.
        - Do not claim equivalence or live acceptance that was not actually exercised.
        """
    )
    deliverable = textwrap.dedent(
        f"""
        ## Deliverable

        Coordination directory: `{coord}`

        Write `probe.md` with methods, evidence, file/line findings, minimal recommendations,
        regression-test suggestions, limitations, and a privacy confirmation. Then create
        `probe.ready` and remain available. Never include credentials, private payloads,
        prompts, provider responses, endpoints, or raw provider errors.
        """
    )
    return join_prompt_sections(
        "# Role: independent technical probe",
        introduction,
        common_project_guidance(project),
        "## Overall task context",
        task,
        "## Focused probe",
        probe_task,
        rules,
        deliverable,
    )


def role_config(args: argparse.Namespace, role: str) -> dict[str, Any]:
    defaults = DEFAULT_MODELS[role]
    config: dict[str, Any] = {
        "provider": getattr(args, f"{role}_provider") or defaults["provider"],
        "model": getattr(args, f"{role}_model") or defaults["model"],
        "thinking": getattr(args, f"{role}_thinking") or defaults["thinking"],
        "tools": None if role == "implementer" else READ_ONLY_TOOLS,
        "pane_id": None,
    }
    return config


def create_tmux_grid(
    session: str,
    project: Path,
    coord: Path,
    roles: list[str],
    manifest: dict[str, Any],
) -> None:
    total_panes = len(roles) + 1
    tmux(
        [
            "new-session",
            "-d",
            "-x",
            "240",
            "-y",
            "80",
            "-s",
            session,
            "-n",
            WINDOW,
            "-c",
            str(project),
        ]
    )
    try:
        for _ in range(total_panes - 1):
            tmux(["split-window", "-d", "-t", f"{session}:{WINDOW}", "-c", str(project)])
        tmux(["select-layout", "-t", f"{session}:{WINDOW}", "tiled"])
        tmux(["set-window-option", "-t", f"{session}:{WINDOW}", "remain-on-exit", "on"])
        tmux(["set-option", "-t", session, "pane-border-status", "top"])
        tmux(
            [
                "set-option",
                "-t",
                session,
                "pane-border-format",
                " #{pane_index} #{pane_title} ",
            ]
        )

        result = tmux(
            [
                "list-panes",
                "-t",
                f"{session}:{WINDOW}",
                "-F",
                "#{pane_index}\t#{pane_id}",
            ],
            capture=True,
        )
        panes: list[tuple[int, str]] = []
        for line in result.stdout.splitlines():
            index, pane_id = line.split("\t", 1)
            panes.append((int(index), pane_id))
        panes.sort()
        if len(panes) != total_panes:
            raise OrchestrationError("tmux created an unexpected number of panes")

        labels = [*roles, "monitor"]
        for label, (_, pane_id) in zip(labels, panes, strict=True):
            if label == "monitor":
                manifest["monitor_pane_id"] = pane_id
                title = "RELAY + STATUS"
            else:
                manifest["roles"][label]["pane_id"] = pane_id
                role = manifest["roles"][label]
                title = f"{label.upper()} · {role['provider']}/{role['model']} · {role['thinking']}"
            tmux(["select-pane", "-t", pane_id, "-T", title])

        tmux(["set-option", "-q", "-t", session, "@pi_agents_coord", str(coord)])
        tmux(["set-option", "-q", "-t", session, "@pi_agents_project", str(project)])
        tmux(["set-option", "-q", "-t", session, "@pi_agents_version", "1"])
        save_manifest(coord, manifest)

        for role_name in roles:
            pane_id = manifest["roles"][role_name]["pane_id"]
            command = shlex.join(
                [str(SCRIPT_PATH), "_run-agent", "--coord", str(coord), "--role", role_name]
            )
            tmux(["respawn-pane", "-k", "-t", pane_id, command])

        relay_command = shlex.join([str(SCRIPT_PATH), "_relay", "--coord", str(coord)])
        tmux(["respawn-pane", "-k", "-t", manifest["monitor_pane_id"], relay_command])
    except Exception:
        tmux(["kill-session", "-t", session], check=False)
        raise


def attach_session(session: str) -> None:
    if os.environ.get("TMUX"):
        tmux(["switch-client", "-t", session])
    else:
        os.execvp(command_path("tmux"), ["tmux", "attach", "-t", session])


def start_command(args: argparse.Namespace) -> int:
    command_path("pi")
    command_path("tmux")
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise OrchestrationError(f"Project directory does not exist: {project}")

    task = read_text_argument(args.task, args.task_file, "task")
    if args.with_probe:
        if args.probe_task is None and args.probe_task_file is None:
            probe_task = (
                "Independently investigate the highest-risk integration, contract, runtime, or "
                "security assumptions in the task. Produce actionable evidence for implementer "
                "and reviewer without modifying project files.\n"
            )
        else:
            probe_task = read_text_argument(args.probe_task, args.probe_task_file, "probe-task")
    else:
        if args.probe_task is not None or args.probe_task_file is not None:
            raise OrchestrationError("--probe-task requires --with-probe")
        probe_task = None

    session = validate_session_name(args.session or f"pi-{slugify(project.name)}-agents")
    if session_exists(session):
        raise OrchestrationError(
            f"tmux session already exists: {session}. Use status/stop or choose --session."
        )

    roles = ["implementer", "reviewer"]
    if args.with_probe:
        roles.append("probe")
    configs = {role: role_config(args, role) for role in roles}
    if not args.skip_model_check:
        for role, config in configs.items():
            validate_model(role, config)

    print(f"Project: {project}")
    print(f"Session: {session}")
    print("Roles:")
    for role in roles:
        config = configs[role]
        print(
            f"  {role}: {config['provider']}/{config['model']} "
            f"thinking={config['thinking']}"
        )
    print("  monitor: relay/status")
    print(f"Child project trust bypass: {'enabled' if args.approve_project else 'disabled'}")
    if args.dry_run:
        print("Dry run complete; no files, sessions, or model requests were created.")
        return 0

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_ROOT.chmod(0o700)
    session_root = STATE_ROOT / session
    session_root.mkdir(exist_ok=True)
    session_root.chmod(0o700)
    coord = session_root / f"{timestamp}-{os.getpid()}"
    coord.mkdir(exist_ok=False)
    coord.chmod(0o700)

    secure_write(coord / "task.md", task)
    if probe_task is not None:
        secure_write(coord / "probe-task.md", probe_task)

    prompt_paths = {
        "implementer": coord / "implementer.prompt.md",
        "reviewer": coord / "reviewer.prompt.md",
    }
    secure_write(prompt_paths["implementer"], implementer_prompt(project, coord, task))
    secure_write(prompt_paths["reviewer"], reviewer_prompt(project, coord, task))
    if probe_task is not None:
        prompt_paths["probe"] = coord / "probe.prompt.md"
        secure_write(prompt_paths["probe"], probe_prompt(project, coord, task, probe_task))

    manifest: dict[str, Any] = {
        "version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session": session,
        "window": WINDOW,
        "project": str(project),
        "coord": str(coord),
        "approve_project": bool(args.approve_project),
        "monitor_pane_id": None,
        "roles": {},
    }
    for role in roles:
        config = configs[role]
        config["prompt_path"] = str(prompt_paths[role])
        config["session_dir"] = str(coord / "sessions" / role)
        manifest["roles"][role] = config
    save_manifest(coord, manifest)

    create_tmux_grid(session, project, coord, roles, manifest)
    print(f"Coordination: {coord}")
    print(f"Status: pi-tmux-agents status {session}")
    print(f"Attach: pi-tmux-agents attach {session}")
    print(f"Stop: pi-tmux-agents stop {session} --yes")
    if args.attach:
        attach_session(session)
    return 0


def list_command(_: argparse.Namespace) -> int:
    sessions = orchestrated_sessions()
    if not sessions:
        print("No running pi-tmux-agents sessions.")
        return 0
    for session, coord in sessions:
        try:
            manifest = load_manifest(coord)
            roles = ",".join(manifest["roles"].keys())
            print(f"{session}\t{manifest['project']}\troles={roles}\t{coord}")
        except OrchestrationError as error:
            print(f"{session}\tinvalid manifest: {error}")
    return 0


def coordination_files(coord: Path) -> list[Path]:
    patterns = (
        "*.started.md",
        "probe.md",
        "handoff-*.md",
        "review-*.md",
        "implementation-ready.md",
    )
    files: set[Path] = set()
    for pattern in patterns:
        files.update(coord.glob(pattern))
    return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))


def status_command(args: argparse.Namespace) -> int:
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord)
    print(f"Session: {session}")
    print(f"Project: {manifest['project']}")
    print(f"Coordination: {coord}")
    result = tmux(
        [
            "list-panes",
            "-t",
            f"{session}:{manifest['window']}",
            "-F",
            "pane=#{pane_index} id=#{pane_id} pid=#{pane_pid} cmd=#{pane_current_command} "
            "dead=#{pane_dead} title=#{pane_title}",
        ],
        capture=True,
    )
    print("Panes:")
    for line in result.stdout.splitlines():
        print(f"  {line}")
    print("Coordination files:")
    files = coordination_files(coord)
    if not files:
        print("  waiting for agent status")
    for path in files:
        try:
            first = path.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            first = ""
        print(f"  {path.name}: {first[:120]}")
    return 0


def attach_command(args: argparse.Namespace) -> int:
    session, _ = resolve_session(args.session)
    attach_session(session)
    return 0


def send_keys(pane_id: str, message: str) -> None:
    tmux(["send-keys", "-t", pane_id, "-l", "--", message])
    tmux(["send-keys", "-t", pane_id, "Enter"])


def send_command(args: argparse.Namespace) -> int:
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord)
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"].keys())
        raise OrchestrationError(f"Role {args.role!r} is not in {session}; available: {available}")
    message = read_text_argument(args.message, args.message_file, "message").strip()
    send_keys(manifest["roles"][args.role]["pane_id"], message)
    print(f"Sent message to {session}/{args.role}")
    return 0


def restart_command(args: argparse.Namespace) -> int:
    if not args.yes:
        raise OrchestrationError("restart replaces the role's Pi conversation; pass --yes")
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord)
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"].keys())
        raise OrchestrationError(f"Role {args.role!r} is not in {session}; available: {available}")
    role = manifest["roles"][args.role]
    if args.provider:
        role["provider"] = args.provider
    if args.model:
        role["model"] = args.model
    if args.thinking:
        role["thinking"] = args.thinking
    if not args.skip_model_check:
        validate_model(args.role, role)
    save_manifest(coord, manifest)
    started = coord / f"{args.role}.started.md"
    if started.exists():
        started.unlink()
    command = shlex.join(
        [str(SCRIPT_PATH), "_run-agent", "--coord", str(coord), "--role", args.role]
    )
    tmux(["respawn-pane", "-k", "-t", role["pane_id"], command])
    print(
        f"Restarted {session}/{args.role} with "
        f"{role['provider']}/{role['model']} thinking={role['thinking']}"
    )
    return 0


def stop_command(args: argparse.Namespace) -> int:
    if not args.yes:
        raise OrchestrationError("stop kills the selected tmux agent grid; pass --yes")
    session, coord = resolve_session(args.session)
    tmux(["kill-session", "-t", session])
    print(f"Stopped {session}")
    print(f"Coordination state retained at {coord}")
    return 0


def doctor_command(_: argparse.Namespace) -> int:
    ok = True
    for name in ("pi", "tmux", "python3"):
        path = shutil.which(name)
        if path:
            print(f"OK   {name}: {path}")
        else:
            print(f"FAIL {name}: not found")
            ok = False
    if not ok:
        return 1

    version = run([command_path("tmux"), "-V"], capture=True).stdout.strip()
    print(f"OK   {version}")
    if list_tmux_sessions():
        extended = tmux(["show-options", "-gv", "extended-keys"], check=False, capture=True)
        key_format = tmux(
            ["show-options", "-gv", "extended-keys-format"],
            check=False,
            capture=True,
        )
        extended_value = extended.stdout.strip() if extended.returncode == 0 else "unknown"
        format_value = key_format.stdout.strip() if key_format.returncode == 0 else "unknown"
        label = "OK" if extended_value == "on" else "WARN"
        print(f"{label:<4} tmux extended-keys: {extended_value}")
        label = "OK" if format_value == "csi-u" else "WARN"
        print(f"{label:<4} tmux extended-keys-format: {format_value}")
    else:
        print("INFO tmux server is not running; extended-key options were not inspected")

    for role, config in DEFAULT_MODELS.items():
        available, detail = model_available(config["provider"], config["model"])
        label = "OK" if available else "WARN"
        print(f"{label:<4} {role}: {config['provider']}/{config['model']} ({detail})")
    print(f"OK   state root: {STATE_ROOT}")
    return 0


def run_agent_command(args: argparse.Namespace) -> int:
    coord = Path(args.coord).expanduser().resolve()
    manifest = load_manifest(coord)
    role = manifest["roles"].get(args.role)
    if role is None:
        raise OrchestrationError(f"Unknown role in manifest: {args.role}")
    project = manifest["project"]
    prompt_path = Path(role["prompt_path"])
    if not prompt_path.is_file():
        raise OrchestrationError(f"Role prompt does not exist: {prompt_path}")
    Path(role["session_dir"]).mkdir(parents=True, exist_ok=True)
    command = [
        command_path("pi"),
        "--session-dir",
        role["session_dir"],
        "--name",
        f"{Path(project).name} {args.role}",
        "--provider",
        role["provider"],
        "--model",
        role["model"],
        "--thinking",
        role["thinking"],
    ]
    if manifest["approve_project"]:
        command.append("--approve")
    if role.get("tools"):
        command.extend(["--tools", role["tools"]])
    command.extend(
        [
            f"@{prompt_path}",
            "Follow the attached role instructions and begin.",
        ]
    )
    environment = os.environ.copy()
    environment["PI_SKIP_VERSION_CHECK"] = "1"
    environment["PI_TELEMETRY"] = "0"
    os.chdir(project)
    os.execvpe(command[0], command, environment)
    return 0


def relay_send(manifest: dict[str, Any], role: str, message: str) -> None:
    role_config_value = manifest["roles"].get(role)
    if not role_config_value:
        return
    try:
        send_keys(role_config_value["pane_id"], message)
    except (OrchestrationError, subprocess.CalledProcessError):
        pass


def mark_seen(seen_dir: Path, token: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", token)
    secure_write(seen_dir / safe, "")


def is_seen(seen_dir: Path, token: str) -> bool:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", token)
    return (seen_dir / safe).exists()


def relay_once(coord: Path, manifest: dict[str, Any], seen_dir: Path) -> None:
    for marker in sorted(coord.glob("handoff-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"handoff-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        relay_send(
            manifest,
            "reviewer",
            f"Coordination notice: implementer handoff round {round_number} is ready at "
            f"{coord}/handoff-{round_number}.md. Review it now and write review-{round_number}.md "
            f"plus review-{round_number}.ready.",
        )
        mark_seen(seen_dir, token)

    for marker in sorted(coord.glob("review-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"review-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        relay_send(
            manifest,
            "implementer",
            f"Coordination notice: reviewer response round {round_number} is ready at "
            f"{coord}/review-{round_number}.md. Read it now; address CHANGES_REQUESTED "
            "or write implementation-ready.md if APPROVED.",
        )
        mark_seen(seen_dir, token)

    probe_marker = coord / "probe.ready"
    if probe_marker.exists() and not is_seen(seen_dir, probe_marker.name):
        message = (
            f"Coordination notice: the independent probe is ready at {coord}/probe.md. "
            "Use valid evidence while preserving its stated limitations."
        )
        relay_send(manifest, "implementer", message)
        relay_send(manifest, "reviewer", message)
        mark_seen(seen_dir, probe_marker.name)

    ready = coord / "implementation-ready.md"
    if ready.exists() and not is_seen(seen_dir, ready.name):
        relay_send(
            manifest,
            "reviewer",
            f"Coordination notice: {ready} exists. Confirm the latest round is approved and "
            "remain available for final questions.",
        )
        mark_seen(seen_dir, ready.name)


def render_monitor(coord: Path, manifest: dict[str, Any]) -> None:
    session = manifest["session"]
    print("\033[H\033[2J", end="")
    print("Pi + tmux agent orchestration")
    print(f"Session: {session}")
    print(f"Project: {manifest['project']}")
    print(f"Coordination: {coord}\n")
    result = tmux(
        [
            "list-panes",
            "-t",
            f"{session}:{manifest['window']}",
            "-F",
            "pane #{pane_index} | #{pane_title} | cmd=#{pane_current_command} | "
            "pid=#{pane_pid} | dead=#{pane_dead} | #{pane_width}x#{pane_height}",
        ],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print("tmux session ended")
        return
    print(result.stdout.rstrip())
    print("\nCoordination files:")
    files = coordination_files(coord)
    if not files:
        print("  waiting for agent status...")
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            first = lines[0] if lines else ""
            size = path.stat().st_size
        except OSError:
            first = ""
            size = 0
        print(f"  {path.name:<30} {size:>7} bytes | {first[:90]}")
    print("\nRelay: handoff → reviewer; review → implementer; probe → both")
    print(f"Attach/switch: pi-tmux-agents attach {session}")
    print(f"Status:        pi-tmux-agents status {session}")
    print(f"Stop:          pi-tmux-agents stop {session} --yes")
    sys.stdout.flush()


def relay_command(args: argparse.Namespace) -> int:
    coord = Path(args.coord).expanduser().resolve()
    manifest = load_manifest(coord)
    seen_dir = coord / ".relay-seen"
    seen_dir.mkdir(parents=True, exist_ok=True)
    seen_dir.chmod(0o700)
    try:
        while session_exists(manifest["session"]):
            relay_once(coord, manifest, seen_dir)
            render_monitor(coord, manifest)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    return 0


def add_model_arguments(parser: argparse.ArgumentParser, role: str) -> None:
    parser.add_argument(f"--{role}-provider")
    parser.add_argument(f"--{role}-model")
    parser.add_argument(f"--{role}-thinking", choices=THINKING_LEVELS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-tmux-agents",
        description="Run coordinated Pi implementer/reviewer/probe agents in a tmux grid.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              pi-tmux-agents doctor
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --approve-project
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --with-probe --attach
              pi-tmux-agents status pi-my-project-agents
              pi-tmux-agents restart pi-my-project-agents --role implementer \\
                --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="create and start an agent grid")
    start.add_argument("--project", default=os.getcwd())
    start.add_argument("--task")
    start.add_argument("--task-file")
    start.add_argument("--session")
    start.add_argument("--with-probe", action="store_true")
    start.add_argument("--probe-task")
    start.add_argument("--probe-task-file")
    start.add_argument("--approve-project", action="store_true")
    start.add_argument("--attach", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--skip-model-check", action="store_true")
    for role_name in ("implementer", "reviewer", "probe"):
        add_model_arguments(start, role_name)
    start.set_defaults(handler=start_command)

    list_parser = subparsers.add_parser("list", help="list running orchestrations")
    list_parser.set_defaults(handler=list_command)

    status = subparsers.add_parser("status", help="show pane and handoff status")
    status.add_argument("session", nargs="?")
    status.set_defaults(handler=status_command)

    attach = subparsers.add_parser("attach", help="attach or switch to an orchestration")
    attach.add_argument("session", nargs="?")
    attach.set_defaults(handler=attach_command)

    send = subparsers.add_parser("send", help="send a steering message to a role")
    send.add_argument("session")
    send.add_argument("--role", required=True, choices=("implementer", "reviewer", "probe"))
    send.add_argument("--message")
    send.add_argument("--message-file")
    send.set_defaults(handler=send_command)

    restart = subparsers.add_parser("restart", help="restart one role, optionally changing model")
    restart.add_argument("session")
    restart.add_argument("--role", required=True, choices=("implementer", "reviewer", "probe"))
    restart.add_argument("--provider")
    restart.add_argument("--model")
    restart.add_argument("--thinking", choices=THINKING_LEVELS)
    restart.add_argument("--skip-model-check", action="store_true")
    restart.add_argument("--yes", action="store_true")
    restart.set_defaults(handler=restart_command)

    stop = subparsers.add_parser("stop", help="stop one orchestration")
    stop.add_argument("session", nargs="?")
    stop.add_argument("--yes", action="store_true")
    stop.set_defaults(handler=stop_command)

    doctor = subparsers.add_parser("doctor", help="check local prerequisites and defaults")
    doctor.set_defaults(handler=doctor_command)

    return parser


def parse_internal_command(argv: list[str]) -> argparse.Namespace | None:
    if not argv or argv[0] not in {"_run-agent", "_relay"}:
        return None
    command = argv[0]
    parser = argparse.ArgumentParser(prog=f"pi-tmux-agents {command}")
    parser.add_argument("--coord", required=True)
    if command == "_run-agent":
        parser.add_argument("--role", required=True)
        parser.set_defaults(handler=run_agent_command)
    else:
        parser.set_defaults(handler=relay_command)
    return parser.parse_args(argv[1:])


def main() -> int:
    args = parse_internal_command(sys.argv[1:])
    if args is None:
        parser = build_parser()
        args = parser.parse_args()
    try:
        return int(args.handler(args))
    except OrchestrationError as error:
        eprint(f"error: {error}")
        return 2
    except subprocess.CalledProcessError as error:
        command = shlex.join(str(value) for value in error.cmd)
        eprint(f"error: command failed ({error.returncode}): {command}")
        if error.stderr:
            eprint(error.stderr.strip())
        return error.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
