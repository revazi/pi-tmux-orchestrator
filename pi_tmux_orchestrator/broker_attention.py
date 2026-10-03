"""Assignment-bound, restart-safe attention and one-shot report recovery.

Only the authenticated live observer receives prose. The store contains enums,
identities, and saturated counters; reminder reservation commits before transport.
An interrupted reservation never grants another automatic request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .broker_store import connect_broker_database, record_event, set_meta, utc_now
from .constants import BROKER_PROTOCOL_VERSION
from .models import OrchestrationError
from .worker_attention import assignment_attention_metadata, validate_attention

if TYPE_CHECKING:
    from .broker import Client


class BrokerAttentionSupport:
    def _active_attention_assignment(
        self, database: Any, client: Client, assignment_id: str
    ) -> Any:
        row = database.execute(
            "SELECT a.*,r.generation FROM assignments a JOIN roles r ON r.active_assignment_id=a.id "
            "WHERE a.id=? AND r.role=? AND a.state IN ('accepted','delivering')",
            (assignment_id, client.role),
        ).fetchone()
        if row is None or row["generation"] != client.generation:
            raise OrchestrationError(
                "Assignment is not active for this worker", "conflict"
            )
        return row

    def _attention_waiting(
        self, database: Any, client: Client, assignment: Any
    ) -> None:
        database.execute(
            "UPDATE roles SET state='waiting',activity=NULL,activity_at=NULL,updated_at=? WHERE role=?",
            (utc_now(), client.role),
        )
        workflow = database.execute(
            "SELECT value FROM meta WHERE key='workflow_state'"
        ).fetchone()["value"]
        if workflow not in {"uncertain", "ready"}:
            set_meta(database, "workflow_state", "needs_attention")
        record_event(
            database,
            "worker_attention",
            role=client.role,
            assignment_id=assignment["id"],
            round_number=assignment["round"],
            status="needs_attention",
        )

    async def broadcast_assignment_state(self, client: Client) -> None:
        with connect_broker_database(self.coord, readonly=True) as database:
            metadata = assignment_attention_metadata(database, client.role)
            state = database.execute(
                "SELECT state FROM roles WHERE role=?", (client.role,)
            ).fetchone()["state"]
        await self.broadcast(
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "assignment_state",
                "session": self.manifest["session"],
                "role": client.role,
                "state": state,
                "assignment": metadata,
            }
        )

    async def handle_rejected_report(
        self, client: Client, message: dict[str, Any]
    ) -> None:
        with connect_broker_database(self.coord) as database:
            assignment = self._active_attention_assignment(
                database, client, message["assignment_id"]
            )
            if assignment["report_attempt"] == "none":
                database.execute(
                    "UPDATE assignments SET report_attempt='rejected' WHERE id=?",
                    (assignment["id"],),
                )
        await self.reply(client, message["id"], True, status="recorded")
        await self.broadcast_assignment_state(client)

    async def handle_attention(self, client: Client, message: dict[str, Any]) -> None:
        attention = validate_attention(message["attention"])
        with connect_broker_database(self.coord) as database:
            assignment = self._active_attention_assignment(
                database, client, message["assignment_id"]
            )
            duplicate = assignment["report_attempt"] == "attention"
            if not duplicate:
                database.execute(
                    "UPDATE assignments SET report_attempt='attention',attention_reason=? WHERE id=?",
                    (attention["reason"], assignment["id"]),
                )
                self._attention_waiting(database, client, assignment)
        await self.reply(
            client, message["id"], True, status="duplicate" if duplicate else "recorded"
        )
        await self.broadcast_assignment_state(client)
        if not duplicate:
            # Deliberately bypass recent_reports, event bodies, and replay queues.
            await self.broadcast(
                {
                    "version": BROKER_PROTOCOL_VERSION,
                    "type": "attention",
                    "session": self.manifest["session"],
                    "role": client.role,
                    "assignment_id": assignment["id"],
                    "attention": attention,
                }
            )
            await self.broadcast_workflow("needs_attention", assignment["round"])

    async def handle_settlement(self, client: Client, message: dict[str, Any]) -> None:
        nudge = False
        actionable = False
        with connect_broker_database(self.coord) as database:
            assignment = self._active_attention_assignment(
                database, client, message["assignment_id"]
            )
            duplicate = assignment["last_settlement_id"] == message["id"]
            if not duplicate:
                count = min(2, assignment["settlement_count"] + 1)
                database.execute(
                    "UPDATE assignments SET settlement_count=?,last_settlement_id=? WHERE id=?",
                    (count, message["id"], assignment["id"]),
                )
                nudge = (
                    count == 1
                    and assignment["reminder_state"] == "none"
                    and assignment["report_attempt"] != "attention"
                )
                if nudge:
                    database.execute(
                        "UPDATE assignments SET reminder_state='reserved' WHERE id=?",
                        (assignment["id"],),
                    )
                    database.execute(
                        "UPDATE roles SET state='waiting',activity=NULL,activity_at=NULL WHERE role=?",
                        (client.role,),
                    )
                    record_event(
                        database,
                        "report_reminder_reserved",
                        role=client.role,
                        assignment_id=assignment["id"],
                        round_number=assignment["round"],
                        status="reserved",
                    )
                else:
                    self._attention_waiting(database, client, assignment)
                    actionable = True
        await self.reply(
            client, message["id"], True, status="duplicate" if duplicate else "recorded"
        )
        await self.broadcast_assignment_state(client)
        if nudge:
            try:
                await self.send(
                    client,
                    {
                        "version": BROKER_PROTOCOL_VERSION,
                        "type": "report_reminder",
                        "id": assignment["id"],
                        "assignment_id": assignment["id"],
                    },
                )
            except Exception:
                with connect_broker_database(self.coord) as database:
                    database.execute(
                        "UPDATE assignments SET reminder_state='used' WHERE id=?",
                        (assignment["id"],),
                    )
                    self._attention_waiting(database, client, assignment)
                await self.broadcast_assignment_state(client)
                await self.broadcast_workflow("needs_attention", assignment["round"])
                raise OrchestrationError(
                    "Report recovery scheduling became uncertain", "broker_uncertain"
                ) from None
        if actionable:
            await self.broadcast_workflow("needs_attention", assignment["round"])

    async def handle_recovery_turn(
        self, client: Client, message: dict[str, Any]
    ) -> None:
        with connect_broker_database(self.coord) as database:
            assignment = self._active_attention_assignment(
                database, client, message["assignment_id"]
            )
            allowed = (
                assignment["reminder_state"] == "reserved"
                and assignment["report_attempt"] != "attention"
            )
            if allowed:
                database.execute(
                    "UPDATE assignments SET reminder_state='used' WHERE id=?",
                    (assignment["id"],),
                )
                database.execute(
                    "UPDATE roles SET state='active' WHERE role=?", (client.role,)
                )
            else:
                self._attention_waiting(database, client, assignment)
        await self.reply(
            client, message["id"], allowed, status="granted" if allowed else "conflict"
        )
        await self.broadcast_assignment_state(client)
        if not allowed:
            await self.broadcast_workflow("needs_attention", assignment["round"])
