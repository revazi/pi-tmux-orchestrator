"""Synchronous authenticated client for broker control commands."""

from __future__ import annotations

import json
import secrets
import socket
from pathlib import Path
from typing import Any

from .broker_store import broker_paths
from .constants import (
    BROKER_PROTOCOL_VERSION,
    MAX_BROKER_FRAME_BYTES,
    RPC_TOKEN_PATTERN,
)
from .models import OrchestrationError
from .protocol import encode_frame
from .storage import read_regular_file


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise OrchestrationError(
                "Broker closed before acknowledging the command", "broker_uncertain"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def broker_control_request(
    coord: Path,
    role: str,
    action: str,
    *,
    message: str | None = None,
    delivery: str | None = None,
    command_id: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    token = (
        read_regular_file(coord / "control.token", "broker control token", 128)
        .decode("ascii")
        .strip()
    )
    request_id = command_id or secrets.token_hex(16)
    if not RPC_TOKEN_PATTERN.fullmatch(token) or not RPC_TOKEN_PATTERN.fullmatch(
        request_id
    ):
        raise OrchestrationError("Broker control identity is invalid")
    request = {
        "version": BROKER_PROTOCOL_VERSION,
        "type": "control",
        "token": token,
        "id": request_id,
        "action": action,
        "role": role,
        "delivery": delivery,
        "message": message,
    }
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.settimeout(timeout)
    try:
        stream.connect(str(broker_paths(coord)["socket"]))
        stream.sendall(encode_frame(request))
        size = int.from_bytes(_recv_exact(stream, 4), "big")
        if not 1 <= size <= MAX_BROKER_FRAME_BYTES:
            raise OrchestrationError("Broker response size is invalid")
        try:
            response = json.loads(_recv_exact(stream, size))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OrchestrationError("Broker response is invalid") from error
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as error:
        raise OrchestrationError(
            "Broker is unavailable; command delivery was not accepted",
            "broker_not_ready",
        ) from error
    except OSError as error:
        raise OrchestrationError(
            "Broker control transport failed; delivery is uncertain",
            "broker_uncertain",
        ) from error
    finally:
        stream.close()
    if (
        not isinstance(response, dict)
        or set(response) != {"version", "type", "id", "success", "status", "duplicate"}
        or response.get("version") != BROKER_PROTOCOL_VERSION
        or response.get("type") != "response"
        or response.get("id") != request_id
        or type(response.get("success")) is not bool
        or type(response.get("duplicate")) is not bool
        or response.get("status") not in {"accepted", "uncertain", "conflict"}
    ):
        raise OrchestrationError("Broker response is invalid")
    if not response["success"]:
        raise OrchestrationError(
            f"Broker reported {response['status']} command delivery",
            "broker_uncertain"
            if response["status"] == "uncertain"
            else "broker_rejected",
        )
    return response
