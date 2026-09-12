"""Start construction and execution for Pi tmux orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shlex
import stat
import threading
import time
from pathlib import Path
from typing import Any

from . import runtime
from .broker import initialize_broker_run
from .budgeting import (
    effective_budget_policy,
    load_budget_config,
)
from .broker_store import (
    broker_paths,
    public_broker_snapshot,
)
from .configuration import (
    effective_model_config,
    empty_model_config,
    load_model_config,
    model_config_path,
    project_model_config,
    public_project_config,
)
from .constants import (
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    DEFAULT_IMPLEMENTATION_FLOW,
    KNOWN_ROLES,
    MAX_CONTEXT_CAPSULE_BYTES,
    READ_ONLY_TOOLS,
    RPC_TRANSPORT,
    TUI_TRANSPORT,
    WINDOW,
)
from .context_capsules import render_worker_baseline
from .custom_role_resources import select_custom_start
from .role_registry import valid_custom_role_id
from .models import CommandResult, OrchestrationError
from .output import human_print, public_role
from .profiles import (
    public_execution_profile,
    resolve_custom_thinking,
    resolve_execution_profile,
)
from .rpc import (
    rpc_role_paths,
)
from .specialist_activation import validate_forced_specialists
from .continuation import repair_policy
from .storage import (
    absolute_path,
    canonical_state_root,
    ensure_private_directory,
    save_manifest,
    secure_write,
)
from .tmux import (
    attach_session,
    command_path,
    exact_session_target,
    exact_window_target,
    read_text_argument,
    session_exists,
    slugify,
    tmux,
    validate_model,
    validate_session_name,
)
from .worker_resources import (
    resolve_worker_skills,
)
from .worker_context import resolve_context_policy
from .workspace_capsules import (
    canonical_project_root,
    construct_workspace_capsule,
    workspace_capsule_metadata,
)


def role_config(
    args: argparse.Namespace,
    role: str,
    model_config: dict[str, Any] | None = None,
    execution_profile: dict[str, Any] | None = None,
    project_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = effective_model_config(
        role,
        model_config or empty_model_config(),
        execution_profile,
        project_config,
    )
    config: dict[str, Any] = {
        "provider": getattr(args, f"{role}_provider") or defaults["provider"],
        "model": getattr(args, f"{role}_model") or defaults["model"],
        "thinking": getattr(args, f"{role}_thinking") or defaults["thinking"],
        "tools": None if role == "implementer" else READ_ONLY_TOOLS,
        "pane_id": None,
    }
    return config


def construct_start_manifest(
    coord: Path,
    project: Path,
    session: str,
    transport: str,
    *,
    approve_project: bool,
    execution_profile: dict[str, Any],
    project_config: dict[str, Any],
    orchestration_config: dict[str, Any],
    roles: list[str],
    configs: dict[str, dict[str, Any]],
    custom_role_registry: str | None,
) -> dict[str, Any]:
    """Construct retained launch metadata; custom resources remain body-free."""
    custom_roles = [role for role in roles if valid_custom_role_id(role)]
    if (
        len(set(roles)) != len(roles)
        or not {"implementer", "reviewer"} <= set(roles)
        or any(
            role not in KNOWN_ROLES and not valid_custom_role_id(role) for role in roles
        )
        or set(configs) != set(roles)
        or bool(custom_roles) != (custom_role_registry is not None)
    ):
        raise OrchestrationError("Start role bindings are inconsistent")
    manifest: dict[str, Any] = {
        "version": 7 if custom_roles else 5,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session": session,
        "window": WINDOW,
        "project": str(project),
        "coord": str(coord),
        "approve_project": approve_project,
        "transport": transport,
        "coordination": BROKER_COORDINATION,
        "protocol_version": BROKER_PROTOCOL_VERSION,
        "execution_profile": execution_profile,
        "project_config": project_config,
        "orchestration_config": orchestration_config,
        "monitor_pane_id": None,
        "roles": {},
    }
    if custom_roles:
        manifest["custom_role_registry"] = custom_role_registry
    for role in roles:
        manifest["roles"][role] = {
            **configs[role],
            "session_dir": str(coord / "sessions" / role),
            "session_id": f"{coord.name}-{role}",
        }
    return manifest


CUSTOM_STARTUP_TIMEOUT_SECONDS = 20.0
CUSTOM_STARTUP_STABLE_SECONDS = 0.25
CUSTOM_STARTUP_WAIT = threading.Event()


def wait_for_custom_startup(
    session: str,
    coord: Path,
    roles: list[str],
    manifest: dict[str, Any],
    *,
    timeout: float = CUSTOM_STARTUP_TIMEOUT_SECONDS,
) -> None:
    """Require a stable authenticated custom broker/worker startup."""
    if manifest.get("version") not in {6, 7} or not any(
        valid_custom_role_id(role) for role in roles
    ):
        raise OrchestrationError("Custom startup admission requires manifest v6+")
    expected_panes = [
        manifest["monitor_pane_id"],
        *(manifest["roles"][role]["pane_id"] for role in roles),
    ]
    if any(not isinstance(pane_id, str) for pane_id in expected_panes):
        raise OrchestrationError("Custom startup panes are incomplete")
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    last_reason = "broker and workers did not authenticate"
    while time.monotonic() < deadline:
        for pane_id in expected_panes:
            result = tmux(
                ["display-message", "-p", "-t", pane_id, "#{pane_dead}"],
                check=False,
                capture=True,
            )
            if result.returncode != 0 or result.stdout.strip() != "0":
                raise OrchestrationError(
                    "Custom startup failed before every pane became healthy",
                    "startup_failed",
                )
        try:
            socket_metadata = broker_paths(coord)["socket"].lstat()
            snapshot = public_broker_snapshot(coord)
        except (FileNotFoundError, OSError, OrchestrationError):
            stable_since = None
            last_reason = "broker did not become ready"
            CUSTOM_STARTUP_WAIT.wait(0.05)
            continue
        role_rows = {row["role"]: row for row in snapshot["roles"]}
        workflow = snapshot["workflow"]["state"]
        admitted = (
            stat.S_ISSOCK(socket_metadata.st_mode)
            and set(role_rows) == set(roles)
            and all(role_rows[role]["connected"] for role in roles)
            and workflow not in {"starting", "connecting", "initializing", "uncertain"}
        )
        if admitted:
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= CUSTOM_STARTUP_STABLE_SECONDS:
                return
        else:
            stable_since = None
            last_reason = "not every selected worker authenticated"
        CUSTOM_STARTUP_WAIT.wait(0.05)
    raise OrchestrationError(
        f"Custom startup failed: {last_reason}",
        "startup_failed",
    )


def rollback_partial_start(session: str, coord: Path) -> None:
    """Stop only the exact new session and bound its broker-socket cleanup."""
    tmux(
        ["kill-session", "-t", exact_session_target(session)],
        check=False,
        capture=True,
    )
    socket_path = broker_paths(coord)["socket"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not session_exists(session) and not socket_path.exists():
            return
        CUSTOM_STARTUP_WAIT.wait(0.05)


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
    custom_selections = getattr(args, "custom_role", [])
    project_input = Path(args.project).expanduser()
    project = project_input.resolve()
    if not project.is_dir():
        raise OrchestrationError(f"Project directory does not exist: {project}")
    custom_selection = select_custom_start(
        project, custom_selections, getattr(args, "role_registry", None)
    )
    command_path("pi")
    command_path("tmux")
    configured_models = load_model_config(project=project)
    matched_project = project_model_config(configured_models, project)
    workspace_requested = getattr(args, "workspace_capsule", None)
    workspace_capsule_enabled = (
        workspace_requested
        if workspace_requested is not None
        else bool(
            matched_project is not None and matched_project["workspace_capsule"] is True
        )
    )
    if workspace_capsule_enabled:
        project = canonical_project_root(project_input)
    project_specialists = (
        matched_project["specialists"]
        if matched_project is not None and matched_project["specialists"] is not None
        else []
    )
    with_probe = (
        args.with_probe
        if args.with_probe is not None
        else "probe" in project_specialists
    )
    with_playwright = (
        args.with_playwright
        if args.with_playwright is not None
        else "playwright" in project_specialists
    )
    with_django_expert = (
        args.with_django_expert
        if args.with_django_expert is not None
        else "django" in project_specialists
    )
    requested_flow = getattr(args, "implementation_flow", None)
    implementation_flow = (
        requested_flow
        if requested_flow is not None
        else (
            matched_project["implementation_flow"]
            if matched_project is not None
            and matched_project["implementation_flow"] is not None
            else DEFAULT_IMPLEMENTATION_FLOW
        )
    )
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
    workspace_relevant_paths = getattr(args, "workspace_relevant_path", [])
    if workspace_relevant_paths and not workspace_capsule_enabled:
        raise OrchestrationError(
            "--workspace-relevant-path requires --workspace-capsule",
            "invalid_arguments",
        )
    workspace_capsule = (
        construct_workspace_capsule(project, workspace_relevant_paths)
        if workspace_capsule_enabled
        else None
    )
    if with_probe:
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

    if with_playwright:
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

    if with_django_expert:
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
    if with_probe:
        roles.append("probe")
    if with_playwright:
        roles.append("playwright")
    if with_django_expert:
        roles.append("django")
    execution_profile = resolve_execution_profile(
        configured_models,
        getattr(args, "profile", None),
        matched_project,
    )
    configs = {
        role: role_config(
            args,
            role,
            configured_models,
            execution_profile,
            matched_project,
        )
        for role in roles
    }
    worker_skills = resolve_worker_skills(
        getattr(args, "worker_skill", None),
        roles,
    )
    for role in roles:
        configs[role]["skills"] = worker_skills[role]
    for role, config in custom_selection["roles"].items():
        thinking, source = resolve_custom_thinking(
            role, config["thinking"], execution_profile
        )
        config["thinking"] = thinking
        config["custom_policy"]["thinking_source"] = source
    configs.update(custom_selection["roles"])
    roles.extend(custom_selection["roles"])
    forced_specialists = validate_forced_specialists(
        getattr(args, "force_specialist", []), roles
    )
    for role in custom_selection["roles"]:
        if role in forced_specialists:
            configs[role]["custom_policy"]["activation_source"] = "per-run-force"
    continuation_policy = repair_policy(getattr(args, "max_repair_rounds", None))
    context_policy = resolve_context_policy(
        getattr(args, "worker_context", None), set(roles)
    )
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
            workspace_capsule=workspace_capsule,
        )
    if not args.skip_model_check:
        model_catalogs = {}
        for role, config in configs.items():
            validate_model(role, config, model_catalogs)

    project_config_metadata = public_project_config(matched_project)
    orchestration_config_metadata = {
        "path": str(model_config_path(project)),
        "version": configured_models["version"],
    }
    data: dict[str, Any] = {
        "project": str(project),
        "session": session,
        "implementation_flow": implementation_flow,
        "forced_specialists": list(forced_specialists),
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
        "continuation_policy": continuation_policy,
        "worker_context_policy": context_policy,
        "execution_profile": public_execution_profile(execution_profile),
        "project_config": project_config_metadata,
        "orchestration_config": orchestration_config_metadata,
        "worker_resources": {
            "skill_discovery": False,
            "skills": {
                role: [skill["path"] for skill in skills]
                for role, skills in worker_skills.items()
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
        "workspace_capsule": workspace_capsule_metadata(workspace_capsule),
    }
    if custom_selection["roles"]:
        data["custom_role_selection"] = {
            "launch_supported": True,
            "resource_verification": (
                "checked_at_selection"
                if args.dry_run
                else "checked_at_selection_and_launch"
            ),
            "models": "explicit-or-profile-thinking",
            "roles": {
                role: {
                    "state": "enabled",
                    **config["custom_policy"],
                }
                for role, config in custom_selection["roles"].items()
            },
            "skills": {
                role: {
                    "source": "registry-bound",
                    "count": len(config["custom_role"]["skills"]),
                }
                for role, config in custom_selection["roles"].items()
            },
        }
        human_print("Custom roles: explicit registry-bound read-only specialists.")
    human_print(f"Project: {project}")
    human_print(f"Session: {session}")
    human_print("Roles:")
    for role in roles:
        config = configs[role]
        custom_policy = config.get("custom_policy")
        policy_text = (
            f" thinking-source={custom_policy['thinking_source']} "
            f"activation={custom_policy['activation_source']}"
            if custom_policy is not None
            else ""
        )
        human_print(
            f"  {role}: {config['provider']}/{config['model']} "
            f"thinking={config['thinking']}{policy_text}"
        )
    human_print("  monitor: broker/status")
    human_print(f"Worker transport: {transport}")
    human_print(f"Implementation flow: {implementation_flow}")
    for role in roles:
        selection = context_policy["overrides"].get(role, "prune")
        source = "per-run" if role in context_policy["overrides"] else "default"
        human_print(f"Worker context: {role}={selection} (source={source})")
    human_print(
        f"Repair-round continuation cap: {continuation_policy['max_repair_rounds'] if continuation_policy['max_repair_rounds'] is not None else 'disabled'}"
    )
    human_print(
        "Forced specialists: "
        + (", ".join(forced_specialists) if forced_specialists else "none")
    )
    human_print(
        f"Execution profile: {execution_profile['name']} "
        f"({execution_profile['kind']}, source={execution_profile['source']})"
    )
    human_print(f"Orchestration config: {model_config_path(project)}")
    human_print(
        "Project mapping: "
        + (
            f"matched {project_config_metadata['directory']}"
            if project_config_metadata["matched"]
            else "none"
        )
    )
    human_print("Worker skill discovery: disabled")
    for role, skills in worker_skills.items():
        paths = [skill["path"] for skill in skills]
        human_print(f"  {role} skills: {', '.join(paths) if paths else 'none'}")
    for role, config in custom_selection["roles"].items():
        human_print(
            f"  {role} skills: registry-bound ({len(config['custom_role']['skills'])})"
        )
    human_print(f"Budget policy mode: {budget_policy['enforcement']} (observational)")
    for level in ("warning", "hard"):
        for scope in ("run", "role", "assignment"):
            thresholds = budget_policy[level][scope]
            rendered = ", ".join(
                f"{metric}={value}" for metric, value in sorted(thresholds.items())
            )
            human_print(f"  {level}.{scope}: {rendered or 'off'}")
    workspace_metadata = workspace_capsule_metadata(workspace_capsule)
    human_print(
        "Experimental workspace capsule: "
        + (
            f"validated schema={workspace_metadata['schema_version']} "
            f"instructions={workspace_metadata['instruction_count']} "
            f"markers={workspace_metadata['marker_count']} "
            f"relevant={workspace_metadata['relevant_path_count']}"
            if workspace_metadata["enabled"]
            else "disabled"
        )
    )
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
        manifest = construct_start_manifest(
            coord,
            project,
            session,
            transport,
            approve_project=bool(args.approve_project),
            execution_profile=public_execution_profile(execution_profile),
            project_config=project_config_metadata,
            orchestration_config=orchestration_config_metadata,
            roles=roles,
            configs=configs,
            custom_role_registry=custom_selection["registry_path"],
        )

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
            workspace_capsule=workspace_capsule,
            budget_policy=budget_policy,
            implementation_flow=implementation_flow,
            forced_specialists=forced_specialists,
            max_repair_rounds=continuation_policy["max_repair_rounds"],
            worker_context_overrides=context_policy["overrides"],
        )
        create_tmux_grid(session, project, coord, roles, manifest)
        if manifest["version"] in {6, 7}:
            wait_for_custom_startup(session, coord, roles, manifest)
        secure_write(coord / "startup-state", "RUNNING\n")
    except BaseException:
        rollback_partial_start(session, coord)
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
