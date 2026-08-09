"""Durable metadata-only registries and journals for RPC workers."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .constants import (
    KNOWN_ROLES,
    MAX_JSON_ITEMS,
    MAX_RPC_ACK_BYTES,
    MAX_RPC_COMMANDS,
    MAX_RPC_EVENT_BYTES,
    MAX_RPC_EVENT_SEGMENT_BYTES,
    MAX_RPC_REGISTRY_BYTES,
    RPC_COMMAND_EVENT_STATUSES,
    RPC_COMMAND_FIELDS,
    RPC_COMMAND_STATUSES,
    RPC_COMMAND_TRANSITIONS,
    RPC_EVENT_FIELDS,
    RPC_LIFECYCLE_EVENT_STATUSES,
    RPC_REGISTRY_FIELDS,
    RPC_STATE_FIELDS,
    RPC_STATUSES,
    RPC_TERMINAL_COMMAND_STATUSES,
    RPC_TOKEN_PATTERN,
)
from .models import OrchestrationError
from .storage import (
    absolute_path,
    atomic_secure_write,
    ensure_private_directory,
    open_directory,
    read_regular_file,
    require_directory,
    require_regular_file,
    validate_coordination_directory,
)


def rpc_role_paths(
    coord: Path,
    role: str,
    *,
    create: bool,
) -> dict[str, Path]:
    coord = validate_coordination_directory(coord)
    if role not in KNOWN_ROLES:
        raise OrchestrationError(f"Unknown RPC role: {role}")
    root = coord / ".rpc" / role
    if create:
        ensure_private_directory(coord / ".rpc")
        root = ensure_private_directory(root)
        inbox = ensure_private_directory(root / "inbox")
        acks = ensure_private_directory(root / "acks")
    else:
        require_directory(coord / ".rpc", "RPC transport directory")
        root = absolute_path(root)
        require_directory(root, f"{role} RPC directory")
        if root.resolve(strict=True) != root:
            raise OrchestrationError(f"{role} RPC directory is not canonical")
        inbox = root / "inbox"
        acks = root / "acks"
        require_directory(inbox, f"{role} RPC inbox")
        require_directory(acks, f"{role} RPC acknowledgements")
    return {
        "root": root,
        "inbox": inbox,
        "acks": acks,
        "state": root / "state.json",
        "registry": root / "registry.json",
        "events": root / "events.jsonl",
        "events_archive": root / "events.previous.jsonl",
        "event_lock": root / "events.lock",
    }


def rpc_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OrchestrationError(f"{label} must be a timestamp string")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise OrchestrationError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise OrchestrationError(f"{label} must include a timezone")
    return value


def validate_rpc_command(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RPC_COMMAND_FIELDS:
        raise OrchestrationError(
            "RPC command registry entry has missing or unknown fields"
        )
    command_id = value["id"]
    if not isinstance(command_id, str) or not RPC_TOKEN_PATTERN.fullmatch(command_id):
        raise OrchestrationError("RPC command registry identity is invalid")
    command = value["command"]
    if command not in {"prompt", "abort"}:
        raise OrchestrationError("RPC command registry type is invalid")
    delivery = value["delivery"]
    if (command == "prompt" and delivery not in {"steer", "follow-up"}) or (
        command == "abort" and delivery is not None
    ):
        raise OrchestrationError("RPC command registry delivery is invalid")
    if value["status"] not in RPC_COMMAND_STATUSES:
        raise OrchestrationError("RPC command registry status is invalid")
    rpc_timestamp(value["received_at"], "RPC command received_at")
    rpc_timestamp(value["updated_at"], "RPC command updated_at")
    if type(value["event_sequence"]) is not int or value["event_sequence"] <= 0:
        raise OrchestrationError("RPC command event sequence is invalid")
    return value


def validate_rpc_registry(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RPC_REGISTRY_FIELDS:
        raise OrchestrationError("RPC worker registry has missing or unknown fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise OrchestrationError("Unsupported RPC worker registry version")
    if value["role"] != role:
        raise OrchestrationError("RPC worker registry identity is invalid")
    worker_id = value["worker_id"]
    if not isinstance(worker_id, str) or not RPC_TOKEN_PATTERN.fullmatch(worker_id):
        raise OrchestrationError("RPC worker identity is invalid")
    if type(value["generation"]) is not int or value["generation"] < 1:
        raise OrchestrationError("RPC worker generation is invalid")
    if type(value["pid"]) is not int or value["pid"] <= 0:
        raise OrchestrationError("RPC worker registry PID is invalid")
    session_id = value["session_id"]
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id or len(session_id) > 256
    ):
        raise OrchestrationError("RPC worker registry session ID is invalid")
    if value["status"] not in RPC_STATUSES:
        raise OrchestrationError("RPC worker registry status is invalid")
    active = value["active_command_ids"]
    if not isinstance(active, list) or len(active) > MAX_RPC_COMMANDS:
        raise OrchestrationError("RPC worker active command registry is invalid")
    if any(
        not isinstance(item, str) or not RPC_TOKEN_PATTERN.fullmatch(item)
        for item in active
    ):
        raise OrchestrationError("RPC worker active command identity is invalid")
    if len(set(active)) != len(active):
        raise OrchestrationError("RPC worker active commands must be unique")
    if value["last_outcome"] not in {
        None,
        "completed",
        "failed",
        "aborted",
        "uncertain",
    }:
        raise OrchestrationError("RPC worker last outcome is invalid")
    if (
        type(value["last_event_sequence"]) is not int
        or value["last_event_sequence"] < 0
    ):
        raise OrchestrationError("RPC worker event cursor is invalid")
    commands = value["commands"]
    if not isinstance(commands, list) or len(commands) > MAX_RPC_COMMANDS:
        raise OrchestrationError("RPC worker command registry exceeds the safety limit")
    validated = [validate_rpc_command(command) for command in commands]
    command_ids = [command["id"] for command in validated]
    if len(set(command_ids)) != len(command_ids):
        raise OrchestrationError("RPC worker command identities must be unique")
    known = {command["id"] for command in validated}
    if not set(active).issubset(known):
        raise OrchestrationError("RPC worker active command is not registered")
    if any(
        command["id"] in active and command["status"] in RPC_TERMINAL_COMMAND_STATUSES
        for command in validated
    ):
        raise OrchestrationError("Terminal RPC commands cannot remain active")
    rpc_timestamp(value["updated_at"], "RPC worker registry updated_at")
    return value


def validate_rpc_event(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RPC_EVENT_FIELDS:
        raise OrchestrationError("RPC event has missing or unknown fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise OrchestrationError("Unsupported RPC event version")
    if type(value["sequence"]) is not int or value["sequence"] <= 0:
        raise OrchestrationError("RPC event sequence is invalid")
    rpc_timestamp(value["timestamp"], "RPC event timestamp")
    if value["role"] != role:
        raise OrchestrationError("RPC event role identity is invalid")
    worker_id = value["worker_id"]
    if not isinstance(worker_id, str) or not RPC_TOKEN_PATTERN.fullmatch(worker_id):
        raise OrchestrationError("RPC event worker identity is invalid")
    if type(value["generation"]) is not int or value["generation"] < 1:
        raise OrchestrationError("RPC event generation is invalid")
    event_name = value["event"]
    if event_name in RPC_COMMAND_EVENT_STATUSES:
        if value["status"] != RPC_COMMAND_EVENT_STATUSES[event_name]:
            raise OrchestrationError("RPC command event status is invalid")
        command_id = value["command_id"]
        if not isinstance(command_id, str) or not RPC_TOKEN_PATTERN.fullmatch(
            command_id
        ):
            raise OrchestrationError("RPC command event identity is invalid")
        if value["command"] not in {"prompt", "abort"}:
            raise OrchestrationError("RPC command event type is invalid")
        if (
            value["command"] == "prompt"
            and value["delivery"] not in {"steer", "follow-up"}
        ) or (value["command"] == "abort" and value["delivery"] is not None):
            raise OrchestrationError("RPC command event delivery is invalid")
    elif event_name in RPC_LIFECYCLE_EVENT_STATUSES:
        if value["status"] != RPC_LIFECYCLE_EVENT_STATUSES[event_name]:
            raise OrchestrationError("RPC lifecycle event status is invalid")
        if any(
            value[field] is not None for field in ("command_id", "command", "delivery")
        ):
            raise OrchestrationError("RPC lifecycle event contains command metadata")
    else:
        raise OrchestrationError("RPC event type is invalid")
    return value


def validate_rpc_state(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != RPC_STATE_FIELDS:
        raise OrchestrationError("RPC role state has missing or unknown fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise OrchestrationError("Unsupported RPC role state version")
    if value["role"] != role:
        raise OrchestrationError("RPC role state identity is invalid")
    if type(value["pid"]) is not int or value["pid"] <= 0:
        raise OrchestrationError("RPC role state PID is invalid")
    if value["status"] not in RPC_STATUSES:
        raise OrchestrationError("RPC role state status is invalid")
    if type(value["is_streaming"]) is not bool:
        raise OrchestrationError("RPC role streaming state is invalid")
    for field in ("steering_count", "follow_up_count"):
        if type(value[field]) is not int or value[field] < 0:
            raise OrchestrationError("RPC role queue state is invalid")
    session_id = value["session_id"]
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id or len(session_id) > 256
    ):
        raise OrchestrationError("RPC role session ID is invalid")
    updated_at = value["updated_at"]
    if not isinstance(updated_at, str):
        raise OrchestrationError("RPC role update timestamp is invalid")
    try:
        parsed = dt.datetime.fromisoformat(updated_at)
    except ValueError as error:
        raise OrchestrationError("RPC role update timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise OrchestrationError("RPC role update timestamp must include a timezone")
    return value


def save_rpc_state(path: Path, state: dict[str, Any], role: str) -> None:
    validate_rpc_state(state, role)
    atomic_secure_write(
        path,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        f"{role} RPC state",
    )


def load_rpc_state(coord: Path, role: str) -> dict[str, Any] | None:
    coord = validate_coordination_directory(coord)
    role_root = coord / ".rpc" / role
    if not role_root.exists() and not role_root.is_symlink():
        return None
    paths = rpc_role_paths(coord, role, create=False)
    state_path = paths["state"]
    if not state_path.exists() and not state_path.is_symlink():
        return None
    try:
        value = json.loads(
            read_regular_file(
                state_path,
                f"{role} RPC state",
                MAX_RPC_ACK_BYTES,
            )
        )
    except UnicodeDecodeError as error:
        raise OrchestrationError(f"{role} RPC state is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise OrchestrationError(f"{role} RPC state is not valid JSON") from error
    return validate_rpc_state(value, role)


def public_rpc_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "status": state["status"],
        "pid": state["pid"],
        "is_streaming": state["is_streaming"],
        "steering_count": state["steering_count"],
        "follow_up_count": state["follow_up_count"],
        "session_id": state["session_id"],
        "updated_at": state["updated_at"],
    }


def load_rpc_registry(coord: Path, role: str) -> dict[str, Any] | None:
    paths = rpc_role_paths(coord, role, create=False)
    path = paths["registry"]
    if not path.exists() and not path.is_symlink():
        return None
    try:
        value = json.loads(
            read_regular_file(
                path, f"{role} RPC worker registry", MAX_RPC_REGISTRY_BYTES
            )
        )
    except UnicodeDecodeError as error:
        raise OrchestrationError(
            f"{role} RPC worker registry is not valid UTF-8"
        ) from error
    except json.JSONDecodeError as error:
        raise OrchestrationError(
            f"{role} RPC worker registry is not valid JSON"
        ) from error
    return validate_rpc_registry(value, role)


def save_rpc_registry(path: Path, registry: dict[str, Any], role: str) -> None:
    validate_rpc_registry(registry, role)
    content = json.dumps(registry, indent=2, sort_keys=True) + "\n"
    if len(content.encode("utf-8")) > MAX_RPC_REGISTRY_BYTES:
        raise OrchestrationError("RPC worker registry exceeds the safety limit")
    atomic_secure_write(path, content, f"{role} RPC worker registry")


def public_rpc_registry(registry: dict[str, Any] | None) -> dict[str, Any] | None:
    if registry is None:
        return None
    counts = {status: 0 for status in sorted(RPC_COMMAND_STATUSES)}
    for command in registry["commands"]:
        counts[command["status"]] += 1
    return {
        "worker_id": registry["worker_id"],
        "generation": registry["generation"],
        "pid": registry["pid"],
        "session_id": registry["session_id"],
        "status": registry["status"],
        "active_command_ids": list(registry["active_command_ids"][:MAX_JSON_ITEMS]),
        "active_commands_truncated": len(registry["active_command_ids"])
        > MAX_JSON_ITEMS,
        "last_outcome": registry["last_outcome"],
        "last_event_sequence": registry["last_event_sequence"],
        "command_count": len(registry["commands"]),
        "command_status_counts": counts,
        "updated_at": registry["updated_at"],
    }


@contextmanager
def rpc_event_file_lock(path: Path, *, exclusive: bool) -> Any:
    parent = ensure_private_directory(path.parent)
    lock_path = parent / path.name
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise OrchestrationError("Cannot open RPC event lock") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OrchestrationError("RPC event lock must be a regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def append_private_regular(path: Path, payload: bytes, label: str) -> None:
    parent = ensure_private_directory(path.parent)
    destination = parent / path.name
    if destination.exists() or destination.is_symlink():
        require_regular_file(destination, label)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        raise OrchestrationError(f"Cannot append {label}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OrchestrationError(f"{label} must be a regular file")
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OrchestrationError(f"Cannot append {label}")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = open_directory(parent)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def read_rpc_event_segment(path: Path, role: str) -> list[dict[str, Any]]:
    if not path.exists() and not path.is_symlink():
        return []
    content = read_regular_file(
        path, f"{role} RPC event journal", MAX_RPC_EVENT_SEGMENT_BYTES
    )
    events: list[dict[str, Any]] = []
    for line in content.split(b"\n"):
        if not line:
            continue
        if line.endswith(b"\r"):
            line = line[:-1]
        if len(line) > MAX_RPC_EVENT_BYTES:
            raise OrchestrationError("RPC event record exceeds the safety limit")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OrchestrationError(
                "RPC event journal contains invalid JSON"
            ) from error
        events.append(validate_rpc_event(value, role))
    return events


def load_rpc_events_from_paths(
    paths: dict[str, Path], role: str
) -> list[dict[str, Any]]:
    if not any(
        path.exists() or path.is_symlink()
        for path in (paths["events_archive"], paths["events"])
    ):
        return []
    with rpc_event_file_lock(paths["event_lock"], exclusive=False):
        events = [
            *read_rpc_event_segment(paths["events_archive"], role),
            *read_rpc_event_segment(paths["events"], role),
        ]
    sequences = [event["sequence"] for event in events]
    if sequences != sorted(set(sequences)) or any(
        current != previous + 1 for previous, current in zip(sequences, sequences[1:])
    ):
        raise OrchestrationError("RPC event journal sequence is invalid")
    worker_ids = {event["worker_id"] for event in events}
    if len(worker_ids) > 1:
        raise OrchestrationError("RPC event journal worker identity changed")
    for previous, current in zip(events, events[1:]):
        if current["generation"] < previous["generation"]:
            raise OrchestrationError("RPC event journal generation moved backwards")
        if current["generation"] > previous["generation"] and (
            current["generation"] != previous["generation"] + 1
            or current["event"] != "supervisor_started"
        ):
            raise OrchestrationError(
                "RPC event journal generation transition is invalid"
            )
    return events


def load_rpc_events(coord: Path, role: str) -> list[dict[str, Any]]:
    return load_rpc_events_from_paths(rpc_role_paths(coord, role, create=False), role)


def append_rpc_event(paths: dict[str, Path], event: dict[str, Any], role: str) -> None:
    validate_rpc_event(event, role)
    payload = (
        json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    )
    if len(payload) > MAX_RPC_EVENT_BYTES:
        raise OrchestrationError("RPC event record exceeds the safety limit")
    with rpc_event_file_lock(paths["event_lock"], exclusive=True):
        current = paths["events"]
        if current.exists() or current.is_symlink():
            metadata = require_regular_file(current, f"{role} RPC event journal")
            if metadata.st_size + len(payload) > MAX_RPC_EVENT_SEGMENT_BYTES:
                archive = paths["events_archive"]
                if archive.exists() or archive.is_symlink():
                    unlink_private_regular(archive, f"{role} RPC event archive")
                os.replace(current, archive)
                directory_descriptor = open_directory(paths["root"])
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        append_private_regular(current, payload, f"{role} RPC event journal")


def rpc_command_map(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {command["id"]: command for command in registry["commands"]}


def check_rpc_event_application(
    registry: dict[str, Any], event: dict[str, Any]
) -> None:
    if event["worker_id"] != registry["worker_id"]:
        raise OrchestrationError("RPC event does not match worker registry")
    if event["sequence"] != registry["last_event_sequence"] + 1:
        raise OrchestrationError("RPC event journal has a sequence gap")
    event_name = event["event"]
    if event_name not in RPC_COMMAND_EVENT_STATUSES:
        return
    command = rpc_command_map(registry).get(event["command_id"])
    if event_name == "command_received":
        if command is not None:
            raise OrchestrationError("RPC command was received more than once")
        if len(registry["commands"]) >= MAX_RPC_COMMANDS:
            raise OrchestrationError("RPC command registry is full")
        return
    if command is None:
        raise OrchestrationError("RPC command event has no received record")
    if (
        command["command"] != event["command"]
        or command["delivery"] != event["delivery"]
    ):
        raise OrchestrationError("RPC command event metadata changed")
    allowed = RPC_COMMAND_TRANSITIONS.get(command["status"], frozenset())
    if event["status"] not in allowed:
        raise OrchestrationError("RPC command lifecycle transition is invalid")


def apply_rpc_event(registry: dict[str, Any], event: dict[str, Any]) -> None:
    if event["sequence"] <= registry["last_event_sequence"]:
        return
    check_rpc_event_application(registry, event)
    event_name = event["event"]
    if event_name in RPC_COMMAND_EVENT_STATUSES:
        commands = rpc_command_map(registry)
        command_id = event["command_id"]
        command = commands.get(command_id)
        if event_name == "command_received":
            command = {
                "id": command_id,
                "command": event["command"],
                "delivery": event["delivery"],
                "status": "received",
                "received_at": event["timestamp"],
                "updated_at": event["timestamp"],
                "event_sequence": event["sequence"],
            }
            registry["commands"].append(command)
        else:
            command["status"] = event["status"]
            command["updated_at"] = event["timestamp"]
            command["event_sequence"] = event["sequence"]
        active = registry["active_command_ids"]
        if event["command"] == "prompt" and event["status"] in {"accepted", "started"}:
            if command_id not in active:
                active.append(command_id)
        if event["status"] in RPC_TERMINAL_COMMAND_STATUSES and command_id in active:
            active.remove(command_id)
    else:
        lifecycle_status = event["status"]
        if event_name == "supervisor_started":
            registry["status"] = "starting"
        elif event_name == "agent_started":
            registry["status"] = "streaming"
        elif event_name in {"agent_completed", "agent_failed", "agent_aborted"}:
            registry["status"] = "settled"
            registry["last_outcome"] = lifecycle_status
        elif event_name == "supervisor_exited":
            registry["status"] = "exited"
        elif event_name == "supervisor_failed":
            registry["status"] = "error"
            registry["last_outcome"] = "failed"
    registry["last_event_sequence"] = event["sequence"]
    registry["updated_at"] = event["timestamp"]


def record_rpc_event(
    paths: dict[str, Path],
    registry: dict[str, Any],
    role: str,
    event_name: str,
    *,
    command_id: str | None = None,
    command: str | None = None,
    delivery: str | None = None,
) -> dict[str, Any]:
    event = {
        "version": 1,
        "sequence": registry["last_event_sequence"] + 1,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "role": role,
        "worker_id": registry["worker_id"],
        "generation": registry["generation"],
        "event": event_name,
        "command_id": command_id,
        "command": command,
        "delivery": delivery,
        "status": (
            RPC_COMMAND_EVENT_STATUSES[event_name]
            if event_name in RPC_COMMAND_EVENT_STATUSES
            else RPC_LIFECYCLE_EVENT_STATUSES[event_name]
        ),
    }
    check_rpc_event_application(registry, event)
    append_rpc_event(paths, event, role)
    apply_rpc_event(registry, event)
    save_rpc_registry(paths["registry"], registry, role)
    return event


def public_rpc_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": event["sequence"],
        "timestamp": event["timestamp"],
        "role": event["role"],
        "worker_id": event["worker_id"],
        "generation": event["generation"],
        "event": event["event"],
        "command_id": event["command_id"],
        "command": event["command"],
        "delivery": event["delivery"],
        "status": event["status"],
    }


def unlink_private_regular(path: Path, label: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    require_regular_file(path, label)
    path.unlink()


def deterministic_rpc_token(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def initialize_rpc_registry(
    coord: Path,
    paths: dict[str, Path],
    role: str,
    pid: int,
) -> dict[str, Any]:
    events = load_rpc_events_from_paths(paths, role)
    registry = load_rpc_registry(coord, role)
    if registry is None:
        if events and events[0]["sequence"] != 1:
            raise OrchestrationError(
                "RPC worker registry cannot be rebuilt from a compacted journal"
            )
        worker_id = events[0]["worker_id"] if events else secrets.token_hex(16)
        registry = {
            "version": 1,
            "role": role,
            "worker_id": worker_id,
            "generation": 1,
            "pid": pid,
            "session_id": None,
            "status": "starting",
            "active_command_ids": [],
            "last_outcome": None,
            "last_event_sequence": 0,
            "commands": [],
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    if events:
        if registry["worker_id"] != events[-1]["worker_id"]:
            raise OrchestrationError(
                "RPC worker registry does not match its event journal"
            )
        latest_sequence = events[-1]["sequence"]
        if registry["last_event_sequence"] > latest_sequence:
            raise OrchestrationError(
                "RPC worker registry is ahead of its event journal"
            )
        newer = [
            event
            for event in events
            if event["sequence"] > registry["last_event_sequence"]
        ]
        if newer and newer[0]["sequence"] != registry["last_event_sequence"] + 1:
            raise OrchestrationError("RPC worker registry recovery has an event gap")
        for event in newer:
            apply_rpc_event(registry, event)
        event_generation = max(event["generation"] for event in events)
        if registry["generation"] > event_generation:
            raise OrchestrationError(
                "RPC worker registry generation is ahead of its journal"
            )
        registry["generation"] = event_generation
    elif registry["last_event_sequence"] != 0:
        raise OrchestrationError("RPC worker event journal is missing")

    previous_generation = max(
        [registry["generation"], *[event["generation"] for event in events]],
        default=0,
    )
    registry["generation"] = previous_generation + 1 if events else 1
    registry["pid"] = pid
    registry["session_id"] = None
    registry["status"] = "starting"
    registry["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    record_rpc_event(paths, registry, role, "supervisor_started")
    for command in list(registry["commands"]):
        if command["status"] not in RPC_TERMINAL_COMMAND_STATUSES:
            record_rpc_event(
                paths,
                registry,
                role,
                "command_uncertain",
                command_id=command["id"],
                command=command["command"],
                delivery=command["delivery"],
            )
    return registry


def mark_rpc_registry_stopped(coord: Path, role: str) -> None:
    registry = load_rpc_registry(coord, role)
    if registry is None:
        return
    paths = rpc_role_paths(coord, role, create=False)
    events = load_rpc_events_from_paths(paths, role)
    newer = [
        event for event in events if event["sequence"] > registry["last_event_sequence"]
    ]
    if newer and newer[0]["sequence"] != registry["last_event_sequence"] + 1:
        raise OrchestrationError("RPC worker stop recovery has an event gap")
    for event in newer:
        apply_rpc_event(registry, event)
    if events:
        event_generation = max(event["generation"] for event in events)
        if registry["generation"] > event_generation:
            raise OrchestrationError(
                "RPC worker registry generation is ahead of its journal"
            )
        registry["generation"] = event_generation
    for command in list(registry["commands"]):
        if command["status"] not in RPC_TERMINAL_COMMAND_STATUSES:
            transition_rpc_command(
                paths,
                registry,
                role,
                command["id"],
                "uncertain",
            )
    if registry["status"] not in {"exited", "error"}:
        record_rpc_event(paths, registry, role, "supervisor_exited")


def transition_rpc_command(
    paths: dict[str, Path],
    registry: dict[str, Any],
    role: str,
    command_id: str,
    status: str,
) -> dict[str, Any]:
    command = rpc_command_map(registry).get(command_id)
    if command is None:
        raise OrchestrationError("RPC command is not registered")
    event_name = {
        "accepted": "command_accepted",
        "started": "command_started",
        "completed": "command_completed",
        "failed": "command_failed",
        "aborted": "command_aborted",
        "rejected": "command_rejected",
        "uncertain": "command_uncertain",
    }.get(status)
    if event_name is None:
        raise OrchestrationError("RPC command transition is unsupported")
    return record_rpc_event(
        paths,
        registry,
        role,
        event_name,
        command_id=command_id,
        command=command["command"],
        delivery=command["delivery"],
    )
