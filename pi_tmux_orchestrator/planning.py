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
from .task_intent import validate_intent_metadata

PLANNING_VERSION = 5
INTENT_PLANNING_VERSION = 4
SCOPED_PLANNING_VERSION = 3
POOL_PLANNING_VERSION = 2
LEGACY_PLANNING_VERSION = 1
PLANNING_DECISION_VERSION = 1
MAX_PLANNING_RECORD_BYTES = 256 * 1024
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
    if not isinstance(value, dict):
        raise OrchestrationError("Planning record must be an object")
    version = value.get("version")
    fields = PLANNING_FIELDS | (
        {"worker_candidates"}
        if version
        in {
            POOL_PLANNING_VERSION,
            SCOPED_PLANNING_VERSION,
            INTENT_PLANNING_VERSION,
            PLANNING_VERSION,
        }
        else set()
    )
    if version in {SCOPED_PLANNING_VERSION, INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        fields |= {"scopes", "locks"}
    if version in {INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        fields |= {"task_intent"}
    if version == PLANNING_VERSION:
        fields |= {"evidence"}
    if set(value) != fields:
        raise OrchestrationError("Planning record has missing or unknown fields")
    if (
        type(version) is not int
        or version
        not in {
            LEGACY_PLANNING_VERSION,
            POOL_PLANNING_VERSION,
            SCOPED_PLANNING_VERSION,
            INTENT_PLANNING_VERSION,
            PLANNING_VERSION,
        }
        or value.get("mode") != "dynamic"
    ):
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
    decision_metadata = {"version": PLANNING_DECISION_VERSION, "roles": roles}
    if version in {SCOPED_PLANNING_VERSION, INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        decision_metadata.update(validate_scope_metadata(value, roles))
    if version in {INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        decision_metadata["task_intent"] = validate_intent_metadata(
            value["task_intent"], launched=True
        )
    result = {
        "version": version,
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
    if version in {SCOPED_PLANNING_VERSION, INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        result.update(validate_scope_metadata(value, roles))
    if version in {INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        result["task_intent"] = decision_metadata["task_intent"]
    if version in {
        POOL_PLANNING_VERSION,
        SCOPED_PLANNING_VERSION,
        INTENT_PLANNING_VERSION,
        PLANNING_VERSION,
    }:
        result["worker_candidates"] = validate_candidate_metadata(
            value["worker_candidates"], identities
        )
    if version == PLANNING_VERSION:
        from .planner_evidence import validate_planner_evidence

        result["evidence"] = validate_planner_evidence(value["evidence"], result)
        from .planner_evidence import evidence_digest

        decision_metadata["evidence"] = evidence_digest(result["evidence"])
    if bindings["decision"] != metadata_digest(decision_metadata):
        raise OrchestrationError("Planning decision binding is invalid")
    return result


def validate_scope_metadata(
    value: dict[str, Any], roles: list[dict[str, str]]
) -> dict[str, Any]:
    scopes = value["scopes"]
    axes = ["topology", "models", "thinking"]
    if (
        not isinstance(scopes, list)
        or not scopes
        or scopes != [axis for axis in axes if axis in scopes]
    ):
        raise OrchestrationError("Planning scopes are invalid")
    locks = value["locks"]
    if not isinstance(locks, list) or not 2 <= len(locks) <= MAX_PLANNING_ROLES:
        raise OrchestrationError("Planning locks are invalid")
    seen = set()
    selected = {role["id"]: role for role in roles}
    for lock in locks:
        if not isinstance(lock, dict) or set(lock) != {
            "role",
            "inclusion",
            "provider",
            "model",
            "thinking",
        }:
            raise OrchestrationError("Planning lock fields are invalid")
        identity = lock["role"]
        if (
            not isinstance(identity, str)
            or identity in seen
            or (identity not in KNOWN_ROLES and not valid_custom_role_id(identity))
        ):
            raise OrchestrationError("Planning lock role is invalid")
        seen.add(identity)
        inclusion = lock["inclusion"]
        if inclusion is not None and type(inclusion) is not bool:
            raise OrchestrationError("Planning inclusion lock is invalid")
        if "topology" not in scopes and inclusion is None:
            raise OrchestrationError("Planning fixed topology requires locks")
        if (
            inclusion is True
            and identity not in selected
            or inclusion is False
            and identity in selected
        ):
            raise OrchestrationError("Planning roster conflicts with locks")
        if (lock["provider"] is None) != (lock["model"] is None):
            raise OrchestrationError("Planning model lock is incomplete")
        for field in ("provider", "model", "thinking"):
            item = lock[field]
            if item is not None:
                if field == "thinking":
                    if item not in THINKING_LEVELS:
                        raise OrchestrationError("Planning thinking lock is invalid")
                else:
                    _identifier(item, "lock identity")
                if identity in selected and selected[identity][field] != item:
                    raise OrchestrationError("Planning assignment conflicts with locks")
        if inclusion is not False and (
            ("models" not in scopes and lock["model"] is None)
            or ("thinking" not in scopes and lock["thinking"] is None)
        ):
            raise OrchestrationError("Planning fixed axes require exact locks")
    if not set(selected).issubset(seen):
        raise OrchestrationError("Planning selected roles lack lock metadata")
    return {"scopes": list(scopes), "locks": [dict(lock) for lock in locks]}


def planning_scopes_label(record: dict[str, Any]) -> str:
    """Display recorded scopes without inferring authority for legacy runs."""
    scopes = record.get("scopes")
    return ",".join(scopes) if scopes else "unavailable (legacy record)"


def planning_lock_lines(record: dict[str, Any]) -> list[str]:
    """Render validated body-free authority metadata for terminal/status reads."""
    locks = record.get("locks", [])
    if not locks:
        return ["Locks: unavailable (legacy record)."]
    lines = ["Authoritative operator/policy locks; planner=authorized unlocked axis:"]
    for lock in locks:
        if lock["inclusion"] is False:
            lines.append(
                f"  {lock['role']}: inclusion=locked omit; "
                "model=not applicable; thinking=not applicable"
            )
            continue
        inclusion = "locked include" if lock["inclusion"] is True else "planner"
        model = (
            f"locked {lock['provider']}/{lock['model']}"
            if lock["provider"] is not None
            else "planner"
        )
        thinking = (
            f"locked {lock['thinking']}" if lock["thinking"] is not None else "planner"
        )
        lines.append(
            f"  {lock['role']}: inclusion={inclusion}; model={model}; thinking={thinking}"
        )
    return lines


def validate_candidate_metadata(
    value: object, selected_roles: list[str]
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "source", "count", "roles"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["source"], str)
        or value["source"] not in {"configured", "authoritative-locks"}
        or type(value["count"]) is not int
        or not 1 <= value["count"] <= 100
        or not isinstance(value["roles"], list)
        or not 2 <= len(value["roles"]) <= MAX_PLANNING_ROLES
    ):
        raise OrchestrationError("Planning worker candidate metadata is invalid")
    roles = []
    for item in value["roles"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "source", "count"}
            or not isinstance(item["role"], str)
            or (
                item["role"] not in KNOWN_ROLES
                and not valid_custom_role_id(item["role"])
            )
            or not isinstance(item["source"], str)
            or item["source"]
            not in {"all-pool", "role-pool", "exact-lock", "custom-binding"}
            or type(item["count"]) is not int
            or not 1 <= item["count"] <= min(32, value["count"])
            or (
                item["source"] in {"exact-lock", "custom-binding"}
                and item["count"] != 1
            )
            or (
                value["source"] == "authoritative-locks"
                and item["source"] not in {"exact-lock", "custom-binding"}
            )
            or (
                valid_custom_role_id(item["role"])
                != (item["source"] == "custom-binding")
            )
        ):
            raise OrchestrationError(
                "Planning worker candidate role metadata is invalid"
            )
        roles.append(dict(item))
    identities = [item["role"] for item in roles]
    if len(set(identities)) != len(identities) or not set(selected_roles).issubset(
        identities
    ):
        raise OrchestrationError("Planning worker candidate roles are invalid")
    return {**value, "roles": roles}


def load_planning_record(
    path: str | None, *, allow_unbound: bool
) -> dict[str, Any] | None:
    if path is None:
        return None
    source = Path(path).expanduser()
    if not source.is_absolute():
        raise OrchestrationError("Planning record path must be absolute")
    from .storage import read_regular_file
    from .configuration import unique_json_object

    try:
        raw = read_regular_file(source, "planning record", MAX_PLANNING_RECORD_BYTES)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_json_object)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise OrchestrationError("Planning record is not valid UTF-8 JSON") from error
    record = validate_planning_record(value, allow_unbound=allow_unbound)
    if record["version"] not in {
        POOL_PLANNING_VERSION,
        SCOPED_PLANNING_VERSION,
        INTENT_PLANNING_VERSION,
        PLANNING_VERSION,
    }:
        raise OrchestrationError(
            "Legacy planning records are read-only; create a fresh approved-pool preview"
        )
    return record


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
    candidate_policy: dict[str, Any] | None = None,
    operator_intent: str | None = None,
) -> dict[str, Any]:
    from .planner_evidence import evidence_digest

    if record["version"] in {INTENT_PLANNING_VERSION, PLANNING_VERSION}:
        if record["task_intent"]["operator"] != operator_intent:
            raise OrchestrationError(
                "Operator intent changed after planning", "stale_planning_binding"
            )
    elif operator_intent is not None:
        raise OrchestrationError(
            "Legacy planning cannot bind explicit intent; create a fresh preview",
            "stale_planning_binding",
        )
    if record["version"] in {
        POOL_PLANNING_VERSION,
        SCOPED_PLANNING_VERSION,
        INTENT_PLANNING_VERSION,
        PLANNING_VERSION,
    }:
        metadata = record["worker_candidates"]
        if (metadata["source"] == "configured") != (candidate_policy is not None):
            raise OrchestrationError(
                "Approved worker pool changed after planning", "stale_planning_binding"
            )
        sources = {item["role"]: item["source"] for item in metadata["roles"]}
        for role in record["roles"]:
            source = sources[role["id"]]
            if source not in {"all-pool", "role-pool"}:
                continue
            pool = candidate_policy["roles"].get(role["id"], candidate_policy["all"])
            expected_source = (
                "role-pool" if role["id"] in candidate_policy["roles"] else "all-pool"
            )
            if (
                source != expected_source
                or {"provider": role["provider"], "model": role["model"]} not in pool
            ):
                raise OrchestrationError(
                    "Planning selected an unapproved worker model",
                    "stale_planning_binding",
                )
        if record["version"] == PLANNING_VERSION and candidate_policy is not None:
            locks = {lock["role"]: lock for lock in record["locks"]}
            for item in record["evidence"]["eligibility"]:
                role = item["role"]
                if sources[role] not in {"all-pool", "role-pool"}:
                    continue
                pool = candidate_policy["roles"].get(role, candidate_policy["all"])
                expected_source = (
                    "role-pool" if role in candidate_policy["roles"] else "all-pool"
                )
                if sources[role] != expected_source:
                    raise OrchestrationError(
                        "Planning evidence pool source changed",
                        "stale_planning_binding",
                    )
                if any(identity not in pool for identity in item["identities"]) or (
                    locks[role]["thinking"] is None
                    and len(item["identities"]) != len(pool)
                ):
                    raise OrchestrationError(
                        "Planning evidence contains an unapproved worker option",
                        "stale_planning_binding",
                    )
        start_config_digest = metadata_digest(
            {
                "resolved_start": start_config_digest,
                **(
                    {"scopes": record["scopes"], "locks": record["locks"]}
                    if record["version"]
                    in {
                        SCOPED_PLANNING_VERSION,
                        INTENT_PLANNING_VERSION,
                        PLANNING_VERSION,
                    }
                    else {}
                ),
                **(
                    {"task_intent": record["task_intent"]}
                    if record["version"] in {INTENT_PLANNING_VERSION, PLANNING_VERSION}
                    else {}
                ),
                **(
                    {"evidence": evidence_digest(record["evidence"])}
                    if record["version"] == PLANNING_VERSION
                    else {}
                ),
                "worker_candidates": metadata,
                "planner_policy": record["bindings"]["planner_policy"],
                "topology_policy": record["bindings"]["topology_policy"],
                "candidate_set": record["bindings"]["candidate_set"],
            }
        )
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


def retained_planning(
    manifest: dict[str, Any], *, summary: bool = False
) -> dict[str, Any]:
    if (
        manifest.get("version") not in {8, 9, 10, 11}
        or manifest.get("planning") is None
    ):
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
            "evidence": {"version": 1, "status": "unavailable"},
        }
    record = validate_planning_record(manifest["planning"])
    if summary and record.get("evidence"):
        evidence = record["evidence"]
        return {
            **record,
            "evidence": {
                "version": 1,
                "projection": "summary",
                "source": evidence["source"],
                "decision_binding": record["bindings"]["decision"],
                "decision_count": len(evidence["decisions"]),
                "provider_comparison": evidence["provider_comparison"],
            },
        }
    return {
        **record,
        "evidence": record.get("evidence", {"version": 1, "status": "unavailable"}),
    }
