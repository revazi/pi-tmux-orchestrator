"""Strict operator-approved exact dynamic worker-model pools (no ranking)."""

from __future__ import annotations

from typing import Any

from .constants import KNOWN_ROLES
from .models import OrchestrationError

WORKER_CANDIDATES_VERSION = 1
MAX_POOL_IDENTITIES = 32
MAX_WORKER_CANDIDATES = 100


def validate_worker_candidates(value: object) -> dict[str, Any]:
    from .configuration import validate_model_fields

    if (
        not isinstance(value, dict)
        or set(value) - {"version", "all", "roles"}
        or type(value.get("version")) is not int
        or value["version"] != WORKER_CANDIDATES_VERSION
        or "all" not in value
    ):
        raise OrchestrationError("workerCandidates requires version 1 and an all pool")

    def pool(raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_POOL_IDENTITIES:
            raise OrchestrationError("workerCandidates pools require 1–32 identities")
        result = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"provider", "model"}:
                raise OrchestrationError(
                    "workerCandidates requires exact provider/model identities"
                )
            identity = validate_model_fields(item, "workerCandidates identity")
            if any("\x7f" in field for field in identity.values()):
                raise OrchestrationError(
                    "workerCandidates identity contains a control character"
                )
            result.append(identity)
        identities = {(item["provider"], item["model"]) for item in result}
        if len(identities) != len(result):
            raise OrchestrationError("workerCandidates pool has duplicate identities")
        return sorted(result, key=lambda item: (item["provider"], item["model"]))

    roles = value.get("roles", {})
    if not isinstance(roles, dict) or set(roles) - KNOWN_ROLES:
        raise OrchestrationError("workerCandidates roles must use built-in role names")
    result = {
        "version": WORKER_CANDIDATES_VERSION,
        "all": pool(value["all"]),
        "roles": {role: pool(raw) for role, raw in sorted(roles.items())},
    }
    distinct = {
        (item["provider"], item["model"])
        for items in [result["all"], *result["roles"].values()]
        for item in items
    }
    if len(distinct) > MAX_WORKER_CANDIDATES:
        raise OrchestrationError(
            "workerCandidates supports at most 100 distinct identities"
        )
    return result
