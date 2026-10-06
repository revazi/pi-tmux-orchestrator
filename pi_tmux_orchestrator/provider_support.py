"""Bounded model-free support admission; catalog hints are never billing."""

from .models import OrchestrationError
from .planner_evidence import LEVELS, evidence_digest

SUPPORT_CHOICES = [
    "none",
    "reasoning",
    "image",
    "context_window",
    "max_output_tokens",
    "thinking",
    "cache_short",
    "cache_long",
    "declared_cost",
]
SUPPORT_ERROR = (
    "Unsupported single-provider plan: choose revised constraints, exact overrides, "
    "explicit static/manual planning, or cancel; no retry or substitute plan."
)


def _advantage(reference, selected, other, thinking, levels):
    left, right = selected["capabilities"], other["capabilities"]
    if reference == "reasoning":
        return left["reasoning"] is True and right["reasoning"] is False
    if reference == "image":
        return left["input"]["image"] is True and right["input"]["image"] is False
    if reference in {"context_window", "max_output_tokens"}:
        a, b = left[reference], right[reference]
        return (
            a["status"] == b["status"] == "declared"
            and a["tokens"] > 0
            and b["tokens"] > 0
            and a["tokens"] >= b["tokens"] * 1.25
        )
    if reference == "thinking":
        return all(LEVELS.index(thinking) > LEVELS.index(level) for level in levels)
    if reference.startswith("cache_"):
        field = reference[6:]
        a, b = left["prompt_cache_retention"], right["prompt_cache_retention"]
        return (
            a["status"] == "declared"
            and a[field] is True
            and b["status"] in {"declared", "missing"}
            and b[field] is False
        )
    if reference == "declared_cost":
        a, b = left["declared_cost"], right["declared_cost"]
        return (
            a["status"] == b["status"] == "declared"
            and not a["tiers"]
            and not b["tiers"]
            and all(
                a[field] > 0 and b[field] > 0 and a[field] <= b[field] * 0.8
                for field in ("input", "output")
            )
            and all(a[field] <= b[field] for field in ("cache_read", "cache_write"))
        )
    return False


def support_identity(reference, facts, catalog):
    return {
        "support": reference,
        "applicability": "unavailable"
        if reference == "none"
        else "material_task_advantage_all_selected_roles",
        "facts": evidence_digest({"catalog": catalog, "composition": facts}),
    }


def derive_provider_support(reference, composition, facts, catalog, roles):
    def fail():
        raise OrchestrationError(SUPPORT_ERROR, "unsupported_provider_support")

    if reference not in SUPPORT_CHOICES:
        fail()
    identities = {role.get("role", role.get("id")) for role in roles}
    selected_roles = [role for role in facts["roles"] if role["role"] in identities]
    providers = [
        {item["provider"] for item in role["candidates"]} for role in selected_roles
    ]
    single = bool(set.intersection(*providers))
    mixed = len(set.union(*providers)) > 1
    feasible = (["single_provider"] if single else []) + (
        ["mixed_provider"] if mixed else []
    )
    actual = (
        "single_provider"
        if len({role["provider"] for role in roles}) == 1
        else "mixed_provider"
    )
    if actual not in feasible or composition not in {actual, "no_material_preference"}:
        fail()
    result = {
        "source": "derived",
        "state": "locked_fixed"
        if len(feasible) == 1
        else "mixed"
        if actual == "mixed_provider"
        else "supported",
        "feasible": feasible,
        "reference": reference,
        "roles": [],
    }
    if result["state"] != "supported":
        if reference != "none":
            fail()
        return result
    if composition != "single_provider" or reference == "none":
        fail()
    lookup = {(item["provider"], item["model"]): item for item in catalog}
    for assignment in roles:
        role = assignment.get("role", assignment.get("id"))
        eligible = next(
            item["candidates"] for item in selected_roles if item["role"] == role
        )
        selected = lookup.get((assignment["provider"], assignment["model"]))
        alternatives = [
            item for item in eligible if item["provider"] != assignment["provider"]
        ]
        if (
            not selected
            or not alternatives
            or not all(
                _advantage(
                    reference,
                    selected,
                    lookup[(item["provider"], item["model"])],
                    assignment["thinking"],
                    item["thinking_levels"],
                )
                for item in alternatives
            )
        ):
            fail()
        result["roles"].append(
            {
                "role": role,
                "selected": {
                    "provider": selected["provider"],
                    "model": selected["model"],
                    "thinking": assignment["thinking"],
                    "facts": evidence_digest(selected),
                },
                "alternatives": alternatives,
            }
        )
    return result


def provider_support_summary(evidence):
    support = evidence.get("provider_support")
    if not support:
        return {"state": "unavailable"}
    return {
        **{
            field: support[field]
            for field in ("source", "state", "reference", "feasible")
        },
        "role_count": len(support["roles"]),
    }


def provider_support_line(evidence):
    item = (
        evidence.get("provider_support", {})
        if evidence.get("projection") == "summary"
        else provider_support_summary(evidence)
    )
    reference = f" reference={item['reference']}" if item.get("reference") else ""
    return (
        f"Single-provider support: {item.get('state', 'unavailable')}; "
        f"{item.get('source', 'unavailable')}{reference} (not planner confidence or reasoning)."
    )
