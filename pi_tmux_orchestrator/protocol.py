"""Strict validation for the local broker/worker coordination protocol."""

from __future__ import annotations

import json
import re
from typing import Any

from .constants import (
    BROKER_PROTOCOL_VERSION,
    KNOWN_ROLES,
    MAX_BROKER_FRAME_BYTES,
    MAX_REPORT_BYTES,
    MAX_REPORT_ITEM_CHARS,
    MAX_REPORT_ITEMS,
    MAX_REPORT_SUMMARY_CHARS,
    RPC_TOKEN_PATTERN,
)
from .models import OrchestrationError

REPORT_KINDS = frozenset({"implementation", "review", "probe", "playwright", "django"})
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


def _bounded_strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_REPORT_ITEMS:
        raise OrchestrationError(f"{label} must be a bounded array", "invalid_protocol")
    return [
        _bounded_string(item, f"{label} item", MAX_REPORT_ITEM_CHARS) for item in value
    ]


def validate_report(value: object, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OrchestrationError("Report must be an object", "invalid_protocol")
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
    if not set(value).issubset(allowed) or not {"kind", "summary"}.issubset(value):
        raise OrchestrationError(
            "Report has missing or unknown fields", "invalid_protocol"
        )
    kind = value["kind"]
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
    report: dict[str, Any] = {
        "kind": kind,
        "summary": _bounded_string(
            value["summary"], "Report summary", MAX_REPORT_SUMMARY_CHARS
        ),
        "changed_paths": [],
        "checks": [],
        "findings": [],
        "risks": [],
        "limitations": [],
        "verdict": None,
    }
    changed_paths = value.get("changed_paths", [])
    report["changed_paths"] = _bounded_strings(changed_paths, "changed_paths")
    for path in report["changed_paths"]:
        if (
            path.startswith("/")
            or path in {".", ".."}
            or ".." in path.split("/")
            or not PATH_PATTERN.fullmatch(path)
        ):
            raise OrchestrationError(
                "changed_paths must be bounded relative paths", "invalid_protocol"
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


def validate_client_message(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != BROKER_PROTOCOL_VERSION:
        raise OrchestrationError(
            "Unsupported broker protocol version", "invalid_protocol"
        )
    message_type = value.get("type")
    base = {"version", "type", "role", "token", "id"}
    if message_type == "hello":
        expected = base
    elif message_type == "report":
        expected = base | {"assignment_id", "report"}
    elif message_type == "lifecycle":
        expected = base | {"state", "usage"}
    elif message_type == "ack":
        expected = base | {"delivery_id", "status"}
    else:
        raise OrchestrationError("Unsupported broker message type", "invalid_protocol")
    if set(value) != expected:
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
