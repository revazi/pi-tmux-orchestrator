"""Strict accepted-plan evidence; supplied facts, never private bodies or reasoning."""

from __future__ import annotations

import json
import math
import struct
from typing import Any

from .models import OrchestrationError

MAX_EVIDENCE_BYTES = 224 * 1024
TOP_ALTERNATIVES = 3
PROBABILITY_TOLERANCE = 1e-6
LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
STATUSES = {"missing", "unavailable", "zero", "declared"}


def _fail() -> None:
    raise OrchestrationError("Planning evidence metadata is invalid")


def _fields(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        _fail()
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    from .planning import metadata_digest

    return metadata_digest(value)


def evidence_digest(value: object) -> str:
    """Match the JS domain-separated binary64 canonical metadata encoding."""

    def encode(item):
        if type(item) in {int, float}:
            return {"$number": struct.pack(">d", float(item) if item else 0.0).hex()}
        if isinstance(item, list):
            return [encode(child) for child in item]
        if isinstance(item, dict):
            return {field: encode(child) for field, child in item.items()}
        return item

    return _digest({"encoding": "binary64-v1", "value": encode(value)})


def _key(value: dict) -> str:
    return value["provider"] + "\0" + value["model"]


def _identifier(value: object) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        _fail()


def _number(value: object, maximum: float = 1) -> bool:
    return (
        type(value) in {int, float} and math.isfinite(value) and 0 <= value <= maximum
    )


def _token_fact(value: object) -> None:
    fact = _fields(value, {"status", "tokens"})
    status, tokens = fact["status"], fact["tokens"]
    if status not in STATUSES:
        _fail()
    if status in {"missing", "unavailable"}:
        if tokens is not None:
            _fail()
    elif (
        type(tokens) is not int
        or not 0 <= tokens < 2**53
        or (tokens == 0) != (status == "zero")
    ):
        _fail()


def _cost_fact(value: object) -> None:
    rates = {"input", "output", "cache_read", "cache_write"}
    fact = _fields(value, rates | {"status", "tiers"})
    status = fact["status"]
    if status not in STATUSES:
        _fail()
    if status in {"missing", "unavailable"}:
        if any(fact[field] is not None for field in rates | {"tiers"}):
            _fail()
        return
    if any(not _number(fact[field], 1_000_000) for field in rates):
        _fail()
    tiers = fact["tiers"]
    if not isinstance(tiers, list) or len(tiers) > 16:
        _fail()
    thresholds = []
    for tier in tiers:
        _fields(tier, rates | {"input_tokens_above"})
        threshold = tier["input_tokens_above"]
        if (
            type(threshold) is not int
            or not 0 <= threshold < 2**53
            or any(not _number(tier[field], 1_000_000) for field in rates)
        ):
            _fail()
        thresholds.append(threshold)
    if thresholds != sorted(set(thresholds)):
        _fail()
    zero = all(fact[field] == 0 for field in rates) and all(
        all(tier[field] == 0 for field in rates) for tier in tiers
    )
    if zero != (status == "zero"):
        _fail()


def _capabilities(value: object) -> None:
    fact = _fields(
        value,
        {
            "reasoning",
            "input",
            "context_window",
            "max_output_tokens",
            "declared_cost",
            "prompt_cache_retention",
        },
    )
    flags = [fact["reasoning"], *_fields(fact["input"], {"text", "image"}).values()]
    if any(
        type(flag) is not bool and flag not in {"missing", "unavailable"}
        for flag in flags
    ):
        _fail()
    inputs = fact["input"]
    if type(inputs["text"]) is not bool or type(inputs["image"]) is not bool:
        if inputs["text"] != inputs["image"]:
            _fail()
    _token_fact(fact["context_window"])
    _token_fact(fact["max_output_tokens"])
    _cost_fact(fact["declared_cost"])
    cache = _fields(fact["prompt_cache_retention"], {"status", "short", "long"})
    if cache["status"] not in {"missing", "unavailable", "declared"} or any(
        type(cache[field]) is not bool and cache[field] != "unavailable"
        for field in ("short", "long")
    ):
        _fail()
    if cache["status"] == "missing" and (
        cache["short"] is not False or cache["long"] is not False
    ):
        _fail()
    if cache["status"] == "declared" and (
        not any(cache[field] is True for field in ("short", "long"))
        or any(type(cache[field]) is not bool for field in ("short", "long"))
    ):
        _fail()
    if cache["status"] == "unavailable" and "unavailable" not in (
        cache["short"],
        cache["long"],
    ):
        _fail()


def _catalog(value: object) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        _fail()
    for item in value:
        _fields(item, {"provider", "model", "thinking_levels", "capabilities"})
        _identifier(item["provider"])
        _identifier(item["model"])
        levels = item["thinking_levels"]
        if (
            not isinstance(levels, list)
            or not levels
            or levels != [level for level in LEVELS if level in levels]
        ):
            _fail()
        _capabilities(item["capabilities"])
    keys = [_key(item) for item in value]
    if keys != sorted(set(keys)):
        _fail()
    return value


def _eligibility(
    value: object, catalog: list[dict], record: dict
) -> dict[str, list[dict]]:
    metadata = record["worker_candidates"]
    if not isinstance(value, list) or len(value) != len(metadata["roles"]):
        _fail()
    available = {_key(item): item for item in catalog}
    result = {}
    for item, expected in zip(value, metadata["roles"], strict=True):
        _fields(item, {"role", "identities"})
        identities = item["identities"]
        if (
            item["role"] != expected["role"]
            or not isinstance(identities, list)
            or len(identities) != expected["count"]
        ):
            _fail()
        for identity in identities:
            _fields(identity, {"provider", "model"})
            _identifier(identity["provider"])
            _identifier(identity["model"])
        keys = [_key(identity) for identity in identities]
        if keys != sorted(set(keys)) or any(key not in available for key in keys):
            _fail()
        result[item["role"]] = [available[key] for key in keys]
    locks = [lock for lock in record["locks"] if lock["inclusion"] is not False]
    if list(result) != [lock["role"] for lock in locks]:
        _fail()
    sources = {item["role"]: item["source"] for item in metadata["roles"]}
    for lock in locks:
        source = sources[lock["role"]]
        if source in {"exact-lock", "custom-binding"}:
            if lock["provider"] is None:
                _fail()
        elif lock["provider"] is not None:
            _fail()
    if (
        set(available) != {_key(item) for items in result.values() for item in items}
        or len(catalog) != metadata["count"]
    ):
        _fail()
    scope = _digest(
        {
            "metadata": metadata,
            "capabilities": evidence_digest(catalog),
            "eligibility": [
                {
                    "role": item["role"],
                    "identities": [_key(identity) for identity in item["identities"]],
                }
                for item in value
            ],
        }
    )
    if (
        _digest(
            {"candidates": scope, "scopes": record["scopes"], "locks": record["locks"]}
        )
        != record["bindings"]["candidate_set"]
    ):
        _fail()
    return result


def _model_identity(role: str, candidate: dict) -> dict:
    return {
        "role": role,
        "provider": candidate["provider"],
        "model": candidate["model"],
        "thinking": None,
        "facts": evidence_digest(candidate),
    }


def _thinking_identity(role: str, thinking: str, candidates: list[dict]) -> dict:
    return {
        "role": role,
        "thinking": thinking,
        "models": [
            {
                "provider": item["provider"],
                "model": item["model"],
                "facts": evidence_digest(item),
            }
            for item in candidates
            if thinking in item["thinking_levels"]
        ],
    }


def _identity_key(value: dict) -> str:
    if "provider_composition" in value:
        return value["provider_composition"]
    if "intent" in value:
        return value["intent"]
    if "decision" in value:
        return value["decision"]
    if "inclusion" in value:
        return value["role"] + "\0" + ("include" if value["inclusion"] else "omit")
    if "model" in value:
        return value["role"] + "\0" + _key(value)
    return value["role"] + "\0" + value["thinking"]


def _decision(
    value: object,
    axis: str,
    role: str | None,
    identities: list[dict],
    authority: str,
    source: str,
    selected: dict | None,
) -> dict:
    item = _fields(
        value,
        {
            "axis",
            "role",
            "authority",
            "selected",
            "confidence",
            "selected_probability",
            "options",
            "alternatives",
        },
    )
    identities = sorted(identities, key=_identity_key)
    if item["axis"] != axis or item["role"] != role or item["authority"] != authority:
        _fail()
    options = item["options"]
    if not isinstance(options, list) or len(options) != len(identities):
        _fail()
    probabilistic = source == "typesafe_choice" and authority == "planner"
    for option, identity in zip(options, identities, strict=True):
        _fields(option, {"id", "identity", "probability"})
        if _canonical(option["identity"]) != _canonical(identity) or option[
            "id"
        ] != _digest(identity):
            _fail()
        if probabilistic:
            if not _number(option["probability"]):
                _fail()
        elif option["probability"] is not None:
            _fail()
    selected_option = next(
        (option for option in options if option["id"] == item["selected"]), None
    )
    if (
        selected_option is None
        or selected is not None
        and _canonical(selected_option["identity"]) != _canonical(selected)
    ):
        _fail()
    if probabilistic:
        if (
            not _number(item["confidence"])
            or not _number(item["selected_probability"])
            or abs(sum(option["probability"] for option in options) - 1)
            > PROBABILITY_TOLERANCE
            or item["selected_probability"] != selected_option["probability"]
        ):
            _fail()
        alternatives = sorted(
            (option for option in options if option != selected_option),
            key=lambda option: (
                -option["probability"],
                _identity_key(option["identity"]),
            ),
        )[:TOP_ALTERNATIVES]
        if item["alternatives"] != [option["id"] for option in alternatives]:
            _fail()
    elif (
        item["confidence"] is not None
        or item["selected_probability"] is not None
        or item["alternatives"] != []
    ):
        _fail()
    return selected_option["identity"]


def _role_decisions(record: dict, eligible: dict, decisions: list, source: str) -> int:
    selected = {role["id"]: role for role in record["roles"]}
    cursor = 0
    for lock in record["locks"]:
        role = lock["role"]
        included = role in selected
        if role in {"implementer", "reviewer"} and lock["inclusion"] is not True:
            _fail()
        if lock["inclusion"] is False and any(
            lock[field] is not None for field in ("provider", "model", "thinking")
        ):
            _fail()
        choices = [included] if lock["inclusion"] is not None else [True, False]
        _decision(
            decisions[cursor],
            "roster",
            role,
            [{"role": role, "inclusion": inclusion} for inclusion in choices],
            "fixed" if lock["inclusion"] is not None else "planner",
            source,
            {"role": role, "inclusion": included},
        )
        cursor += 1
        if lock["inclusion"] is False:
            continue
        models = eligible.get(role, [])
        if lock["provider"] is not None:
            if (
                len(models) != 1
                or models[0]["provider"] != lock["provider"]
                or models[0]["model"] != lock["model"]
            ):
                _fail()
        models = [
            item
            for item in models
            if lock["thinking"] is None or lock["thinking"] in item["thinking_levels"]
        ]
        if not models:
            _fail()
        model = _decision(
            decisions[cursor],
            "model",
            role,
            [_model_identity(role, item) for item in models],
            "fixed" if len(models) == 1 else "planner",
            source,
            None,
        )
        cursor += 1
        levels = [
            level
            for level in LEVELS
            if (lock["thinking"] is None or lock["thinking"] == level)
            and any(level in item["thinking_levels"] for item in models)
        ]
        thinking = _decision(
            decisions[cursor],
            "thinking",
            role,
            [_thinking_identity(role, level, models) for level in levels],
            "fixed" if len(levels) == 1 else "planner",
            source,
            None,
        )
        cursor += 1
        candidate = next(
            item
            for item in models
            if item["provider"] == model["provider"] and item["model"] == model["model"]
        )
        if (
            thinking["thinking"] not in candidate["thinking_levels"]
            or included
            and any(
                selected[role][field] != value
                for field, value in (
                    ("provider", model["provider"]),
                    ("model", model["model"]),
                    ("thinking", thinking["thinking"]),
                )
            )
        ):
            _fail()
    return cursor


def _provider_composition_facts(eligible: dict, locks: list[dict]) -> dict:
    roles = []
    for lock in locks:
        if lock["inclusion"] is False:
            continue
        candidates = []
        for item in eligible[lock["role"]]:
            if lock["provider"] is not None and (
                item["provider"] != lock["provider"] or item["model"] != lock["model"]
            ):
                continue
            levels = [
                level
                for level in item["thinking_levels"]
                if lock["thinking"] is None or level == lock["thinking"]
            ]
            if levels:
                candidates.append(
                    {
                        "provider": item["provider"],
                        "model": item["model"],
                        "thinking_levels": levels,
                        "facts": evidence_digest(item),
                    }
                )
        if not candidates:
            _fail()
        roles.append(
            {
                "role": lock["role"],
                "inclusion": lock["inclusion"],
                "candidates": candidates,
            }
        )
    providers = [{item["provider"] for item in role["candidates"]} for role in roles]
    required = [
        items
        for role, items in zip(roles, providers, strict=True)
        if role["inclusion"] is True
    ]
    if len(required) < 2:
        _fail()
    single = bool(set.intersection(*required))
    mixed = len(set.union(*providers)) > 1
    return {
        "feasible": (["single_provider"] if single else [])
        + (["mixed_provider"] if mixed else []),
        "roles": roles,
    }


def _provider_composition_decision(
    evidence: dict, record: dict, eligible: dict, cursor: int
) -> None:
    facts = _provider_composition_facts(eligible, record["locks"])
    if _canonical(evidence["provider_composition"]) != _canonical(facts):
        _fail()
    choices = facts["feasible"]
    planner = len(choices) > 1
    if planner:
        choices = [*choices, "no_material_preference"]
    selected = _decision(
        evidence["decisions"][cursor],
        "provider_composition",
        None,
        [
            {"provider_composition": choice, "facts": evidence_digest(facts)}
            for choice in choices
        ],
        "planner" if planner else "fixed",
        evidence["source"],
        None,
    )
    actual = (
        "single_provider"
        if len({role["provider"] for role in record["roles"]}) == 1
        else "mixed_provider"
    )
    if selected["provider_composition"] not in {actual, "no_material_preference"}:
        _fail()


def provider_composition_summary(evidence: dict) -> dict:
    item = next(
        (
            item
            for item in evidence.get("decisions", [])
            if item["axis"] == "provider_composition"
        ),
        None,
    )
    if item is None:
        return {"state": "unavailable"}
    options = {option["id"]: option for option in item["options"]}
    return {
        "state": "fixed" if item["authority"] == "fixed" else "choice",
        "selected": options[item["selected"]]["identity"]["provider_composition"],
        "authority": item["authority"],
        "confidence": item["confidence"],
        "selected_probability": item["selected_probability"],
        "feasible": evidence["provider_composition"]["feasible"],
        "alternatives": [
            {
                "composition": options[identity]["identity"]["provider_composition"],
                "probability": options[identity]["probability"],
            }
            for identity in item["alternatives"]
        ],
    }


def validate_planner_evidence(value: object, record: dict[str, Any]) -> dict:
    try:
        evidence = _fields(
            value,
            {
                "version",
                "source",
                "catalog",
                "eligibility",
                "decisions",
                "provider_comparison",
            }
            | ({"provider_composition"} if record["version"] == 6 else set()),
        )
        if (
            len(_canonical(evidence).encode("utf-8")) > MAX_EVIDENCE_BYTES
            or type(evidence["version"]) is not int
            or evidence["version"] != (2 if record["version"] == 6 else 1)
            or evidence["source"] not in {"typesafe_choice", "pi_selection"}
        ):
            _fail()
        source = evidence["source"]
        if (source == "typesafe_choice") != (
            record["decision_model"]["provider"] == "typesafe"
        ):
            _fail()
        catalog = _catalog(evidence["catalog"])
        eligible = _eligibility(evidence["eligibility"], catalog, record)
        decisions = evidence["decisions"]
        if not isinstance(decisions, list) or not 7 <= len(decisions) <= 42:
            _fail()
        cursor = _role_decisions(record, eligible, decisions, source)
        _decision(
            decisions[cursor],
            "task_intent",
            None,
            [
                {"intent": intent}
                for intent in ("change", "investigation", "review", "advisory")
            ],
            "planner",
            source,
            {"intent": record["task_intent"]["recommendation"]},
        )
        cursor += 1
        # The compatibility suitability question exists only when all worker axes are fixed.
        if all(item["authority"] == "fixed" for item in decisions[: cursor - 1]):
            _decision(
                decisions[cursor],
                "composition",
                None,
                [{"decision": "accept"}, {"decision": "reject"}],
                "planner",
                source,
                {"decision": "accept"},
            )
            cursor += 1
        if evidence["version"] == 2:
            _provider_composition_decision(evidence, record, eligible, cursor)
            cursor += 1
        if len(decisions) != cursor:
            _fail()
        comparison = _fields(evidence["provider_comparison"], {"state", "rationale"})
        state = (
            "homogeneous"
            if len({role["provider"] for role in record["roles"]}) == 1
            else "mixed"
        )
        if comparison != {"state": state, "rationale": "rationale_unavailable"}:
            _fail()
        return evidence
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        StopIteration,
        OverflowError,
        RecursionError,
    ) as error:
        raise OrchestrationError("Planning evidence metadata is invalid") from error


def _option_label(option: dict) -> str:
    identity = option["identity"]
    if "provider_composition" in identity:
        return identity["provider_composition"]
    if "intent" in identity:
        return identity["intent"]
    if "decision" in identity:
        return identity["decision"]
    if "inclusion" in identity:
        return "include" if identity["inclusion"] else "omit"
    if "model" in identity:
        return f"{identity['provider']}/{identity['model']}"
    models = ",".join(
        f"{item['provider']}/{item['model']}" for item in identity["models"]
    )
    return f"{identity['thinking']} [{models}]"


def planner_evidence_lines(record: dict) -> list[str]:
    evidence = record.get("evidence")
    if (
        not evidence
        or evidence.get("status") == "unavailable"
        or evidence.get("version") not in {1, 2}
    ):
        return [
            "Planner evidence: unavailable (static or legacy record).",
            "Provider composition decision: unavailable.",
        ]
    composition = (
        evidence.get("provider_composition")
        if evidence.get("projection") == "summary"
        else provider_composition_summary(evidence)
    )
    lines = [
        f"Planner evidence v{evidence['version']}: {evidence['source']}; independent axes, not joint confidence or reasoning.",
        f"Provider comparison: {evidence['provider_comparison']['state']}; rationale_unavailable (not Jev reasoning).",
    ]
    if composition and composition.get("selected"):
        probability = (
            "probabilities unavailable"
            if composition["confidence"] is None
            else f"confidence={composition['confidence']}; probability={composition['selected_probability']}"
        )
        lines.append(
            f"Provider composition decision: {composition['selected']}; {composition['state']} authority={composition['authority']}; {probability}."
        )
    else:
        lines.append("Provider composition decision: unavailable (legacy record).")
    if evidence.get("projection") == "summary":
        return [
            *lines,
            "Summary projection; exact status/snapshot provides axis alternatives and facts.",
        ]
    for item in evidence["decisions"]:
        options = {option["id"]: option for option in item["options"]}
        selected = _option_label(options[item["selected"]])
        confidence = (
            "probabilities unavailable"
            if item["confidence"] is None
            else f"confidence={item['confidence']}; probability={item['selected_probability']}"
        )
        alternatives = "; ".join(
            f"{_option_label(options[identity])}={options[identity]['probability']}"
            for identity in item["alternatives"]
        )
        lines.append(
            f"{item['role'] or 'plan'}/{item['axis']}: {item['authority']} {selected}; {confidence}"
            + (f"; alternatives: {alternatives}" if alternatives else "")
        )
    return lines
