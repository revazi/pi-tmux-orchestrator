"""Read-only parent-observer transport and snapshot projection for the broker."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .broker_store import connect_broker_database
from .constants import BROKER_PROTOCOL_VERSION, RPC_TOKEN_PATTERN
from .models import OrchestrationError
from .protocol import encode_frame


@dataclass(eq=False)
class Observer:
    writer: asyncio.StreamWriter
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


MAX_OBSERVER_REPORTS = 100


class BrokerObserverSupport:
    """Observer operations mixed into the single-writer broker runtime."""

    coord: Path
    manifest: dict[str, Any]
    observers: set[Observer]
    recent_reports: list[dict[str, Any]]

    async def send_raw(
        self, writer: asyncio.StreamWriter, value: dict[str, Any]
    ) -> None:
        writer.write(encode_frame(value))
        await writer.drain()

    async def send_observer(self, observer: Observer, value: dict[str, Any]) -> None:
        async with observer.send_lock:
            await self.send_raw(observer.writer, value)

    async def broadcast(self, value: dict[str, Any]) -> None:
        if not self.observers:
            return

        async def deliver(observer: Observer) -> None:
            try:
                await asyncio.wait_for(self.send_observer(observer, value), timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.observers.discard(observer)
                observer.writer.close()

        await asyncio.gather(*(deliver(observer) for observer in list(self.observers)))

    def observer_snapshot(self) -> dict[str, Any]:
        with connect_broker_database(self.coord, readonly=True) as database:
            state = database.execute(
                "SELECT value FROM meta WHERE key='workflow_state'"
            ).fetchone()["value"]
            round_number = int(
                database.execute("SELECT value FROM meta WHERE key='round'").fetchone()[
                    "value"
                ]
            )
            roles = [
                {"role": row["role"], "state": row["state"]}
                for row in database.execute(
                    "SELECT role,state FROM roles ORDER BY role"
                )
            ]
            report_count = database.execute(
                "SELECT COUNT(*) AS count FROM reports"
            ).fetchone()["count"]
        return {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "snapshot",
            "session": self.manifest["session"],
            "state": state,
            "round": round_number,
            "roles": roles,
            "report_count": report_count,
            "report_replay_complete": report_count <= len(self.recent_reports),
        }

    async def broadcast_workflow(self, state: str, round_number: int) -> None:
        await self.broadcast(
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "workflow",
                "session": self.manifest["session"],
                "state": state,
                "round": round_number,
            }
        )

    async def handle_observer(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        message: dict[str, Any],
    ) -> None:
        if (
            set(message) != {"version", "type", "token", "id"}
            or message.get("version") != BROKER_PROTOCOL_VERSION
        ):
            raise OrchestrationError("Observer hello is invalid", "invalid_protocol")
        request_id = message.get("id")
        token = message.get("token")
        if (
            not isinstance(request_id, str)
            or not RPC_TOKEN_PATTERN.fullmatch(request_id)
            or not isinstance(token, str)
            or not RPC_TOKEN_PATTERN.fullmatch(token)
        ):
            raise OrchestrationError("Observer identity is invalid", "invalid_protocol")
        with connect_broker_database(self.coord, readonly=True) as database:
            stored_token = database.execute(
                "SELECT value FROM meta WHERE key='control_token'"
            ).fetchone()["value"]
        if not secrets.compare_digest(stored_token, token):
            raise OrchestrationError("Observer authentication failed", "unauthorized")

        observer = Observer(writer)
        self.observers.add(observer)
        try:
            await self.send_observer(
                observer,
                {
                    "version": BROKER_PROTOCOL_VERSION,
                    "type": "response",
                    "id": request_id,
                    "success": True,
                    "status": "observing",
                },
            )
            for report in list(self.recent_reports):
                await self.send_observer(observer, report)
            await self.send_observer(observer, self.observer_snapshot())
            unexpected = await reader.read(1)
            if unexpected:
                raise OrchestrationError(
                    "Observer connections are read-only", "invalid_protocol"
                )
        finally:
            self.observers.discard(observer)
