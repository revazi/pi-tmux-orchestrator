"""Strict RPC framing and private mailbox client protocol."""

from __future__ import annotations

import json
import os
import queue
import secrets
import time
from pathlib import Path
from typing import Any

from .constants import (
    MAX_RPC_ACK_BYTES,
    MAX_RPC_RECORD_BYTES,
    MAX_TASK_BYTES,
    RPC_ACK_TIMEOUT_SECONDS,
    RPC_COMMAND_STATUSES,
    RPC_TOKEN_PATTERN,
    RPC_TRANSPORT,
)
from .models import OrchestrationError
from .output import bounded_message
from .rpc_store import rpc_role_paths, unlink_private_regular
from .storage import (
    atomic_secure_create,
    atomic_secure_write,
    manifest_transport,
    read_regular_file,
)


def rpc_control_request(
    coord: Path,
    manifest: dict[str, Any],
    role: str,
    command_type: str,
    *,
    message: str | None = None,
    delivery: str = "steer",
    command_id: str | None = None,
    timeout: float = RPC_ACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if manifest_transport(manifest) != RPC_TRANSPORT:
        raise OrchestrationError("Selected orchestration does not use RPC workers")
    if role not in manifest["roles"]:
        raise OrchestrationError(f"Role {role!r} is not enabled")
    try:
        paths = rpc_role_paths(coord, role, create=False)
    except (FileNotFoundError, OrchestrationError) as error:
        raise OrchestrationError(
            f"RPC supervisor is not ready for {role}",
            "rpc_not_ready",
        ) from error
    token = command_id or secrets.token_hex(16)
    if not RPC_TOKEN_PATTERN.fullmatch(token):
        raise OrchestrationError(
            "RPC command ID must be exactly 32 lowercase hexadecimal characters"
        )
    request_path = paths["inbox"] / f"{token}.json"
    ack_path = paths["acks"] / f"{token}.json"
    unlink_private_regular(ack_path, "stale RPC acknowledgement")
    if command_type == "prompt":
        if message is None or not message.strip():
            raise OrchestrationError("RPC message cannot be empty")
        if len(message.encode("utf-8")) > MAX_TASK_BYTES:
            raise OrchestrationError("RPC message exceeds the safety limit")
        if delivery not in {"steer", "follow-up"}:
            raise OrchestrationError("RPC delivery must be steer or follow-up")
        request = {
            "version": 1,
            "id": token,
            "type": "prompt",
            "delivery": delivery,
            "message": message,
        }
    elif command_type == "abort":
        request = {"version": 1, "id": token, "type": "abort"}
    else:
        raise OrchestrationError("Unsupported RPC control command")
    atomic_secure_create(
        request_path,
        json.dumps(request, separators=(",", ":"), sort_keys=True) + "\n",
        "RPC request",
    )
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if ack_path.exists() or ack_path.is_symlink():
                try:
                    value = json.loads(
                        read_regular_file(
                            ack_path,
                            "RPC acknowledgement",
                            MAX_RPC_ACK_BYTES,
                        )
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise OrchestrationError(
                        "RPC acknowledgement is invalid",
                        "invalid_rpc_ack",
                    ) from error
                if not isinstance(value, dict) or type(value.get("version")) is not int:
                    raise OrchestrationError(
                        "RPC acknowledgement is invalid", "invalid_rpc_ack"
                    )
                if value["version"] == 1:
                    valid = (
                        set(value) == {"version", "id", "command", "success"}
                        and value.get("id") == token
                        and value.get("command") == command_type
                        and type(value.get("success")) is bool
                    )
                    if valid:
                        value = {
                            **value,
                            "status": "accepted" if value["success"] else "rejected",
                            "duplicate": False,
                            "event_sequence": None,
                        }
                elif value["version"] == 2:
                    valid = (
                        set(value)
                        == {
                            "version",
                            "id",
                            "command",
                            "success",
                            "status",
                            "duplicate",
                            "event_sequence",
                        }
                        and value.get("id") == token
                        and value.get("command") == command_type
                        and type(value.get("success")) is bool
                        and value.get("status") in RPC_COMMAND_STATUSES | {"conflict"}
                        and type(value.get("duplicate")) is bool
                        and value.get("success")
                        == (
                            value.get("status")
                            in {"accepted", "started", "completed", "failed", "aborted"}
                        )
                        and (
                            value.get("event_sequence") is None
                            or type(value.get("event_sequence")) is int
                            and value["event_sequence"] > 0
                        )
                    )
                else:
                    valid = False
                if not valid:
                    raise OrchestrationError(
                        "RPC acknowledgement is invalid",
                        "invalid_rpc_ack",
                    )
                if not value["success"]:
                    status = value["status"]
                    code = {
                        "uncertain": "rpc_uncertain",
                        "conflict": "rpc_conflict",
                    }.get(status, "rpc_rejected")
                    raise OrchestrationError(
                        f"RPC worker reported {status} delivery for {command_type} "
                        f"(command ID {token})",
                        code,
                    )
                return value
            time.sleep(0.05)
        raise OrchestrationError(
            f"Timed out waiting for {role} RPC acknowledgement (command ID {token})",
            "rpc_timeout",
        )
    finally:
        unlink_private_regular(request_path, "RPC request")
        unlink_private_regular(ack_path, "RPC acknowledgement")


def write_rpc_record(stream: Any, value: dict[str, Any]) -> None:
    payload = (
        json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_RPC_RECORD_BYTES:
        raise OrchestrationError("RPC command exceeds the safety limit")
    stream.write(payload)
    stream.flush()


def strict_rpc_reader(
    stream: Any, channel: str, records: queue.Queue[tuple[str, Any]]
) -> None:
    buffer = b""
    try:
        while True:
            chunk = os.read(stream.fileno(), 8192)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > MAX_RPC_RECORD_BYTES and b"\n" not in buffer:
                records.put(("protocol_error", "RPC record exceeds the safety limit"))
                return
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.endswith(b"\r"):
                    line = line[:-1]
                if len(line) > MAX_RPC_RECORD_BYTES:
                    records.put(
                        ("protocol_error", "RPC record exceeds the safety limit")
                    )
                    return
                records.put((channel, line))
        if buffer:
            if buffer.endswith(b"\r"):
                buffer = buffer[:-1]
            records.put((channel, buffer))
    except OSError as error:
        records.put(("reader_error", bounded_message(error, 160)))
    finally:
        records.put((f"{channel}_eof", None))


def rpc_acknowledge(
    path: Path,
    token: str,
    command: str,
    success: bool,
    *,
    status: str | None = None,
    duplicate: bool = False,
    event_sequence: int | None = None,
) -> None:
    resolved_status = status or ("accepted" if success else "rejected")
    if not RPC_TOKEN_PATTERN.fullmatch(token) or command not in {"prompt", "abort"}:
        raise OrchestrationError("RPC acknowledgement identity is invalid")
    if type(success) is not bool or type(duplicate) is not bool:
        raise OrchestrationError("RPC acknowledgement flags are invalid")
    if resolved_status not in RPC_COMMAND_STATUSES | {"conflict"}:
        raise OrchestrationError("RPC acknowledgement status is invalid")
    accepted_statuses = {"accepted", "started", "completed", "failed", "aborted"}
    if success != (resolved_status in accepted_statuses):
        raise OrchestrationError("RPC acknowledgement success/status is inconsistent")
    if event_sequence is not None and (
        type(event_sequence) is not int or event_sequence <= 0
    ):
        raise OrchestrationError("RPC acknowledgement event sequence is invalid")
    atomic_secure_write(
        path,
        json.dumps(
            {
                "version": 2,
                "id": token,
                "command": command,
                "success": success,
                "status": resolved_status,
                "duplicate": duplicate,
                "event_sequence": event_sequence,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        "RPC acknowledgement",
    )


def read_rpc_mailbox_request(path: Path, token: str) -> dict[str, Any]:
    try:
        value = json.loads(
            read_regular_file(path, "RPC mailbox request", MAX_RPC_RECORD_BYTES)
        )
    except UnicodeDecodeError as error:
        raise OrchestrationError("RPC mailbox request is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise OrchestrationError("RPC mailbox request is not valid JSON") from error
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value.get("version") != 1
        or value.get("id") != token
    ):
        raise OrchestrationError("RPC mailbox request has invalid identity")
    request_type = value.get("type")
    expected = (
        {"version", "id", "type", "delivery", "message"}
        if request_type == "prompt"
        else {"version", "id", "type"}
    )
    if set(value) != expected or request_type not in {"prompt", "abort"}:
        raise OrchestrationError("RPC mailbox request has invalid fields")
    if request_type == "prompt":
        message = value["message"]
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message.encode("utf-8")) > MAX_TASK_BYTES
        ):
            raise OrchestrationError("RPC mailbox message is invalid")
        if value["delivery"] not in {"steer", "follow-up"}:
            raise OrchestrationError("RPC mailbox delivery is invalid")
    return value
