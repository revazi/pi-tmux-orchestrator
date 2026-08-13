"""Storage support for Pi tmux orchestration."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from . import runtime
from .constants import (
    CONTROLLER_PI_SESSION_ID,
    CONTROLLER_STATE_FIELDS,
    CONTROLLER_STATE_VERSION,
    CONTROLLER_TMUX_SESSION,
    CONTROLLER_WINDOW,
    BROKER_COORDINATION,
    BROKER_PROTOCOL_VERSION,
    KNOWN_ROLES,
    MANIFEST_FIELDS,
    MANIFEST_V1_FIELDS,
    MANIFEST_V3_FIELDS,
    MAX_CONTROLLER_STATE_BYTES,
    MAX_MANIFEST_BYTES,
    PANE_ID_PATTERN,
    READ_ONLY_TOOLS,
    ROLE_FIELDS,
    ROLE_V3_FIELDS,
    RPC_TRANSPORT,
    THINKING_LEVELS,
    TUI_TRANSPORT,
    WINDOW,
)
from .models import OrchestrationError
from .tmux import validate_session_name


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def configured_controller_root() -> Path:
    configured = os.environ.get("PI_TMUX_CONTROLLER_HOME")
    if configured:
        return absolute_path(Path(configured))
    return absolute_path(runtime.STATE_ROOT).parent / "orchestrator-controller"


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
        raise OrchestrationError(
            "Cannot safely open private state directory"
        ) from error
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
            raise OrchestrationError(
                "Cannot create private state directory at filesystem root"
            )
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
            raise OrchestrationError(
                "Cannot set private state directory permissions"
            ) from error
        finally:
            os.close(descriptor)
    return canonical


def canonical_state_root(*, create: bool) -> Path:
    root = absolute_path(runtime.STATE_ROOT)
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        if not create:
            raise OrchestrationError("Orchestration state root does not exist")
        return ensure_private_directory(root, parents=True)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OrchestrationError(
            "Orchestration state root must be a non-symlink directory"
        )
    return ensure_private_directory(root)


def validate_coordination_directory(coord: Path) -> Path:
    root = canonical_state_root(create=False)
    candidate = absolute_path(coord)
    require_directory(candidate, "coordination run directory")
    canonical = candidate.resolve(strict=True)
    try:
        relative = canonical.relative_to(root)
    except ValueError as error:
        raise OrchestrationError(
            "Coordination path is outside the configured state root"
        ) from error
    if len(relative.parts) != 2:
        raise OrchestrationError(
            "Coordination path must identify one session and one run"
        )
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
                raise OrchestrationError(
                    "Private state destination must be a regular file"
                )
            if (
                before is not None
                and not hasattr(os, "O_NOFOLLOW")
                and (opened.st_dev != before.st_dev or opened.st_ino != before.st_ino)
            ):
                raise OrchestrationError(
                    "Private state destination changed during write"
                )
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
    version = value.get("version")
    if type(version) is not int or version not in {1, 2, 3}:
        raise OrchestrationError("Unsupported orchestration manifest version")
    expected_fields = {
        1: MANIFEST_V1_FIELDS,
        2: MANIFEST_FIELDS,
        3: MANIFEST_V3_FIELDS,
    }[version]
    if set(value) != expected_fields:
        raise OrchestrationError(
            "Orchestration manifest has missing or unknown top-level fields"
        )
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
    if session != coord.parent.name or (
        expected_session is not None and session != expected_session
    ):
        raise OrchestrationError(
            "Manifest session does not match its coordination path"
        )
    if value["window"] != WINDOW or not isinstance(value["window"], str):
        raise OrchestrationError("Manifest window is invalid")
    if value["coord"] != str(coord) or not isinstance(value["coord"], str):
        raise OrchestrationError("Manifest coordination path is not canonical")
    if type(value["approve_project"]) is not bool:
        raise OrchestrationError("Manifest approve_project must be a boolean")
    if version >= 2 and value["transport"] not in {TUI_TRANSPORT, RPC_TRANSPORT}:
        raise OrchestrationError("Manifest transport is invalid")
    if version == 3 and (
        value["coordination"] != BROKER_COORDINATION
        or value["protocol_version"] != BROKER_PROTOCOL_VERSION
    ):
        raise OrchestrationError("Manifest broker coordination protocol is invalid")

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
    if not isinstance(monitor_pane_id, str) or not PANE_ID_PATTERN.fullmatch(
        monitor_pane_id
    ):
        raise OrchestrationError("Manifest monitor pane ID is invalid")
    roles = value["roles"]
    if not isinstance(roles, dict):
        raise OrchestrationError("Manifest roles must be an object")
    if not {"implementer", "reviewer"}.issubset(roles) or not set(roles).issubset(
        KNOWN_ROLES
    ):
        raise OrchestrationError("Manifest roles contain missing or unknown role names")

    pane_ids = {monitor_pane_id}
    for role_name, role in roles.items():
        expected_role_fields = ROLE_V3_FIELDS if version == 3 else ROLE_FIELDS
        if not isinstance(role, dict) or set(role) != expected_role_fields:
            raise OrchestrationError(f"Manifest role {role_name} has invalid fields")
        for field in ("provider", "model"):
            field_value = role[field]
            if (
                not isinstance(field_value, str)
                or not field_value
                or len(field_value) > 256
            ):
                raise OrchestrationError(
                    f"Manifest role {role_name} has invalid {field}"
                )
        if role["thinking"] not in THINKING_LEVELS or not isinstance(
            role["thinking"], str
        ):
            raise OrchestrationError(
                f"Manifest role {role_name} has invalid thinking level"
            )
        expected_tools = None if role_name == "implementer" else READ_ONLY_TOOLS
        if role["tools"] != expected_tools:
            raise OrchestrationError(
                f"Manifest role {role_name} has invalid tool configuration"
            )
        pane_id = role["pane_id"]
        if not isinstance(pane_id, str) or not PANE_ID_PATTERN.fullmatch(pane_id):
            raise OrchestrationError(f"Manifest role {role_name} has invalid pane ID")
        if pane_id in pane_ids:
            raise OrchestrationError("Manifest pane IDs must be unique")
        pane_ids.add(pane_id)

        session_value = role["session_dir"]
        if not isinstance(session_value, str):
            raise OrchestrationError(f"Manifest role {role_name} paths must be strings")
        expected_session_dir = coord / "sessions" / role_name
        if Path(session_value) != expected_session_dir:
            raise OrchestrationError(f"Manifest role {role_name} paths are invalid")
        if version == 3:
            session_id = role["session_id"]
            if not isinstance(session_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,128}", session_id
            ):
                raise OrchestrationError(
                    f"Manifest role {role_name} session ID is invalid"
                )
        else:
            prompt_value = role["prompt_path"]
            expected_prompt = coord / f"{role_name}.prompt.md"
            if (
                not isinstance(prompt_value, str)
                or Path(prompt_value) != expected_prompt
            ):
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
                raise OrchestrationError(
                    f"Manifest role {role_name} session path is not canonical"
                )
    return value


def atomic_secure_write(path: Path, content: str, label: str) -> None:
    path = absolute_path(path)
    parent = ensure_private_directory(path.parent, parents=True)
    destination = parent / path.name
    if destination.exists() or destination.is_symlink():
        require_regular_file(destination, label)
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    try:
        secure_write(temporary, content, exclusive=True)
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
        directory_descriptor = open_directory(parent)
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


def atomic_secure_create(path: Path, content: str, label: str) -> None:
    path = absolute_path(path)
    parent = ensure_private_directory(path.parent, parents=True)
    destination = parent / path.name
    if destination.exists() or destination.is_symlink():
        raise OrchestrationError(f"{label} already exists")
    temporary = parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_created = False
    try:
        secure_write(temporary, content, exclusive=True)
        temporary_created = True
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as error:
            raise OrchestrationError(f"{label} already exists") from error
        except OSError as error:
            raise OrchestrationError(f"Cannot create {label}") from error
        temporary.unlink()
        temporary_created = False
        directory_descriptor = open_directory(parent)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_created:
            try:
                metadata = temporary.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(
                    metadata.st_mode
                ):
                    temporary.unlink()


def save_manifest(coord: Path, manifest: dict[str, Any]) -> None:
    coord = validate_coordination_directory(coord)
    validate_manifest(manifest, coord, expected_session=manifest.get("session"))
    atomic_secure_write(
        coord / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        "orchestration manifest",
    )


def load_manifest(
    coord: Path, *, expected_session: str | None = None
) -> dict[str, Any]:
    coord = validate_coordination_directory(coord)
    manifest_path = coord / "manifest.json"
    try:
        content = read_regular_file(
            manifest_path, "orchestration manifest", MAX_MANIFEST_BYTES
        )
        value = json.loads(content)
    except UnicodeDecodeError as error:
        raise OrchestrationError("Orchestration manifest is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise OrchestrationError("Orchestration manifest is not valid JSON") from error
    return validate_manifest(value, coord, expected_session=expected_session)


def manifest_transport(manifest: dict[str, Any]) -> str:
    return manifest.get("transport", TUI_TRANSPORT)


def retained_coordination(session: str, run_id: str | None = None) -> Path:
    validate_session_name(session)
    root = canonical_state_root(create=False)
    session_root = absolute_path(root / session)
    require_directory(session_root, "orchestration session state")
    if session_root.resolve(strict=True) != session_root or session_root.parent != root:
        raise OrchestrationError("Orchestration session state path is invalid")
    if run_id is not None:
        if run_id in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise OrchestrationError("Coordination run ID is invalid")
        candidates = [session_root / run_id]
    else:
        candidates = sorted(session_root.iterdir(), reverse=True)
    for candidate in candidates:
        try:
            require_directory(candidate, "coordination run")
            coord = validate_coordination_directory(candidate)
            load_manifest(coord, expected_session=session)
        except (FileNotFoundError, OrchestrationError):
            if run_id is not None:
                raise OrchestrationError("Requested coordination run is unavailable")
            continue
        return coord
    raise OrchestrationError(f"No retained orchestration state was found for {session}")


def controller_state_root(*, create: bool) -> Path:
    root = configured_controller_root()
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        if not create:
            raise OrchestrationError("Controller state root does not exist")
        return ensure_private_directory(root, parents=True)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OrchestrationError(
            "Controller state root must be a non-symlink directory"
        )
    return ensure_private_directory(root)


def validate_controller_state(value: object, root: Path) -> dict[str, Any]:
    configured_root = controller_state_root(create=False)
    if absolute_path(root) != configured_root:
        raise OrchestrationError("Controller state path is outside the configured root")
    root = configured_root
    if not isinstance(value, dict) or set(value) != CONTROLLER_STATE_FIELDS:
        raise OrchestrationError("Controller state has missing or unknown fields")
    if (
        type(value["version"]) is not int
        or value["version"] != CONTROLLER_STATE_VERSION
    ):
        raise OrchestrationError("Unsupported controller state version")
    for field in ("created_at", "last_started_at"):
        timestamp = value[field]
        if not isinstance(timestamp, str):
            raise OrchestrationError(f"Controller {field} must be a timestamp string")
        try:
            parsed = dt.datetime.fromisoformat(timestamp)
        except ValueError as error:
            raise OrchestrationError(f"Controller {field} is invalid") from error
        if parsed.tzinfo is None:
            raise OrchestrationError(f"Controller {field} must include a timezone")
    if value["session"] != CONTROLLER_TMUX_SESSION:
        raise OrchestrationError("Controller tmux session is invalid")
    if value["window"] != CONTROLLER_WINDOW:
        raise OrchestrationError("Controller tmux window is invalid")
    if value["pi_session_id"] != CONTROLLER_PI_SESSION_ID:
        raise OrchestrationError("Controller Pi session ID is invalid")

    expected_paths = {
        "root": root,
        "workspace": root / "workspace",
        "session_dir": root / "sessions",
    }
    for field, expected in expected_paths.items():
        raw_path = value[field]
        if not isinstance(raw_path, str) or Path(raw_path) != expected:
            raise OrchestrationError(f"Controller {field} path is invalid")
        require_directory(expected, f"controller {field}")
        if expected.resolve(strict=True) != expected:
            raise OrchestrationError(f"Controller {field} path is not canonical")
    return value


def controller_state_path(root: Path) -> Path:
    return root / "state.json"


def load_controller_state(root: Path) -> dict[str, Any]:
    try:
        content = read_regular_file(
            controller_state_path(root),
            "controller state",
            MAX_CONTROLLER_STATE_BYTES,
        )
        value = json.loads(content)
    except UnicodeDecodeError as error:
        raise OrchestrationError("Controller state is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise OrchestrationError("Controller state is not valid JSON") from error
    return validate_controller_state(value, root)


def save_controller_state(root: Path, state: dict[str, Any]) -> None:
    validate_controller_state(state, root)
    atomic_secure_write(
        controller_state_path(root),
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        "controller state",
    )
