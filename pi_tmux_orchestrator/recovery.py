"""Bounded recovery metadata and duplicate-safe local stop receipts."""

from __future__ import annotations

import json
import os
import re
from itertools import islice
from pathlib import Path
from typing import Any, Callable

from .broker_store import public_broker_snapshot
from .constants import MAX_JSON_ITEMS, MAX_RPC_COMMANDS, RPC_TOKEN_PATTERN
from .models import OrchestrationError
from .storage import (
    ensure_private_directory,
    load_manifest,
    read_regular_file,
    secure_write,
)
from .tmux import session_option


def require_live_run(
    session: str, coord: Path, resolver: Callable[[str | None], tuple[str, Path]]
) -> None:
    _, live_coord = resolver(session)
    if live_coord != coord:
        raise OrchestrationError(
            "Control requires the exact run hosted by the live tmux session",
            "broker_not_live",
        )


def collision_metadata(session: str) -> dict[str, Any]:
    """Optional retained state is not a liveness or control authorization claim."""
    value: dict[str, Any] = {
        "session": session,
        "orchestrated": False,
        "state_source": "unavailable",
        "workflow": None,
        "roles": [],
        "next_actions": [{"action": "start", "requires": "different_exact_session"}],
    }
    try:
        coord_name = session_option(session, "@pi_agents_coord")
        if not coord_name:
            return value
        coord = Path(coord_name)
        manifest = load_manifest(coord, expected_session=session)
        value["orchestrated"] = True
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", coord.name) and coord.name not in {
            ".",
            "..",
        }:
            value["run_id"] = coord.name
        value["next_actions"] = [{"action": "status", "session": session}]
        if manifest.get("version", 0) >= 3:
            value["next_actions"].append(
                {
                    "action": "attach",
                    "session": session,
                    "requires": "interactive_tui_tmux_live_broker",
                }
            )
        value["next_actions"].extend(
            [
                {
                    "action": "stop",
                    "session": session,
                    "requires": "interactive_confirmation",
                },
                {"action": "start", "requires": "different_exact_session"},
            ]
        )
        value["roles"] = [
            {"role": role, "state": None}
            for role in list(manifest["roles"])[:MAX_JSON_ITEMS]
        ]
        if manifest.get("version", 0) >= 3:
            snapshot = public_broker_snapshot(coord)
            workflow = snapshot["workflow"]
            workflow_states = {
                "starting",
                "connecting",
                "initializing",
                "active",
                "routing",
                "ready",
                "needs_attention",
                "uncertain",
                "stopped",
            }
            role_states = {
                "starting",
                "idle",
                "active",
                "waiting",
                "disconnected",
                "restarting",
                "recovering",
                "uncertain",
                "stopped",
            }
            value["workflow"] = {
                "state": workflow["state"]
                if workflow["state"] in workflow_states
                else "unknown",
                "round": workflow["round"]
                if type(workflow["round"]) is int
                and 0 <= workflow["round"] <= 1_000_000
                else None,
                "implementation_flow": workflow["implementation_flow"]
                if workflow["implementation_flow"] in {"single", "phased"}
                else None,
            }
            value["roles"] = [
                {
                    "role": role["role"],
                    "state": role["state"]
                    if role["state"] in role_states
                    else "unknown",
                    "generation": role["generation"]
                    if type(role["generation"]) is int
                    and 0 < role["generation"] <= 1_000_000
                    else None,
                }
                for role in snapshot["roles"][:MAX_JSON_ITEMS]
                if role["role"] in manifest["roles"]
            ]
            value["state_source"] = "retained-broker-state-not-liveness"
    except Exception:
        # No raw error, path, prompt, report, or provider payload escapes this read.
        pass
    return value


def claim_stop(coord: Path, command_id: str) -> tuple[Path, bool, str]:
    if not isinstance(command_id, str) or not RPC_TOKEN_PATTERN.fullmatch(command_id):
        raise OrchestrationError("Control command ID is invalid", "invalid_arguments")
    directory = coord / "stop-receipts"
    ensure_private_directory(directory)
    receipt = directory / f"{command_id}.json"
    try:
        receipt.lstat()
        value = json.loads(read_regular_file(receipt, "stop receipt", 1024))
    except FileNotFoundError:
        if (
            sum(1 for _ in islice(directory.iterdir(), MAX_RPC_COMMANDS))
            >= MAX_RPC_COMMANDS
        ):
            raise OrchestrationError("Stop receipt registry is full", "rejected")
        try:
            descriptor = os.open(
                receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            # A concurrent claim must not repeat the kill.
            return receipt, True, "uncertain"
        with os.fdopen(descriptor, "w") as stream:
            stream.write('{"status":"uncertain"}')
            stream.flush()
            os.fsync(stream.fileno())
        return receipt, False, "uncertain"
    except (ValueError, OrchestrationError):
        raise OrchestrationError(
            "Stop receipt is unavailable", "broker_uncertain"
        ) from None
    if value not in ({"status": "uncertain"}, {"status": "completed"}):
        raise OrchestrationError("Stop receipt is invalid", "broker_uncertain")
    return receipt, True, value["status"]


def complete_stop(receipt: Path) -> None:
    secure_write(receipt, '{"status":"completed"}')
