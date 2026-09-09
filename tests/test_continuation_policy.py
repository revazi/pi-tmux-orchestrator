"""Read-only policy tests, independent of broker startup, routing, and CLI."""

from __future__ import annotations

import json
import sqlite3
import unittest

from pi_tmux_orchestrator.continuation import (
    MAX_REPAIR_ROUNDS,
    approved_repair_extension,
    continuation_status,
    repair_limit_reached,
    repair_policy,
    retained_repair_policy,
)
from pi_tmux_orchestrator.models import OrchestrationError


class ContinuationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.addCleanup(self.database.close)
        self.database.row_factory = sqlite3.Row
        self.database.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE assignments(role TEXT, kind TEXT, round INTEGER, state TEXT);
            CREATE TABLE roles(role TEXT, state TEXT, active_assignment_id TEXT);
            INSERT INTO meta VALUES ('schema_version','8');
            INSERT INTO meta VALUES ('workflow_state','needs_attention');
            INSERT INTO roles VALUES ('implementer','idle',NULL);
            INSERT INTO roles VALUES ('reviewer','idle',NULL);
        """)

    def metadata(self, key: str, value: str) -> None:
        self.database.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def test_limit_requires_an_explicit_nonnegative_integer_or_disabled(self) -> None:
        for value in (None, 0, 3, MAX_REPAIR_ROUNDS):
            self.assertEqual(
                repair_policy(value), {"version": 1, "max_repair_rounds": value}
            )
        for value in (True, False, -1, 1.5, "3", {}, MAX_REPAIR_ROUNDS + 1):
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                repair_policy(value)

    def test_legacy_absence_is_disabled_but_new_schema_requires_policy(self) -> None:
        self.assertEqual(retained_repair_policy(self.database), repair_policy(None))
        self.metadata("schema_version", "9")
        with self.assertRaisesRegex(OrchestrationError, "policy is missing"):
            retained_repair_policy(self.database)
        self.metadata("continuation_policy", json.dumps(repair_policy(0)))
        self.assertEqual(retained_repair_policy(self.database), repair_policy(0))

    def test_retained_policy_rejects_ambiguous_or_body_bearing_input(self) -> None:
        for value in (
            "invalid",
            "null",
            "[]",
            " " * 257,
            '{"version":true,"max_repair_rounds":1}',
            '{"version":2,"max_repair_rounds":1}',
            '{"version":1,"max_repair_rounds":1,"max_repair_rounds":null}',
            '{"version":1,"max_repair_rounds":1,"body":"not metadata"}',
        ):
            with self.subTest(value=value):
                self.metadata("continuation_policy", value)
                with self.assertRaises(OrchestrationError):
                    retained_repair_policy(self.database)

    def test_count_is_distinct_additional_implementation_rounds_in_all_states(
        self,
    ) -> None:
        self.database.executemany(
            "INSERT INTO assignments VALUES (?,?,?,?)",
            [
                ("implementer", "plan", 1, "completed"),
                ("implementer", "implementation", 1, "completed"),
                ("implementer", "implementation", 2, "uncertain"),
                ("implementer", "implementation", 2, "accepted"),
                ("implementer", "implementation", 3, "delivering"),
                ("reviewer", "review", 3, "completed"),
                ("django", "django", 3, "completed"),
            ],
        )
        self.assertEqual(
            continuation_status(self.database)["repair_rounds_admitted"], 2
        )
        self.metadata("continuation_policy", json.dumps(repair_policy(2)))
        self.assertTrue(
            repair_limit_reached(self.database, "implementer", "implementation", 4)
        )
        for role, kind, round_number in (
            ("implementer", "plan", 1),
            ("implementer", "implementation", 1),
            ("reviewer", "review", 4),
            ("django", "django", 4),
        ):
            self.assertFalse(
                repair_limit_reached(self.database, role, kind, round_number)
            )

    def test_approval_returns_one_increment_without_writing_state(self) -> None:
        self.metadata("continuation_policy", json.dumps(repair_policy(0)))
        self.metadata("pending_repair_round", "2")
        changes = self.database.total_changes
        self.assertTrue(
            repair_limit_reached(self.database, "implementer", "implementation", 2)
        )
        self.assertEqual(
            approved_repair_extension(self.database), (2, repair_policy(1))
        )
        self.assertEqual(self.database.total_changes, changes)
        self.assertEqual(retained_repair_policy(self.database), repair_policy(0))

    def test_busy_or_assigned_roles_prevent_approval(self) -> None:
        self.metadata("continuation_policy", json.dumps(repair_policy(0)))
        self.metadata("pending_repair_round", "2")
        for state, assignment in (
            ("active", None),
            ("waiting", None),
            ("restarting", None),
            ("recovering", None),
            ("uncertain", None),
            ("idle", "assignment-id"),
        ):
            with self.subTest(state=state, assignment=assignment):
                self.database.execute(
                    "UPDATE roles SET state=?,active_assignment_id=? WHERE role='implementer'",
                    (state, assignment),
                )
                with self.assertRaisesRegex(OrchestrationError, "safely paused"):
                    approved_repair_extension(self.database)

    def test_no_approval_when_unpaused_unbounded_or_pending_state_missing(self) -> None:
        with self.assertRaises(OrchestrationError):
            approved_repair_extension(self.database)
        self.metadata("continuation_policy", json.dumps(repair_policy(0)))
        with self.assertRaises(OrchestrationError):
            approved_repair_extension(self.database)
        self.metadata("pending_repair_round", "2")
        for state in ("active", "routing", "ready", "uncertain"):
            self.metadata("workflow_state", state)
            with self.subTest(state=state), self.assertRaises(OrchestrationError):
                approved_repair_extension(self.database)

    def test_invalid_pending_round_never_yields_a_decision(self) -> None:
        for value in ("unknown", "1", "-2"):
            self.metadata("pending_repair_round", value)
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                continuation_status(self.database)
