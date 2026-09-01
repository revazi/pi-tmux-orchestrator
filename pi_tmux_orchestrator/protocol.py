"""Strict validation for the local broker/worker coordination protocol."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .constants import (
    BROKER_PROTOCOL_VERSION,
    KNOWN_ROLES,
    MAX_BROKER_FRAME_BYTES,
    MAX_PLAN_REPORT_ITEM_CHARS,
    MAX_PLAN_REPORT_ITEMS,
    MAX_PLAN_REPORT_SUMMARY_CHARS,
    MAX_REPORT_BYTES,
    MAX_REPORT_ITEM_CHARS,
    MAX_REPORT_ITEMS,
    MAX_REPORT_SUMMARY_CHARS,
    RPC_TOKEN_PATTERN,
    WORKER_ACTIVITY_PHASES,
)
from .models import OrchestrationError

REPORT_KINDS = frozenset(
    {"plan", "implementation", "review", "probe", "playwright", "django"}
)
VERDICTS = frozenset(
    {
        "approved",
        "changes_requested",
        "pass",
        "fail",
        "advisory_approved",
        "issues_found",
    }
)
CHECK_STATUSES = frozenset({"passed", "failed", "skipped", "unknown"})
SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})
PATH_PATTERN = re.compile(r"[^\x00-\x1f\x7f]{1,500}")
ASSIGNMENT_GUARDRAIL_LEVELS = frozenset({"warning", "hard"})
ASSIGNMENT_GUARDRAIL_METRICS = frozenset(
    {"provider_calls", "context_tokens", "context_percent"}
)
ASSIGNMENT_GUARDRAIL_INTEGER_METRICS = frozenset({"provider_calls", "context_tokens"})


def _bounded_string(
    value: object, label: str, maximum: int, *, empty: bool = False
) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise OrchestrationError(
            f"{label} must be a non-empty string", "invalid_protocol"
        )
    if len(value) > maximum or "\x00" in value:
        raise OrchestrationError(
            f"{label} exceeds the protocol limit", "invalid_protocol"
        )
    return value


def _bounded_strings(
    value: object,
    label: str,
    *,
    maximum_items: int = MAX_REPORT_ITEMS,
    maximum_chars: int = MAX_REPORT_ITEM_CHARS,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise OrchestrationError(f"{label} must be a bounded array", "invalid_protocol")
    return [_bounded_string(item, f"{label} item", maximum_chars) for item in value]


def _relative_paths(
    value: object,
    label: str,
    *,
    maximum_items: int,
    maximum_chars: int = MAX_REPORT_ITEM_CHARS,
) -> list[str]:
    paths = _bounded_strings(
        value,
        label,
        maximum_items=maximum_items,
        maximum_chars=maximum_chars,
    )
    for path in paths:
        if (
            path.startswith("/")
            or path in {".", ".."}
            or ".." in path.split("/")
            or not PATH_PATTERN.fullmatch(path)
        ):
            raise OrchestrationError(
                f"{label} must contain bounded relative paths", "invalid_protocol"
            )
    return paths


def _empty_common_report(kind: str, summary: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "summary": summary,
        "changed_paths": [],
        "checks": [],
        "findings": [],
        "risks": [],
        "limitations": [],
        "verdict": None,
    }


def _validate_plan_report(value: dict[str, Any], role: str) -> dict[str, Any]:
    fields = {
        "kind",
        "summary",
        "relevant_paths",
        "relevant_symbols",
        "intended_changes",
        "required_checks",
        "risks",
        "open_questions",
    }
    # The worker bridge adds the empty common report envelope before wire delivery.
    normalized_fields = fields | {
        "changed_paths",
        "checks",
        "findings",
        "limitations",
        "verdict",
    }
    if role != "implementer":
        raise OrchestrationError(
            "Plan reports are permitted only for the implementer", "forbidden"
        )
    if set(value) not in {frozenset(fields), frozenset(normalized_fields)}:
        raise OrchestrationError(
            "Plan report has missing or unknown fields", "invalid_protocol"
        )
    if set(value) == normalized_fields and (
        value["changed_paths"] != []
        or value["checks"] != []
        or value["findings"] != []
        or value["limitations"] != []
        or value["verdict"] is not None
    ):
        raise OrchestrationError(
            "Plan report cannot contain implementation or review claims",
            "invalid_protocol",
        )
    report = _empty_common_report(
        "plan",
        _bounded_string(
            value["summary"],
            "Plan summary",
            MAX_PLAN_REPORT_SUMMARY_CHARS,
        ),
    )
    report.update(
        {
            "relevant_paths": _relative_paths(
                value["relevant_paths"],
                "relevant_paths",
                maximum_items=MAX_PLAN_REPORT_ITEMS,
                maximum_chars=MAX_PLAN_REPORT_ITEM_CHARS,
            ),
            "relevant_symbols": _bounded_strings(
                value["relevant_symbols"],
                "relevant_symbols",
                maximum_items=MAX_PLAN_REPORT_ITEMS,
                maximum_chars=MAX_PLAN_REPORT_ITEM_CHARS,
            ),
            "intended_changes": _bounded_strings(
                value["intended_changes"],
                "intended_changes",
                maximum_items=MAX_PLAN_REPORT_ITEMS,
                maximum_chars=MAX_PLAN_REPORT_ITEM_CHARS,
            ),
            "required_checks": _bounded_strings(
                value["required_checks"],
                "required_checks",
                maximum_items=MAX_PLAN_REPORT_ITEMS,
                maximum_chars=MAX_PLAN_REPORT_ITEM_CHARS,
            ),
            "risks": _bounded_strings(
                value["risks"],
                "risks",
                maximum_items=MAX_PLAN_REPORT_ITEMS,
                maximum_chars=MAX_PLAN_REPORT_ITEM_CHARS,
            ),
            "open_questions": _bounded_strings(
                value["open_questions"],
                "open_questions",
                maximum_items=MAX_PLAN_REPORT_ITEMS,
                maximum_chars=MAX_PLAN_REPORT_ITEM_CHARS,
            ),
        }
    )
    return report


def validate_report(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationError("Report must be an object", "invalid_protocol")
    if not {"kind", "summary"}.issubset(value):
        raise OrchestrationError(
            "Report has missing or unknown fields", "invalid_protocol"
        )
    kind = value["kind"]
    if not isinstance(kind, str):
        raise OrchestrationError("Report kind is invalid", "invalid_protocol")
    if kind == "plan":
        report = _validate_plan_report(value, role)
        encoded = json.dumps(report, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > MAX_REPORT_BYTES:
            raise OrchestrationError(
                "Report exceeds the protocol limit", "invalid_protocol"
            )
        return report
    allowed = {
        "kind",
        "summary",
        "changed_paths",
        "checks",
        "findings",
        "risks",
        "limitations",
        "verdict",
    }
    if not set(value).issubset(allowed):
        raise OrchestrationError(
            "Report has missing or unknown fields", "invalid_protocol"
        )
    expected_kind = {
        "implementer": "implementation",
        "reviewer": "review",
        "probe": "probe",
        "playwright": "playwright",
        "django": "django",
    }.get(role)
    if kind not in REPORT_KINDS or kind != expected_kind:
        raise OrchestrationError(
            "Report kind is not permitted for this role", "forbidden"
        )
    report = _empty_common_report(
        kind,
        _bounded_string(value["summary"], "Report summary", MAX_REPORT_SUMMARY_CHARS),
    )
    changed_paths = value.get("changed_paths", [])
    report["changed_paths"] = _relative_paths(
        changed_paths,
        "changed_paths",
        maximum_items=MAX_REPORT_ITEMS,
    )
    if role != "implementer" and report["changed_paths"]:
        raise OrchestrationError(
            "Read-only roles cannot report changed paths", "forbidden"
        )
    for check in value.get("checks", []):
        if not isinstance(check, dict) or set(check) != {"name", "status"}:
            raise OrchestrationError("Check entries are invalid", "invalid_protocol")
        status = check["status"]
        if status not in CHECK_STATUSES:
            raise OrchestrationError("Check status is invalid", "invalid_protocol")
        report["checks"].append(
            {
                "name": _bounded_string(
                    check["name"], "Check name", MAX_REPORT_ITEM_CHARS
                ),
                "status": status,
            }
        )
    if len(report["checks"]) > MAX_REPORT_ITEMS:
        raise OrchestrationError("Too many checks", "invalid_protocol")
    for finding in value.get("findings", []):
        if not isinstance(finding, dict):
            raise OrchestrationError("Finding entries are invalid", "invalid_protocol")
        allowed_finding = {"severity", "path", "line", "summary", "acceptance"}
        if not set(finding).issubset(allowed_finding) or not {
            "severity",
            "summary",
        }.issubset(finding):
            raise OrchestrationError("Finding fields are invalid", "invalid_protocol")
        severity = finding["severity"]
        if severity not in SEVERITIES:
            raise OrchestrationError("Finding severity is invalid", "invalid_protocol")
        path_value = finding.get("path")
        if path_value is not None:
            path_value = _bounded_string(
                path_value, "Finding path", MAX_REPORT_ITEM_CHARS
            )
        line = finding.get("line")
        if line is not None and (type(line) is not int or line <= 0):
            raise OrchestrationError("Finding line is invalid", "invalid_protocol")
        acceptance = finding.get("acceptance")
        if acceptance is not None:
            acceptance = _bounded_string(
                acceptance, "Finding acceptance", MAX_REPORT_ITEM_CHARS
            )
        report["findings"].append(
            {
                "severity": severity,
                "path": path_value,
                "line": line,
                "summary": _bounded_string(
                    finding["summary"], "Finding summary", MAX_REPORT_ITEM_CHARS
                ),
                "acceptance": acceptance,
            }
        )
    if len(report["findings"]) > MAX_REPORT_ITEMS:
        raise OrchestrationError("Too many findings", "invalid_protocol")
    report["risks"] = _bounded_strings(value.get("risks", []), "risks")
    report["limitations"] = _bounded_strings(
        value.get("limitations", []), "limitations"
    )
    verdict = value.get("verdict")
    if role in {"reviewer", "playwright", "django"}:
        expected_verdicts = {
            "reviewer": {"approved", "changes_requested"},
            "playwright": {"pass", "fail"},
            "django": {"advisory_approved", "issues_found"},
        }[role]
        if verdict not in expected_verdicts:
            raise OrchestrationError(
                "Report verdict is invalid for this role", "invalid_protocol"
            )
        report["verdict"] = verdict
    elif verdict is not None:
        raise OrchestrationError("This role cannot submit a verdict", "forbidden")
    encoded = json.dumps(report, separators=(",", ":"), ensure_ascii=False).encode()
    if len(encoded) > MAX_REPORT_BYTES:
        raise OrchestrationError(
            "Report exceeds the protocol limit", "invalid_protocol"
        )
    return report


def _valid_guardrail_number(value: object, *, integer: bool) -> bool:
    if integer:
        return type(value) is int and value >= 0
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _validate_guardrail_message(value: dict[str, Any]) -> None:
    assignment_id = value.get("assignment_id")
    level = value.get("level")
    metric = value.get("metric")
    observed = value.get("observed")
    threshold = value.get("threshold")
    if not isinstance(assignment_id, str) or not RPC_TOKEN_PATTERN.fullmatch(
        assignment_id
    ):
        raise OrchestrationError(
            "Guardrail assignment ID is invalid", "invalid_protocol"
        )
    if level not in ASSIGNMENT_GUARDRAIL_LEVELS:
        raise OrchestrationError("Guardrail level is invalid", "invalid_protocol")
    if metric not in ASSIGNMENT_GUARDRAIL_METRICS:
        raise OrchestrationError("Guardrail metric is invalid", "invalid_protocol")
    integer = metric in ASSIGNMENT_GUARDRAIL_INTEGER_METRICS
    if (
        not _valid_guardrail_number(observed, integer=integer)
        or not _valid_guardrail_number(threshold, integer=integer)
        or threshold <= 0
        or observed < threshold
    ):
        raise OrchestrationError("Guardrail values are invalid", "invalid_protocol")


def validate_client_message(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != BROKER_PROTOCOL_VERSION:
        raise OrchestrationError(
            "Unsupported broker protocol version", "invalid_protocol"
        )
    message_type = value.get("type")
    base = {"version", "type", "role", "token", "id"}
    if message_type == "hello":
        expected = (base | {"generation"},)
    elif message_type == "report":
        legacy = base | {"assignment_id", "report"}
        expected = (legacy, legacy | {"usage"})
    elif message_type == "lifecycle":
        expected = (base | {"state", "usage"},)
    elif message_type == "progress":
        expected = (base | {"assignment_id", "phase", "usage"},)
    elif message_type == "guardrail":
        expected = (
            base | {"assignment_id", "level", "metric", "observed", "threshold"},
        )
    elif message_type == "ack":
        expected = (base | {"delivery_id", "status"},)
    else:
        raise OrchestrationError("Unsupported broker message type", "invalid_protocol")
    if set(value) not in expected:
        raise OrchestrationError(
            "Broker message has missing or unknown fields", "invalid_protocol"
        )
    role = value.get("role")
    if role not in KNOWN_ROLES:
        raise OrchestrationError("Broker role is invalid", "invalid_protocol")
    token = value.get("token")
    message_id = value.get("id")
    if not isinstance(token, str) or not RPC_TOKEN_PATTERN.fullmatch(token):
        raise OrchestrationError("Broker token is invalid", "invalid_protocol")
    if not isinstance(message_id, str) or not RPC_TOKEN_PATTERN.fullmatch(message_id):
        raise OrchestrationError("Broker message ID is invalid", "invalid_protocol")
    if message_type == "hello" and (
        type(value.get("generation")) is not int or value["generation"] < 1
    ):
        raise OrchestrationError(
            "Broker worker generation is invalid", "invalid_protocol"
        )
    if message_type == "guardrail":
        _validate_guardrail_message(value)
    if message_type == "progress":
        assignment_id = value.get("assignment_id")
        if not isinstance(assignment_id, str) or not RPC_TOKEN_PATTERN.fullmatch(
            assignment_id
        ):
            raise OrchestrationError(
                "Progress assignment ID is invalid", "invalid_protocol"
            )
        if value.get("phase") not in WORKER_ACTIVITY_PHASES:
            raise OrchestrationError(
                "Worker progress phase is invalid", "invalid_protocol"
            )
    return value


def encode_frame(value: dict[str, Any]) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
    if len(payload) > MAX_BROKER_FRAME_BYTES:
        raise OrchestrationError(
            "Broker frame exceeds the safety limit", "invalid_protocol"
        )
    return len(payload).to_bytes(4, "big") + payload


def decode_frame(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > MAX_BROKER_FRAME_BYTES:
        raise OrchestrationError("Broker frame size is invalid", "invalid_protocol")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OrchestrationError(
            "Broker frame is not valid JSON", "invalid_protocol"
        ) from error
    if not isinstance(value, dict):
        raise OrchestrationError("Broker frame must be an object", "invalid_protocol")
    return value
