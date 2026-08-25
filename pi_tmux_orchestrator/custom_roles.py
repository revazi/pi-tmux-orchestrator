"""Strict user-global registry for read-only custom specialist roles."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Any

from . import runtime
from .constants import KNOWN_ROLES
from .models import OrchestrationError
from .storage import read_regular_file

CUSTOM_ROLE_REGISTRY_VERSION = 1
CUSTOM_ROLE_REGISTRY_ENV = "PI_TMUX_ORCHESTRATOR_ROLE_REGISTRY"
MAX_CUSTOM_ROLE_REGISTRY_BYTES = 64 * 1024
MAX_CUSTOM_ROLES = 8
MAX_CUSTOM_ROLE_ID_CHARS = 48
MAX_CUSTOM_ROLE_DESCRIPTION_CHARS = 240
MAX_CUSTOM_ROLE_RULE_CHARS = 500
MAX_CUSTOM_ROLE_TEXT_BYTES = 2 * 1024
MAX_CUSTOM_ROLE_PATH_CHARS = 1024
MAX_CUSTOM_ROLE_PATH_BYTES = 4 * 1024
MAX_CUSTOM_ROLE_PROMPT_BYTES = 64 * 1024
MAX_CUSTOM_ROLE_SKILL_BYTES = 256 * 1024
MAX_CUSTOM_ROLE_SKILLS = 4

_REGISTRY_FIELDS = frozenset({"version", "roles"})
_ROLE_FIELDS = frozenset({"id", "description", "assignmentRule", "prompt", "skills"})
_RESOURCE_FIELDS = frozenset({"path", "sha256"})
_DIGEST_CHARS = frozenset("0123456789abcdef")
_ROLE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

# These values are workflow identities, compatibility aliases, tmux/control
# targets, public commands, or authority-bearing names. Custom specialists are
# deliberately namespaced away from them before any launch integration exists.
RESERVED_CUSTOM_ROLE_NAMES = frozenset(
    {
        *KNOWN_ROLES,
        "agent",
        "agents",
        "all",
        "browser",
        "browser-tester",
        "broker",
        "builtin",
        "command",
        "control",
        "controller",
        "coder",
        "django-expert",
        "implementor",
        "implementation",
        "legacy",
        "monitor",
        "observer",
        "orchestrator",
        "parent",
        "playwright-tester",
        "review",
        "system",
        "technical-probe",
        "tmux",
        "worker",
        "writer",
        # CLI, extension, Supervisor API, and internal command names.
        "abort",
        "about",
        "attach",
        "capabilities",
        "doctor",
        "events",
        "help",
        "list",
        "models",
        "orchestrate",
        "orchestrations",
        "relay",
        "restart",
        "run-agent",
        "runs",
        "send",
        "sessions",
        "snapshot",
        "start",
        "status",
        "stop",
        "supervisor",
        "usage",
        "watch",
    }
)
RESERVED_CUSTOM_ROLE_PREFIXES = (
    "builtin-",
    "broker-",
    "controller-",
    "django-",
    "implementer-",
    "legacy-",
    "monitor-",
    "or-",
    "orchestrator-",
    "pi-",
    "playwright-",
    "probe-",
    "reviewer-",
    "system-",
    "tmux-",
    "worker-",
    "x-",
)
_RESERVED_AUTHORITY_TOKENS = frozenset(
    {
        "agent",
        "broker",
        "controller",
        "implementer",
        "implementor",
        "monitor",
        "review",
        "reviewer",
        "supervisor",
        "worker",
        "writer",
    }
)


def empty_custom_role_registry() -> dict[str, Any]:
    """Return the compatibility state used when no registry exists."""
    return {
        "version": CUSTOM_ROLE_REGISTRY_VERSION,
        "roles": [],
        "serialized_bytes": 0,
    }


def custom_role_registry_path(project: Path | None = None) -> Path:
    """Resolve the sole user-global registry location and reject project overrides."""
    configured = os.environ.get(CUSTOM_ROLE_REGISTRY_ENV)
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise OrchestrationError(
                f"{CUSTOM_ROLE_REGISTRY_ENV} must be an absolute path"
            )
    else:
        if not runtime.PI_HOME.is_absolute():
            raise OrchestrationError(
                "Pi configuration directory must be an absolute path"
            )
        path = runtime.PI_HOME / "tmux-orchestrator-roles.json"
    return _validate_registry_location(path, project)


def _validate_registry_location(path: Path, project: Path | None) -> Path:
    raw_path = Path(path).expanduser()
    raw_value = os.fspath(raw_path)
    if (
        not raw_path.is_absolute()
        or len(raw_value) > MAX_CUSTOM_ROLE_PATH_CHARS
        or len(raw_value.encode("utf-8")) > MAX_CUSTOM_ROLE_PATH_BYTES
        or not _is_safe_unicode(raw_value, multiline=False)
        or os.path.normpath(raw_value) != raw_value
        or raw_value.endswith(os.sep)
        or raw_path.suffix.lower() != ".json"
    ):
        raise OrchestrationError(
            "Custom role registry path must be a bounded canonical absolute JSON path"
        )
    candidate = Path(os.path.abspath(raw_value))
    if project is not None:
        project_root = project.resolve(strict=True)
        resolved = candidate.resolve(strict=False)
        if resolved == project_root or project_root in resolved.parents:
            raise OrchestrationError(
                "Custom role registry must remain outside the target project"
            )
    return candidate


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _current_uid() -> int:
    return os.getuid()


def _is_safe_unicode(value: str, *, multiline: bool) -> bool:
    for character in value:
        codepoint = ord(character)
        if character == "\n" and multiline:
            continue
        if character == "\t" and multiline:
            continue
        if (
            codepoint < 32
            or 0x7F <= codepoint <= 0x9F
            or character in {"\u2028", "\u2029"}
            or unicodedata.category(character).startswith("C")
        ):
            return False
    return True


def _bounded_text(value: object, label: str, maximum_chars: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum_chars
        or len(value.encode("utf-8")) > MAX_CUSTOM_ROLE_TEXT_BYTES
        or not _is_safe_unicode(value, multiline=False)
    ):
        raise OrchestrationError(f"Custom role {label} is invalid or oversized")
    return value


def custom_role_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= MAX_CUSTOM_ROLE_ID_CHARS
        or value[0] not in "abcdefghijklmnopqrstuvwxyz"
        or value[-1] == "-"
        or "--" in value
        or any(character not in _ROLE_ID_CHARS for character in value)
    ):
        raise OrchestrationError(
            "Custom role id must match a lowercase hyphenated identifier of 3 to 48 characters"
        )
    if (
        value in RESERVED_CUSTOM_ROLE_NAMES
        or value.startswith(RESERVED_CUSTOM_ROLE_PREFIXES)
        or set(value.split("-")).intersection(_RESERVED_AUTHORITY_TOKENS)
    ):
        raise OrchestrationError("Custom role id collides with a reserved name")
    return value


def _canonical_resource_path(value: object, project: Path | None) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_CUSTOM_ROLE_PATH_CHARS
        or len(value.encode("utf-8")) > MAX_CUSTOM_ROLE_PATH_BYTES
        or not _is_safe_unicode(value, multiline=False)
    ):
        raise OrchestrationError("Custom role resource path is invalid or oversized")
    candidate = Path(value)
    if (
        not candidate.is_absolute()
        or os.path.normpath(value) != value
        or value.endswith(os.sep)
    ):
        raise OrchestrationError(
            "Custom role resources require canonical absolute paths"
        )
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as error:
        raise OrchestrationError("Custom role resource is unavailable") from error
    if canonical != candidate:
        raise OrchestrationError(
            "Custom role resources must not contain symlink components"
        )
    if project is not None:
        project_root = project.resolve(strict=True)
        if canonical == project_root or project_root in canonical.parents:
            raise OrchestrationError(
                "Custom role resources must remain outside the target project"
            )
    return canonical


def _validate_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise OrchestrationError(
            "Custom role resource sha256 must be 64 lowercase hexadecimal characters"
        )
    return value


def _resource_metadata(
    value: object,
    kind: str,
    *,
    project: Path | None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _RESOURCE_FIELDS:
        raise OrchestrationError(
            f"Custom role {kind} resource must contain exactly path and sha256"
        )
    path = _canonical_resource_path(value["path"], project)
    digest = _validate_digest(value["sha256"])
    if path.suffix.lower() != ".md":
        raise OrchestrationError("Custom role resources must be Markdown files")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise OrchestrationError("Cannot inspect custom role resource") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OrchestrationError(
            "Custom role resource must be a regular non-symlink file"
        )
    if metadata.st_uid != _current_uid():
        raise OrchestrationError(
            "Custom role resources must be owned by the current user"
        )
    permissions = stat.S_IMODE(metadata.st_mode)
    if permissions & (0o7000 | 0o111) or not permissions & stat.S_IRUSR:
        raise OrchestrationError(
            "Custom role resources must be owner-readable, non-executable, and free of special mode bits"
        )
    if kind == "prompt":
        if permissions & 0o077:
            raise OrchestrationError(
                "Custom role prompt resources must use private mode"
            )
        maximum_bytes = MAX_CUSTOM_ROLE_PROMPT_BYTES
    else:
        if permissions & 0o022:
            raise OrchestrationError(
                "Custom role skill resources must not be group- or world-writable"
            )
        maximum_bytes = MAX_CUSTOM_ROLE_SKILL_BYTES
    if metadata.st_size == 0:
        raise OrchestrationError("Custom role resources must be non-empty")
    if metadata.st_size > maximum_bytes:
        raise OrchestrationError(f"Custom role {kind} resource exceeds its size limit")
    try:
        content = read_regular_file(path, f"custom role {kind} resource", maximum_bytes)
        body = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise OrchestrationError(
            f"Custom role {kind} resource must be readable UTF-8"
        ) from error
    if not body.strip():
        raise OrchestrationError(f"Custom role {kind} resource must be non-empty")
    if not _is_safe_unicode(body, multiline=True):
        raise OrchestrationError(
            f"Custom role {kind} resource contains unsafe Unicode or control characters"
        )
    if hashlib.sha256(content).hexdigest() != digest:
        raise OrchestrationError(f"Custom role {kind} resource digest changed")
    return {
        "path": str(path),
        "sha256": digest,
        "size_bytes": len(content),
    }


def _serialized_size(value: object) -> int:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError, RecursionError) as error:
        raise OrchestrationError(
            "Custom role registry is not serializable JSON"
        ) from error
    if len(encoded) > MAX_CUSTOM_ROLE_REGISTRY_BYTES:
        raise OrchestrationError("Custom role registry exceeds the 64 KiB limit")
    return len(encoded)


def validate_custom_role_registry(
    value: object,
    *,
    project: Path | None = None,
    serialized_bytes: int | None = None,
) -> dict[str, Any]:
    """Validate the exact v1 registry and return body-free resolved definitions."""
    if not isinstance(value, dict) or set(value) != _REGISTRY_FIELDS:
        raise OrchestrationError(
            "Custom role registry must contain exactly version and roles"
        )
    if (
        type(value["version"]) is not int
        or value["version"] != CUSTOM_ROLE_REGISTRY_VERSION
    ):
        raise OrchestrationError(
            f"Custom role registry version must be {CUSTOM_ROLE_REGISTRY_VERSION}"
        )
    raw_roles = value["roles"]
    if not isinstance(raw_roles, list):
        raise OrchestrationError("Custom role registry roles must be an array")
    if len(raw_roles) > MAX_CUSTOM_ROLES:
        raise OrchestrationError(
            f"Custom role registry supports at most {MAX_CUSTOM_ROLES} roles"
        )
    measured_size = _serialized_size(value)
    if serialized_bytes is not None:
        if (
            type(serialized_bytes) is not int
            or not 0 <= serialized_bytes <= MAX_CUSTOM_ROLE_REGISTRY_BYTES
        ):
            raise OrchestrationError("Custom role registry serialized size is invalid")
        measured_size = serialized_bytes

    roles: list[dict[str, Any]] = []
    role_ids: set[str] = set()
    resource_paths: set[str] = set()
    for raw_role in raw_roles:
        if not isinstance(raw_role, dict) or set(raw_role) != _ROLE_FIELDS:
            raise OrchestrationError(
                "Custom role definitions have missing or unsupported fields"
            )
        role_id = custom_role_id(raw_role["id"])
        if role_id in role_ids:
            raise OrchestrationError("Custom role ids must be unique")
        role_ids.add(role_id)
        description = _bounded_text(
            raw_role["description"],
            f"{role_id} description",
            MAX_CUSTOM_ROLE_DESCRIPTION_CHARS,
        )
        assignment_rule = _bounded_text(
            raw_role["assignmentRule"],
            f"{role_id} assignmentRule",
            MAX_CUSTOM_ROLE_RULE_CHARS,
        )
        prompt = _resource_metadata(raw_role["prompt"], "prompt", project=project)
        raw_skills = raw_role["skills"]
        if not isinstance(raw_skills, list) or len(raw_skills) > MAX_CUSTOM_ROLE_SKILLS:
            raise OrchestrationError(
                f"Custom role {role_id} supports at most {MAX_CUSTOM_ROLE_SKILLS} skills"
            )
        skills = [
            _resource_metadata(resource, "skill", project=project)
            for resource in raw_skills
        ]
        role_paths = [prompt["path"], *(skill["path"] for skill in skills)]
        if len(role_paths) != len(set(role_paths)) or resource_paths.intersection(
            role_paths
        ):
            raise OrchestrationError(
                "Custom role resource paths cannot be duplicated or shared"
            )
        resource_paths.update(role_paths)
        roles.append(
            {
                "id": role_id,
                "description": description,
                "assignment_rule": assignment_rule,
                "contract": "read-only-specialist",
                "authority": "supplemental-evidence-only",
                "tool_policy": "workflow-read-only-with-bash",
                "prompt": prompt,
                "skills": skills,
                "lifecycle": "registry-only",
                "launchable": False,
            }
        )
    roles.sort(key=lambda role: role["id"])
    return {
        "version": CUSTOM_ROLE_REGISTRY_VERSION,
        "roles": roles,
        "serialized_bytes": measured_size,
    }


def load_custom_role_registry(
    path: Path | None = None,
    *,
    project: Path | None = None,
) -> dict[str, Any]:
    """Load and validate the global registry without retaining resource bodies."""
    registry_path = (
        _validate_registry_location(path, project)
        if path is not None
        else custom_role_registry_path(project)
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(registry_path, flags)
    except FileNotFoundError:
        return empty_custom_role_registry()
    except OSError as error:
        raise OrchestrationError(
            "Custom role registry cannot be opened safely"
        ) from error
    try:
        try:
            canonical = registry_path.resolve(strict=True)
        except OSError as error:
            raise OrchestrationError(
                "Custom role registry path is unavailable"
            ) from error
        if canonical != registry_path:
            raise OrchestrationError(
                "Custom role registry must use a canonical non-symlink path"
            )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OrchestrationError(
                "Custom role registry must be a regular non-symlink file"
            )
        permissions = stat.S_IMODE(metadata.st_mode)
        if (
            metadata.st_uid != _current_uid()
            or permissions & (0o7000 | 0o133)
            or not permissions & stat.S_IRUSR
        ):
            raise OrchestrationError(
                "Custom role registry must be current-user-owned, owner-readable, non-executable, free of special mode bits, and not group- or world-writable"
            )
        if metadata.st_size > MAX_CUSTOM_ROLE_REGISTRY_BYTES:
            raise OrchestrationError("Custom role registry exceeds the 64 KiB limit")
        chunks: list[bytes] = []
        remaining = MAX_CUSTOM_ROLE_REGISTRY_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > MAX_CUSTOM_ROLE_REGISTRY_BYTES:
            raise OrchestrationError("Custom role registry exceeds the 64 KiB limit")
        value = json.loads(
            content.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except OrchestrationError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise OrchestrationError(
            "Custom role registry is not valid strict UTF-8 JSON"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return validate_custom_role_registry(
        value,
        project=project,
        serialized_bytes=len(content),
    )


def public_custom_role_registry(registry: dict[str, Any]) -> dict[str, Any]:
    """Project bounded digest metadata only; never project resource paths or bodies."""
    roles = [
        {
            "id": role["id"],
            "contract": "read-only-specialist",
            "lifecycle": "registry-only",
            "launchable": False,
            "prompt_sha256": role["prompt"]["sha256"],
            "skill_sha256": [skill["sha256"] for skill in role["skills"]],
        }
        for role in registry["roles"]
    ]
    return {
        "version": CUSTOM_ROLE_REGISTRY_VERSION,
        "configured": bool(roles),
        "count": len(roles),
        "names": [role["id"] for role in roles],
        "roles": roles,
        "lifecycle": "registry-only",
        "launchable": False,
    }
