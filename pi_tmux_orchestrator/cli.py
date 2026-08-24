"""Cli support for Pi tmux orchestration."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap

from . import runtime
from .constants import (
    MAX_JSON_ITEMS,
    MAX_RPC_EVENTS,
    RPC_TOKEN_PATTERN,
    THINKING_LEVELS,
    VERSION,
)
from .broker import broker_command
from .budgeting import BUDGET_ENFORCEMENT, parse_budget_override
from .controller import (
    controller_attach_command,
    controller_start_command,
    controller_status_command,
    controller_stop_command,
)
from .commands import (
    abort_command,
    attach_command,
    doctor_command,
    events_command,
    list_command,
    restart_command,
    run_agent_command,
    send_command,
    start_command,
    status_command,
    stop_command,
)
from .models import CommandResult, OrchestrationArgumentParser, OrchestrationError
from .output import bounded_message, emit_json, eprint
from .relay import relay_command
from .supervisor_commands import (
    capabilities_command,
    supervisor_command_command,
    supervisor_events_command,
    supervisor_runs_command,
    supervisor_sessions_command,
    supervisor_snapshot_command,
    supervisor_usage_command,
)
from .worker_resources import worker_skill_argument


def worker_skill(value: str) -> tuple[str, str]:
    try:
        return worker_skill_argument(value)
    except OrchestrationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def rpc_event_cursor(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("event cursor must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("event cursor cannot be negative")
    return parsed


def rpc_event_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("event limit must be an integer") from error
    if not 1 <= parsed <= MAX_RPC_EVENTS:
        raise argparse.ArgumentTypeError(
            f"event limit must be between 1 and {MAX_RPC_EVENTS}"
        )
    return parsed


def supervisor_run_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("run limit must be an integer") from error
    if not 1 <= parsed <= MAX_JSON_ITEMS:
        raise argparse.ArgumentTypeError(
            f"run limit must be between 1 and {MAX_JSON_ITEMS}"
        )
    return parsed


def supervisor_usage_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("usage limit must be an integer") from error
    if not 1 <= parsed <= MAX_JSON_ITEMS:
        raise argparse.ArgumentTypeError(
            f"usage limit must be between 1 and {MAX_JSON_ITEMS}"
        )
    return parsed


def budget_override(
    value: str,
) -> tuple[str, str, str, int | float | None]:
    try:
        return parse_budget_override(value)
    except OrchestrationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def rpc_command_id(value: str) -> str:
    if not RPC_TOKEN_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "command ID must be exactly 32 lowercase hexadecimal characters"
        )
    return value


def supervisor_cursor(value: str) -> tuple[str, int]:
    role, separator, sequence = value.partition("=")
    roles = {"implementer", "reviewer", "probe", "playwright", "django"}
    if separator != "=" or role not in roles or not sequence:
        raise argparse.ArgumentTypeError(
            "supervisor cursor must use ROLE=SEQUENCE for a known role"
        )
    return role, rpc_event_cursor(sequence)


def add_model_arguments(parser: argparse.ArgumentParser, role: str) -> None:
    parser.add_argument(f"--{role}-provider")
    parser.add_argument(f"--{role}-model")
    parser.add_argument(f"--{role}-thinking", choices=THINKING_LEVELS)


def build_parser() -> argparse.ArgumentParser:
    parser = OrchestrationArgumentParser(
        prog="pi-tmux-agents",
        description=(
            "Run coordinated Pi implementer/reviewer/probe agents with TUI or RPC workers "
            "in a tmux grid."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              pi-tmux-agents doctor
              pi-tmux-agents controller start
              pi-tmux-agents controller attach
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --approve-project
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --with-probe --attach
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md --rpc-workers
              pi-tmux-agents start --project "$PWD" --task-file /tmp/task.md \\
                --with-probe --with-playwright
              pi-tmux-agents status pi-my-project-agents
              pi-tmux-agents restart pi-my-project-agents --role implementer \\
                --provider openai-codex --model gpt-5.6-sol --thinking xhigh --yes
            """),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit one versioned JSON object on stdout",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    controller = subparsers.add_parser(
        "controller",
        help="manage the persistent project-neutral Pi controller session",
    )
    controller_actions = controller.add_subparsers(
        dest="controller_action",
        required=True,
    )
    controller_start = controller_actions.add_parser(
        "start",
        help="start the detached controller Pi session",
    )
    controller_start.set_defaults(handler=controller_start_command)
    controller_status = controller_actions.add_parser(
        "status",
        help="show controller process and persistence metadata",
    )
    controller_status.set_defaults(handler=controller_status_command)
    controller_attach = controller_actions.add_parser(
        "attach",
        help="attach or switch to the controller Pi session",
    )
    controller_attach.set_defaults(handler=controller_attach_command)
    controller_stop = controller_actions.add_parser(
        "stop",
        help="stop the controller while retaining its Pi conversation",
    )
    controller_stop.add_argument("--confirm", action="store_true")
    controller_stop.set_defaults(handler=controller_stop_command)

    start = subparsers.add_parser("start", help="create and start an agent grid")
    start.add_argument("--project", default=os.getcwd())
    start.add_argument("--task")
    start.add_argument("--task-file")
    start.add_argument("--context-capsule")
    start.add_argument("--context-capsule-file")
    start.add_argument("--session")
    start.add_argument("--with-probe", action="store_true")
    start.add_argument("--probe-task")
    start.add_argument("--probe-task-file")
    start.add_argument("--with-playwright", action="store_true")
    start.add_argument("--playwright-task")
    start.add_argument("--playwright-task-file")
    start.add_argument("--with-django-expert", action="store_true")
    start.add_argument("--django-task")
    start.add_argument("--django-task-file")
    start.add_argument("--approve-project", action="store_true")
    start.add_argument(
        "--worker-skill",
        action="append",
        type=worker_skill,
        default=[],
        metavar="ROLE=PATH",
        help="load one explicitly reviewed Markdown skill for one worker role; repeatable",
    )
    start.add_argument(
        "--rpc-workers",
        action="store_true",
        help="run workers behind acknowledged Pi RPC supervisors",
    )
    start.add_argument("--attach", action="store_true")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--skip-model-check", action="store_true")
    start.add_argument(
        "--budget-enforcement",
        choices=BUDGET_ENFORCEMENT,
        help="override the observational budget policy mode for this run",
    )
    start.add_argument(
        "--budget-override",
        action="append",
        type=budget_override,
        default=[],
        metavar="LEVEL.SCOPE.METRIC=VALUE",
        help="override one warning/hard run/role/assignment threshold; use =off to disable",
    )
    for role_name in ("implementer", "reviewer", "probe", "playwright", "django"):
        add_model_arguments(start, role_name)
    start.set_defaults(handler=start_command)

    supervisor = subparsers.add_parser(
        "supervisor",
        help="query the versioned durable supervisor API",
    )
    supervisor_actions = supervisor.add_subparsers(
        dest="supervisor_action",
        required=True,
    )
    supervisor_capabilities = supervisor_actions.add_parser(
        "capabilities",
        help="describe the stable supervisor API surface",
    )
    supervisor_capabilities.set_defaults(handler=capabilities_command)
    supervisor_sessions = supervisor_actions.add_parser(
        "sessions",
        help="list newest retained runs without querying tmux",
    )
    supervisor_sessions.set_defaults(handler=supervisor_sessions_command)
    supervisor_runs = supervisor_actions.add_parser(
        "runs",
        help="list retained runs for an exact orchestration session",
    )
    supervisor_runs.add_argument("session")
    supervisor_runs.add_argument("--limit", type=supervisor_run_limit, default=100)
    supervisor_runs.set_defaults(handler=supervisor_runs_command)
    supervisor_snapshot = supervisor_actions.add_parser(
        "snapshot",
        help="read one retained run and its durable worker state",
    )
    supervisor_snapshot.add_argument("session")
    supervisor_snapshot.add_argument("--run", help="exact retained coordination run ID")
    supervisor_snapshot.set_defaults(handler=supervisor_snapshot_command)
    supervisor_usage = supervisor_actions.add_parser(
        "usage",
        help="summarize bounded cumulative and assignment-local provider usage",
    )
    supervisor_usage.add_argument("session")
    supervisor_usage.add_argument("--run", help="exact retained coordination run ID")
    supervisor_usage.add_argument(
        "--limit", type=supervisor_usage_limit, default=MAX_JSON_ITEMS
    )
    supervisor_usage.set_defaults(handler=supervisor_usage_command)
    supervisor_events = supervisor_actions.add_parser(
        "events",
        help="read per-role durable event pages with independent cursors",
    )
    supervisor_events.add_argument("session")
    supervisor_events.add_argument("--run", help="exact retained coordination run ID")
    supervisor_events.add_argument(
        "--role",
        action="append",
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
        help="enabled role to include; repeat for multiple roles",
    )
    supervisor_events.add_argument(
        "--cursor",
        action="append",
        type=supervisor_cursor,
        help="per-role cursor in ROLE=SEQUENCE form; repeat for multiple roles",
    )
    supervisor_events.add_argument("--limit", type=rpc_event_limit, default=50)
    supervisor_events.set_defaults(handler=supervisor_events_command)
    supervisor_command = supervisor_actions.add_parser(
        "command",
        help="query one retained idempotent RPC command",
    )
    supervisor_command.add_argument("session")
    supervisor_command.add_argument("--run", help="exact retained coordination run ID")
    supervisor_command.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
    supervisor_command.add_argument("--command-id", required=True, type=rpc_command_id)
    supervisor_command.set_defaults(handler=supervisor_command_command)

    list_parser = subparsers.add_parser("list", help="list running orchestrations")
    list_parser.set_defaults(handler=list_command)

    status = subparsers.add_parser("status", help="show pane and workflow status")
    status.add_argument("session", nargs="?")
    status.set_defaults(handler=status_command)

    events = subparsers.add_parser(
        "events",
        help="read durable metadata-only RPC supervisor events",
    )
    events.add_argument("session")
    events.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
    events.add_argument("--run", help="exact retained coordination run ID")
    events.add_argument("--after", type=rpc_event_cursor, default=0)
    events.add_argument("--limit", type=rpc_event_limit, default=50)
    events.set_defaults(handler=events_command)

    attach = subparsers.add_parser(
        "attach", help="attach or switch to an orchestration"
    )
    attach.add_argument("session", nargs="?")
    attach.set_defaults(handler=attach_command)

    send = subparsers.add_parser(
        "send", help="send a steer/follow-up message to a role"
    )
    send.add_argument("session")
    send.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
    send.add_argument("--message")
    send.add_argument("--message-file")
    send.add_argument("--run", help="exact retained RPC coordination run ID")
    send.add_argument(
        "--command-id",
        type=rpc_command_id,
        help="optional 32-character lowercase hexadecimal idempotency key for RPC delivery",
    )
    send.add_argument(
        "--delivery",
        choices=("steer", "follow-up"),
        default="steer",
        help="RPC queue behavior; follow-up requires --rpc-workers",
    )
    send.set_defaults(handler=send_command)

    abort = subparsers.add_parser("abort", help="abort one active RPC worker operation")
    abort.add_argument("session")
    abort.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
    abort.add_argument(
        "--command-id",
        type=rpc_command_id,
        help="optional 32-character lowercase hexadecimal idempotency key",
    )
    abort.add_argument("--run", help="exact retained RPC coordination run ID")
    abort.set_defaults(handler=abort_command)

    restart = subparsers.add_parser(
        "restart",
        help="restart one role while preserving its brokered Pi session",
        description=(
            "Respawn one role's worker process while preserving its brokered Pi "
            "conversation and JSONL history."
        ),
    )
    restart.add_argument("session")
    restart.add_argument(
        "--role",
        required=True,
        choices=("implementer", "reviewer", "probe", "playwright", "django"),
    )
    restart.add_argument("--provider")
    restart.add_argument("--model")
    restart.add_argument("--thinking", choices=THINKING_LEVELS)
    restart.add_argument("--skip-model-check", action="store_true")
    restart.add_argument(
        "--yes",
        action="store_true",
        help="confirm the worker-process respawn",
    )
    restart.set_defaults(handler=restart_command)

    stop = subparsers.add_parser("stop", help="stop one orchestration")
    stop.add_argument("session", nargs="?")
    stop.add_argument("--yes", action="store_true")
    stop.set_defaults(handler=stop_command)

    doctor = subparsers.add_parser(
        "doctor", help="check local prerequisites and defaults"
    )
    doctor.set_defaults(handler=doctor_command)

    return parser


def parse_internal_command(argv: list[str]) -> argparse.Namespace | None:
    if not argv or argv[0] not in {"_run-agent", "_broker", "_relay"}:
        return None
    command = argv[0]
    parser = argparse.ArgumentParser(prog=f"pi-tmux-agents {command}")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--coord", required=True)
    if command == "_run-agent":
        parser.add_argument("--role", required=True)
        parser.set_defaults(handler=run_agent_command)
    elif command == "_broker":
        parser.set_defaults(handler=broker_command)
    else:
        # Retained 0.4.x sessions may still respawn their legacy relay. New runs never use it.
        parser.set_defaults(handler=relay_command)
    return parser.parse_args(argv[1:])


def requested_command(argv: list[str]) -> str:
    public_commands = {
        "doctor",
        "controller",
        "supervisor",
        "abort",
        "list",
        "status",
        "events",
        "start",
        "attach",
        "send",
        "restart",
        "stop",
    }
    for value in argv:
        if value == "--json":
            continue
        if value.startswith("-"):
            return "unknown"
        return value if value in public_commands else "unknown"
    return "unknown"


def main() -> int:
    argv = sys.argv[1:]
    internal = parse_internal_command(argv)
    if internal is not None:
        try:
            return int(internal.handler(internal))
        except OrchestrationError as error:
            eprint(f"error: {bounded_message(error)}")
            return 2
        except subprocess.CalledProcessError as error:
            eprint(f"error: local command failed ({error.returncode})")
            if error.stderr:
                eprint(bounded_message(error.stderr))
            return error.returncode or 1

    runtime.JSON_MODE = "--json" in argv
    command = requested_command(argv)
    if runtime.JSON_MODE and "--version" in argv:
        emit_json("version", CommandResult(data={"version": VERSION}))
        return 0
    if runtime.JSON_MODE and any(value in {"-h", "--help"} for value in argv):
        result = CommandResult(
            code=2,
            error_code="interactive_help_only",
            error_message="CLI help is human-readable; omit --json to display it",
        )
        emit_json(command, result)
        return result.code
    parse_argv = list(argv)
    if runtime.JSON_MODE:
        parse_argv.remove("--json")
        parse_argv.insert(0, "--json")
    try:
        parser = build_parser()
        args = parser.parse_args(parse_argv)
        command = args.command
        outcome = args.handler(args)
        result = (
            outcome
            if isinstance(outcome, CommandResult)
            else CommandResult(code=int(outcome))
        )
    except OrchestrationError as error:
        result = CommandResult(
            code=2,
            error_code=error.code,
            error_message=bounded_message(error),
        )
    except subprocess.CalledProcessError as error:
        result = CommandResult(
            code=error.returncode or 1,
            error_code="subprocess_failed",
            error_message=f"A required local command failed with exit code {error.returncode}",
        )
    except (OSError, ValueError) as error:
        result = CommandResult(
            code=1,
            error_code="local_runtime_error",
            error_message="A bounded local runtime error prevented the command from completing",
        )
        if not runtime.JSON_MODE:
            result.error_message = bounded_message(error)
    except Exception:
        result = CommandResult(
            code=1,
            error_code="internal_error",
            error_message="An unexpected internal error prevented the command from completing",
        )

    if runtime.JSON_MODE:
        emit_json(command, result)
    elif result.code != 0:
        eprint(f"error: {bounded_message(result.error_message or 'command failed')}")
    return result.code
