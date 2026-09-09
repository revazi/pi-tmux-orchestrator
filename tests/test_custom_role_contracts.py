"""Custom identity is never writer/reviewer authority or a peer-selected contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pi_tmux_orchestrator.constants import KNOWN_ROLES
from pi_tmux_orchestrator.models import OrchestrationError
from pi_tmux_orchestrator.protocol import validate_client_message, validate_report
from pi_tmux_orchestrator.role_contracts import (
    resolve_role_contract,
    validate_custom_contracts,
)
from pi_tmux_orchestrator.role_registry import RESERVED_SUFFIXES, valid_custom_role_id

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/custom-role-contracts.json").read_text()
)


class CustomRoleContractTests(unittest.TestCase):
    def test_cross_language_identity_bounds_match_registry(self):
        for suffix in RESERVED_SUFFIXES:
            self.assertIn(f"custom-{suffix}", FIXTURE["invalid_ids"])
        for role in FIXTURE["valid_ids"]:
            self.assertTrue(valid_custom_role_id(role), role)
        for role in FIXTURE["invalid_ids"]:
            self.assertFalse(valid_custom_role_id(role), role)
        for role in KNOWN_ROLES:
            self.assertEqual(resolve_role_contract(role), role)

    def test_custom_contracts_require_explicit_bounded_trusted_bindings(self):
        self.assertEqual(validate_custom_contracts(None), {})
        self.assertEqual(validate_custom_contracts({}), {})
        for value in (
            [],
            True,
            {"reviewer": "probe"},
            {"custom-security": "reviewer"},
            {"custom-security": "implementer"},
            {"custom-security": None},
            {"custom-security": {}},
            {f"custom-case-{i}": "probe" for i in range(9)},
        ):
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                validate_custom_contracts(value)
        bindings = {f"custom-case-{i}": "probe" for i in range(8)}
        self.assertEqual(validate_custom_contracts(bindings), bindings)
        with self.assertRaises(OrchestrationError):
            resolve_role_contract("custom-security")
        with self.assertRaises(OrchestrationError):
            resolve_role_contract([], bindings)

    def test_messages_preserve_identity_and_cannot_supply_their_own_contract(self):
        for case in FIXTURE["cases"]:
            bindings = {case["role"]: case["contract"]}
            message = {
                "version": 1,
                "type": "hello",
                "role": case["role"],
                "token": "a" * 32,
                "id": "b" * 32,
                "generation": 1,
            }
            with self.assertRaises(OrchestrationError):
                validate_client_message(message)
            self.assertIs(
                validate_client_message(message, custom_contracts=bindings), message
            )
            self.assertEqual(message["role"], case["role"])
            with self.assertRaises(OrchestrationError):
                validate_client_message(
                    {**message, "contract": "reviewer"}, custom_contracts=bindings
                )
            with self.assertRaises(OrchestrationError):
                validate_client_message(
                    {**message, "role": "custom-unregistered"},
                    custom_contracts=bindings,
                )
            with self.assertRaises(OrchestrationError):
                validate_client_message(
                    {**message, "generation": 0}, custom_contracts=bindings
                )

    def test_custom_reports_use_builtin_specialist_schema_without_inheriting_identity(
        self,
    ):
        for case in FIXTURE["cases"]:
            bindings = {case["role"]: case["contract"]}
            expected = validate_report(case["report"], case["contract"])
            self.assertEqual(
                validate_report(
                    case["report"], case["role"], custom_contracts=bindings
                ),
                expected,
            )
            with self.assertRaises(OrchestrationError):
                validate_report(case["report"], case["role"])
            for kind in ("plan", "implementation", "review"):
                with self.assertRaises(OrchestrationError):
                    validate_report(
                        {"kind": kind, "summary": "forbidden", "verdict": "approved"},
                        case["role"],
                        custom_contracts=bindings,
                    )
            for change in (
                {"changed_paths": ["src/file.py"]},
                {"verdict": "approved"},
                {"authority": "reviewer"},
            ):
                with self.assertRaises(OrchestrationError):
                    validate_report(
                        {**case["report"], **change},
                        case["role"],
                        custom_contracts=bindings,
                    )
