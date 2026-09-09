"""Descriptor-relative reads of explicit user-owned global registry resources."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .models import OrchestrationError

MAX_RESOURCE_PATH = 1024


def global_resource_path(value: object, project: Path) -> Path:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_RESOURCE_PATH
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or not value.startswith("/")
        or value.startswith("//")
        or os.path.normpath(value) != value
    ):
        raise OrchestrationError(
            "Registry paths must be bounded canonical absolute paths"
        )
    path = Path(value)
    if path == project or project in path.parents:
        raise OrchestrationError(
            "Custom-role registry resources must be outside the project"
        )
    return path


def _safe_directory(metadata: os.stat_result) -> bool:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.getuid()}:
        return False
    writable = metadata.st_mode & 0o022
    root_sticky = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
    return not writable or bool(root_sticky)


def _check_directory(descriptor: int, blocked: os.stat_result) -> None:
    directory = os.fstat(descriptor)
    if (directory.st_dev, directory.st_ino) == (blocked.st_dev, blocked.st_ino):
        raise OrchestrationError(
            "Registry resource directory aliases the target project"
        )
    if not _safe_directory(directory):
        raise OrchestrationError(
            "Registry resource directory ownership or permissions are unsafe"
        )


def _open_resource(path: Path, project: Path) -> int:
    try:
        blocked = project.stat()
    except OSError as error:
        raise OrchestrationError(
            "Registry validation project is unavailable"
        ) from error
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    parent = os.open("/", flags | os.O_DIRECTORY)
    try:
        _check_directory(parent, blocked)
        for component in path.parts[1:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = child
            _check_directory(parent, blocked)
        return os.open(path.name, flags | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def read_global_resource(
    path: Path, limit: int, *, project: Path, missing_ok: bool = False
) -> bytes | None:
    descriptor = None
    try:
        descriptor = _open_resource(path, project)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or not 0 < before.st_size <= limit
        ):
            raise OrchestrationError(
                "Registry resource type, ownership, permissions, or size is unsafe"
            )
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(8192, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) > limit or _file_identity(before) != _file_identity(after):
            raise OrchestrationError(
                "Registry resource changed during validation or exceeds its size limit"
            )
        content.decode("utf-8")
        return bytes(content)
    except FileNotFoundError as error:
        if missing_ok:
            return None
        raise OrchestrationError("Registry resource is missing") from error
    except (OSError, UnicodeError) as error:
        raise OrchestrationError(
            "Registry resource cannot be read safely as UTF-8"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
