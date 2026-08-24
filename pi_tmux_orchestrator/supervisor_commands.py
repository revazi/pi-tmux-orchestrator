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
    supervisor_usage,
)
from .tmux import validate_session_name


def capabilities_command(_: argparse.Namespace) -> CommandResult:
    data = supervisor_capabilities()
    human_print(f"Supervisor API version: {data['api_version']}")
    human_print("Durable read plane: sessions, runs, snapshot, usage, events, command")
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


def _usage_value(value: object) -> str:
    return "unavailable" if value is None else str(value)


def _usage_text(usage: dict[str, object] | None) -> str:
    if usage is None:
        return "usage=unavailable"
    return (
        f"calls={_usage_value(usage['provider_calls'])} input={usage['input_tokens']} "
        f"cache-read={usage['cache_read_tokens']} cache-write={usage['cache_write_tokens']} "
        f"output={usage['output_tokens']} "
        f"reasoning={_usage_value(usage['reasoning_tokens'])} "
        f"cost={_usage_value(usage['cost_total'])} "
        f"operational={usage['operational_tokens']} "
        f"context={_usage_value(usage.get('context_tokens'))}/"
        f"{_usage_value(usage.get('context_window'))} "
        f"context-percent={_usage_value(usage.get('context_percent'))} "
        f"peak={_usage_value(usage.get('peak_context_tokens'))}"
    )


def supervisor_usage_command(args: argparse.Namespace) -> CommandResult:
    data = supervisor_usage(args.session, args.run, limit=args.limit)
    human_print(
        f"Supervisor usage: {data['session']} run={data['run_id']} "
        f"availability={data['availability']}"
    )
    if data["cumulative"] is not None:
        human_print(f"  cumulative: {_usage_text(data['cumulative'])}")
    for role in data["roles"]:
        human_print(f"  {role['role']} cumulative: {_usage_text(role['cumulative'])}")
        for assignment in role["assignments"]:
            human_print(
                f"    round={assignment['round']} kind={assignment['kind']} "
                f"assignment={assignment['assignment_id']} "
                f"{_usage_text(assignment['usage'])}"
            )
    if data["truncated"]:
        human_print(f"  truncated at {data['limit']} latest assignments")
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
