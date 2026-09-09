"""Authenticated operator control-command handling for the broker."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from .broker_store import connect_broker_database, record_event, set_meta, utc_now
from .constants import BROKER_PROTOCOL_VERSION, MAX_RPC_COMMANDS, RPC_TOKEN_PATTERN
from .models import OrchestrationError
from .continuation import approved_repair_extension
from .role_registry import valid_custom_role_id
from .worker_resources import revalidate_worker_resources


class BrokerControlSupport:
    """Control operations mixed into the single-writer broker runtime."""

    coord: Path
    manifest: dict[str, Any]
    custom_contracts: dict[str, str]
    clients: dict[str, Any]
    worker_baselines: dict[str, str]

    async def handle_control(
        self,
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        message: dict[str, Any],
    ) -> None:
        expected = {
            "version",
            "type",
            "token",
            "id",
            "action",
            "role",
            "delivery",
            "message",
        }
        if (
            set(message) != expected
            or message.get("version") != BROKER_PROTOCOL_VERSION
        ):
            raise OrchestrationError("Control message is invalid", "invalid_protocol")
        command_id = message.get("id")
        token = message.get("token")
        role = message.get("role")
        action = message.get("action")
        delivery = message.get("delivery")
        body = message.get("message")
        if (
            not isinstance(command_id, str)
            or not RPC_TOKEN_PATTERN.fullmatch(command_id)
            or not isinstance(token, str)
            or not RPC_TOKEN_PATTERN.fullmatch(token)
            or not isinstance(role, str)
            or role not in self.manifest["roles"]
            or action not in {"send", "abort", "restart", "restart_failed", "continue"}
            or (action == "continue" and role != "implementer")
            or delivery not in {None, "steer", "follow-up"}
            or (action == "send" and (not isinstance(body, str) or not body.strip()))
            or (action != "send" and (body is not None or delivery is not None))
        ):
            raise OrchestrationError(
                "Control message fields are invalid", "invalid_protocol"
            )
        repair_round: int | None = None
        uncertain_round: int | None = None
        restarted_client: Any | None = None
        with connect_broker_database(self.coord) as database:
            stored_token = database.execute(
                "SELECT value FROM meta WHERE key='control_token'"
            ).fetchone()["value"]
            if not secrets.compare_digest(stored_token, token):
                raise OrchestrationError(
                    "Control authentication failed", "unauthorized"
                )
            known = database.execute(
                "SELECT action,role,delivery,status FROM control_commands WHERE id=?",
                (command_id,),
            ).fetchone()
            if known is not None:
                if (
                    known["action"] != action
                    or known["role"] != role
                    or known["delivery"] != delivery
                ):
                    raise OrchestrationError("Control command ID conflicts", "conflict")
                await self.send_raw(
                    writer,
                    {
                        "version": BROKER_PROTOCOL_VERSION,
                        "type": "response",
                        "id": command_id,
                        "success": known["status"] == "accepted",
                        "status": known["status"],
                        "duplicate": True,
                    },
                )
                return
            command_count = database.execute(
                "SELECT COUNT(*) AS count FROM control_commands"
            ).fetchone()["count"]
            role_row = database.execute(
                "SELECT roles.state,roles.active_assignment_id,"
                "assignments.state AS assignment_state "
                "FROM roles LEFT JOIN assignments "
                "ON assignments.id=roles.active_assignment_id WHERE roles.role=?",
                (role,),
            ).fetchone()
            role_state = role_row["state"]
            handover_failure_exception = action == "restart_failed" and role_state in {
                "restarting",
                "recovering",
            }
            if command_count >= MAX_RPC_COMMANDS and not handover_failure_exception:
                raise OrchestrationError("Broker command registry is full", "rejected")
            if action == "restart_failed":
                if role_state in {"restarting", "recovering"}:
                    status = "accepted"
                    uncertain_round = self._mark_handover_uncertain(
                        database, role, delivery_id=command_id
                    )
                elif role_state == "uncertain":
                    status = "accepted"
                else:
                    status = "conflict"
            elif role not in self.clients or (
                action in {"restart", "continue"} and role not in self.worker_baselines
            ):
                status = "uncertain"
            elif action == "continue":
                try:
                    repair_round, policy = approved_repair_extension(database)
                except OrchestrationError as error:
                    if error.code != "conflict":
                        raise
                    status = "conflict"
                else:
                    status = "accepted"
                    set_meta(
                        database,
                        "continuation_policy",
                        json.dumps(policy, separators=(",", ":")),
                    )
                    # A crash after approval must not silently replay the new assignment.
                    set_meta(database, "workflow_state", "routing")
                    record_event(
                        database,
                        "continuation_approved",
                        role=role,
                        round_number=repair_round,
                        delivery_id=command_id,
                        status="one_repair_round",
                    )
            elif (
                action == "send"
                and database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()["value"]
                == "needs_attention"
                and (
                    role_state != "waiting"
                    or role_row["active_assignment_id"] is None
                    or role_row["assignment_state"] != "accepted"
                )
            ):
                status = "conflict"
            else:
                status = "accepted"
                if action == "send":
                    repair_round = await self._handle_operator_send(
                        database,
                        role,
                        body.strip(),
                        command_id,
                    )
                elif action == "abort":
                    await self.send(
                        self.clients[role],
                        {
                            "version": BROKER_PROTOCOL_VERSION,
                            "type": "abort",
                            "id": command_id,
                        },
                    )
                else:
                    if valid_custom_role_id(role):
                        verified = revalidate_worker_resources(self.manifest, role)
                        if (
                            verified is None
                            or verified.contract != self.custom_contracts.get(role)
                        ):
                            raise OrchestrationError(
                                "Custom restart contract is not bound"
                            )
                    restarted_client = self.clients[role]
                    database.execute(
                        "UPDATE roles SET generation=generation+1,state='restarting',"
                        "updated_at=? WHERE role=?",
                        (utc_now(), role),
                    )
                    record_event(
                        database,
                        "worker_restart_requested",
                        role=role,
                        status="accepted",
                        delivery_id=command_id,
                    )
            now = utc_now()
            database.execute(
                "INSERT INTO control_commands(id,action,role,delivery,status,received_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (command_id, action, role, delivery, status, now, now),
            )
            record_event(
                database,
                "control_command",
                role=role,
                status=status,
                delivery_id=command_id,
            )
        response = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "response",
            "id": command_id,
            "success": status == "accepted",
            "status": status,
            "duplicate": False,
        }
        if action == "continue" and repair_round is not None:
            await self._finish_repair_continuation(writer, response, repair_round)
            return
        await self.send_raw(writer, response)
        if restarted_client is not None:
            restarted_client.writer.close()
        self.refresh_dashboard()
        if repair_round is not None:
            await self.broadcast_workflow("active", repair_round)
            await self.assign(
                "implementer",
                "implementation",
                repair_round,
                self._assignment("implementer", repair_round),
            )
            self.refresh_dashboard()
        if uncertain_round is not None:
            await self.broadcast_workflow("uncertain", uncertain_round)

    async def _finish_repair_continuation(
        self,
        writer: asyncio.StreamWriter,
        response: dict[str, Any],
        round_number: int,
    ) -> None:
        try:
            await self.assign(
                "implementer",
                "implementation",
                round_number,
                self._assignment("implementer", round_number),
            )
            with connect_broker_database(self.coord, readonly=True) as database:
                pending = database.execute(
                    "SELECT value FROM meta WHERE key='pending_repair_round'"
                ).fetchone()
                assignment = database.execute(
                    "SELECT 1 FROM assignments WHERE role='implementer' "
                    "AND kind='implementation' AND round=?",
                    (round_number,),
                ).fetchone()
                state = database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()[0]
                current_round = self.current_round(database)
            if (
                pending is not None and int(pending["value"]) == round_number
            ) or assignment is None:
                raise OrchestrationError(
                    "Approved repair was not assigned", "broker_uncertain"
                )
            # A worker may already have reported while its delivery drained.
            # Do not overwrite a newer attention/ready/uncertain observer state.
            await self.broadcast_workflow(state, current_round)
            await self.send_raw(writer, response)
        except (Exception, asyncio.CancelledError) as error:
            with connect_broker_database(self.coord) as database:
                set_meta(database, "workflow_state", "uncertain")
                record_event(
                    database,
                    "continuation_uncertain",
                    role="implementer",
                    round_number=round_number,
                    status="uncertain",
                )
            await self.broadcast_workflow("uncertain", round_number)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise OrchestrationError(
                "Repair continuation became uncertain", "broker_uncertain"
            ) from error
        finally:
            self.refresh_dashboard()

    async def _handle_operator_send(
        self,
        database: Any,
        role: str,
        body: str,
        command_id: str,
    ) -> int | None:
        self.evidence_reuse.invalidate_guidance()
        current_round = self.current_round(database)
        workflow_state = database.execute(
            "SELECT value FROM meta WHERE key='workflow_state'"
        ).fetchone()["value"]
        if workflow_state == "ready" and role == "implementer":
            repair_round = current_round + 1
            await self._deliver_run_state((role,), current_round)
            await self._flush_pending_run_state(role, current_round)
            await self.deliver(
                role,
                "operator_message",
                repair_round,
                body,
                trigger=False,
            )
            set_meta(database, "round", str(repair_round))
            set_meta(database, "workflow_state", "active")
            record_event(
                database,
                "workflow_reopened",
                role=role,
                round_number=repair_round,
                delivery_id=command_id,
                status="active",
            )
            return repair_round
        await self.deliver(
            role,
            "operator_message",
            current_round,
            body,
            trigger=True,
        )
        return None
