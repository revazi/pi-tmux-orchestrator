"""Versioned tmux-independent read API for durable worker supervisor state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import runtime
from .broker_store import (
    public_assignment_usage,
    public_broker_events,
    public_broker_snapshot,
)
from .constants import (
    MAX_JSON_ITEMS,
    MAX_RPC_COMMANDS,
    MAX_RPC_EVENTS,
    MAX_SUPERVISOR_SCAN_ENTRIES,
    RPC_TERMINAL_COMMAND_STATUSES,
    RPC_TOKEN_PATTERN,
    RPC_TRANSPORT,
    SUPERVISOR_API_VERSION,
)
from .models import OrchestrationError
from .output import bounded_message, public_role
from .profiles import retained_execution_profile
from .rpc_store import (
    load_rpc_events,
    load_rpc_registry,
    load_rpc_state,
    public_rpc_event,
    public_rpc_registry,
    public_rpc_state,
    rpc_command_map,
)
from .storage import (
    absolute_path,
    canonical_state_root,
    load_manifest,
    manifest_transport,
    require_directory,
    retained_coordination,
    validate_coordination_directory,
)
from .tmux import validate_session_name


def supervisor_capabilities() -> dict[str, Any]:
    """Return the stable feature description consumed by supervisor clients."""
    return {
        "api_version": SUPERVISOR_API_VERSION,
        "envelope_schema_version": "1",
        "state_plane": "metadata-only-sqlite",
        "worker_transport": "tui-or-rpc-with-shared-bridge",
        "coordination_protocol": {
            "name": "broker-v1",
            "transport": "owner-only-unix-socket",
            "payload_files": False,
            "polling": False,
        },
        "host_adapter": {
            "name": "tmux",
            "attachment": "terminal-only",
            "runtime_observed_by_read_api": False,
        },
        "read_operations": [
            "capabilities",
            "sessions",
            "runs",
            "snapshot",
            "usage",
            "events",
            "command",
        ],
        "control_semantics": {
            "acknowledgement": "acceptance-or-queueing",
            "completion_observed_via": ["events", "command"],
            "crash_ambiguity": "uncertain",
            "exactly_once": False,
        },
        "control_commands": {
            "send": {
                "command": "send",
                "deliveries": ["steer", "follow-up"],
                "exact_run_option": "--run",
            },
            "abort": {
                "command": "abort",
                "exact_run_option": "--run",
            },
        },
        "event_cursor": {
            "scope": "role",
            "initial": 0,
            "argument": "--cursor ROLE=SEQUENCE",
            "retention_gap_reported": True,
        },
        "execution_profiles": {
            "manifest_metadata_since": 4,
            "legacy": "unavailable",
            "provider_usage_and_quality_evidence": "unavailable",
        },
        "usage_accounting": {
            "cumulative_role_usage": True,
            "latest_assignment_usage": True,
            "bounded_assignment_usage_page": True,
            "assignment_result_immutable": True,
            "legacy_assignment_usage": "unavailable",
            "provider_reported_only": True,
            "in_assignment_guardrails": True,
            "guardrail_metadata": True,
            "guardrail_mode": "observational",
            "budget_routing_gates": False,
        },
        "limits": {
            "page_items": MAX_JSON_ITEMS,
            "events_per_role": MAX_RPC_EVENTS,
            "commands_per_role": MAX_RPC_COMMANDS,
            "state_scan_entries": MAX_SUPERVISOR_SCAN_ENTRIES,
        },
        "metadata_only": True,
    }


def bounded_children(path: Path, maximum: int | None = None) -> tuple[list[Path], bool]:
    """Read a bounded number of direct children without following them."""
    resolved_maximum = MAX_SUPERVISOR_SCAN_ENTRIES if maximum is None else maximum
    if (
        type(resolved_maximum) is not int
        or not 1 <= resolved_maximum <= MAX_SUPERVISOR_SCAN_ENTRIES
    ):
        raise OrchestrationError(
            "Supervisor state scan limit is invalid", "invalid_arguments"
        )
    children: list[Path] = []
    truncated = False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if len(children) >= resolved_maximum:
                    truncated = True
                    break
                children.append(path / entry.name)
    except OSError as error:
        raise OrchestrationError(
            "Cannot enumerate retained orchestration state"
        ) from error
    return children, truncated


def public_supervisor_run(coord: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    transport = manifest_transport(manifest)
    return {
        "session": manifest["session"],
        "run_id": coord.name,
        "created_at": manifest["created_at"],
        "project": manifest["project"],
        "transport": transport,
        "execution_profile": retained_execution_profile(manifest),
        "durable_workers": manifest.get("version", 0) >= 3
        or transport == RPC_TRANSPORT,
        "roles": [
            public_role(role, config, transport)
            for role, config in manifest["roles"].items()
        ],
        "paths": {"coordination": str(coord)},
    }


def retained_runs(
    session: str,
    *,
    limit: int,
) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, str]], bool]:
    """Return newest valid retained runs with bounded tamper diagnostics."""
    return _retained_runs(
        session,
        limit=limit,
        scan_limit=MAX_SUPERVISOR_SCAN_ENTRIES,
    )


def _retained_runs(
    session: str,
    *,
    limit: int,
    scan_limit: int,
) -> tuple[list[tuple[Path, dict[str, Any]]], list[dict[str, str]], bool]:
    if type(limit) is not int or not 1 <= limit <= MAX_JSON_ITEMS:
        raise OrchestrationError(
            f"Supervisor run limit must be between 1 and {MAX_JSON_ITEMS}",
            "invalid_arguments",
        )
    session = validate_session_name(session)
    root = canonical_state_root(create=False)
    session_root = absolute_path(root / session)
    try:
        require_directory(session_root, "orchestration session state")
    except (FileNotFoundError, OrchestrationError) as error:
        raise OrchestrationError(
            f"No retained orchestration state was found for {session}",
            "orchestration_state_not_found",
        ) from error
    if session_root.resolve(strict=True) != session_root or session_root.parent != root:
        raise OrchestrationError("Orchestration session state path is invalid")

    candidates, scan_truncated = bounded_children(session_root, scan_limit)
    selected: list[tuple[Path, dict[str, Any]]] = []
    issues: list[dict[str, str]] = []
    more_valid = False
    for candidate in sorted(candidates, key=lambda item: item.name, reverse=True):
        try:
            require_directory(candidate, "coordination run")
            coord = validate_coordination_directory(candidate)
            manifest = load_manifest(coord, expected_session=session)
        except (FileNotFoundError, OSError, OrchestrationError) as error:
            if len(issues) < MAX_JSON_ITEMS:
                issues.append(
                    {
                        "run_id": bounded_message(candidate.name, 160),
                        "message": bounded_message(error),
                    }
                )
            continue
        if len(selected) >= limit:
            more_valid = True
            break
        selected.append((coord, manifest))
    return selected, issues, scan_truncated or more_valid


def retained_sessions() -> dict[str, Any]:
    root_path = absolute_path(runtime.STATE_ROOT)
    try:
        root = canonical_state_root(create=False)
    except OrchestrationError:
        try:
            root_path.lstat()
        except FileNotFoundError:
            return {
                "api_version": SUPERVISOR_API_VERSION,
                "sessions": [],
                "issues": [],
                "truncated": False,
            }
        raise

    candidates, scan_truncated = bounded_children(root)
    sessions: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    more_valid = False
    page_truncated = False
    for candidate in sorted(candidates, key=lambda item: item.name):
        if len(sessions) + len(issues) >= MAX_JSON_ITEMS:
            page_truncated = True
            break
        try:
            session = validate_session_name(candidate.name)
            runs, run_issues, runs_truncated = _retained_runs(
                session,
                limit=1,
                scan_limit=MAX_JSON_ITEMS,
            )
            if not runs:
                raise OrchestrationError("No valid retained coordination run was found")
            coord, manifest = runs[0]
        except (FileNotFoundError, OSError, OrchestrationError) as error:
            if len(issues) < MAX_JSON_ITEMS:
                issues.append(
                    {
                        "session": bounded_message(candidate.name, 160),
                        "message": bounded_message(error),
                    }
                )
            continue
        if len(sessions) >= MAX_JSON_ITEMS:
            more_valid = True
            break
        value = public_supervisor_run(coord, manifest)
        value["runs_truncated"] = runs_truncated
        value["run_issue_count"] = len(run_issues)
        sessions.append(value)
    return {
        "api_version": SUPERVISOR_API_VERSION,
        "sessions": sessions,
        "issues": issues,
        "truncated": scan_truncated or page_truncated or more_valid,
    }


def resolve_supervisor_target(
    session: str,
    run_id: str | None,
    *,
    require_rpc: bool,
) -> tuple[Path, dict[str, Any]]:
    session = validate_session_name(session)
    coord = retained_coordination(session, run_id)
    manifest = load_manifest(coord, expected_session=session)
    if require_rpc and manifest_transport(manifest) != RPC_TRANSPORT:
        raise OrchestrationError(
            "Supervisor operation requires an orchestration using RPC workers",
            "supervisor_requires_rpc",
        )
    return coord, manifest


def rpc_event_page(
    events: list[dict[str, Any]],
    *,
    after: int,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if type(after) is not int or after < 0:
        raise OrchestrationError(
            "Supervisor event cursor must be a non-negative integer",
            "invalid_arguments",
        )
    if type(limit) is not int or not 1 <= limit <= MAX_RPC_EVENTS:
        raise OrchestrationError(
            f"Supervisor event limit must be between 1 and {MAX_RPC_EVENTS}",
            "invalid_arguments",
        )
    selected_candidates = [event for event in events if event["sequence"] > after]
    selected = selected_candidates[:limit]
    earliest = events[0]["sequence"] if events else None
    latest = events[-1]["sequence"] if events else 0
    return selected, {
        "after": after,
        "next": selected[-1]["sequence"] if selected else after,
        "earliest_retained": earliest,
        "latest": latest,
        "gap": earliest is not None and earliest > after + 1,
        "truncated": len(selected_candidates) > len(selected),
    }


def public_runtime_record(state: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "source": "retained-state",
        "liveness": "not-observed",
        "state": public_rpc_state(state),
    }


def supervisor_snapshot(session: str, run_id: str | None) -> dict[str, Any]:
    coord, manifest = resolve_supervisor_target(session, run_id, require_rpc=False)
    transport = manifest_transport(manifest)
    if manifest.get("version", 0) >= 3:
        snapshot = public_broker_snapshot(coord)
        role_state = {value["role"]: value for value in snapshot["roles"]}
        roles = []
        for role, config in manifest["roles"].items():
            value = public_role(role, config, transport)
            value["runtime"] = {
                "source": "retained-broker-state",
                "liveness": "not-observed",
                "state": role_state[role],
            }
            value["worker"] = None
            value["event_cursor"] = snapshot["event_cursor"]
            roles.append(value)
        return {
            "api_version": SUPERVISOR_API_VERSION,
            "session": manifest["session"],
            "run_id": coord.name,
            "created_at": manifest["created_at"],
            "project": manifest["project"],
            "transport": transport,
            "execution_profile": retained_execution_profile(manifest),
            "coordination": manifest["coordination"],
            "durable_workers": True,
            "host_adapter": {"name": "tmux", "runtime_status": "not_observed"},
            "workflow": snapshot["workflow"],
            "guardrails": snapshot["guardrails"],
            "usage": snapshot["usage"],
            "roles": roles,
            "paths": {"coordination": str(coord)},
        }
    roles: list[dict[str, Any]] = []
    for role, config in manifest["roles"].items():
        value = public_role(role, config, transport)
        if transport == RPC_TRANSPORT:
            events = load_rpc_events(coord, role)
            value["runtime"] = public_runtime_record(load_rpc_state(coord, role))
            worker = public_rpc_registry(load_rpc_registry(coord, role))
            latest = events[-1]["sequence"] if events else 0
            value["worker"] = worker
            value["event_cursor"] = {
                "earliest_retained": events[0]["sequence"] if events else None,
                "latest": latest,
                "registry_sequence": (
                    worker["last_event_sequence"] if worker is not None else None
                ),
                "synchronized": (
                    worker is not None and worker["last_event_sequence"] == latest
                ),
            }
        else:
            value["runtime"] = None
            value["worker"] = None
            value["event_cursor"] = None
        roles.append(value)
    return {
        "api_version": SUPERVISOR_API_VERSION,
        "session": manifest["session"],
        "run_id": coord.name,
        "created_at": manifest["created_at"],
        "project": manifest["project"],
        "transport": transport,
        "execution_profile": retained_execution_profile(manifest),
        "durable_workers": transport == RPC_TRANSPORT,
        "host_adapter": {
            "name": "tmux",
            "runtime_status": "not_observed",
        },
        "roles": roles,
        "paths": {"coordination": str(coord)},
    }


def _role_cumulative_usage(role: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider_calls": role.get("provider_calls"),
        "input_tokens": role["input_tokens"],
        "output_tokens": role["output_tokens"],
        "cache_read_tokens": role["cache_read_tokens"],
        "cache_write_tokens": role["cache_write_tokens"],
        "reasoning_tokens": role["reasoning_tokens"],
        "cost_total": role["cost_total"],
        "operational_tokens": role["total_tokens"],
        "context_tokens": role["context_tokens"],
        "context_window": role["context_window"],
        "context_percent": role["context_percent"],
        "actual_provider_usage_only": True,
    }


def _run_cumulative_usage(roles: list[dict[str, Any]]) -> dict[str, Any]:
    provider_calls = [role.get("provider_calls") for role in roles]
    reasoning = [role["reasoning_tokens"] for role in roles]
    return {
        "provider_calls": (
            sum(provider_calls)
            if all(value is not None for value in provider_calls)
            else None
        ),
        "input_tokens": sum(role["input_tokens"] for role in roles),
        "output_tokens": sum(role["output_tokens"] for role in roles),
        "cache_read_tokens": sum(role["cache_read_tokens"] for role in roles),
        "cache_write_tokens": sum(role["cache_write_tokens"] for role in roles),
        "reasoning_tokens": (
            sum(reasoning) if all(value is not None for value in reasoning) else None
        ),
        "cost_total": sum(role["cost_total"] for role in roles),
        "operational_tokens": sum(role["total_tokens"] for role in roles),
        "actual_provider_usage_only": True,
    }


def supervisor_usage(session: str, run_id: str | None, *, limit: int) -> dict[str, Any]:
    """Return bounded assignment-local and cumulative provider usage metadata."""

    if type(limit) is not int or not 1 <= limit <= MAX_JSON_ITEMS:
        raise OrchestrationError(
            f"Supervisor usage limit must be between 1 and {MAX_JSON_ITEMS}",
            "invalid_arguments",
        )
    coord, manifest = resolve_supervisor_target(session, run_id, require_rpc=False)
    base = {
        "api_version": SUPERVISOR_API_VERSION,
        "session": manifest["session"],
        "run_id": coord.name,
        "available": manifest.get("version", 0) >= 3,
        "semantics": {
            "cumulative": "complete retained role usage",
            "assignment": "immutable delta from the accepted assignment boundary",
            "operational_tokens": "input + output + cache read + cache write; not a billing unit",
            "cost": "provider-reported cumulative cost or its assignment delta only",
            "payload_bodies_included": False,
        },
        "paths": {"coordination": str(coord)},
    }
    if manifest.get("version", 0) < 3:
        return {
            **base,
            "availability": "unavailable_legacy_coordination",
            "cumulative": None,
            "roles": [],
            "assignment_count": 0,
            "assignment_usage_unavailable": 0,
            "truncated": False,
            "limit": limit,
        }
    snapshot = public_broker_snapshot(coord)
    page = public_assignment_usage(coord, limit=limit)
    by_role: dict[str, list[dict[str, Any]]] = {role: [] for role in manifest["roles"]}
    unavailable = 0
    for assignment in page["assignments"]:
        role = assignment.get("role")
        if role not in by_role:
            continue
        value = {key: item for key, item in assignment.items() if key != "role"}
        by_role[role].append(value)
        if value["usage"] is None:
            unavailable += 1
    role_state = {role["role"]: role for role in snapshot["roles"]}
    roles = [
        {
            "role": role,
            "cumulative": _role_cumulative_usage(role_state[role]),
            "assignments": by_role[role],
        }
        for role in manifest["roles"]
    ]
    return {
        **base,
        "availability": "available",
        "cumulative": _run_cumulative_usage(snapshot["roles"]),
        "roles": roles,
        "assignment_count": sum(len(value) for value in by_role.values()),
        "assignment_usage_unavailable": unavailable,
        "truncated": page["truncated"],
        "limit": page["limit"],
    }


def selected_supervisor_roles(
    manifest: dict[str, Any], requested: list[str] | None
) -> list[str]:
    enabled = list(manifest["roles"])
    if requested is None:
        return enabled
    if not isinstance(requested, list) or any(
        not isinstance(role, str) for role in requested
    ):
        raise OrchestrationError(
            "Supervisor event roles must be a list of role names",
            "invalid_arguments",
        )
    if not requested:
        return enabled
    if len(set(requested)) != len(requested):
        raise OrchestrationError(
            "Supervisor event roles must be unique", "invalid_arguments"
        )
    unavailable = [role for role in requested if role not in manifest["roles"]]
    if unavailable:
        raise OrchestrationError(
            f"Roles are not enabled for this orchestration: {', '.join(unavailable)}",
            "invalid_arguments",
        )
    return requested


def supervisor_event_batch(
    session: str,
    run_id: str | None,
    *,
    requested_roles: list[str] | None,
    cursors: dict[str, int],
    limit: int,
) -> dict[str, Any]:
    if not isinstance(cursors, dict) or any(
        not isinstance(role, str) for role in cursors
    ):
        raise OrchestrationError(
            "Supervisor event cursors must map role names to sequences",
            "invalid_arguments",
        )
    if type(limit) is not int or not 1 <= limit <= MAX_RPC_EVENTS:
        raise OrchestrationError(
            f"Supervisor event limit must be between 1 and {MAX_RPC_EVENTS}",
            "invalid_arguments",
        )
    if any(type(sequence) is not int or sequence < 0 for sequence in cursors.values()):
        raise OrchestrationError(
            "Supervisor event cursors must be non-negative integers",
            "invalid_arguments",
        )
    coord, manifest = resolve_supervisor_target(session, run_id, require_rpc=False)
    roles = selected_supervisor_roles(manifest, requested_roles)
    invalid_cursors = [role for role in cursors if role not in roles]
    if invalid_cursors:
        raise OrchestrationError(
            f"Cursors require a selected enabled role: {', '.join(invalid_cursors)}",
            "invalid_arguments",
        )
    if manifest.get("version", 0) >= 3:
        values = []
        for role in roles:
            page = public_broker_events(
                coord, after=cursors.get(role, 0), limit=limit, role=role
            )
            values.append(
                {
                    "role": role,
                    "runtime": {
                        "source": "retained-broker-state",
                        "liveness": "not-observed",
                    },
                    "worker": None,
                    "events": page["events"],
                    "cursor": page["cursor"],
                }
            )
        return {
            "api_version": SUPERVISOR_API_VERSION,
            "session": manifest["session"],
            "run_id": coord.name,
            "roles": values,
            "paths": {"coordination": str(coord)},
        }
    if manifest_transport(manifest) != RPC_TRANSPORT:
        raise OrchestrationError(
            "Supervisor events require a brokered or RPC-worker orchestration",
            "supervisor_requires_rpc",
        )
    values: list[dict[str, Any]] = []
    for role in roles:
        after = cursors.get(role, 0)
        selected, cursor = rpc_event_page(
            load_rpc_events(coord, role), after=after, limit=limit
        )
        worker = public_rpc_registry(load_rpc_registry(coord, role))
        cursor["registry_sequence"] = (
            worker["last_event_sequence"] if worker is not None else None
        )
        cursor["synchronized"] = (
            worker is not None and worker["last_event_sequence"] == cursor["latest"]
        )
        values.append(
            {
                "role": role,
                "runtime": public_runtime_record(load_rpc_state(coord, role)),
                "worker": worker,
                "events": [public_rpc_event(event) for event in selected],
                "cursor": cursor,
            }
        )
    return {
        "api_version": SUPERVISOR_API_VERSION,
        "session": manifest["session"],
        "run_id": coord.name,
        "roles": values,
        "paths": {"coordination": str(coord)},
    }


def public_rpc_command(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": command["id"],
        "command": command["command"],
        "delivery": command["delivery"],
        "status": command["status"],
        "terminal": command["status"] in RPC_TERMINAL_COMMAND_STATUSES,
        "received_at": command["received_at"],
        "updated_at": command["updated_at"],
        "event_sequence": command["event_sequence"],
    }


def supervisor_command_status(
    session: str,
    run_id: str | None,
    *,
    role: str,
    command_id: str,
) -> dict[str, Any]:
    if not isinstance(command_id, str) or not RPC_TOKEN_PATTERN.fullmatch(command_id):
        raise OrchestrationError(
            "Command ID must be exactly 32 lowercase hexadecimal characters",
            "invalid_arguments",
        )
    coord, manifest = resolve_supervisor_target(session, run_id, require_rpc=False)
    if role not in manifest["roles"]:
        raise OrchestrationError(
            f"Role {role!r} is not enabled for this orchestration",
            "invalid_arguments",
        )
    if manifest.get("version", 0) >= 3:
        from .broker_store import connect_broker_database

        with connect_broker_database(coord, readonly=True) as database:
            command = database.execute(
                "SELECT id,action,role,delivery,status,received_at,updated_at "
                "FROM control_commands WHERE id=? AND role=?",
                (command_id, role),
            ).fetchone()
        if command is None:
            raise OrchestrationError(
                f"Broker command {command_id} is not retained for {role}",
                "broker_command_not_found",
            )
        value = dict(command)
        value["terminal"] = value["status"] in {"rejected", "uncertain"}
        return {
            "api_version": SUPERVISOR_API_VERSION,
            "session": manifest["session"],
            "run_id": coord.name,
            "role": role,
            "command": value,
            "paths": {"coordination": str(coord)},
        }
    if manifest.get("transport") != RPC_TRANSPORT:
        raise OrchestrationError(
            "Supervisor command status requires a brokered or RPC-worker orchestration",
            "supervisor_requires_rpc",
        )
    registry = load_rpc_registry(coord, role)
    if registry is None:
        raise OrchestrationError(
            f"RPC supervisor registry is unavailable for {role}",
            "supervisor_not_ready",
        )
    command = rpc_command_map(registry).get(command_id)
    if command is None:
        raise OrchestrationError(
            f"RPC command {command_id} is not retained for {role}",
            "rpc_command_not_found",
        )
    return {
        "api_version": SUPERVISOR_API_VERSION,
        "session": manifest["session"],
        "run_id": coord.name,
        "role": role,
        "worker_id": registry["worker_id"],
        "generation": registry["generation"],
        "command": public_rpc_command(command),
        "paths": {"coordination": str(coord)},
    }


def supervisor_cursor_arguments(
    values: list[tuple[str, int]] | None,
) -> dict[str, int]:
    cursors: dict[str, int] = {}
    for role, sequence in values or []:
        if role not in {"implementer", "reviewer", "probe", "playwright", "django"}:
            raise OrchestrationError(
                "Supervisor cursor role is invalid", "invalid_arguments"
            )
        if type(sequence) is not int or sequence < 0:
            raise OrchestrationError(
                "Supervisor event cursor must be a non-negative integer",
                "invalid_arguments",
            )
        if role in cursors:
            raise OrchestrationError(
                f"Supervisor cursor for {role} was provided more than once",
                "invalid_arguments",
            )
        cursors[role] = sequence
    return cursors
