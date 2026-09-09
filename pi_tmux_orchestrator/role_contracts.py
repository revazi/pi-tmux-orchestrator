"""Separate authenticated worker identity from its trusted read-only contract."""

from __future__ import annotations

from .constants import KNOWN_ROLES
from .models import OrchestrationError
from .role_registry import CONTRACTS, MAX_CUSTOM_ROLES, valid_custom_role_id


def validate_custom_contracts(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_CUSTOM_ROLES:
        raise OrchestrationError(
            "Custom role contract bindings are invalid", "invalid_protocol"
        )
    for role, contract in value.items():
        if (
            not valid_custom_role_id(role)
            or not isinstance(contract, str)
            or contract not in CONTRACTS
        ):
            raise OrchestrationError(
                "Custom role contract bindings are invalid", "invalid_protocol"
            )
    return dict(value)


def validate_assignment_kind(
    role: str, kind: str, *, custom_contracts: object = None
) -> None:
    contract = resolve_role_contract(role, custom_contracts)
    allowed = {
        "implementer": {"plan", "implementation"},
        "reviewer": {"review"},
    }.get(contract, {contract})
    if not isinstance(kind, str) or kind not in allowed:
        raise OrchestrationError(
            "Assignment kind is not permitted for this role", "forbidden"
        )


def resolve_role_contract(role: object, custom_contracts: object = None) -> str:
    """Bindings must come from trusted run metadata, never a worker frame."""
    contracts = validate_custom_contracts(custom_contracts)
    if not isinstance(role, str):
        raise OrchestrationError("Broker role is invalid", "invalid_protocol")
    if role in KNOWN_ROLES:
        return role
    if role in contracts:
        return contracts[role]
    raise OrchestrationError("Broker role is not registered", "invalid_protocol")
