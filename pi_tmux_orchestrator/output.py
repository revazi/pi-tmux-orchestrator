"""Output support for Pi tmux orchestration."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from . import runtime
from .constants import JSON_SCHEMA_VERSION, MAX_ERROR_CHARS, TUI_TRANSPORT
from .models import CommandResult


def bounded_message(value: object, limit: int = MAX_ERROR_CHARS) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def human_print(*values: object) -> None:
    if not runtime.JSON_MODE:
        print(*values)


def eprint(*values: object) -> None:
    if not runtime.JSON_MODE:
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


def public_role(
    role: str,
    config: dict[str, Any],
    transport: str = TUI_TRANSPORT,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": role,
        "transport": transport,
        "provider": config["provider"],
        "model": config["model"],
        "thinking": config["thinking"],
        "tool_policy": (
            "default" if config.get("tools") is None else "workflow-read-only-with-bash"
        ),
    }
    if config.get("pane_id") is not None:
        value["pane_id"] = config["pane_id"]
    return value
