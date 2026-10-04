"""Strict attention validation and body-free assignment metadata projection."""

from __future__ import annotations

from typing import Any

from .models import OrchestrationError

ATTENTION_REASONS = frozenset(
    {"clarification", "blocked", "tool_failure", "report_failure"}
)
ATTENTION_TEXT_LIMIT = 500


def validate_attention(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not {"reason"}.issubset(value)
        or not set(value).issubset({"reason", "summary", "question"})
        or not isinstance(value["reason"], str)
        or value["reason"] not in ATTENTION_REASONS
    ):
        raise OrchestrationError("Worker attention is invalid", "invalid_protocol")
    for key in ("summary", "question"):
        if key not in value:
            continue
        text = value[key]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > ATTENTION_TEXT_LIMIT
            or any(
                ord(char) < 32
                or 127 <= ord(char) <= 159
                or 0xD800 <= ord(char) <= 0xDFFF
                for char in text
            )
        ):
            raise OrchestrationError("Worker attention is invalid", "invalid_protocol")
    return dict(value)


def record_report_rejection(
    database: Any, assignment_id: object, role: str, generation: int
) -> None:
    # The authenticated connection supplies identity; SQL fences stale assignment
    # and generation without retaining any malformed envelope or error content.
    database.execute(
        "UPDATE assignments SET report_attempt='rejected' WHERE id=? AND role=? "
        "AND report_attempt='none' AND state IN ('delivering','accepted') "
        "AND id=(SELECT active_assignment_id FROM roles WHERE role=? AND generation=?)",
        (
            assignment_id if isinstance(assignment_id, str) else None,
            role,
            role,
            generation,
        ),
    )


def assignment_attention_metadata(database: Any, role: str) -> dict[str, Any] | None:
    row = database.execute(
        "SELECT a.id AS assignment_id,a.kind AS assignment_kind,a.settlement_count,"
        "a.report_attempt,a.attention_reason,a.reminder_state,a.last_phase AS activity_phase "
        "FROM roles r JOIN assignments a ON a.id=COALESCE(r.active_assignment_id, "
        "(SELECT id FROM assignments WHERE role=r.role ORDER BY created_at DESC LIMIT 1)) WHERE r.role=?",
        (role,),
    ).fetchone()
    return dict(row) if row is not None else None
