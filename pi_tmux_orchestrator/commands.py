"""Commands support for Pi tmux orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import stat
from pathlib import Path
from typing import Any

from . import runtime
from .broker import initialize_broker_run
from .budgeting import (
    budget_config_path,
    effective_budget_policy,
    load_budget_config,
    packaged_budget_policy,
)
from .broker_client import broker_control_request
from .broker_store import (
    broker_paths,
    broker_role_generation,
    public_broker_snapshot,
    worker_guardrail_policy,
)
from .configuration import (
    effective_model_config,
    empty_model_config,
    load_model_config,
    model_config_path,
)
from .constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    BROKER_READ_ONLY_TOOLS,
    DEFAULT_MODELS,
    MAX_CONTEXT_CAPSULE_BYTES,
    MAX_JSON_ITEMS,
    READ_ONLY_TOOLS,
    RPC_TRANSPORT,
    TUI_TRANSPORT,
    WINDOW,
)
from .context_capsules import render_worker_baseline
from .models import CommandResult, OrchestrationError
from .output import bounded_message, human_print, public_role
from .prompts import role_system_prompt
from .rpc import (
    load_rpc_events,
    load_rpc_registry,
    load_rpc_state,
    mark_rpc_registry_stopped,
    public_rpc_event,
    public_rpc_registry,
    public_rpc_state,
    rpc_control_request,
    rpc_role_paths,
    run_rpc_agent,
    unlink_private_regular,
)
from .supervisor_api import rpc_event_page, resolve_supervisor_target
from .storage import (
    absolute_path,
    canonical_state_root,
    ensure_private_directory,
    load_manifest,
    manifest_transport,
    require_regular_file,
    retained_coordination,
    save_manifest,
    secure_write,
    validate_coordination_directory,
)
from .tmux import (
    attach_session,
    command_path,
    exact_session_target,
    exact_window_target,
    list_tmux_sessions,
    model_available,
    orchestrated_sessions,
    read_text_argument,
    resolve_session,
    run,
    session_exists,
    slugify,
    tmux,
    validate_model,
    validate_session_name,
)
from .worker_resources import append_worker_resource_args, resolve_worker_skills


def role_config(
    args: argparse.Namespace,
    role: str,
    model_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = effective_model_config(role, model_config or empty_model_config())
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
    session_target = exact_session_target(session)
    window_target = exact_window_target(session)
    try:
        for _ in range(total_panes - 1):
            tmux(["split-window", "-d", "-t", window_target, "-c", str(project)])
        tmux(["select-layout", "-t", window_target, "tiled"])
        tmux(["set-window-option", "-t", window_target, "remain-on-exit", "on"])
        tmux(["set-window-option", "-t", window_target, "pane-border-status", "top"])
        tmux(
            [
                "set-window-option",
                "-t",
                window_target,
                "pane-border-format",
                " #{pane_index} #{pane_title} ",
            ]
        )

        result = tmux(
            [
                "list-panes",
                "-t",
                window_target,
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
                title = "BROKER + STATUS"
            else:
                manifest["roles"][label]["pane_id"] = pane_id
                role = manifest["roles"][label]
                title = f"{label.upper()} · {role['provider']}/{role['model']} · {role['thinking']}"
            tmux(["select-pane", "-t", pane_id, "-T", title])

        tmux(["set-option", "-q", "-t", window_target, "@pi_agents_coord", str(coord)])
        tmux(
            [
                "set-option",
                "-q",
                "-t",
                window_target,
                "@pi_agents_project",
                str(project),
            ]
        )
        tmux(
            [
                "set-option",
                "-q",
                "-t",
                window_target,
                "@pi_agents_version",
                str(manifest["version"]),
            ]
        )
        save_manifest(coord, manifest)

        for role_name in roles:
            pane_id = manifest["roles"][role_name]["pane_id"]
            command = shlex.join(
                [
                    str(runtime.SCRIPT_PATH),
                    "_run-agent",
                    "--state-root",
                    str(coord.parent.parent),
                    "--coord",
                    str(coord),
                    "--role",
                    role_name,
                ]
            )
            tmux(["respawn-pane", "-k", "-t", pane_id, command])

        broker_command = shlex.join(
            [
                str(runtime.SCRIPT_PATH),
                "_broker",
                "--state-root",
                str(coord.parent.parent),
                "--coord",
                str(coord),
            ]
        )
        tmux(["respawn-pane", "-k", "-t", manifest["monitor_pane_id"], broker_command])
    except Exception:
        tmux(["kill-session", "-t", session_target], check=False)
        raise


def start_command(args: argparse.Namespace) -> CommandResult:
    if getattr(args, "json_output", False) and args.attach:
        raise OrchestrationError(
            "start --attach is interactive-only and cannot be used with --json",
            "interactive_only",
        )
    command_path("pi")
    command_path("tmux")
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        raise OrchestrationError(f"Project directory does not exist: {project}")
    transport = RPC_TRANSPORT if getattr(args, "rpc_workers", False) else TUI_TRANSPORT

    task = read_text_argument(args.task, args.task_file, "task")
    context_capsule_text = getattr(args, "context_capsule", None)
    context_capsule_file = getattr(args, "context_capsule_file", None)
    context_capsule = (
        read_text_argument(
            context_capsule_text,
            context_capsule_file,
            "context-capsule",
            max_bytes=MAX_CONTEXT_CAPSULE_BYTES,
        )
        if context_capsule_text is not None or context_capsule_file is not None
        else ""
    )
    if args.with_probe:
        if args.probe_task is None and args.probe_task_file is None:
            probe_task = (
                "Independently investigate the highest-risk integration, contract, runtime, or "
                "security assumptions in the task. Produce actionable evidence for implementer "
                "and reviewer without modifying project files.\n"
            )
        else:
            probe_task = read_text_argument(
                args.probe_task, args.probe_task_file, "probe-task"
            )
    else:
        if args.probe_task is not None or args.probe_task_file is not None:
            raise OrchestrationError("--probe-task requires --with-probe")
        probe_task = None

    if args.with_playwright:
        if args.playwright_task is None and args.playwright_task_file is None:
            playwright_task = (
                "Run an independent browser smoke against the actual local test application "
                "after each brokered implementation report. Verify the task's user-visible "
                "behavior and a relevant failure path with synthetic data, then report "
                "limitations.\n"
            )
        else:
            playwright_task = read_text_argument(
                args.playwright_task,
                args.playwright_task_file,
                "playwright-task",
            )
    else:
        if args.playwright_task is not None or args.playwright_task_file is not None:
            raise OrchestrationError("--playwright-task requires --with-playwright")
        playwright_task = None

    if args.with_django_expert:
        if args.django_task is None and args.django_task_file is None:
            django_task = (
                "Independently review each brokered implementation report for Django ORM, "
                "settings, lifecycle, database, security, testing, and operational best "
                "practices. Separate blocking findings from optional future improvements.\n"
            )
        else:
            django_task = read_text_argument(
                args.django_task,
                args.django_task_file,
                "django-task",
            )
    else:
        if args.django_task is not None or args.django_task_file is not None:
            raise OrchestrationError("--django-task requires --with-django-expert")
        django_task = None

    role_tasks = {
        role: value
        for role, value in {
            "probe": probe_task,
            "playwright": playwright_task,
            "django": django_task,
        }.items()
        if value is not None
    }

    session = validate_session_name(
        args.session or f"pi-{slugify(project.name)}-agents"
    )
    if session_exists(session):
        raise OrchestrationError(
            f"tmux session already exists: {session}. Use status/stop or choose --session."
        )

    roles = ["implementer", "reviewer"]
    if args.with_probe:
        roles.append("probe")
    if args.with_playwright:
        roles.append("playwright")
    if args.with_django_expert:
        roles.append("django")
    configured_models = load_model_config()
    configs = {role: role_config(args, role, configured_models) for role in roles}
    worker_skills = resolve_worker_skills(
        getattr(args, "worker_skill", None),
        roles,
    )
    for role in roles:
        configs[role]["skills"] = worker_skills[role]
    configured_budget = load_budget_config(project=project)
    budget_policy = effective_budget_policy(
        configured_budget,
        enforcement=getattr(args, "budget_enforcement", None),
        overrides=getattr(args, "budget_override", None),
    )
    for role in roles:
        render_worker_baseline(
            str(project),
            role,
            task,
            context_capsule,
            role_tasks.get(role, ""),
        )
    if not args.skip_model_check:
        for role, config in configs.items():
            validate_model(role, config)

    data: dict[str, Any] = {
        "project": str(project),
        "session": session,
        "roles": [
            public_role(
                role,
                configs[role],
                transport,
            )
            for role in roles
        ],
        "monitor": {"kind": "broker/status"},
        "transport": transport,
        "coordination_protocol": {
            "name": BROKER_COORDINATION,
            "version": BROKER_PROTOCOL_VERSION,
            "payload_files": False,
            "polling": False,
        },
        "budget_policy": budget_policy,
        "worker_resources": {
            "skill_discovery": False,
            "skills": {
                role: [skill["path"] for skill in worker_skills[role]] for role in roles
            },
        },
        "trust": {
            "child_bypass": bool(args.approve_project),
            "policy": (
                "approve"
                if args.approve_project
                else (
                    "saved-or-global-policy"
                    if transport == RPC_TRANSPORT
                    else "native-prompts"
                )
            ),
        },
        "dry_run": bool(args.dry_run),
        "paths": {
            "state_root": str(absolute_path(runtime.STATE_ROOT)),
            "coordination": None,
            "observer_socket": None,
        },
        "state_retained_on_stop": True,
        "context_capsule": {
            "present": bool(context_capsule),
            "chars": len(context_capsule.rstrip("\n")),
        },
    }
    human_print(f"Project: {project}")
    human_print(f"Session: {session}")
    human_print("Roles:")
    for role in roles:
        config = configs[role]
        human_print(
            f"  {role}: {config['provider']}/{config['model']} "
            f"thinking={config['thinking']}"
        )
    human_print("  monitor: broker/status")
    human_print(f"Worker transport: {transport}")
    human_print("Worker skill discovery: disabled")
    for role in roles:
        paths = [skill["path"] for skill in worker_skills[role]]
        human_print(f"  {role} skills: {', '.join(paths) if paths else 'none'}")
    human_print(f"Budget policy mode: {budget_policy['enforcement']} (observational)")
    for level in ("warning", "hard"):
        for scope in ("run", "role", "assignment"):
            thresholds = budget_policy[level][scope]
            rendered = ", ".join(
                f"{metric}={value}" for metric, value in sorted(thresholds.items())
            )
            human_print(f"  {level}.{scope}: {rendered or 'off'}")
    human_print(
        f"Child project trust bypass: {'enabled' if args.approve_project else 'disabled'}"
    )
    if transport == RPC_TRANSPORT and not args.approve_project:
        human_print(
            "RPC trust: saved decision or global defaultProjectTrust applies; "
            "ask/never ignores project-local executable resources without a prompt"
        )
    if args.dry_run:
        human_print(
            "Dry run complete; no files, sessions, or model requests were created."
        )
        return CommandResult(data=data)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = canonical_state_root(create=True)
    session_root = ensure_private_directory(root / session)
    coord = ensure_private_directory(session_root / f"{timestamp}-{os.getpid()}")

    try:
        secure_write(coord / "startup-state", "STARTING\n")
        manifest: dict[str, Any] = {
            "version": 3,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "session": session,
            "window": WINDOW,
            "project": str(project),
            "coord": str(coord),
            "approve_project": bool(args.approve_project),
            "transport": transport,
            "coordination": BROKER_COORDINATION,
            "protocol_version": BROKER_PROTOCOL_VERSION,
            "monitor_pane_id": None,
            "roles": {},
        }
        for role in roles:
            config = configs[role]
            config["session_dir"] = str(coord / "sessions" / role)
            config["session_id"] = f"{coord.name}-{role}"
            manifest["roles"][role] = config

        ensure_private_directory(coord / "sessions")
        for role in roles:
            ensure_private_directory(Path(manifest["roles"][role]["session_dir"]))
        if transport == RPC_TRANSPORT:
            for role in roles:
                rpc_role_paths(coord, role, create=True)
        initialize_broker_run(
            coord,
            manifest,
            task,
            role_tasks,
            context_capsule=context_capsule,
            budget_policy=budget_policy,
        )
        create_tmux_grid(session, project, coord, roles, manifest)
        secure_write(coord / "startup-state", "RUNNING\n")
    except BaseException:
        tmux(
            ["kill-session", "-t", exact_session_target(session)],
            check=False,
            capture=True,
        )
        try:
            secure_write(coord / "startup-state", "FAILED\n")
        except OrchestrationError:
            try:
                coord.rmdir()
            except OSError:
                pass
        raise
    data["paths"]["coordination"] = str(coord)
    data["paths"]["observer_socket"] = str(broker_paths(coord)["socket"])
    human_print(f"Coordination: {coord}")
    human_print(f"Status: pi-tmux-agents status {session}")
    human_print(f"Attach: pi-tmux-agents attach {session}")
    human_print(f"Stop: pi-tmux-agents stop {session} --yes")
    if args.attach:
        attach_session(session)
    return CommandResult(data=data)


def list_command(_: argparse.Namespace) -> CommandResult:
    sessions = orchestrated_sessions()
    values: list[dict[str, Any]] = []
    if not sessions:
        human_print("No running pi-tmux-agents sessions.")
        return CommandResult(
            data={"sessions": values, "truncated": False, "total_sessions": 0}
        )
    selected_sessions = sessions[:MAX_JSON_ITEMS] if runtime.JSON_MODE else sessions
    for session, coord in selected_sessions:
        try:
            manifest = load_manifest(coord, expected_session=session)
            role_values = [
                public_role(role, config, manifest_transport(manifest))
                for role, config in manifest["roles"].items()
            ]
            values.append(
                {
                    "session": session,
                    "valid": True,
                    "project": manifest["project"],
                    "roles": role_values,
                    "paths": {"coordination": str(coord)},
                }
            )
            roles = ",".join(manifest["roles"].keys())
            human_print(f"{session}\t{manifest['project']}\troles={roles}\t{coord}")
        except OrchestrationError as error:
            message = bounded_message(error)
            values.append(
                {
                    "session": session,
                    "valid": False,
                    "project": None,
                    "roles": [],
                    "paths": {"coordination": str(coord)},
                    "error": {"code": error.code, "message": message},
                }
            )
            human_print(f"{session}\tinvalid manifest: {message}")
    return CommandResult(
        data={
            "sessions": values,
            "truncated": runtime.JSON_MODE and len(sessions) > len(selected_sessions),
            "total_sessions": len(sessions),
        }
    )


def coordination_files(coord: Path) -> list[tuple[Path, os.stat_result]]:
    """List legacy 0.4.x report files for retained-run compatibility only."""
    coord = validate_coordination_directory(coord)
    patterns = (
        "*.started.md",
        "probe.md",
        "playwright-*.md",
        "django-review-*.md",
        "handoff-*.md",
        "review-*.md",
        "implementation-ready.md",
    )
    files: set[Path] = set()
    for pattern in patterns:
        files.update(coord.glob(pattern))
    metadata = [
        (path, require_regular_file(path, f"coordination file {path.name}"))
        for path in files
    ]
    return sorted(metadata, key=lambda item: (item[1].st_mtime, item[0].name))


def status_roles(coord: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    transport = manifest_transport(manifest)
    broker_roles: dict[str, dict[str, Any]] = {}
    if manifest.get("version") == 3:
        broker_roles = {
            value["role"]: value for value in public_broker_snapshot(coord)["roles"]
        }
    for role, config in manifest["roles"].items():
        value = public_role(role, config, transport)
        if role in broker_roles:
            value["broker_state"] = broker_roles[role]
        if transport == RPC_TRANSPORT:
            value["rpc_state"] = public_rpc_state(load_rpc_state(coord, role))
            value["rpc_registry"] = public_rpc_registry(load_rpc_registry(coord, role))
        values.append(value)
    return values


def _usage_display(value: object, *, suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "unavailable"


def _status_assignment_usage(role: dict[str, Any]) -> str | None:
    latest = role.get("latest_assignment_usage")
    if not isinstance(latest, dict):
        return None
    prefix = f"latest round={latest['round']} kind={latest['kind']}"
    usage = latest.get("usage")
    if not isinstance(usage, dict):
        return f"{prefix} usage=unavailable"
    return (
        f"{prefix} calls={usage['provider_calls']} input={usage['input_tokens']} "
        f"cache-read={usage['cache_read_tokens']} cache-write={usage['cache_write_tokens']} "
        f"output={usage['output_tokens']} "
        f"reasoning={_usage_display(usage['reasoning_tokens'])} "
        f"cost={_usage_display(usage['cost_total'])} "
        f"operational={usage['operational_tokens']} "
        f"context={_usage_display(usage['context_percent'], suffix='%')} "
        f"peak={_usage_display(usage['peak_context_tokens'])}"
    )


def status_command(args: argparse.Namespace) -> CommandResult:
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord, expected_session=session)
    human_print(f"Session: {session}")
    human_print(f"Project: {manifest['project']}")
    human_print(f"Coordination: {coord}")
    result = tmux(
        [
            "list-panes",
            "-t",
            exact_window_target(session, manifest["window"]),
            "-F",
            "#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t"
            "#{pane_dead}\t#{pane_title}",
        ],
        capture=True,
    )
    panes: list[dict[str, Any]] = []
    human_print("Panes:")
    for line in result.stdout.splitlines():
        columns = line.split("\t", 5)
        if len(columns) != 6:
            raise OrchestrationError(
                "tmux returned invalid pane metadata", "invalid_tmux_output"
            )
        index, pane_id, pid, current_command, dead, title = columns
        pane = {
            "index": int(index),
            "id": pane_id,
            "pid": int(pid),
            "command": bounded_message(current_command, 128),
            "dead": dead == "1",
            "title": bounded_message(title, 256),
        }
        if not runtime.JSON_MODE or len(panes) < MAX_JSON_ITEMS:
            panes.append(pane)
        human_print(
            f"  pane={index} id={pane_id} pid={pid} cmd={current_command} "
            f"dead={dead} title={title}"
        )
    broker_snapshot: dict[str, Any] | None = None
    files: list[tuple[Path, os.stat_result]] = []
    file_values: list[dict[str, Any]] = []
    if manifest.get("version") == 3:
        broker_snapshot = public_broker_snapshot(coord)
        workflow = broker_snapshot["workflow"]
        usage = broker_snapshot["usage"]
        total_warning = " budget=warning" if usage["soft_total_budget_exceeded"] else ""
        human_print(
            f"Workflow: {workflow['state']} round={workflow['round']} "
            f"tokens={usage['total_tokens']}{total_warning}"
        )
        for worker in broker_snapshot["roles"]:
            human_print(
                f"  {worker['role']}: {worker['state']} connected={worker['connected']} "
                f"tokens={worker['total_tokens']}"
                f"{' budget=warning' if worker['soft_budget_exceeded'] else ''}"
            )
            latest_usage = _status_assignment_usage(worker)
            if latest_usage is not None:
                human_print(f"    {latest_usage}")
            for guardrail in worker.get("assignment_guardrails", []):
                human_print(
                    f"    guardrail={guardrail['level']} metric={guardrail['metric']} "
                    f"observed={guardrail['observed']} threshold={guardrail['threshold']}"
                )
    else:
        human_print("Legacy coordination files:")
        files = coordination_files(coord)
        selected_files = files[:MAX_JSON_ITEMS] if runtime.JSON_MODE else files
        file_values = [
            {"name": path.name, "size_bytes": metadata.st_size}
            for path, metadata in selected_files
        ]
        if not files:
            human_print("  waiting for legacy agent status")
        for path, metadata in files:
            human_print(f"  {path.name}: {metadata.st_size} bytes")
    role_values = status_roles(coord, manifest)
    if manifest_transport(manifest) == RPC_TRANSPORT:
        human_print("RPC workers:")
        for role_value in role_values:
            rpc_state = role_value.get("rpc_state")
            if rpc_state is None:
                human_print(f"  {role_value['name']}: starting/unavailable")
            else:
                registry = role_value.get("rpc_registry")
                registry_suffix = (
                    f" generation={registry['generation']} event={registry['last_event_sequence']}"
                    if registry is not None
                    else " registry=unavailable"
                )
                human_print(
                    f"  {role_value['name']}: {rpc_state['status']} "
                    f"streaming={rpc_state['is_streaming']} "
                    f"queue={rpc_state['steering_count']}+{rpc_state['follow_up_count']}"
                    f"{registry_suffix}"
                )
    paths = {"coordination": str(coord)}
    if manifest.get("version") == 3:
        paths["observer_socket"] = str(broker_paths(coord)["socket"])
    return CommandResult(
        data={
            "session": session,
            "project": manifest["project"],
            "paths": paths,
            "roles": role_values,
            "panes": panes,
            "broker": broker_snapshot,
            "files": file_values,
            "truncated": {
                "panes": runtime.JSON_MODE
                and len(result.stdout.splitlines()) > len(panes),
                "files": False,
            },
        }
    )


def events_command(args: argparse.Namespace) -> CommandResult:
    session = validate_session_name(args.session)
    coord = retained_coordination(session, getattr(args, "run", None))
    manifest = load_manifest(coord, expected_session=session)
    if manifest_transport(manifest) != RPC_TRANSPORT:
        raise OrchestrationError("events require an orchestration using RPC workers")
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"])
        raise OrchestrationError(
            f"Role {args.role!r} is not in {session}; available: {available}"
        )
    events = load_rpc_events(coord, args.role)
    after = args.after
    selected, cursor = rpc_event_page(events, after=after, limit=args.limit)
    gap = cursor["gap"]
    registry = load_rpc_registry(coord, args.role)
    human_print(
        f"Events: {session}/{args.role} run={coord.name} "
        f"after={after} returned={len(selected)} latest={cursor['latest']}"
    )
    if gap:
        human_print("Warning: requested cursor predates the retained journal window")
    for event in selected:
        human_print(
            f"  {event['sequence']} {event['timestamp']} {event['event']} "
            f"status={event['status']} command={event['command_id'] or '-'}"
        )
    return CommandResult(
        data={
            "session": session,
            "role": args.role,
            "run_id": coord.name,
            "paths": {"coordination": str(coord)},
            "registry": public_rpc_registry(registry),
            "events": [public_rpc_event(event) for event in selected],
            "cursor": cursor,
        }
    )


def attach_command(args: argparse.Namespace) -> CommandResult:
    json_output = getattr(args, "json_output", False)
    inside_tmux = bool(os.environ.get("TMUX"))
    if json_output and not inside_tmux:
        raise OrchestrationError(
            "Parent attach requires Pi to be running inside tmux",
            "interactive_only",
        )
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord, expected_session=session)
    attach_session(session)
    if inside_tmux:
        tmux(
            [
                "display-message",
                "-d",
                "5000",
                f"Attached to {session} · prefix then L detaches back without stopping workers",
            ],
            check=False,
        )
    return CommandResult(
        data={
            "session": session,
            "project": manifest["project"],
            "transport": manifest_transport(manifest),
            "mode": "switch-client" if inside_tmux else "attach-client",
            "return_hint": (
                "Press the tmux prefix, then L, to detach back to the invoking Pi without stopping workers."
                if inside_tmux
                else None
            ),
        }
    )


def send_keys(pane_id: str, message: str) -> None:
    """Legacy 0.4.x retained-run transport; v0.5.0 runs never call this."""
    tmux(["send-keys", "-t", pane_id, "-l", "--", message])
    tmux(["send-keys", "-t", pane_id, "Enter"])


def control_target(args: argparse.Namespace) -> tuple[str, Path, dict[str, Any]]:
    run_id = getattr(args, "run", None)
    if run_id is not None:
        coord, manifest = resolve_supervisor_target(
            args.session, run_id, require_rpc=True
        )
        return manifest["session"], coord, manifest
    session, coord = resolve_session(args.session)
    return session, coord, load_manifest(coord, expected_session=session)


def send_command(args: argparse.Namespace) -> CommandResult:
    session, coord, manifest = control_target(args)
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"].keys())
        raise OrchestrationError(
            f"Role {args.role!r} is not in {session}; available: {available}"
        )
    message = read_text_argument(args.message, args.message_file, "message").strip()
    transport = manifest_transport(manifest)
    acknowledgement: dict[str, Any] | None = None
    if manifest.get("version") == 3:
        if getattr(args, "run", None) is not None:
            _session, live_coord = resolve_session(args.session)
            if live_coord != coord:
                raise OrchestrationError(
                    "Broker control requires the exact run to be hosted by the live tmux session",
                    "broker_not_live",
                )
        acknowledgement = broker_control_request(
            coord,
            args.role,
            "send",
            message=message,
            delivery=args.delivery,
            command_id=getattr(args, "command_id", None),
        )
        acknowledged = True
    elif transport == RPC_TRANSPORT:
        acknowledgement = rpc_control_request(
            coord,
            manifest,
            args.role,
            "prompt",
            message=message,
            delivery=args.delivery,
            command_id=getattr(args, "command_id", None),
        )
        acknowledged = True
    else:
        if args.delivery != "steer":
            raise OrchestrationError("follow-up delivery requires RPC workers")
        if getattr(args, "command_id", None) is not None:
            raise OrchestrationError("command IDs require RPC workers")
        # Retained 0.4.x runs remain operable. Manifest v3 runs are rejected above.
        send_keys(manifest["roles"][args.role]["pane_id"], message)
        acknowledged = False
    suffix = (
        f" (status={acknowledgement['status']} id={acknowledgement['id']})"
        if acknowledgement is not None
        else ""
    )
    human_print(f"Sent message to {session}/{args.role} via {transport}{suffix}")
    return CommandResult(
        data={
            "session": session,
            "run_id": coord.name,
            "role": args.role,
            "sent": True,
            "transport": transport,
            "delivery": args.delivery,
            "acknowledged": acknowledged,
            "command_id": acknowledgement["id"] if acknowledgement else None,
            "command_status": acknowledgement["status"] if acknowledgement else None,
            "duplicate": acknowledgement["duplicate"] if acknowledgement else False,
            "event_sequence": (
                acknowledgement.get("event_sequence") if acknowledgement else None
            ),
        }
    )


def abort_command(args: argparse.Namespace) -> CommandResult:
    session, coord, manifest = control_target(args)
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"].keys())
        raise OrchestrationError(
            f"Role {args.role!r} is not in {session}; available: {available}"
        )
    if manifest.get("version") == 3:
        if getattr(args, "run", None) is not None:
            _session, live_coord = resolve_session(args.session)
            if live_coord != coord:
                raise OrchestrationError(
                    "Broker control requires the exact run to be hosted by the live tmux session",
                    "broker_not_live",
                )
        acknowledgement = broker_control_request(
            coord,
            args.role,
            "abort",
            command_id=getattr(args, "command_id", None),
        )
    else:
        if manifest_transport(manifest) != RPC_TRANSPORT:
            raise OrchestrationError(
                "abort requires a brokered or RPC-worker orchestration"
            )
        acknowledgement = rpc_control_request(
            coord,
            manifest,
            args.role,
            "abort",
            command_id=getattr(args, "command_id", None),
        )
    human_print(
        f"Abort acknowledged by {session}/{args.role} "
        f"(status={acknowledgement['status']} id={acknowledgement['id']})"
    )
    return CommandResult(
        data={
            "session": session,
            "run_id": coord.name,
            "role": args.role,
            "aborted": True,
            "transport": manifest_transport(manifest),
            "acknowledged": True,
            "command_id": acknowledgement["id"],
            "command_status": acknowledgement["status"],
            "duplicate": acknowledgement["duplicate"],
            "event_sequence": acknowledgement.get("event_sequence"),
        }
    )


def restart_command(args: argparse.Namespace) -> CommandResult:
    if not args.yes:
        raise OrchestrationError(
            "restart respawns the role's worker process and preserves its brokered "
            "Pi conversation and JSONL history; pass --yes"
        )
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord, expected_session=session)
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"].keys())
        raise OrchestrationError(
            f"Role {args.role!r} is not in {session}; available: {available}"
        )
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
    broker_handover_prepared = manifest.get("version") == 3
    if broker_handover_prepared:
        broker_control_request(coord, args.role, "restart")
    try:
        if manifest_transport(manifest) == RPC_TRANSPORT:
            rpc_paths = rpc_role_paths(coord, args.role, create=True)
            unlink_private_regular(rpc_paths["state"], f"{args.role} RPC state")
        command = shlex.join(
            [
                str(runtime.SCRIPT_PATH),
                "_run-agent",
                "--state-root",
                str(coord.parent.parent),
                "--coord",
                str(coord),
                "--role",
                args.role,
            ]
        )
        tmux(["respawn-pane", "-k", "-t", role["pane_id"], command])
    except Exception:
        if broker_handover_prepared:
            try:
                broker_control_request(coord, args.role, "restart_failed")
            except OrchestrationError as error:
                raise OrchestrationError(
                    "role respawn failed after broker restart preparation; "
                    "handover failure acknowledgement is uncertain",
                    "broker_uncertain",
                ) from error
        raise
    human_print(
        f"Restarted {session}/{args.role} with "
        f"{role['provider']}/{role['model']} thinking={role['thinking']}"
    )
    return CommandResult(
        data={
            "session": session,
            "role": public_role(args.role, role, manifest_transport(manifest)),
            "restarted": True,
        }
    )


def stop_command(args: argparse.Namespace) -> CommandResult:
    if not args.yes:
        raise OrchestrationError("stop kills the selected tmux agent grid; pass --yes")
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord, expected_session=session)
    tmux(["kill-session", "-t", exact_session_target(session)])
    if manifest.get("version") == 3:
        socket_path = broker_paths(coord)["socket"]
        try:
            metadata = socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISSOCK(metadata.st_mode):
                socket_path.unlink()
    registry_finalization_failures: list[str] = []
    if manifest_transport(manifest) == RPC_TRANSPORT:
        for role in manifest["roles"]:
            try:
                mark_rpc_registry_stopped(coord, role)
            except OrchestrationError:
                registry_finalization_failures.append(role)
    human_print(f"Stopped {session}")
    if registry_finalization_failures:
        human_print(
            "Warning: retained RPC registry finalization failed for "
            + ", ".join(registry_finalization_failures)
        )
    human_print(f"Coordination state retained at {coord}")
    return CommandResult(
        data={
            "session": session,
            "stopped": True,
            "state_retained": True,
            "paths": {"coordination": str(coord)},
            "registry_finalization": {
                "failed_roles": registry_finalization_failures,
            },
        }
    )


def doctor_command(_: argparse.Namespace) -> CommandResult:
    configured_models = load_model_config()
    config_path = model_config_path()
    config_in_use = bool(
        configured_models["defaults"] or any(configured_models["roles"].values())
    )
    configured_budget = load_budget_config()
    budget_path = budget_config_path()
    budget_in_use = configured_budget != packaged_budget_policy()
    budget_data = {
        "config_path": str(budget_path),
        "configured": budget_in_use,
        "effective": configured_budget,
    }
    ok = True
    command_checks: list[dict[str, Any]] = []
    for name in ("pi", "tmux", "python3"):
        path = shutil.which(name)
        command_checks.append(
            {"name": name, "status": "ok" if path else "fail", "path": path}
        )
        if path:
            human_print(f"OK   {name}: {path}")
        else:
            human_print(f"FAIL {name}: not found")
            ok = False
    if not ok:
        return CommandResult(
            data={
                "commands": command_checks,
                "tmux": None,
                "model_checks": [],
                "model_policy": {
                    "config_path": str(config_path),
                    "configured": config_in_use,
                },
                "budget_policy": budget_data,
                "paths": {
                    "state_root": str(absolute_path(runtime.STATE_ROOT)),
                    "model_config": str(config_path),
                    "budget_config": str(budget_path),
                },
            },
            code=1,
            error_code="missing_prerequisite",
            error_message="One or more required local commands are unavailable",
        )

    version = bounded_message(
        run([command_path("tmux"), "-V"], capture=True).stdout, 128
    )
    human_print(f"OK   {version}")
    tmux_data: dict[str, Any] = {
        "version": version,
        "server_running": bool(list_tmux_sessions()),
        "extended_keys": None,
        "extended_keys_format": None,
    }
    if tmux_data["server_running"]:
        extended = tmux(
            ["show-options", "-gv", "extended-keys"], check=False, capture=True
        )
        key_format = tmux(
            ["show-options", "-gv", "extended-keys-format"],
            check=False,
            capture=True,
        )
        extended_value = (
            bounded_message(extended.stdout, 64)
            if extended.returncode == 0
            else "unknown"
        )
        format_value = (
            bounded_message(key_format.stdout, 64)
            if key_format.returncode == 0
            else "unknown"
        )
        label = "OK" if extended_value == "on" else "WARN"
        human_print(f"{label:<4} tmux extended-keys: {extended_value}")
        label = "OK" if format_value == "csi-u" else "WARN"
        human_print(f"{label:<4} tmux extended-keys-format: {format_value}")
        tmux_data["extended_keys"] = extended_value
        tmux_data["extended_keys_format"] = format_value
    else:
        human_print(
            "INFO tmux server is not running; extended-key options were not inspected"
        )

    model_checks: list[dict[str, Any]] = []
    for role in DEFAULT_MODELS:
        config = effective_model_config(role, configured_models)
        available, detail = model_available(config["provider"], config["model"])
        label = "OK" if available else "WARN"
        human_print(
            f"{label:<4} {role}: {config['provider']}/{config['model']} ({detail})"
        )
        model_checks.append(
            {
                "role": role,
                "provider": config["provider"],
                "model": config["model"],
                "available": available,
                "detail": bounded_message(detail, 256),
            }
        )
    human_print(
        f"OK   model config: {config_path} ({'configured' if config_in_use else 'packaged defaults'})"
    )
    human_print(
        f"OK   budget config: {budget_path} "
        f"({'configured' if budget_in_use else 'packaged defaults'}; "
        f"mode={configured_budget['enforcement']}, observational=true)"
    )
    human_print(f"OK   state root: {runtime.STATE_ROOT}")
    return CommandResult(
        data={
            "commands": command_checks,
            "tmux": tmux_data,
            "model_checks": model_checks,
            "model_policy": {
                "config_path": str(config_path),
                "configured": config_in_use,
            },
            "budget_policy": budget_data,
            "paths": {
                "state_root": str(absolute_path(runtime.STATE_ROOT)),
                "model_config": str(config_path),
                "budget_config": str(budget_path),
            },
        }
    )


def run_agent_command(args: argparse.Namespace) -> int:
    runtime.STATE_ROOT = Path(args.state_root)
    coord = absolute_path(Path(args.coord))
    manifest = load_manifest(coord)
    role = manifest["roles"].get(args.role)
    if role is None:
        raise OrchestrationError(f"Unknown role in manifest: {args.role}")
    if manifest.get("version") == 3:
        os.environ["PI_TMUX_ORCHESTRATOR_GENERATION"] = str(
            broker_role_generation(coord, args.role)
        )
    if manifest_transport(manifest) == RPC_TRANSPORT:
        return run_rpc_agent(coord, manifest, args.role, role)
    project = manifest["project"]
    ensure_private_directory(Path(role["session_dir"]), parents=True)
    if manifest.get("version") != 3:
        prompt_path = Path(role["prompt_path"])
        require_regular_file(prompt_path, "role prompt", nonempty=True)
    command = [
        command_path("pi"),
        "--session-dir",
        role["session_dir"],
        "--name",
        f"{Path(project).name} {args.role}",
    ]
    if manifest.get("version") == 3:
        command.extend(["--session-id", role["session_id"]])
    command.extend(
        [
            "--provider",
            role["provider"],
            "--model",
            role["model"],
            "--thinking",
            role["thinking"],
        ]
    )
    if manifest["approve_project"]:
        command.append("--approve")
    if role.get("tools"):
        tools = (
            BROKER_READ_ONLY_TOOLS if manifest.get("version") == 3 else role["tools"]
        )
        command.extend(["--tools", tools])
    elif manifest.get("version") == 3:
        command.extend(
            ["--tools", "read,bash,edit,write,grep,find,ls,orchestrator_report"]
        )
    if manifest.get("version") == 3:
        token_path = coord / f"{args.role}.token"
        require_regular_file(token_path, "worker broker token", nonempty=True)
        token = token_path.read_text(encoding="utf-8").strip()
        system_prompt_path = coord / f"{args.role}.system.md"
        secure_write(system_prompt_path, role_system_prompt(Path(project), args.role))
        append_worker_resource_args(
            command,
            role,
            args.role,
            runtime.WORKER_EXTENSION_PATH,
            system_prompt_path,
        )
    else:
        command.extend(
            [
                f"@{prompt_path}",
                "Follow the attached role instructions and begin.",
            ]
        )
    environment = os.environ.copy()
    environment.pop("PI_TMUX_CONTROLLER", None)
    environment.pop("PI_TMUX_CONTROLLER_HOME", None)
    environment["PI_SKIP_VERSION_CHECK"] = "1"
    environment["PI_TELEMETRY"] = "0"
    if manifest.get("version") == 3:
        guardrails = worker_guardrail_policy(coord)
        environment["PI_TMUX_ORCHESTRATOR_ROLE"] = args.role
        environment["PI_TMUX_ORCHESTRATOR_TOKEN"] = token
        environment["PI_TMUX_ORCHESTRATOR_SOCKET"] = str(broker_paths(coord)["socket"])
        environment["PI_TMUX_ORCHESTRATOR_GUARDRAILS"] = json.dumps(
            guardrails, separators=(",", ":"), sort_keys=True
        )
    os.chdir(project)
    os.execvpe(command[0], command, environment)
    return 0
