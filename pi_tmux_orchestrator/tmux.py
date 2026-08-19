"""Tmux support for Pi tmux orchestration."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import runtime
from .constants import (
    CONTROLLER_OPTION_VERSION,
    CONTROLLER_STATE_VERSION,
    CONTROLLER_TMUX_SESSION,
    CONTROLLER_WINDOW,
    MAX_TASK_BYTES,
    WINDOW,
)
from .models import OrchestrationError


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
    capture_output = capture or runtime.JSON_MODE
    return subprocess.run(
        args,
        check=check,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def tmux(
    args: list[str], *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return run([command_path("tmux"), *args], check=check, capture=capture)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "project")[:48].rstrip("-")


def validate_session_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise OrchestrationError(
            "Session names may contain only letters, digits, underscores, dots, and hyphens"
        )
    return value


def exact_session_target(session: str) -> str:
    return f"={validate_session_name(session)}"


def exact_window_target(session: str, window: str = WINDOW) -> str:
    if window != WINDOW:
        raise OrchestrationError(f"Unexpected orchestration window: {window}")
    return f"{exact_session_target(session)}:={window}"


def session_exists(session: str) -> bool:
    result = tmux(
        ["has-session", "-t", exact_session_target(session)],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def list_tmux_sessions() -> list[str]:
    result = tmux(["list-sessions", "-F", "#{session_name}"], check=False, capture=True)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def session_option(session: str, option: str) -> str | None:
    result = tmux(
        ["show-options", "-qv", "-t", exact_window_target(session), option],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def controller_window_target() -> str:
    return f"{exact_session_target(CONTROLLER_TMUX_SESSION)}:={CONTROLLER_WINDOW}"


def controller_session_option(option: str) -> str | None:
    result = tmux(
        ["show-options", "-qv", "-t", controller_window_target(), option],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def controller_is_marked() -> bool:
    return controller_session_option(CONTROLLER_OPTION_VERSION) == str(
        CONTROLLER_STATE_VERSION
    )


def orchestrated_sessions() -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for session in list_tmux_sessions():
        try:
            validate_session_name(session)
        except OrchestrationError:
            continue
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
            raise OrchestrationError(
                f"tmux session was not created by pi-tmux-agents: {requested}"
            )
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
    raise OrchestrationError(
        f"Multiple orchestrations are running; specify one: {names}"
    )


def read_text_argument(
    text: str | None,
    file_name: str | None,
    label: str,
    *,
    max_bytes: int = MAX_TASK_BYTES,
) -> str:
    if text is not None and file_name is not None:
        raise OrchestrationError(f"Use either --{label} or --{label}-file, not both")
    if file_name is not None:
        source = Path(file_name).expanduser().resolve()
        try:
            value = source.read_text(encoding="utf-8")
        except OSError as error:
            raise OrchestrationError(
                f"Cannot read {label} file {source}: {error}"
            ) from error
    elif text is not None:
        value = text
    else:
        raise OrchestrationError(f"Provide --{label} or --{label}-file")
    if not value.strip():
        raise OrchestrationError(f"{label.capitalize()} cannot be empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise OrchestrationError(
            f"{label.capitalize()} exceeds the {max_bytes // 1024} KiB safety limit"
        )
    return value.strip() + "\n"


def model_available(provider: str, model: str) -> tuple[bool, str]:
    pi = command_path("pi")
    result = run([pi, "--list-models", provider], check=False, capture=True)
    if result.returncode != 0:
        return False, f"pi --list-models failed with exit code {result.returncode}"
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 2 and columns[0] == provider and columns[1] == model:
            return True, "available"
    return False, f"{provider}/{model} is not listed as available"


def validate_model(role: str, config: dict[str, str]) -> None:
    available, detail = model_available(config["provider"], config["model"])
    if not available:
        raise OrchestrationError(f"{role} model unavailable: {detail}")


def attach_session(session: str) -> None:
    target = exact_session_target(session)
    if os.environ.get("TMUX"):
        tmux(["switch-client", "-t", target])
    else:
        os.execvp(command_path("tmux"), ["tmux", "attach", "-t", target])
