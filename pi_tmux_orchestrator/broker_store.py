"""Private metadata-only SQLite state for brokered orchestration."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .budgeting import packaged_budget_policy, validate_budget_config
from .constants import BROKER_PROTOCOL_VERSION, MAX_BROKER_EVENTS, MAX_JSON_ITEMS
from .models import OrchestrationError
from .storage import ensure_private_directory, validate_coordination_directory

SCHEMA_VERSION = 3


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def broker_paths(coord: Path) -> dict[str, Path]:
    coord = validate_coordination_directory(coord)
    socket_name = hashlib.sha256(os.fsencode(coord)).hexdigest()[:24] + ".sock"
    socket_root = Path(f"/tmp/pi-tmux-orchestrator-{os.getuid()}")
    return {
        "database": coord / "broker.sqlite3",
        "socket": socket_root / socket_name,
    }


@contextmanager
def connect_broker_database(
    coord: Path, *, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    paths = broker_paths(coord)
    database = paths["database"]
    if readonly:
        if not database.is_file() or database.is_symlink():
            raise OrchestrationError("Broker state is unavailable", "broker_not_ready")
        uri = f"file:{database}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    else:
        ensure_private_directory(database.parent)
        if database.exists() and (database.is_symlink() or not database.is_file()):
            raise OrchestrationError("Broker database path is unsafe")
        previous_umask = os.umask(0o077)
        try:
            connection = sqlite3.connect(database, timeout=5.0)
        finally:
            os.umask(previous_umask)
    try:
        if not readonly:
            os.chmod(database, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not readonly:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_broker_database(
    coord: Path,
    manifest: dict[str, Any],
    tokens: dict[str, str],
    control_token: str,
    *,
    soft_role_tokens: int,
    soft_total_tokens: int,
    budget_policy: dict[str, Any] | None = None,
) -> None:
    selected_policy = (
        packaged_budget_policy() if budget_policy is None else budget_policy
    )
    if budget_policy is None:
        for scope, threshold in (
            ("role", soft_role_tokens),
            ("run", soft_total_tokens),
        ):
            if threshold == 0:
                selected_policy["warning"][scope].pop("operational_tokens", None)
            else:
                selected_policy["warning"][scope]["operational_tokens"] = threshold
    policy = validate_budget_config(selected_policy)
    with connect_broker_database(coord) as database:
        database.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roles (
                role TEXT PRIMARY KEY,
                auth_token TEXT NOT NULL,
                state TEXT NOT NULL,
                connected INTEGER NOT NULL DEFAULT 0,
                active_assignment_id TEXT,
                generation INTEGER NOT NULL DEFAULT 1,
                provider_calls INTEGER DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER,
                cost_total REAL NOT NULL DEFAULT 0,
                context_tokens INTEGER,
                context_window INTEGER,
                context_percent REAL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assignments (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL REFERENCES roles(role),
                round INTEGER NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                delivery_id TEXT NOT NULL UNIQUE,
                boundary_effective INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL UNIQUE REFERENCES assignments(id),
                role TEXT NOT NULL REFERENCES roles(role),
                round INTEGER NOT NULL,
                kind TEXT NOT NULL,
                verdict TEXT,
                summary_chars INTEGER NOT NULL,
                changed_path_count INTEGER NOT NULL,
                check_count INTEGER NOT NULL,
                finding_count INTEGER NOT NULL,
                risk_count INTEGER NOT NULL,
                limitation_count INTEGER NOT NULL,
                provider_calls INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                reasoning_tokens INTEGER,
                cost_total REAL,
                context_tokens INTEGER,
                context_window INTEGER,
                context_percent REAL,
                peak_context_tokens INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS control_commands (
                id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                role TEXT NOT NULL REFERENCES roles(role),
                delivery TEXT,
                status TEXT NOT NULL,
                received_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event TEXT NOT NULL,
                role TEXT,
                round INTEGER,
                assignment_id TEXT,
                delivery_id TEXT,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_role_sequence ON events(role, sequence);
        """)
        existing = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if existing is not None:
            raise OrchestrationError("Broker database already exists")
        now = utc_now()
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "protocol_version": str(BROKER_PROTOCOL_VERSION),
            "workflow_state": "starting",
            "round": "1",
            "control_token": control_token,
            "soft_role_tokens": str(soft_role_tokens),
            "soft_total_tokens": str(soft_total_tokens),
            "budget_policy": json.dumps(policy, separators=(",", ":"), sort_keys=True),
            "created_at": now,
            "updated_at": now,
        }
        database.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)", metadata.items()
        )
        database.executemany(
            "INSERT INTO roles(role, auth_token, state, updated_at) VALUES (?, ?, 'disconnected', ?)",
            [(role, tokens[role], now) for role in manifest["roles"]],
        )
        record_event(database, "broker_initialized", status="starting")


def prepare_broker_database(coord: Path) -> None:
    """Apply metadata-only forward migrations needed by the live broker."""

    with connect_broker_database(coord) as database:
        row = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise OrchestrationError("Broker database schema is unavailable")
        try:
            version = int(row["value"])
        except ValueError as error:
            raise OrchestrationError("Broker database schema is invalid") from error
        if version == 1:
            database.execute(
                "ALTER TABLE assignments ADD COLUMN "
                "boundary_effective INTEGER NOT NULL DEFAULT 0"
            )
            version = 2
        if version == 2:
            database.execute("ALTER TABLE roles ADD COLUMN provider_calls INTEGER")
            for column, kind in (
                ("provider_calls", "INTEGER"),
                ("input_tokens", "INTEGER"),
                ("output_tokens", "INTEGER"),
                ("cache_read_tokens", "INTEGER"),
                ("cache_write_tokens", "INTEGER"),
                ("reasoning_tokens", "INTEGER"),
                ("cost_total", "REAL"),
                ("context_tokens", "INTEGER"),
                ("context_window", "INTEGER"),
                ("context_percent", "REAL"),
                ("peak_context_tokens", "INTEGER"),
            ):
                database.execute(f"ALTER TABLE reports ADD COLUMN {column} {kind}")
            version = 3
        if version != SCHEMA_VERSION:
            raise OrchestrationError("Broker database schema is unsupported")
        database.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (str(version),)
        )


def record_event(
    database: sqlite3.Connection,
    event: str,
    *,
    role: str | None = None,
    round_number: int | None = None,
    assignment_id: str | None = None,
    delivery_id: str | None = None,
    status: str,
) -> int:
    cursor = database.execute(
        "INSERT INTO events(timestamp,event,role,round,assignment_id,delivery_id,status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utc_now(), event, role, round_number, assignment_id, delivery_id, status),
    )
    database.execute(
        "DELETE FROM events WHERE sequence <= "
        "(SELECT COALESCE(MAX(sequence), 0) - ? FROM events)",
        (MAX_BROKER_EVENTS,),
    )
    return int(cursor.lastrowid)


def set_meta(database: sqlite3.Connection, key: str, value: str) -> None:
    database.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
    database.execute("UPDATE meta SET value=? WHERE key='updated_at'", (utc_now(),))


def broker_role_generation(coord: Path, role: str) -> int:
    """Return the broker-authoritative worker generation without exposing tokens."""

    with connect_broker_database(coord, readonly=True) as database:
        row = database.execute(
            "SELECT generation FROM roles WHERE role=?", (role,)
        ).fetchone()
    if row is None or type(row["generation"]) is not int or row["generation"] < 1:
        raise OrchestrationError("Broker worker generation is unavailable")
    return int(row["generation"])


def _public_assignment_usage(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    value = {
        "assignment_id": row["assignment_id"],
        "round": row["round"],
        "kind": row["kind"],
        "usage": None,
    }
    if "role" in row.keys():
        value["role"] = row["role"]
    if "provider_calls" not in row.keys() or row["provider_calls"] is None:
        return value
    usage = {
        "provider_calls": row["provider_calls"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "reasoning_tokens": row["reasoning_tokens"],
        "cost_total": row["cost_total"],
        "context_tokens": row["context_tokens"],
        "context_window": row["context_window"],
        "context_percent": row["context_percent"],
        "peak_context_tokens": row["peak_context_tokens"],
        "operational_tokens": (
            row["input_tokens"]
            + row["output_tokens"]
            + row["cache_read_tokens"]
            + row["cache_write_tokens"]
        ),
        "actual_provider_usage_only": True,
    }
    value["usage"] = usage
    return value


def public_broker_snapshot(coord: Path) -> dict[str, Any]:
    with connect_broker_database(coord, readonly=True) as database:
        meta = {
            row["key"]: row["value"]
            for row in database.execute("SELECT key,value FROM meta")
        }
        schema_version = int(meta.get("schema_version", "0"))
        provider_calls = (
            "provider_calls" if schema_version >= 3 else "NULL AS provider_calls"
        )
        roles = []
        for row in database.execute(
            f"SELECT role,state,connected,active_assignment_id,generation,{provider_calls},"
            "input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,reasoning_tokens,"
            "cost_total,context_tokens,context_window,context_percent,updated_at "
            "FROM roles ORDER BY role"
        ):
            value = dict(row)
            value["connected"] = bool(value["connected"])
            assignment_id = value.pop("active_assignment_id")
            assignment = None
            if assignment_id is not None:
                assignment_row = database.execute(
                    "SELECT round,kind,state FROM assignments WHERE id=?",
                    (assignment_id,),
                ).fetchone()
                if assignment_row is not None:
                    assignment = dict(assignment_row)
            if schema_version >= 3:
                latest = database.execute(
                    "SELECT assignment_id,round,kind,provider_calls,input_tokens,output_tokens,"
                    "cache_read_tokens,cache_write_tokens,reasoning_tokens,cost_total,"
                    "context_tokens,context_window,context_percent,peak_context_tokens "
                    "FROM reports WHERE role=? ORDER BY round DESC,created_at DESC LIMIT 1",
                    (value["role"],),
                ).fetchone()
            else:
                latest = database.execute(
                    "SELECT assignment_id,round,kind FROM reports WHERE role=? "
                    "ORDER BY round DESC,created_at DESC LIMIT 1",
                    (value["role"],),
                ).fetchone()
            value["assignment"] = assignment
            value["latest_assignment_usage"] = _public_assignment_usage(latest)
            roles.append(value)
        event_bounds = database.execute(
            "SELECT MIN(sequence) AS earliest, MAX(sequence) AS latest FROM events"
        ).fetchone()
        total_tokens = sum(
            role["input_tokens"]
            + role["output_tokens"]
            + role["cache_read_tokens"]
            + role["cache_write_tokens"]
            for role in roles
        )
        role_budget = int(meta.get("soft_role_tokens", "0"))
        total_budget = int(meta.get("soft_total_tokens", "0"))
        for role in roles:
            role["total_tokens"] = (
                role["input_tokens"]
                + role["output_tokens"]
                + role["cache_read_tokens"]
                + role["cache_write_tokens"]
            )
            role["soft_budget_exceeded"] = (
                role_budget > 0 and role["total_tokens"] >= role_budget
            )
        return {
            "workflow": {
                "state": meta.get("workflow_state", "unknown"),
                "round": int(meta.get("round", "0")),
                "updated_at": meta.get("updated_at"),
            },
            "roles": roles,
            "usage": {
                "provider_calls": (
                    sum(role["provider_calls"] for role in roles)
                    if all(role["provider_calls"] is not None for role in roles)
                    else None
                ),
                "total_tokens": total_tokens,
                "soft_role_tokens": role_budget,
                "soft_total_tokens": total_budget,
                "soft_total_budget_exceeded": total_budget > 0
                and total_tokens >= total_budget,
                "actual_provider_usage_only": True,
            },
            "event_cursor": {
                "earliest_retained": event_bounds["earliest"],
                "latest": event_bounds["latest"] or 0,
            },
        }


def public_assignment_usage(coord: Path, *, limit: int) -> dict[str, Any]:
    """Return a bounded latest assignment-usage page without payload bodies."""

    if type(limit) is not int or not 1 <= limit <= MAX_JSON_ITEMS:
        raise OrchestrationError(
            f"Assignment usage limit must be between 1 and {MAX_JSON_ITEMS}",
            "invalid_arguments",
        )
    with connect_broker_database(coord, readonly=True) as database:
        row = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise OrchestrationError("Broker database schema is unavailable")
        schema_version = int(row["value"])
        if schema_version >= 3:
            fields = (
                "assignment_id,role,round,kind,provider_calls,input_tokens,output_tokens,"
                "cache_read_tokens,cache_write_tokens,reasoning_tokens,cost_total,"
                "context_tokens,context_window,context_percent,peak_context_tokens"
            )
        else:
            fields = "assignment_id,role,round,kind"
        rows = list(
            database.execute(
                f"SELECT {fields} FROM reports ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (limit + 1,),
            )
        )
    selected = [_public_assignment_usage(row) for row in rows[:limit]]
    selected.sort(
        key=lambda value: (
            value["round"],
            value.get("role", ""),
            value["kind"],
            value["assignment_id"],
        )
    )
    return {
        "assignments": selected,
        "truncated": len(rows) > limit,
        "limit": limit,
    }


def public_broker_events(
    coord: Path, *, after: int, limit: int, role: str | None = None
) -> dict[str, Any]:
    if (
        type(after) is not int
        or after < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise OrchestrationError(
            "Broker event cursor or limit is invalid", "invalid_arguments"
        )
    with connect_broker_database(coord, readonly=True) as database:
        where = "WHERE sequence > ?"
        values: list[Any] = [after]
        if role is not None:
            where += " AND role = ?"
            values.append(role)
        rows = list(
            database.execute(
                f"SELECT sequence,timestamp,event,role,round,assignment_id,delivery_id,status "
                f"FROM events {where} ORDER BY sequence LIMIT ?",
                (*values, limit + 1),
            )
        )
        bounds = database.execute(
            "SELECT MIN(sequence) AS earliest, MAX(sequence) AS latest FROM events"
        ).fetchone()
    selected = [dict(row) for row in rows[:limit]]
    earliest = bounds["earliest"]
    return {
        "events": selected,
        "cursor": {
            "after": after,
            "next": selected[-1]["sequence"] if selected else after,
            "earliest_retained": earliest,
            "latest": bounds["latest"] or 0,
            "gap": earliest is not None and earliest > after + 1,
            "truncated": len(rows) > limit,
        },
    }


def metadata_json(value: object) -> str:
    """Canonical compact JSON for metadata fields only."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
