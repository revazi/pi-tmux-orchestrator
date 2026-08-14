"""Controller support for Pi tmux orchestration."""

from __future__ import annotations

import argparse
import datetime as dt
import shlex
import textwrap
from pathlib import Path
from typing import Any

from . import runtime
from .constants import (
    CONTROLLER_OPTION_ROOT,
    CONTROLLER_OPTION_SESSION_ID,
    CONTROLLER_OPTION_VERSION,
    CONTROLLER_PI_SESSION_ID,
    CONTROLLER_STATE_VERSION,
    CONTROLLER_TMUX_SESSION,
    CONTROLLER_WINDOW,
    PANE_ID_PATTERN,
)
from .models import CommandResult, OrchestrationError
from .output import bounded_message, human_print
from .storage import (
    canonical_state_root,
    configured_controller_root,
    controller_state_path,
    controller_state_root,
    ensure_private_directory,
    load_controller_state,
    save_controller_state,
    secure_write,
)
from .tmux import (
    attach_session,
    command_path,
    controller_is_marked,
    controller_session_option,
    controller_window_target,
    exact_session_target,
    session_exists,
    tmux,
)


def controller_prompt_text(state_root: Path) -> str:
    return (
        textwrap.dedent(f"""
        # Dedicated Pi Tmux Orchestrator Controller

        This is the persistent, project-neutral control session for Pi Tmux Orchestrator.
        Manage orchestrations; do not implement target-project changes from this session.

        - Always identify an explicit target project before starting an orchestration.
        - Use the bundled orchestrator extension or the authoritative CLI at {runtime.SCRIPT_PATH}.
        - Act as the parent supervisor: use tmux as the live worker view, interpret
          returned structured reports, and decide or request explicit follow-up.
        - Select interactive TUI or acknowledged RPC control per orchestration; tmux is
          the view host, not the coordination transport or controller identity.
        - Keep task and message bodies out of argv, status, widgets, and notifications.
        - Preserve one writer, independent review, project instructions, trust prompts, and
          explicit stop/restart confirmations.
        - Never inspect or copy Pi/provider credentials.
        - External orchestration state is rooted at {state_root}.

        Use /orchestrator-help for the available Pi commands. Attach and role restart remain
        terminal-only operations.
        """).strip()
        + "\n"
    )


def controller_pi_command(root: Path, prompt_path: Path) -> list[str]:
    package_root = runtime.SCRIPT_PATH.parent.parent
    extension_path = package_root / "extensions" / "tmux-orchestrator.js"
    skill_path = package_root / "SKILL.md"
    command = [
        command_path("pi"),
        "--session-dir",
        str(root / "sessions"),
        "--session-id",
        CONTROLLER_PI_SESSION_ID,
        "--name",
        "Pi Tmux Orchestrator Controller",
        "--no-context-files",
        "--no-approve",
        "--tools",
        "tmux_orchestrator,read,bash,grep,find,ls",
        "--append-system-prompt",
        str(prompt_path),
    ]
    if extension_path.is_file():
        command.extend(["--no-extensions", "--extension", str(extension_path)])
    if skill_path.is_file():
        command.extend(["--skill", str(skill_path)])
    return command


def controller_public_data(
    state: dict[str, Any] | None,
    pane: dict[str, Any] | None,
) -> dict[str, Any]:
    if state is None:
        root = configured_controller_root()
        paths = {
            "root": str(root),
            "workspace": str(root / "workspace"),
            "session_dir": str(root / "sessions"),
        }
        created_at = None
        last_started_at = None
    else:
        paths = {
            "root": state["root"],
            "workspace": state["workspace"],
            "session_dir": state["session_dir"],
        }
        created_at = state["created_at"]
        last_started_at = state["last_started_at"]
    return {
        "session": CONTROLLER_TMUX_SESSION,
        "window": CONTROLLER_WINDOW,
        "pi_session_id": CONTROLLER_PI_SESSION_ID,
        "tmux_session_exists": pane is not None,
        "running": pane is not None and not pane["dead"],
        "pane": pane,
        "created_at": created_at,
        "last_started_at": last_started_at,
        "paths": paths,
        "state_retained": state is not None,
    }


def retained_controller_state() -> dict[str, Any] | None:
    root_path = configured_controller_root()
    try:
        root_path.lstat()
    except FileNotFoundError:
        return None
    root = controller_state_root(create=False)
    state_path = controller_state_path(root)
    if not state_path.exists() and not state_path.is_symlink():
        return None
    return load_controller_state(root)


def controller_details() -> dict[str, Any] | None:
    if not session_exists(CONTROLLER_TMUX_SESSION):
        return None
    if not controller_is_marked():
        raise OrchestrationError(
            f"tmux session was not created by the controller: {CONTROLLER_TMUX_SESSION}",
            "session_collision",
        )
    root = controller_state_root(create=False)
    if controller_session_option(CONTROLLER_OPTION_ROOT) != str(root):
        raise OrchestrationError("Controller tmux state root marker is invalid")
    if (
        controller_session_option(CONTROLLER_OPTION_SESSION_ID)
        != CONTROLLER_PI_SESSION_ID
    ):
        raise OrchestrationError("Controller tmux Pi session marker is invalid")
    state = load_controller_state(root)
    result = tmux(
        [
            "list-panes",
            "-t",
            controller_window_target(),
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_dead}\t#{pane_title}",
        ],
        capture=True,
    )
    lines = [line for line in result.stdout.splitlines() if line]
    if len(lines) != 1:
        raise OrchestrationError("Controller tmux window must contain exactly one pane")
    columns = lines[0].split("\t", 4)
    if len(columns) != 5 or not PANE_ID_PATTERN.fullmatch(columns[0]):
        raise OrchestrationError("tmux returned invalid controller pane metadata")
    pane_id, pid, current_command, dead, title = columns
    try:
        pane_pid = int(pid)
    except ValueError as error:
        raise OrchestrationError(
            "tmux returned invalid controller pane metadata"
        ) from error
    if dead not in {"0", "1"}:
        raise OrchestrationError("tmux returned invalid controller pane metadata")
    pane = {
        "id": pane_id,
        "pid": pane_pid,
        "command": bounded_message(current_command, 128),
        "dead": dead == "1",
        "title": bounded_message(title, 256),
    }
    return controller_public_data(state, pane)


def create_controller_tmux(
    root: Path,
    orchestration_root: Path,
    prompt_path: Path,
) -> str:
    created = False
    try:
        tmux(
            [
                "new-session",
                "-d",
                "-x",
                "180",
                "-y",
                "50",
                "-s",
                CONTROLLER_TMUX_SESSION,
                "-n",
                CONTROLLER_WINDOW,
                "-c",
                str(root / "workspace"),
            ]
        )
        created = True
        target = controller_window_target()
        tmux(["set-window-option", "-t", target, "remain-on-exit", "on"])
        pane_result = tmux(
            ["list-panes", "-t", target, "-F", "#{pane_id}"],
            capture=True,
        )
        pane_ids = [
            line.strip() for line in pane_result.stdout.splitlines() if line.strip()
        ]
        if len(pane_ids) != 1 or not PANE_ID_PATTERN.fullmatch(pane_ids[0]):
            raise OrchestrationError("tmux created an invalid controller pane")
        pane_id = pane_ids[0]
        tmux(["select-pane", "-t", pane_id, "-T", "PI ORCHESTRATOR CONTROLLER"])
        tmux(
            [
                "set-option",
                "-q",
                "-t",
                target,
                CONTROLLER_OPTION_VERSION,
                str(CONTROLLER_STATE_VERSION),
            ]
        )
        tmux(["set-option", "-q", "-t", target, CONTROLLER_OPTION_ROOT, str(root)])
        tmux(
            [
                "set-option",
                "-q",
                "-t",
                target,
                CONTROLLER_OPTION_SESSION_ID,
                CONTROLLER_PI_SESSION_ID,
            ]
        )
        environment = {
            "PI_TMUX_CONTROLLER": "1",
            "PI_TMUX_CONTROLLER_HOME": str(root),
            "PI_TMUX_AGENTS_HOME": str(orchestration_root),
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
        session_target = exact_session_target(CONTROLLER_TMUX_SESSION)
        for name, value in environment.items():
            tmux(["set-environment", "-t", session_target, name, value])
        shell_command = (
            f"umask 077; exec {shlex.join(controller_pi_command(root, prompt_path))}"
        )
        tmux(["respawn-pane", "-k", "-t", pane_id, shell_command])
        return pane_id
    except BaseException:
        if created:
            tmux(
                ["kill-session", "-t", exact_session_target(CONTROLLER_TMUX_SESSION)],
                check=False,
                capture=True,
            )
        raise


def controller_start_command(_: argparse.Namespace) -> CommandResult:
    command_path("pi")
    command_path("tmux")
    if session_exists(CONTROLLER_TMUX_SESSION):
        if controller_is_marked():
            raise OrchestrationError(
                "The dedicated orchestrator controller is already running",
                "already_running",
            )
        raise OrchestrationError(
            f"tmux session already exists and is not managed by the controller: "
            f"{CONTROLLER_TMUX_SESSION}",
            "session_collision",
        )

    orchestration_root = canonical_state_root(create=True)
    root = controller_state_root(create=True)
    workspace = ensure_private_directory(root / "workspace")
    session_dir = ensure_private_directory(root / "sessions")
    prompt_path = root / "controller.prompt.md"
    secure_write(prompt_path, controller_prompt_text(orchestration_root))

    state_file = controller_state_path(root)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if state_file.exists() or state_file.is_symlink():
        previous = load_controller_state(root)
        created_at = previous["created_at"]
    else:
        created_at = now
    state = {
        "version": CONTROLLER_STATE_VERSION,
        "created_at": created_at,
        "last_started_at": now,
        "session": CONTROLLER_TMUX_SESSION,
        "window": CONTROLLER_WINDOW,
        "pi_session_id": CONTROLLER_PI_SESSION_ID,
        "root": str(root),
        "workspace": str(workspace),
        "session_dir": str(session_dir),
    }
    save_controller_state(root, state)
    create_controller_tmux(root, orchestration_root, prompt_path)
    details = controller_details()
    if details is None:
        raise OrchestrationError("Controller tmux session disappeared during startup")
    human_print(f"Controller: {CONTROLLER_TMUX_SESSION}")
    human_print(f"Pi session ID: {CONTROLLER_PI_SESSION_ID}")
    human_print(f"Workspace: {workspace}")
    human_print("Status: pi-tmux-agents controller status")
    human_print("Attach: pi-tmux-agents controller attach")
    human_print("Stop: pi-tmux-agents controller stop --confirm")
    return CommandResult(data=details)


def controller_status_command(_: argparse.Namespace) -> CommandResult:
    details = controller_details()
    if details is None:
        human_print("The dedicated orchestrator controller is not running.")
        return CommandResult(
            data=controller_public_data(retained_controller_state(), None)
        )
    human_print(f"Controller: {details['session']}")
    human_print(f"Pi session ID: {details['pi_session_id']}")
    human_print(f"Running: {'yes' if details['running'] else 'no (pane exited)'}")
    human_print(f"Workspace: {details['paths']['workspace']}")
    return CommandResult(data=details)


def controller_attach_command(args: argparse.Namespace) -> CommandResult:
    if getattr(args, "json_output", False):
        raise OrchestrationError(
            "controller attach is interactive-only and cannot be used with --json",
            "interactive_only",
        )
    details = controller_details()
    if details is None:
        raise OrchestrationError("The dedicated orchestrator controller is not running")
    attach_session(CONTROLLER_TMUX_SESSION)
    return CommandResult(data=details)


def controller_stop_command(args: argparse.Namespace) -> CommandResult:
    if not args.confirm:
        raise OrchestrationError(
            "controller stop terminates the dedicated controller process; pass --confirm"
        )
    details = controller_details()
    if details is None:
        raise OrchestrationError("The dedicated orchestrator controller is not running")
    tmux(["kill-session", "-t", exact_session_target(CONTROLLER_TMUX_SESSION)])
    human_print(f"Stopped controller {CONTROLLER_TMUX_SESSION}")
    human_print(
        f"Controller conversation retained at {details['paths']['session_dir']}"
    )
    return CommandResult(
        data={
            **details,
            "tmux_session_exists": False,
            "running": False,
            "pane": None,
            "stopped": True,
        }
    )
