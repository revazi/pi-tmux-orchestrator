"""Explicit provider-turn ceilings and durable, conservative admission accounting.

Only the broker writes this ledger. A committed allowance is never refunded:
transport loss or cancellation cannot establish that a request was not sent.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from typing import Any

from .models import OrchestrationError

MAX_CALL_ALLOWANCE = 1_000_000
CALL_SCOPES = ("run", "role", "assignment")


def require_admission_runtime() -> None:
    # Version output only; no session, registry, credentials or provider request.
    try:
        result = subprocess.run(
            ["pi", "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        raise OrchestrationError(
            "Provider-call enforcement requires verified Pi 1.4.0"
        ) from None
    if result.returncode != 0 or result.stdout.strip() != "1.4.0":
        raise OrchestrationError("Provider-call enforcement requires verified Pi 1.4.0")


def call_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CALL_ALLOWANCE:
        raise OrchestrationError("Call allowance must be an integer from 1 to 1000000")
    return value


def parse_call_limit(value: str) -> tuple[str, int]:
    scope, separator, amount = value.partition("=")
    if (
        scope not in CALL_SCOPES
        or separator != "="
        or len(amount) > 7
        or not amount.isascii()
        or not amount.isdecimal()
    ):
        raise OrchestrationError("Call limit must be run|role|assignment=INTEGER")
    return scope, call_limit(int(amount))


def call_policy(value: object = None) -> dict[str, Any]:
    if value is None:
        return {"version": 1, "limits": {}}
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "limits"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["limits"], dict)
        or set(value["limits"]) - set(CALL_SCOPES)
    ):
        raise OrchestrationError("Provider-call admission policy is invalid")
    return {
        "version": 1,
        "limits": {key: call_limit(item) for key, item in value["limits"].items()},
    }


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate policy field")
    return result


def retained_call_policy(database: sqlite3.Connection) -> dict[str, Any]:
    row = database.execute(
        "SELECT value FROM meta WHERE key='call_admission_policy'"
    ).fetchone()
    if row is None:
        schema = database.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if schema is not None and int(schema[0]) >= 13:
            raise OrchestrationError("Provider-call admission policy is missing")
        return call_policy()
    try:
        if len(row[0]) > 256:
            raise ValueError("oversized")
        value = json.loads(row[0], object_pairs_hook=_unique_fields)
        if not isinstance(value, dict):
            raise ValueError("invalid policy")
    except (ValueError, TypeError) as error:
        raise OrchestrationError("Provider-call admission policy is invalid") from error
    return call_policy(value)


def create_admission_tables(database: sqlite3.Connection) -> None:
    database.execute("""
        CREATE TABLE IF NOT EXISTS provider_admissions (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL REFERENCES roles(role),
            assignment_id TEXT NOT NULL REFERENCES assignments(id),
            generation INTEGER NOT NULL,
            resume_id TEXT,
            granted INTEGER NOT NULL CHECK(granted IN (0,1))
        )
    """)
    database.execute(
        "CREATE INDEX IF NOT EXISTS admissions_role ON provider_admissions(role,granted)"
    )
    database.execute(
        "CREATE INDEX IF NOT EXISTS admissions_assignment ON provider_admissions(assignment_id,granted)"
    )


def admission_status(database: sqlite3.Connection) -> dict[str, Any]:
    policy = retained_call_policy(database)
    enabled = bool(policy["limits"])
    paused = database.execute(
        "SELECT value FROM meta WHERE key='call_admission_paused'"
    ).fetchone()
    if (enabled and paused is None) or (
        paused is not None and paused[0] not in {"0", "1"}
    ):
        raise OrchestrationError("Provider-call pause metadata is invalid")
    count = 0
    if enabled:
        count = database.execute(
            "SELECT COUNT(*) FROM provider_admissions WHERE granted=1"
        ).fetchone()[0]
    return {
        **policy,
        "mode": "enforcing" if enabled else "disabled",
        "admitted_turns": count,
        "paused": paused is not None and paused[0] == "1",
        "cost_source": "unavailable",
        "cost_enforcement": "unavailable",
    }


def reserve_provider_turn(
    database: sqlite3.Connection,
    *,
    request_id: str,
    role: str,
    assignment_id: str,
    generation: int,
    resume_id: str | None,
) -> bool:
    """Caller holds BEGIN IMMEDIATE through commit-before-reply.

    Even a duplicate granted receipt is NOT a second dispatch authorization.
    The worker never retries an uncertain authorization; it aborts the turn.
    """
    policy = retained_call_policy(database)
    if not policy["limits"]:
        raise OrchestrationError("Provider-call enforcement is not enabled", "conflict")
    previous = database.execute(
        "SELECT * FROM provider_admissions WHERE id=?", (request_id,)
    ).fetchone()
    if previous is not None:
        if any(
            previous[key] != value
            for key, value in {
                "role": role,
                "assignment_id": assignment_id,
                "generation": generation,
                "resume_id": resume_id,
            }.items()
        ):
            raise OrchestrationError(
                "Provider authorization identity conflicts", "conflict"
            )
        return False
    # Bound retained metadata independently of any configured call threshold.
    if (
        database.execute("SELECT COUNT(*) FROM provider_admissions").fetchone()[0]
        >= MAX_CALL_ALLOWANCE
    ):
        database.execute("UPDATE meta SET value='1' WHERE key='call_admission_paused'")
        return False
    granted = not admission_status(database)["paused"]
    for scope, ceiling in policy["limits"].items():
        clause, arguments = {
            "run": ("", ()),
            "role": (" AND role=?", (role,)),
            "assignment": (" AND assignment_id=?", (assignment_id,)),
        }[scope]
        used = database.execute(
            "SELECT COUNT(*) FROM provider_admissions WHERE granted=1" + clause,
            arguments,
        ).fetchone()[0]
        granted = granted and used < ceiling
    database.execute(
        "INSERT INTO provider_admissions(id,role,assignment_id,generation,resume_id,granted) VALUES (?,?,?,?,?,?)",
        (request_id, role, assignment_id, generation, resume_id, int(granted)),
    )
    if not granted:
        database.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES ('call_admission_paused','1')"
        )
    return granted


def extend_call_allowance(database: sqlite3.Connection, additional: object) -> None:
    extra = call_limit(additional)
    status = admission_status(database)
    if not status["paused"] or not status["limits"]:
        raise OrchestrationError(
            "No enforced provider-call pause is available", "conflict"
        )
    limits = {
        scope: call_limit(value + extra) for scope, value in status["limits"].items()
    }
    database.execute(
        "UPDATE meta SET value=? WHERE key='call_admission_policy'",
        (
            json.dumps(
                call_policy({"version": 1, "limits": limits}), separators=(",", ":")
            ),
        ),
    )
    database.execute("UPDATE meta SET value='0' WHERE key='call_admission_paused'")
    # Deliberately leave the workflow waiting. Existing explicit operator-send
    # binds the next turn to its exact waiting assignment and resume identity.
