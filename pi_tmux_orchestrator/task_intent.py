"""Bounded Stage 1 intent metadata; non-change work is not a coding workflow."""

from __future__ import annotations

from typing import Any

from .models import CommandResult, OrchestrationError
from .output import human_print

TASK_INTENTS = ("change", "investigation", "review", "advisory")
NON_CHANGE_NOTICE = (
    "Coding orchestration may be unnecessary for non-change work. The parent can "
    "answer directly; no implementation or change report will be created. No tmux "
    "session, broker, workers, or manifest will be started. To request repository "
    "changes, submit a fresh explicit change task."
)


def validate_task_intent(value: object) -> str:
    if not isinstance(value, str) or value not in TASK_INTENTS:
        raise OrchestrationError("Task intent is invalid", "invalid_task_intent")
    return value


def task_intent_metadata(
    operator: str | None, recommendation: str | None = None
) -> dict[str, Any]:
    if operator is not None:
        validate_task_intent(operator)
    if recommendation is not None:
        validate_task_intent(recommendation)
    return {
        "version": 1,
        "operator": operator,
        "recommendation": recommendation,
        "effective": operator or recommendation or "change",
        "source": (
            "operator"
            if operator is not None
            else "planner"
            if recommendation is not None
            else "default"
        ),
    }


def validate_intent_metadata(
    value: object, *, launched: bool = False
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {"version", "operator", "recommendation", "effective", "source"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise OrchestrationError("Task intent metadata is invalid")
    expected = task_intent_metadata(value["operator"], value["recommendation"])
    if value != expected or (launched and value["effective"] != "change"):
        raise OrchestrationError("Task intent admission metadata is inconsistent")
    return expected


def redirect_task_intent(operator: str, *, dry_run: bool) -> CommandResult:
    intent = task_intent_metadata(operator)
    human_print(f"Task intent: {intent['effective']} (source=operator).")
    human_print(NON_CHANGE_NOTICE)
    return CommandResult(
        data={
            "task_intent": intent,
            "disposition": "direct-parent",
            "launched": False,
            "dry_run": dry_run,
            "message": NON_CHANGE_NOTICE,
        }
    )


def retained_task_intent(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Legacy runs do not provide intent evidence; never infer it from reports."""
    if manifest.get("version") not in {10, 11}:
        return None
    return validate_intent_metadata(manifest["task_intent"], launched=True)
