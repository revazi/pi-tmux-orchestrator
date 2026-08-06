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
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

SCRIPT_PATH = Path(__file__).resolve()
STATE_ROOT = Path(
    os.environ.get(
        "PI_TMUX_AGENTS_HOME",
        str(Path.home() / ".pi" / "agent" / "orchestrations"),
    )
).expanduser()
VERSION = "0.4.0-dev.0"
JSON_SCHEMA_VERSION = "1"
JSON_MODE = False
MAX_ERROR_CHARS = 512
MAX_JSON_ITEMS = 100
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
    "playwright": {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "thinking": "high",
    },
    "django": {
        "provider": "openai-codex",
        "model": "gpt-5.4",
        "thinking": "high",
    },
}
READ_ONLY_TOOLS = "read,bash,grep,find,ls"
MAX_TASK_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
KNOWN_ROLES = frozenset(DEFAULT_MODELS)
MANIFEST_FIELDS = frozenset(
    {
        "version",
        "created_at",
        "session",
        "window",
        "project",
        "coord",
        "approve_project",
        "monitor_pane_id",
        "roles",
    }
)
ROLE_FIELDS = frozenset(
    {"provider", "model", "thinking", "tools", "pane_id", "prompt_path", "session_dir"}
)
PANE_ID_PATTERN = re.compile(r"%[0-9]+")


class OrchestrationError(RuntimeError):
    def __init__(self, message: str, code: str = "orchestration_error") -> None:
        super().__init__(message)
        self.code = code


class CLIUsageError(OrchestrationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "invalid_arguments")


class OrchestrationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if JSON_MODE:
            raise CLIUsageError(message)
        super().error(message)


class CommandResult:
    def __init__(
        self,
        data: dict[str, Any] | None = None,
        code: int = 0,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.data = data
        self.code = code
        self.error_code = error_code
        self.error_message = error_message


def bounded_message(value: object, limit: int = MAX_ERROR_CHARS) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def human_print(*values: object) -> None:
    if not JSON_MODE:
        print(*values)


def eprint(*values: object) -> None:
    if not JSON_MODE:
        print(*values, file=sys.stderr)


def emit_json(command: str, result: CommandResult) -> None:
    error = None
    if result.code != 0:
        error = {
            "code": result.error_code or "command_failed",
            "message": bounded_message(result.error_message or "Command failed"),
        }
    envelope = {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "success": result.code == 0,
        "data": result.data,
        "error": error,
    }
    print(json.dumps(envelope, separators=(",", ":"), sort_keys=True))


def public_role(role: str, config: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": role,
        "provider": config["provider"],
        "model": config["model"],
        "thinking": config["thinking"],
        "tool_policy": (
            "default"
            if config.get("tools") is None
            else "workflow-read-only-with-bash"
        ),
    }
    if config.get("pane_id") is not None:
        value["pane_id"] = config["pane_id"]
    return value


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
    capture_output = capture or JSON_MODE
    return subprocess.run(
        args,
        check=check,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def tmux(args: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run([command_path("tmux"), *args], check=check, capture=capture)


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def path_lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise OrchestrationError(f"Cannot inspect {label}") from error


def require_directory(path: Path, label: str) -> os.stat_result:
    metadata = path_lstat(path, label)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OrchestrationError(f"{label} must be a non-symlink directory")
    return metadata


def require_regular_file(
    path: Path,
    label: str,
    *,
    nonempty: bool = False,
) -> os.stat_result:
    metadata = path_lstat(path, label)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OrchestrationError(f"{label} must be a non-symlink regular file")
    if nonempty and metadata.st_size == 0:
        raise OrchestrationError(f"{label} must be non-empty")
    return metadata


def open_directory(path: Path) -> int:
    expected = require_directory(path, "private state directory")
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OrchestrationError("Cannot safely open private state directory") from error
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != expected.st_dev
        or metadata.st_ino != expected.st_ino
    ):
        os.close(descriptor)
        raise OrchestrationError("Private state directory changed while opening")
    return descriptor


def ensure_private_directory(path: Path, *, parents: bool = False) -> Path:
    return _ensure_private_directory(path, parents=parents, enforce_existing=True)


def _ensure_private_directory(
    path: Path,
    *,
    parents: bool,
    enforce_existing: bool,
) -> Path:
    path = absolute_path(path)
    created = False
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if path.parent == path:
            raise OrchestrationError("Cannot create private state directory at filesystem root")
        if parents:
            parent = _ensure_private_directory(
                path.parent,
                parents=True,
                enforce_existing=False,
            )
        else:
            parent = absolute_path(path.parent)
            require_directory(parent, "private state parent")
            parent = parent.resolve(strict=True)
        parent_descriptor = open_directory(parent)
        try:
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError as error:
                raise OrchestrationError(
                    "Private state directory changed during creation"
                ) from error
            created = True
        except OrchestrationError:
            raise
        except OSError as error:
            raise OrchestrationError("Cannot create private state directory") from error
        finally:
            os.close(parent_descriptor)
        metadata = path_lstat(path, "private state directory")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OrchestrationError("Private state path must be a non-symlink directory")
    canonical = path.resolve(strict=True)
    if created or enforce_existing:
        descriptor = open_directory(canonical)
        try:
            os.fchmod(descriptor, 0o700)
        except OSError as error:
            raise OrchestrationError("Cannot set private state directory permissions") from error
        finally:
            os.close(descriptor)
    return canonical


def canonical_state_root(*, create: bool) -> Path:
    root = absolute_path(STATE_ROOT)
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        if not create:
            raise OrchestrationError("Orchestration state root does not exist")
        return ensure_private_directory(root, parents=True)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OrchestrationError("Orchestration state root must be a non-symlink directory")
    return ensure_private_directory(root)


def validate_coordination_directory(coord: Path) -> Path:
    root = canonical_state_root(create=False)
    candidate = absolute_path(coord)
    require_directory(candidate, "coordination run directory")
    canonical = candidate.resolve(strict=True)
    try:
        relative = canonical.relative_to(root)
    except ValueError as error:
        raise OrchestrationError("Coordination path is outside the configured state root") from error
    if len(relative.parts) != 2:
        raise OrchestrationError("Coordination path must identify one session and one run")
    require_directory(root / relative.parts[0], "orchestration session directory")
    require_directory(canonical, "coordination run directory")
    return canonical


def secure_write(
    path: Path,
    content: str,
    mode: int = 0o600,
    *,
    exclusive: bool = False,
) -> None:
    path = absolute_path(path)
    parent = ensure_private_directory(path.parent, parents=True)
    parent_descriptor = open_directory(parent)
    before: os.stat_result | None = None
    try:
        try:
            before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if before is not None and (
            stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
        ):
            raise OrchestrationError("Private state destination must be a regular file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if exclusive:
            flags |= os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path.name, flags, mode, dir_fd=parent_descriptor)
        except OSError as error:
            raise OrchestrationError("Cannot safely open private state file") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OrchestrationError("Private state destination must be a regular file")
            if before is not None and not hasattr(os, "O_NOFOLLOW") and (
                opened.st_dev != before.st_dev or opened.st_ino != before.st_ino
            ):
                raise OrchestrationError("Private state destination changed during write")
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def read_regular_file(path: Path, label: str, maximum_bytes: int) -> bytes:
    metadata = require_regular_file(path, label)
    if metadata.st_size > maximum_bytes:
        raise OrchestrationError(f"{label} exceeds the safety limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OrchestrationError(f"Cannot safely open {label}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OrchestrationError(f"{label} must be a regular file")
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            raise OrchestrationError(f"{label} changed while opening")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum_bytes:
            raise OrchestrationError(f"{label} exceeds the safety limit")
        return content
    finally:
        os.close(descriptor)


def validate_manifest(
    value: object,
    coord: Path,
    *,
    expected_session: str | None = None,
) -> dict[str, Any]:
    coord = validate_coordination_directory(coord)
    if not isinstance(value, dict):
        raise OrchestrationError("Orchestration manifest must be a JSON object")
    if set(value) != MANIFEST_FIELDS:
        raise OrchestrationError("Orchestration manifest has missing or unknown top-level fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise OrchestrationError("Unsupported orchestration manifest version")
    if not isinstance(value["created_at"], str):
        raise OrchestrationError("Manifest created_at must be a timestamp string")
    try:
        created_at = dt.datetime.fromisoformat(value["created_at"])
    except ValueError as error:
        raise OrchestrationError("Manifest created_at is invalid") from error
    if created_at.tzinfo is None:
        raise OrchestrationError("Manifest created_at must include a timezone")

    session = value["session"]
    if not isinstance(session, str):
        raise OrchestrationError("Manifest session must be a string")
    validate_session_name(session)
    if session != coord.parent.name or (expected_session is not None and session != expected_session):
        raise OrchestrationError("Manifest session does not match its coordination path")
    if value["window"] != WINDOW or not isinstance(value["window"], str):
        raise OrchestrationError("Manifest window is invalid")
    if value["coord"] != str(coord) or not isinstance(value["coord"], str):
        raise OrchestrationError("Manifest coordination path is not canonical")
    if type(value["approve_project"]) is not bool:
        raise OrchestrationError("Manifest approve_project must be a boolean")

    project_value = value["project"]
    if not isinstance(project_value, str):
        raise OrchestrationError("Manifest project must be a path string")
    project = Path(project_value)
    if not project.is_absolute():
        raise OrchestrationError("Manifest project path must be absolute")
    try:
        project_canonical = project.resolve(strict=True)
    except OSError as error:
        raise OrchestrationError("Manifest project path does not exist") from error
    if project_canonical != project or not project.is_dir():
        raise OrchestrationError("Manifest project path must be a canonical directory")

    monitor_pane_id = value["monitor_pane_id"]
    if not isinstance(monitor_pane_id, str) or not PANE_ID_PATTERN.fullmatch(monitor_pane_id):
        raise OrchestrationError("Manifest monitor pane ID is invalid")
    roles = value["roles"]
    if not isinstance(roles, dict):
        raise OrchestrationError("Manifest roles must be an object")
    if not {"implementer", "reviewer"}.issubset(roles) or not set(roles).issubset(KNOWN_ROLES):
        raise OrchestrationError("Manifest roles contain missing or unknown role names")

    pane_ids = {monitor_pane_id}
    for role_name, role in roles.items():
        if not isinstance(role, dict) or set(role) != ROLE_FIELDS:
            raise OrchestrationError(f"Manifest role {role_name} has invalid fields")
        for field in ("provider", "model"):
            field_value = role[field]
            if not isinstance(field_value, str) or not field_value or len(field_value) > 256:
                raise OrchestrationError(f"Manifest role {role_name} has invalid {field}")
        if role["thinking"] not in THINKING_LEVELS or not isinstance(role["thinking"], str):
            raise OrchestrationError(f"Manifest role {role_name} has invalid thinking level")
        expected_tools = None if role_name == "implementer" else READ_ONLY_TOOLS
        if role["tools"] != expected_tools:
            raise OrchestrationError(f"Manifest role {role_name} has invalid tool configuration")
        pane_id = role["pane_id"]
        if not isinstance(pane_id, str) or not PANE_ID_PATTERN.fullmatch(pane_id):
            raise OrchestrationError(f"Manifest role {role_name} has invalid pane ID")
        if pane_id in pane_ids:
            raise OrchestrationError("Manifest pane IDs must be unique")
        pane_ids.add(pane_id)

        prompt_value = role["prompt_path"]
        session_value = role["session_dir"]
        if not isinstance(prompt_value, str) or not isinstance(session_value, str):
            raise OrchestrationError(f"Manifest role {role_name} paths must be strings")
        expected_prompt = coord / f"{role_name}.prompt.md"
        expected_session_dir = coord / "sessions" / role_name
        if Path(prompt_value) != expected_prompt or Path(session_value) != expected_session_dir:
            raise OrchestrationError(f"Manifest role {role_name} paths are invalid")
        require_regular_file(expected_prompt, f"{role_name} prompt", nonempty=True)
        sessions_parent = coord / "sessions"
        if sessions_parent.exists() or sessions_parent.is_symlink():
            require_directory(sessions_parent, "role sessions directory")
            if sessions_parent.resolve(strict=True) != sessions_parent:
                raise OrchestrationError("Manifest sessions path is not canonical")
        if expected_session_dir.exists() or expected_session_dir.is_symlink():
            require_directory(expected_session_dir, f"{role_name} session directory")
            if expected_session_dir.resolve(strict=True) != expected_session_dir:
                raise OrchestrationError(f"Manifest role {role_name} session path is not canonical")
    return value


def save_manifest(coord: Path, manifest: dict[str, Any]) -> None:
    coord = validate_coordination_directory(coord)
    validate_manifest(manifest, coord, expected_session=manifest.get("session"))
    destination = coord / "manifest.json"
    if destination.exists() or destination.is_symlink():
        require_regular_file(destination, "orchestration manifest")
    temporary = coord / f".manifest.json.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    try:
        secure_write(
            temporary,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            exclusive=True,
        )
        temporary_created = True
        os.replace(temporary, destination)
        temporary_created = False
        destination_descriptor = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fchmod(destination_descriptor, 0o600)
            os.fsync(destination_descriptor)
        finally:
            os.close(destination_descriptor)
        directory_descriptor = open_directory(coord)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            metadata = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            if (
                temporary_created
                and stat.S_ISREG(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
            ):
                temporary.unlink()


def load_manifest(coord: Path, *, expected_session: str | None = None) -> dict[str, Any]:
    coord = validate_coordination_directory(coord)
    manifest_path = coord / "manifest.json"
    try:
        content = read_regular_file(manifest_path, "orchestration manifest", MAX_MANIFEST_BYTES)
        value = json.loads(content)
    except UnicodeDecodeError as error:
        raise OrchestrationError("Orchestration manifest is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise OrchestrationError("Orchestration manifest is not valid JSON") from error
    return validate_manifest(value, coord, expected_session=expected_session)


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
        - The reviewer, optional probe, optional Playwright tester, and optional
          Django expert are read-only; do not ask them to edit source.
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
           For each handoff round, also read any matching `playwright-N.md` and
           `django-review-N.md` before responding to reviewer findings.
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
        3. If `playwright.prompt.md` exists, wait for `playwright-N.ready` and inspect
           `playwright-N.md`. If `django.prompt.md` exists, wait for
           `django-review-N.ready` and inspect `django-review-N.md`. Independently
           evaluate all evidence and limitations. Then write `review-N.md`. The first
           line must be exactly `APPROVED` or
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


def playwright_prompt(project: Path, coord: Path, task: str, playwright_task: str) -> str:
    introduction = textwrap.dedent(
        """
        You are a read-only Playwright test agent. Do not edit tracked files, commit,
        push, merge, publish, deploy, change dependency declarations, or access
        credentials/private project data. Browser downloads, test databases, logs,
        screenshots, and traces are allowed only in ignored or external temporary paths.
        """
    )
    rules = textwrap.dedent(
        """
        ## Browser-test rules

        - Independently inspect the actual current worktree and the applicable handoff.
        - Wait for each `handoff-N.ready` before testing that round.
        - Exercise the real test application through a browser, not only HTTP clients or
          unit tests. Verify visible user behavior and at least one relevant failure path.
        - Use only synthetic local data and test-owned credentials. Never enter or record
          secrets, provider payloads, private data, or raw external errors.
        - Start and stop local application/database processes in a bounded command with
          cleanup traps. Do not leave servers or browser processes behind.
        - Do not treat a browser smoke as authorization, semantic proof, security audit,
          or complete adapter coverage.
        """
    )
    deliverable = textwrap.dedent(
        f"""
        ## Deliverable

        Coordination directory: `{coord}`

        1. Write `playwright.started.md`, then wait for `handoff-1.ready` or relay notice.
        2. For each handoff round N, run the authorized Playwright test-app checks.
        3. Write `playwright-N.md`; its first line must be exactly `PASS` or `FAIL`.
           Include tested commit/worktree, commands, browser/version, routes and visible
           assertions, database/backend, artifacts, failures, limitations, process cleanup,
           and privacy confirmation.
        4. Create `playwright-N.ready` and wait for another round.
        5. Never include credentials, private payloads, prompts, provider responses,
           endpoints beyond local test URLs, or raw provider errors.
        """
    )
    return join_prompt_sections(
        "# Role: independent Playwright test agent",
        introduction,
        common_project_guidance(project),
        "## Overall task context",
        task,
        "## Focused Playwright task",
        playwright_task,
        rules,
        deliverable,
    )


def django_expert_prompt(project: Path, coord: Path, task: str, django_task: str) -> str:
    introduction = textwrap.dedent(
        """
        You are a read-only senior Django expert. Do not edit tracked files, commit,
        push, merge, publish, deploy, change dependencies, or access credentials/private
        project data. Review actual Django behavior, public APIs, lifecycle, database
        semantics, test architecture, and operational best practices independently.
        """
    )
    standard = textwrap.dedent(
        """
        ## Django review standard

        - Wait for each `handoff-N.ready`, then inspect the full diff and handoff.
        - Prioritize supported Django APIs, settings/app lifecycle, migrations, ORM
          semantics, transaction/test-database behavior, backend portability within the
          authorized PostgreSQL scope, security boundaries, maintainability, and CI.
        - Distinguish blocking correctness/security findings from optional style or future
          best practices. Do not demand speculative abstractions or out-of-scope features.
        - Run read-only focused checks when useful and report exact environments/results.
        - Treat browser and generic reviewer evidence as inputs, not substitutes for your
          own Django-specific inspection.
        """
    )
    deliverable = textwrap.dedent(
        f"""
        ## Deliverable

        Coordination directory: `{coord}`

        1. Write `django.started.md`, then wait for `handoff-1.ready` or relay notice.
        2. For each round N, write `django-review-N.md`; its first line must be exactly
           `ADVISORY_APPROVED` or `ISSUES_FOUND`.
        3. Include severity-ordered findings with file/line references, focused commands,
           concrete corrections, accepted best-practice observations, residual risks,
           limitations, and privacy confirmation.
        4. Create `django-review-N.ready` and wait for another round.
        5. Never edit source or include credentials, private payloads, provider responses,
           endpoints, or raw external errors.
        """
    )
    return join_prompt_sections(
        "# Role: independent senior Django expert",
        introduction,
        common_project_guidance(project),
        "## Overall task context",
        task,
        "## Focused Django review task",
        django_task,
        standard,
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
                title = "RELAY + STATUS"
            else:
                manifest["roles"][label]["pane_id"] = pane_id
                role = manifest["roles"][label]
                title = f"{label.upper()} · {role['provider']}/{role['model']} · {role['thinking']}"
            tmux(["select-pane", "-t", pane_id, "-T", title])

        tmux(["set-option", "-q", "-t", window_target, "@pi_agents_coord", str(coord)])
        tmux(["set-option", "-q", "-t", window_target, "@pi_agents_project", str(project)])
        tmux(["set-option", "-q", "-t", window_target, "@pi_agents_version", "1"])
        save_manifest(coord, manifest)

        for role_name in roles:
            pane_id = manifest["roles"][role_name]["pane_id"]
            command = shlex.join(
                [
                    str(SCRIPT_PATH),
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

        relay_command = shlex.join(
            [
                str(SCRIPT_PATH),
                "_relay",
                "--state-root",
                str(coord.parent.parent),
                "--coord",
                str(coord),
            ]
        )
        tmux(["respawn-pane", "-k", "-t", manifest["monitor_pane_id"], relay_command])
    except Exception:
        tmux(["kill-session", "-t", session_target], check=False)
        raise


def attach_session(session: str) -> None:
    target = exact_session_target(session)
    if os.environ.get("TMUX"):
        tmux(["switch-client", "-t", target])
    else:
        os.execvp(command_path("tmux"), ["tmux", "attach", "-t", target])


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

    if args.with_playwright:
        if args.playwright_task is None and args.playwright_task_file is None:
            playwright_task = (
                "Run an independent browser smoke against the actual local test application "
                "after each implementer handoff. Verify the task's user-visible behavior and "
                "a relevant failure path with synthetic data, then report limitations.\n"
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
                "Independently review each handoff for Django ORM, settings, lifecycle, "
                "database, security, testing, and operational best practices. Separate "
                "blocking findings from optional future improvements.\n"
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

    session = validate_session_name(args.session or f"pi-{slugify(project.name)}-agents")
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
    configs = {role: role_config(args, role) for role in roles}
    if not args.skip_model_check:
        for role, config in configs.items():
            validate_model(role, config)

    data: dict[str, Any] = {
        "project": str(project),
        "session": session,
        "roles": [public_role(role, configs[role]) for role in roles],
        "monitor": {"kind": "relay/status"},
        "trust": {
            "child_bypass": bool(args.approve_project),
            "policy": "approve" if args.approve_project else "native-prompts",
        },
        "dry_run": bool(args.dry_run),
        "paths": {
            "state_root": str(absolute_path(STATE_ROOT)),
            "coordination": None,
        },
        "state_retained_on_stop": True,
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
    human_print("  monitor: relay/status")
    human_print(f"Child project trust bypass: {'enabled' if args.approve_project else 'disabled'}")
    if args.dry_run:
        human_print("Dry run complete; no files, sessions, or model requests were created.")
        return CommandResult(data=data)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = canonical_state_root(create=True)
    session_root = ensure_private_directory(root / session)
    coord = ensure_private_directory(session_root / f"{timestamp}-{os.getpid()}")

    try:
        secure_write(coord / "startup-state", "STARTING\n")
        secure_write(coord / "task.md", task)
        if probe_task is not None:
            secure_write(coord / "probe-task.md", probe_task)
        if playwright_task is not None:
            secure_write(coord / "playwright-task.md", playwright_task)
        if django_task is not None:
            secure_write(coord / "django-task.md", django_task)

        prompt_paths = {
            "implementer": coord / "implementer.prompt.md",
            "reviewer": coord / "reviewer.prompt.md",
        }
        secure_write(prompt_paths["implementer"], implementer_prompt(project, coord, task))
        secure_write(prompt_paths["reviewer"], reviewer_prompt(project, coord, task))
        if probe_task is not None:
            prompt_paths["probe"] = coord / "probe.prompt.md"
            secure_write(prompt_paths["probe"], probe_prompt(project, coord, task, probe_task))
        if playwright_task is not None:
            prompt_paths["playwright"] = coord / "playwright.prompt.md"
            secure_write(
                prompt_paths["playwright"],
                playwright_prompt(project, coord, task, playwright_task),
            )
        if django_task is not None:
            prompt_paths["django"] = coord / "django.prompt.md"
            secure_write(
                prompt_paths["django"],
                django_expert_prompt(project, coord, task, django_task),
            )

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
    selected_sessions = sessions[:MAX_JSON_ITEMS] if JSON_MODE else sessions
    for session, coord in selected_sessions:
        try:
            manifest = load_manifest(coord, expected_session=session)
            role_values = [
                public_role(role, config) for role, config in manifest["roles"].items()
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
            "truncated": JSON_MODE and len(sessions) > len(selected_sessions),
            "total_sessions": len(sessions),
        }
    )


def coordination_files(coord: Path) -> list[tuple[Path, os.stat_result]]:
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
            raise OrchestrationError("tmux returned invalid pane metadata", "invalid_tmux_output")
        index, pane_id, pid, current_command, dead, title = columns
        pane = {
            "index": int(index),
            "id": pane_id,
            "pid": int(pid),
            "command": bounded_message(current_command, 128),
            "dead": dead == "1",
            "title": bounded_message(title, 256),
        }
        if not JSON_MODE or len(panes) < MAX_JSON_ITEMS:
            panes.append(pane)
        human_print(
            f"  pane={index} id={pane_id} pid={pid} cmd={current_command} "
            f"dead={dead} title={title}"
        )
    human_print("Coordination files:")
    files = coordination_files(coord)
    selected_files = files[:MAX_JSON_ITEMS] if JSON_MODE else files
    file_values = [
        {"name": path.name, "size_bytes": metadata.st_size}
        for path, metadata in selected_files
    ]
    if not files:
        human_print("  waiting for agent status")
    for path, metadata in files:
        human_print(f"  {path.name}: {metadata.st_size} bytes")
    return CommandResult(
        data={
            "session": session,
            "project": manifest["project"],
            "paths": {"coordination": str(coord)},
            "roles": [
                public_role(role, config) for role, config in manifest["roles"].items()
            ],
            "panes": panes,
            "files": file_values,
            "truncated": {
                "panes": JSON_MODE and len(result.stdout.splitlines()) > len(panes),
                "files": JSON_MODE and len(files) > len(selected_files),
            },
        }
    )


def attach_command(args: argparse.Namespace) -> CommandResult:
    if getattr(args, "json_output", False):
        raise OrchestrationError(
            "attach is interactive-only and cannot be used with --json",
            "interactive_only",
        )
    session, coord = resolve_session(args.session)
    load_manifest(coord, expected_session=session)
    attach_session(session)
    return CommandResult(data={"session": session})


def send_keys(pane_id: str, message: str) -> None:
    tmux(["send-keys", "-t", pane_id, "-l", "--", message])
    tmux(["send-keys", "-t", pane_id, "Enter"])


def send_command(args: argparse.Namespace) -> CommandResult:
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord, expected_session=session)
    if args.role not in manifest["roles"]:
        available = ", ".join(manifest["roles"].keys())
        raise OrchestrationError(f"Role {args.role!r} is not in {session}; available: {available}")
    message = read_text_argument(args.message, args.message_file, "message").strip()
    send_keys(manifest["roles"][args.role]["pane_id"], message)
    human_print(f"Sent message to {session}/{args.role}")
    return CommandResult(data={"session": session, "role": args.role, "sent": True})


def restart_command(args: argparse.Namespace) -> CommandResult:
    if not args.yes:
        raise OrchestrationError("restart replaces the role's Pi conversation; pass --yes")
    session, coord = resolve_session(args.session)
    manifest = load_manifest(coord, expected_session=session)
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
    if started.exists() or started.is_symlink():
        require_regular_file(started, f"{args.role} started state")
        started.unlink()
    command = shlex.join(
        [
            str(SCRIPT_PATH),
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
    human_print(
        f"Restarted {session}/{args.role} with "
        f"{role['provider']}/{role['model']} thinking={role['thinking']}"
    )
    return CommandResult(
        data={"session": session, "role": public_role(args.role, role), "restarted": True}
    )


def stop_command(args: argparse.Namespace) -> CommandResult:
    if not args.yes:
        raise OrchestrationError("stop kills the selected tmux agent grid; pass --yes")
    session, coord = resolve_session(args.session)
    load_manifest(coord, expected_session=session)
    tmux(["kill-session", "-t", exact_session_target(session)])
    human_print(f"Stopped {session}")
    human_print(f"Coordination state retained at {coord}")
    return CommandResult(
        data={
            "session": session,
            "stopped": True,
            "state_retained": True,
            "paths": {"coordination": str(coord)},
        }
    )


def doctor_command(_: argparse.Namespace) -> CommandResult:
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
                "paths": {"state_root": str(absolute_path(STATE_ROOT))},
            },
            code=1,
            error_code="missing_prerequisite",
            error_message="One or more required local commands are unavailable",
        )

    version = bounded_message(run([command_path("tmux"), "-V"], capture=True).stdout, 128)
    human_print(f"OK   {version}")
    tmux_data: dict[str, Any] = {
        "version": version,
        "server_running": bool(list_tmux_sessions()),
        "extended_keys": None,
        "extended_keys_format": None,
    }
    if tmux_data["server_running"]:
        extended = tmux(["show-options", "-gv", "extended-keys"], check=False, capture=True)
        key_format = tmux(
            ["show-options", "-gv", "extended-keys-format"],
            check=False,
            capture=True,
        )
        extended_value = (
            bounded_message(extended.stdout, 64) if extended.returncode == 0 else "unknown"
        )
        format_value = (
            bounded_message(key_format.stdout, 64) if key_format.returncode == 0 else "unknown"
        )
        label = "OK" if extended_value == "on" else "WARN"
        human_print(f"{label:<4} tmux extended-keys: {extended_value}")
        label = "OK" if format_value == "csi-u" else "WARN"
        human_print(f"{label:<4} tmux extended-keys-format: {format_value}")
        tmux_data["extended_keys"] = extended_value
        tmux_data["extended_keys_format"] = format_value
    else:
        human_print("INFO tmux server is not running; extended-key options were not inspected")

    model_checks: list[dict[str, Any]] = []
    for role, config in DEFAULT_MODELS.items():
        available, detail = model_available(config["provider"], config["model"])
        label = "OK" if available else "WARN"
        human_print(f"{label:<4} {role}: {config['provider']}/{config['model']} ({detail})")
        model_checks.append(
            {
                "role": role,
                "provider": config["provider"],
                "model": config["model"],
                "available": available,
                "detail": bounded_message(detail, 256),
            }
        )
    human_print(f"OK   state root: {STATE_ROOT}")
    return CommandResult(
        data={
            "commands": command_checks,
            "tmux": tmux_data,
            "model_checks": model_checks,
            "paths": {"state_root": str(absolute_path(STATE_ROOT))},
        }
    )


def run_agent_command(args: argparse.Namespace) -> int:
    global STATE_ROOT
    STATE_ROOT = Path(args.state_root)
    coord = absolute_path(Path(args.coord))
    manifest = load_manifest(coord)
    role = manifest["roles"].get(args.role)
    if role is None:
        raise OrchestrationError(f"Unknown role in manifest: {args.role}")
    project = manifest["project"]
    prompt_path = Path(role["prompt_path"])
    require_regular_file(prompt_path, "role prompt", nonempty=True)
    ensure_private_directory(Path(role["session_dir"]), parents=True)
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


def relay_send(manifest: dict[str, Any], role: str, message: str) -> bool:
    role_config_value = manifest["roles"].get(role)
    if not role_config_value:
        return False
    try:
        send_keys(role_config_value["pane_id"], message)
    except (OrchestrationError, subprocess.CalledProcessError):
        return False
    return True


def seen_path(seen_dir: Path, token: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", token)
    return seen_dir / safe


def mark_seen(seen_dir: Path, token: str) -> None:
    secure_write(seen_path(seen_dir, token), "")


def is_seen(seen_dir: Path, token: str) -> bool:
    path = seen_path(seen_dir, token)
    if not path.exists() and not path.is_symlink():
        return False
    require_regular_file(path, "relay delivery state")
    return True


def report_first_line(path: Path) -> str:
    metadata = require_regular_file(path, f"coordination report {path.name}", nonempty=True)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OrchestrationError("Cannot safely inspect coordination report") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise OrchestrationError("Coordination report changed while opening")
        first_line = os.read(descriptor, 257).split(b"\n", 1)[0]
        if len(first_line) > 256:
            raise OrchestrationError("Coordination report first line is too long")
        try:
            return first_line.decode("utf-8").rstrip("\r")
        except UnicodeDecodeError as error:
            raise OrchestrationError("Coordination report first line is not valid UTF-8") from error
    finally:
        os.close(descriptor)


def ready_report_is_valid(
    marker: Path,
    report: Path,
    allowed_first_lines: frozenset[str] | None = None,
) -> bool:
    try:
        require_regular_file(marker, f"coordination marker {marker.name}")
        require_regular_file(report, f"coordination report {report.name}", nonempty=True)
        if allowed_first_lines is not None and report_first_line(report) not in allowed_first_lines:
            return False
    except OrchestrationError:
        return False
    return True


def deliver_marker(
    manifest: dict[str, Any],
    seen_dir: Path,
    token: str,
    recipients: dict[str, str],
) -> None:
    enabled = {role: message for role, message in recipients.items() if role in manifest["roles"]}
    for role, message in enabled.items():
        recipient_token = f"{token}--{role}"
        if is_seen(seen_dir, recipient_token):
            continue
        if relay_send(manifest, role, message):
            mark_seen(seen_dir, recipient_token)
    if enabled and all(is_seen(seen_dir, f"{token}--{role}") for role in enabled):
        mark_seen(seen_dir, token)


def relay_once(coord: Path, manifest: dict[str, Any], seen_dir: Path) -> None:
    coord = validate_coordination_directory(coord)
    seen_dir = ensure_private_directory(seen_dir, parents=True)
    for marker in sorted(coord.glob("handoff-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"handoff-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"handoff-{round_number}.md"
        if not ready_report_is_valid(marker, report):
            continue
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {
                "reviewer": (
                    f"Coordination notice: implementer handoff round {round_number} is ready at "
                    f"{report}. Review it now and write review-{round_number}.md plus "
                    f"review-{round_number}.ready."
                ),
                "playwright": (
                    f"Coordination notice: implementer handoff round {round_number} is ready at "
                    f"{report}. Run the browser test now and write playwright-{round_number}.md "
                    f"plus playwright-{round_number}.ready."
                ),
                "django": (
                    f"Coordination notice: implementer handoff round {round_number} is ready at "
                    f"{report}. Run the Django expert review now and write "
                    f"django-review-{round_number}.md plus django-review-{round_number}.ready."
                ),
            },
        )

    for marker in sorted(coord.glob("playwright-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"playwright-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"playwright-{round_number}.md"
        if not ready_report_is_valid(marker, report, frozenset({"PASS", "FAIL"})):
            continue
        message = (
            f"Coordination notice: Playwright report round {round_number} is ready at "
            f"{report}. Evaluate the evidence and failures."
        )
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {"implementer": message, "reviewer": message},
        )

    for marker in sorted(coord.glob("django-review-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"django-review-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"django-review-{round_number}.md"
        if not ready_report_is_valid(
            marker,
            report,
            frozenset({"ADVISORY_APPROVED", "ISSUES_FOUND"}),
        ):
            continue
        message = (
            f"Coordination notice: Django expert review round {round_number} is ready at "
            f"{report}. Evaluate the findings and best-practice recommendations within "
            "authorized scope."
        )
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {"implementer": message, "reviewer": message},
        )

    for marker in sorted(coord.glob("review-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"review-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"review-{round_number}.md"
        if not ready_report_is_valid(
            marker,
            report,
            frozenset({"APPROVED", "CHANGES_REQUESTED"}),
        ):
            continue
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {
                "implementer": (
                    f"Coordination notice: reviewer response round {round_number} is ready at "
                    f"{report}. Read it now; address CHANGES_REQUESTED or write "
                    "implementation-ready.md if APPROVED."
                )
            },
        )

    probe_marker = coord / "probe.ready"
    if (
        (probe_marker.exists() or probe_marker.is_symlink())
        and not is_seen(seen_dir, probe_marker.name)
        and ready_report_is_valid(probe_marker, coord / "probe.md")
    ):
        message = (
            f"Coordination notice: the independent probe is ready at {coord}/probe.md. "
            "Use valid evidence while preserving its stated limitations."
        )
        deliver_marker(
            manifest,
            seen_dir,
            probe_marker.name,
            {"implementer": message, "reviewer": message},
        )

    ready = coord / "implementation-ready.md"
    if (
        (ready.exists() or ready.is_symlink())
        and not is_seen(seen_dir, ready.name)
        and ready_report_is_valid(ready, ready)
    ):
        deliver_marker(
            manifest,
            seen_dir,
            ready.name,
            {
                "reviewer": (
                    f"Coordination notice: {ready} exists. Confirm the latest round is approved "
                    "and remain available for final questions."
                )
            },
        )


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
            exact_window_target(session, manifest["window"]),
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
    for path, metadata in files:
        print(f"  {path.name:<30} {metadata.st_size:>7} bytes")
    print(
        "\nRelay: handoff → reviewer/playwright/django; specialist reports → "
        "implementer/reviewer; review → implementer; probe → both"
    )
    print(f"Attach/switch: pi-tmux-agents attach {session}")
    print(f"Status:        pi-tmux-agents status {session}")
    print(f"Stop:          pi-tmux-agents stop {session} --yes")
    sys.stdout.flush()


def relay_command(args: argparse.Namespace) -> int:
    global STATE_ROOT
    STATE_ROOT = Path(args.state_root)
    coord = absolute_path(Path(args.coord))
    manifest = load_manifest(coord)
    seen_dir = ensure_private_directory(coord / ".relay-seen", parents=True)
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
    parser = OrchestrationArgumentParser(
        prog="pi-tmux-agents",
        description="Run coordinated Pi implementer/reviewer/probe agents in a tmux grid.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              pi-tmux-agents doctor
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --approve-project
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --with-probe --attach
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md \\
                --with-probe --with-playwright
              pi-tmux-agents status pi-my-project-agents
              pi-tmux-agents restart pi-my-project-agents --role implementer \\
                --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit one versioned JSON object on stdout",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="create and start an agent grid")
    start.add_argument("--project", default=os.getcwd())
    start.add_argument("--task")
    start.add_argument("--task-file")
    start.add_argument("--session")
    start.add_argument("--with-probe", action="store_true")
    start.add_argument("--probe-task")
    start.add_argument("--probe-task-file")
    start.add_argument("--with-playwright", action="store_true")
    start.add_argument("--playwright-task")
    start.add_argument("--playwright-task-file")
    start.add_argument("--with-django-expert", action="store_true")
    start.add_argument("--django-task")
    start.add_argument("--django-task-file")
    start.add_argument("--approve-project", action="store_true")
    start.add_argument("--attach", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--skip-model-check", action="store_true")
    for role_name in ("implementer", "reviewer", "probe", "playwright", "django"):
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
    send.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
    send.add_argument("--message")
    send.add_argument("--message-file")
    send.set_defaults(handler=send_command)

    restart = subparsers.add_parser("restart", help="restart one role, optionally changing model")
    restart.add_argument("session")
    restart.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
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
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--coord", required=True)
    if command == "_run-agent":
        parser.add_argument("--role", required=True)
        parser.set_defaults(handler=run_agent_command)
    else:
        parser.set_defaults(handler=relay_command)
    return parser.parse_args(argv[1:])


def requested_command(argv: list[str]) -> str:
    public_commands = {"doctor", "list", "status", "start", "attach", "send", "restart", "stop"}
    for value in argv:
        if value == "--json":
            continue
        if value.startswith("-"):
            return "unknown"
        return value if value in public_commands else "unknown"
    return "unknown"


def main() -> int:
    global JSON_MODE
    argv = sys.argv[1:]
    internal = parse_internal_command(argv)
    if internal is not None:
        try:
            return int(internal.handler(internal))
        except OrchestrationError as error:
            eprint(f"error: {bounded_message(error)}")
            return 2
        except subprocess.CalledProcessError as error:
            eprint(f"error: local command failed ({error.returncode})")
            if error.stderr:
                eprint(bounded_message(error.stderr))
            return error.returncode or 1

    JSON_MODE = "--json" in argv
    command = requested_command(argv)
    if JSON_MODE and "--version" in argv:
        emit_json("version", CommandResult(data={"version": VERSION}))
        return 0
    if JSON_MODE and any(value in {"-h", "--help"} for value in argv):
        result = CommandResult(
            code=2,
            error_code="interactive_help_only",
            error_message="CLI help is human-readable; omit --json to display it",
        )
        emit_json(command, result)
        return result.code
    parse_argv = list(argv)
    if JSON_MODE:
        parse_argv.remove("--json")
        parse_argv.insert(0, "--json")
    try:
        parser = build_parser()
        args = parser.parse_args(parse_argv)
        command = args.command
        outcome = args.handler(args)
        result = outcome if isinstance(outcome, CommandResult) else CommandResult(code=int(outcome))
    except OrchestrationError as error:
        result = CommandResult(
            code=2,
            error_code=error.code,
            error_message=bounded_message(error),
        )
    except subprocess.CalledProcessError as error:
        result = CommandResult(
            code=error.returncode or 1,
            error_code="subprocess_failed",
            error_message=f"A required local command failed with exit code {error.returncode}",
        )
    except (OSError, ValueError) as error:
        result = CommandResult(
            code=1,
            error_code="local_runtime_error",
            error_message="A bounded local runtime error prevented the command from completing",
        )
        if not JSON_MODE:
            result.error_message = bounded_message(error)
    except Exception:
        result = CommandResult(
            code=1,
            error_code="internal_error",
            error_message="An unexpected internal error prevented the command from completing",
        )

    if JSON_MODE:
        emit_json(command, result)
    elif result.code != 0:
        eprint(f"error: {bounded_message(result.error_message or 'command failed')}")
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
