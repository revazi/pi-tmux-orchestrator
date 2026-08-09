"""CLI handlers for the versioned durable supervisor API."""

from __future__ import annotations

import argparse

from .constants import SUPERVISOR_API_VERSION
from .models import CommandResult
from .output import human_print
from .supervisor_api import (
    public_supervisor_run,
    retained_runs,
    retained_sessions,
    supervisor_capabilities,
    supervisor_command_status,
    supervisor_cursor_arguments,
    supervisor_event_batch,
    supervisor_snapshot,
)
from .tmux import validate_session_name


def capabilities_command(_: argparse.Namespace) -> CommandResult:
    data = supervisor_capabilities()
    human_print(f"Supervisor API version: {data['api_version']}")
    human_print("Durable read plane: sessions, runs, snapshot, events, command")
    human_print(
        "Control plane: canonical send/abort commands with optional exact --run"
    )
    return CommandResult(data=data)


def supervisor_sessions_command(_: argparse.Namespace) -> CommandResult:
    data = retained_sessions()
    if not data["sessions"]:
        human_print("No retained orchestrations were found.")
    for value in data["sessions"]:
        human_print(
            f"{value['session']} run={value['run_id']} transport={value['transport']} "
            f"roles={','.join(role['name'] for role in value['roles'])}"
        )
    if data["issues"]:
        human_print(
            f"Warning: {len(data['issues'])} retained state entries were invalid"
        )
    return CommandResult(data=data)


def supervisor_runs_command(args: argparse.Namespace) -> CommandResult:
    runs, issues, truncated = retained_runs(args.session, limit=args.limit)
    values = [public_supervisor_run(coord, manifest) for coord, manifest in runs]
    for value in values:
        human_print(
            f"{value['session']} run={value['run_id']} transport={value['transport']}"
        )
    return CommandResult(
        data={
            "api_version": SUPERVISOR_API_VERSION,
            "session": validate_session_name(args.session),
            "runs": values,
            "issues": issues,
            "truncated": truncated,
        }
    )


def supervisor_snapshot_command(args: argparse.Namespace) -> CommandResult:
    data = supervisor_snapshot(args.session, args.run)
    human_print(
        f"Supervisor snapshot: {data['session']} run={data['run_id']} "
        f"transport={data['transport']}"
    )
    for role in data["roles"]:
        worker = role["worker"]
        suffix = (
            f"status={worker['status']} generation={worker['generation']} "
            f"event={worker['last_event_sequence']}"
            if worker is not None
            else "durable-supervisor=unavailable"
        )
        human_print(f"  {role['name']}: {suffix}")
    return CommandResult(data=data)


def supervisor_events_command(args: argparse.Namespace) -> CommandResult:
    data = supervisor_event_batch(
        args.session,
        args.run,
        requested_roles=args.role,
        cursors=supervisor_cursor_arguments(args.cursor),
        limit=args.limit,
    )
    human_print(f"Supervisor events: {data['session']} run={data['run_id']}")
    for role in data["roles"]:
        cursor = role["cursor"]
        human_print(
            f"  {role['role']}: returned={len(role['events'])} "
            f"next={cursor['next']} latest={cursor['latest']} gap={cursor['gap']}"
        )
    return CommandResult(data=data)


def supervisor_command_command(args: argparse.Namespace) -> CommandResult:
    data = supervisor_command_status(
        args.session,
        args.run,
        role=args.role,
        command_id=args.command_id,
    )
    command = data["command"]
    human_print(
        f"Supervisor command: {data['session']}/{data['role']} "
        f"id={command['id']} status={command['status']}"
    )
    return CommandResult(data=data)
