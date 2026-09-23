"""Strict body-free dynamic-planning provenance and launch admission."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .constants import KNOWN_ROLES, THINKING_LEVELS
from .models import OrchestrationError
from .role_registry import valid_custom_role_id

PLANNING_VERSION = 1
PLANNING_DECISION_VERSION = 1
MAX_PLANNING_RECORD_BYTES = 32 * 1024
MAX_PLANNING_ROLES = len(KNOWN_ROLES) + 8
DIGEST_PATTERN = re.compile(r"[a-f0-9]{64}")
REQUEST_ID_PATTERN = re.compile(r"[a-f0-9]{32}")
PLANNING_SOURCES = frozenset(
    {
        "explicit",
        "configured-preferred",
        "configured-fallback",
        "typesafe-auth",
        "typesafe-environment",
    }
)
PLANNING_FIELDS = frozenset(
    {
        "version",
        "mode",
        "request_id",
        "status",
        "created_at_ms",
        "accepted_at_ms",
        "decision_schema_version",
        "decision_model",
        "roles",
        "bindings",
        "usage",
    }
)
DECISION_MODEL_FIELDS = frozenset({"provider", "model", "thinking", "source"})
PLANNING_ROLE_FIELDS = frozenset({"id", "contract", "provider", "model", "thinking"})
PLANNING_BINDING_FIELDS = frozenset(
    {
        "input",
        "start_config",
        "planner_policy",
        "topology_policy",
        "candidate_set",
        "decision",
    }
)
PLANNING_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "cost_total",
    }
)
BUILTIN_CONTRACTS = {
    "implementer": "implementer",
    "reviewer": "reviewer",
    "probe": "probe",
    "playwright": "playwright",
    "django": "django",
}


def metadata_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise OrchestrationError(f"Planning {label} is invalid")
    return value


def _digest(value: object, label: str, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise OrchestrationError(f"Planning {label} digest is invalid")
    return value


def _usage_number(value: object, label: str) -> int | float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise OrchestrationError(f"Planning usage {label} is invalid")
    return value


def _planning_usage(value: object) -> dict[str, int | float | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != PLANNING_USAGE_FIELDS:
        raise OrchestrationError("Planning usage metadata is invalid")
    return {
        field: _usage_number(value[field], field) for field in PLANNING_USAGE_FIELDS
    }


def _planning_role(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != PLANNING_ROLE_FIELDS:
        raise OrchestrationError("Planning role metadata is invalid")
    role = value.get("id")
    if not isinstance(role, str) or (
        role not in KNOWN_ROLES and not valid_custom_role_id(role)
    ):
        raise OrchestrationError("Planning role identity is invalid")
    contract = value.get("contract")
    if role in KNOWN_ROLES:
        if contract != BUILTIN_CONTRACTS[role]:
            raise OrchestrationError("Planning built-in role contract is invalid")
    elif contract not in {"probe", "playwright", "django"}:
        raise OrchestrationError("Planning custom role contract is invalid")
    thinking = value.get("thinking")
    if thinking not in THINKING_LEVELS:
        raise OrchestrationError("Planning role thinking is invalid")
    return {
        "id": role,
        "contract": contract,
        "provider": _identifier(value.get("provider"), "role provider"),
        "model": _identifier(value.get("model"), "role model"),
        "thinking": thinking,
    }


def validate_planning_record(
    value: object, *, allow_unbound: bool = False
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != PLANNING_FIELDS:
        raise OrchestrationError("Planning record has missing or unknown fields")
    if value.get("version") != PLANNING_VERSION or value.get("mode") != "dynamic":
        raise OrchestrationError("Planning record version or mode is invalid")
    if value.get("status") != "accepted":
        raise OrchestrationError("Planning record status is invalid")
    if value.get("decision_schema_version") != PLANNING_DECISION_VERSION:
        raise OrchestrationError("Planning decision schema version is invalid")
    created_at_ms = value.get("created_at_ms")
    accepted_at_ms = value.get("accepted_at_ms")
    if (
        type(created_at_ms) is not int
        or type(accepted_at_ms) is not int
        or not 0 < created_at_ms <= accepted_at_ms < 2**53
    ):
        raise OrchestrationError("Planning timestamps are invalid")
    request_id = value.get("request_id")
    if (
        not isinstance(request_id, str)
        or REQUEST_ID_PATTERN.fullmatch(request_id) is None
    ):
        raise OrchestrationError("Planning request ID is invalid")

    model = value.get("decision_model")
    if not isinstance(model, dict) or set(model) != DECISION_MODEL_FIELDS:
        raise OrchestrationError("Planning decision model metadata is invalid")
    thinking = model.get("thinking")
    source = model.get("source")
    if thinking not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise OrchestrationError("Planning decision model thinking is invalid")
    if source not in PLANNING_SOURCES:
        raise OrchestrationError("Planning decision model source is invalid")
    decision_model = {
        "provider": _identifier(model.get("provider"), "decision provider"),
        "model": _identifier(model.get("model"), "decision model"),
        "thinking": thinking,
        "source": source,
    }

    raw_roles = value.get("roles")
    if not isinstance(raw_roles, list) or not 2 <= len(raw_roles) <= MAX_PLANNING_ROLES:
        raise OrchestrationError("Planning role count is invalid")
    roles = [_planning_role(role) for role in raw_roles]
    identities = [role["id"] for role in roles]
    if len(set(identities)) != len(identities):
        raise OrchestrationError("Planning roles must be unique")
    if identities.count("implementer") != 1 or identities.count("reviewer") != 1:
        raise OrchestrationError("Planning must retain implementer and reviewer")

    raw_bindings = value.get("bindings")
    if (
        not isinstance(raw_bindings, dict)
        or set(raw_bindings) != PLANNING_BINDING_FIELDS
    ):
        raise OrchestrationError("Planning bindings are invalid")
    bindings = {
        field: _digest(
            raw_bindings[field],
            field,
            nullable=allow_unbound and field in {"input", "start_config"},
        )
        for field in PLANNING_BINDING_FIELDS
    }
    expected_decision = metadata_digest(
        {"version": PLANNING_DECISION_VERSION, "roles": roles}
    )
    if bindings["decision"] != expected_decision:
        raise OrchestrationError("Planning decision binding is invalid")
    return {
        "version": PLANNING_VERSION,
        "mode": "dynamic",
        "request_id": request_id,
        "status": "accepted",
        "created_at_ms": created_at_ms,
        "accepted_at_ms": accepted_at_ms,
        "decision_schema_version": PLANNING_DECISION_VERSION,
        "decision_model": decision_model,
        "roles": roles,
        "bindings": bindings,
        "usage": _planning_usage(value.get("usage")),
    }


def load_planning_record(
    path: str | None, *, allow_unbound: bool
) -> dict[str, Any] | None:
    if path is None:
        return None
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise OrchestrationError("Planning record path must be absolute")
    from .storage import read_regular_file

    try:
        raw = read_regular_file(source, "planning record", MAX_PLANNING_RECORD_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise OrchestrationError("Planning record is not valid UTF-8 JSON") from error
    return validate_planning_record(value, allow_unbound=allow_unbound)


def planning_input_digest(
    task: str,
    context_capsule: str,
    role_tasks: dict[str, str],
) -> str:
    return metadata_digest(
        {
            "task": task,
            "context_capsule": context_capsule,
            "role_tasks": {role: role_tasks[role] for role in sorted(role_tasks)},
        }
    )


def planning_start_config_digest(
    *,
    project: Path,
    configured_models: dict[str, Any],
    project_config: dict[str, Any] | None,
    execution_profile: dict[str, Any],
    roles: list[str],
    configs: dict[str, dict[str, Any]],
) -> str:
    role_bindings: dict[str, Any] = {}
    for role in roles:
        config = configs[role]
        binding: dict[str, Any] = {
            "provider": config["provider"],
            "model": config["model"],
            "thinking": config["thinking"],
        }
        if valid_custom_role_id(role):
            binding["custom_role"] = config["custom_role"]
            binding["custom_policy"] = config["custom_policy"]
        role_bindings[role] = binding
    return metadata_digest(
        {
            "project": str(project),
            "model_config": configured_models,
            "project_config": project_config,
            "execution_profile": execution_profile,
            "roles": role_bindings,
        }
    )


def bind_planning_record(
    record: dict[str, Any],
    *,
    input_digest: str,
    start_config_digest: str,
    roles: list[str],
    configs: dict[str, dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    expected_roles = []
    for role in roles:
        config = configs[role]
        expected_roles.append(
            {
                "id": role,
                "contract": (
                    config["custom_role"]["contract"]
                    if valid_custom_role_id(role)
                    else BUILTIN_CONTRACTS[role]
                ),
                "provider": config["provider"],
                "model": config["model"],
                "thinking": config["thinking"],
            }
        )
    if record["roles"] != expected_roles:
        raise OrchestrationError(
            "Accepted planning topology does not match resolved start policy",
            "stale_planning_binding",
        )
    bound = {
        **record,
        "bindings": {
            **record["bindings"],
            "input": input_digest,
            "start_config": start_config_digest,
        },
    }
    if not dry_run and (
        record["bindings"]["input"] != input_digest
        or record["bindings"]["start_config"] != start_config_digest
    ):
        raise OrchestrationError(
            "Accepted planning inputs changed after preview",
            "stale_planning_binding",
        )
    return validate_planning_record(bound)


def retained_planning(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("version") not in {8, 9}:
        return {
            "mode": "static",
            "status": "unavailable",
            "created_at_ms": None,
            "accepted_at_ms": None,
            "decision_schema_version": None,
            "decision_model": None,
            "roles": [],
            "bindings": None,
            "usage": None,
        }
    return validate_planning_record(manifest["planning"])
