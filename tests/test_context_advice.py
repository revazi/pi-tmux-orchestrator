"""Model-free assignment advice and its retained CLI-only projection."""

from __future__ import annotations

import argparse
import itertools
import json
import unittest
from unittest import mock

from broker_test_support import BrokerFixture, assignment_usage_snapshot

from pi_tmux_orchestrator import broker_store, commands, evidence_reuse
from pi_tmux_orchestrator.broker import Broker, Client, initialize_broker_run
from pi_tmux_orchestrator.context_advice import (
    MAX_CONTEXT_ADVICE_BYTES,
    context_advice,
    context_pressure,
    read_context_advice,
)
from pi_tmux_orchestrator.rpc_store import rpc_role_paths
from pi_tmux_orchestrator.storage import save_manifest


class ContextAdvicePolicyTests(unittest.TestCase):
    def test_policy_matrix_is_deterministic_and_bounded(self):
        for relation, evidence, pressure in itertools.product(
            ("same", "related", "different", "unknown"),
            ("unchanged", "stale", "unknown"),
            ("low", "high", "unknown"),
        ):
            with self.subTest(relation=relation, evidence=evidence, pressure=pressure):
                facts = {
                    "relationship": relation,
                    "evidence": evidence,
                    "pressure": pressure,
                }
                result = context_advice(facts)
                if relation == "unknown":
                    expected = None
                elif (
                    relation == "different" or evidence == "stale" or pressure == "high"
                ):
                    expected = "prune"
                else:
                    expected = (
                        "retain"
                        if (evidence, pressure) == ("unchanged", "low")
                        else None
                    )
                self.assertEqual(result["recommendation"], expected)
                self.assertEqual(result["version"], 1)
                self.assertEqual(result["source"], "deterministic-assignment-boundary")
                self.assertEqual(
                    result["unknown_inputs"],
                    [key for key in facts if facts[key] == "unknown"],
                )
                self.assertEqual(
                    context_advice(dict(reversed(list(facts.items())))), result
                )
                self.assertLessEqual(len(result["reasons"]), 3)
                self.assertLessEqual(len(result["tradeoffs"]), 2)
                record = {"round": 2**63 - 1, "kind": "implementation", **result}
                serialized = json.dumps(record, separators=(",", ":"))
                self.assertLessEqual(len(serialized.encode()), MAX_CONTEXT_ADVICE_BYTES)
                self.assertEqual(read_context_advice(serialized), record)

    def test_unknown_is_not_low_pressure_and_saturation_is_high(self):
        for value in (None, False, "30", -1, float("nan"), float("inf")):
            self.assertEqual(context_pressure(value), "unknown")
        for value, expected in (
            (0, "low"),
            (79.99, "low"),
            (80, "high"),
            (125, "high"),
        ):
            self.assertEqual(context_pressure(value), expected)

    def test_input_and_retained_projection_reject_wrong_types_keys_and_enums(self):
        facts = {"relationship": "same", "evidence": "unchanged", "pressure": "low"}
        for value in (
            None,
            [],
            {},
            {**facts, "note": "private"},
            {**facts, "evidence": True},
            {**facts, "pressure": []},
            {**facts, "relationship": "same\n"},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                context_advice(value)
        record = {"round": 1, "kind": "implementation", **context_advice(facts)}
        self.assertEqual(
            record["reasons"], ["same_work", "unchanged_evidence", "low_pressure"]
        )
        self.assertEqual(
            record["tradeoffs"], ["keeps_prior_exchanges", "may_increase_context"]
        )
        for patch in (
            {"version": True},
            {"version": 1.0},
            {"version": 2},
            {"round": True},
            {"round": 0},
            {"round": 2**63},
            {"kind": "private"},
            {"source": "worker"},
            {"recommendation": "fresh"},
            {"recommendation": "prune"},
            {"body": "private"},
            {"reasons": ["private"]},
            {"tradeoffs": ["private"]},
            {"unknown_inputs": ["pressure"]},
            {"facts": {**facts, "pressure": "private"}},
        ):
            with self.subTest(patch=patch):
                self.assertIsNone(read_context_advice(json.dumps({**record, **patch})))
        serialized = json.dumps(record)
        for raw in (
            None,
            14,
            "[0]",
            " " * MAX_CONTEXT_ADVICE_BYTES + serialized,
            '{"version":1,' + serialized[1:],
        ):
            self.assertIsNone(read_context_advice(raw))


class ContextAdviceBoundaryTests(BrokerFixture, unittest.IsolatedAsyncioTestCase):
    async def start_plan(self, *, retained=False, transport="tui"):
        self.manifest["transport"] = transport
        save_manifest(self.coord, self.manifest)
        initialize_broker_run(
            self.coord,
            self.manifest,
            "ADVICE_TASK_CANARY",
            {},
            implementation_flow="phased",
            worker_context_overrides={"implementer": "retain"} if retained else {},
        )
        broker = Broker(self.coord, self.manifest)
        broker.clients = {
            role: Client(role, mock.Mock(), mock.Mock())
            for role in self.manifest["roles"]
        }
        for method in ("send", "broadcast", "broadcast_workflow", "reply"):
            setattr(broker, method, mock.AsyncMock())
        broker.worker_baselines["implementer"] = "ADVICE_CAPSULE_CANARY"
        with broker_store.connect_broker_database(self.coord) as database:
            broker_store.set_meta(database, "workflow_state", "active")
        await broker.assign("implementer", "plan", 1, "ADVICE_ASSIGNMENT_CANARY")
        return broker

    async def finish_plan(self, broker, usage=None):
        frame = broker.send.await_args.args[1]
        message = {
            "id": "b" * 32,
            "assignment_id": frame["assignment_id"],
            "report": {
                "kind": "plan",
                "summary": "ADVICE_REPORT_CANARY",
                "relevant_paths": ["ADVICE_PATH_CANARY"],
                "relevant_symbols": [],
                "intended_changes": [],
                "required_checks": [],
                "risks": [],
                "open_questions": [],
            },
        }
        if usage is not None:
            message["usage"] = usage
        await broker.handle_report(broker.clients["implementer"], message)
        return message

    def snapshot(self):
        return broker_store.public_broker_snapshot(
            self.coord, include_context_advice=True
        )

    def advice(self):
        return self.snapshot()["roles"][0]["context_advice"]

    async def test_default_prune_uses_report_sample_without_inventory_or_wire_changes(
        self,
    ):
        with mock.patch.object(evidence_reuse, "worktree_stamp") as stamp:
            broker = await self.start_plan()
            self.assertEqual(self.advice()["facts"]["relationship"], "unknown")
            usage = assignment_usage_snapshot()
            usage["assignment"]["contextPercent"] = 80
            await self.finish_plan(broker, usage)
        stamp.assert_not_called()
        advice = self.advice()
        self.assertEqual(
            advice["facts"],
            {"relationship": "same", "evidence": "unknown", "pressure": "high"},
        )
        self.assertEqual(advice["recommendation"], "prune")
        self.assertEqual(advice["reasons"], ["same_work", "high_pressure"])
        self.assertEqual(
            broker_store.worker_context_mode(self.coord, "implementer"), "prune"
        )
        self.assertIsNone(self.snapshot()["roles"][1]["assignment"])
        self.assertEqual(broker.send.await_args.args[1]["kind"], "implementation")
        self.assertNotIn("context_advice", str(broker.send.await_args_list))
        self.assertNotIn("context_advice", str(broker.broadcast.await_args_list))

    async def test_retained_phase_reuses_existing_observation_and_survives_replay_and_restart(
        self,
    ):
        with mock.patch.object(
            evidence_reuse, "worktree_stamp", return_value="inventory-canary"
        ) as stamp:
            broker = await self.start_plan(retained=True, transport="rpc")
            report = await self.finish_plan(broker, assignment_usage_snapshot())
            self.assertEqual(stamp.call_count, 2)
            advice = self.advice()
            self.assertEqual(advice["recommendation"], "retain")
            self.assertEqual(advice["round"], 1)
            self.assertEqual(advice["kind"], "implementation")
            client = broker.clients["implementer"]
            frame = broker.send.await_args.args[1]
            await broker.handle_report(client, report)
            self.assertEqual(broker.reply.await_args.kwargs["status"], "duplicate")
            await broker.handle_delivery_ack(
                client,
                {"id": "c" * 32, "delivery_id": frame["id"], "status": "accepted"},
            )
            stamp.return_value = "changed-inventory"
            await broker.recover_role(client, handover=True)
            replay = broker.send.await_args.args[1]
            self.assertEqual(replay["assignment_id"], frame["assignment_id"])
            self.assertNotEqual(replay["id"], frame["id"])
            self.assertEqual(stamp.call_count, 3)
            broker_store.prepare_broker_database(self.coord)
            reopened = Broker(self.coord, self.manifest)
            self.assertEqual(reopened.evidence_reuse.receipts, {})
        self.assertEqual(advice, self.advice())
        self.assertEqual(
            broker_store.worker_context_mode(self.coord, "implementer"), "retain"
        )
        with broker_store.connect_broker_database(
            self.coord, readonly=True
        ) as database:
            self.assertEqual(
                database.execute("SELECT count(*) FROM assignments").fetchone()[0], 2
            )
            dump = "\n".join(database.iterdump())
        for body in (
            "ADVICE_TASK_CANARY",
            "ADVICE_ASSIGNMENT_CANARY",
            "ADVICE_REPORT_CANARY",
            "ADVICE_PATH_CANARY",
            "ADVICE_CAPSULE_CANARY",
            "inventory-canary",
        ):
            self.assertNotIn(body, dump)
            self.assertNotIn(body, json.dumps(self.snapshot()))

    async def test_guidance_arriving_during_flush_makes_unchanged_evidence_stale(self):
        with mock.patch.object(evidence_reuse, "worktree_stamp", return_value="one"):
            broker = await self.start_plan(retained=True)

            # This callback runs during delivery, after the shared snapshot is computed.
            def send(_client, frame):
                if frame.get("kind") == "run_state":
                    broker.evidence_reuse.invalidate_guidance()

            broker.send.side_effect = send
            await self.finish_plan(broker)
        self.assertEqual(
            self.advice()["facts"],
            {"relationship": "same", "evidence": "stale", "pressure": "unknown"},
        )
        self.assertEqual(self.advice()["recommendation"], "prune")
        self.assertEqual(
            broker_store.worker_context_mode(self.coord, "implementer"), "retain"
        )

    async def test_prior_report_from_replaced_generation_is_not_used(self):
        broker = await self.start_plan()
        route = broker.route_report

        async def replace_generation(*args):
            with broker_store.connect_broker_database(self.coord) as database:
                database.execute(
                    "UPDATE roles SET generation=2 WHERE role='implementer'"
                )
            broker.clients["implementer"].generation = 2
            await route(*args)

        with mock.patch.object(broker, "route_report", new=replace_generation):
            await self.finish_plan(broker, assignment_usage_snapshot())
        self.assertEqual(
            self.advice()["unknown_inputs"], ["relationship", "evidence", "pressure"]
        )
        self.assertIsNone(self.advice()["recommendation"])

    async def test_status_renders_only_bounded_advice_for_both_transports(self):
        broker = await self.start_plan()
        await self.finish_plan(broker)
        for transport in ("tui", "rpc"):
            manifest = {**self.manifest, "transport": transport}
            save_manifest(self.coord, manifest)
            if transport == "rpc":
                for role in manifest["roles"]:
                    rpc_role_paths(self.coord, role, create=True)
            with (
                mock.patch.object(
                    commands,
                    "resolve_session",
                    return_value=(manifest["session"], self.coord),
                ),
                mock.patch.object(commands, "tmux", return_value=mock.Mock(stdout="")),
                mock.patch.object(commands, "human_print") as human,
            ):
                result = commands.status_command(
                    argparse.Namespace(session=manifest["session"])
                )
            self.assertEqual(
                result.data["broker"]["roles"][0]["context_advice"], self.advice()
            )
            self.assertNotIn("context_advice", result.data["roles"][0]["broker_state"])
            self.assertEqual(self.advice()["unknown_inputs"], ["evidence", "pressure"])
            self.assertIn("no recommendation (not applied)", str(human.call_args_list))
            self.assertNotIn("ADVICE_REPORT_CANARY", json.dumps(result.data))

    async def test_schema_thirteen_and_malformed_optional_metadata_remain_readable(
        self,
    ):
        await self.start_plan()
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("ALTER TABLE roles DROP COLUMN context_advice")
            broker_store.set_meta(database, "schema_version", "13")
        self.assertIsNone(self.advice())
        broker_store.prepare_broker_database(self.coord)
        self.assertIsNone(self.advice())
        for raw in (
            "ADVICE_PRIVATE_CANARY",
            "\0" + "a" * MAX_CONTEXT_ADVICE_BYTES,
            b"not-text",
        ):
            with broker_store.connect_broker_database(self.coord) as database:
                database.execute("UPDATE roles SET context_advice=?", (raw,))
            self.assertIsNone(self.advice())
        with broker_store.connect_broker_database(self.coord) as database:
            database.execute("UPDATE roles SET context_advice=CAST(X'80' AS TEXT)")
        self.assertIsNone(self.advice())
