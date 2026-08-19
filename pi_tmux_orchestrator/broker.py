"""Event-driven private Unix-socket broker for one orchestration run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import runtime
from .broker_store import (
    broker_paths,
    connect_broker_database,
    record_event,
    set_meta,
    utc_now,
)
from .constants import (
    BROKER_PROTOCOL_VERSION,
    DEFAULT_SOFT_ROLE_TOKENS,
    DEFAULT_SOFT_TOTAL_TOKENS,
    MAX_BROKER_FRAME_BYTES,
    MAX_CONTEXT_CAPSULE_BYTES,
    MAX_RPC_COMMANDS,
    RPC_TOKEN_PATTERN,
)
from .context_capsules import render_run_state_capsule, render_worker_baseline
from .dashboard import BrokerDashboard
from .models import OrchestrationError
from .output import bounded_message
from .protocol import encode_frame, validate_client_message, validate_report
from .storage import load_manifest, secure_write


@dataclass
class Client:
    role: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(eq=False)
class Observer:
    writer: asyncio.StreamWriter
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


MAX_OBSERVER_REPORTS = 100


class Broker:
    def __init__(self, coord: Path, manifest: dict[str, Any]) -> None:
        self.coord = coord
        self.manifest = manifest
        self.paths = broker_paths(coord)
        self.clients: dict[str, Client] = {}
        self.observers: set[Observer] = set()
        self.recent_reports: list[dict[str, Any]] = []
        self.server: asyncio.AbstractServer | None = None
        self.stopping = asyncio.Event()
        self.task_bodies = self._load_startup_payload()
        self.dashboard = BrokerDashboard(manifest)
        self.dashboard_active = False

    def _load_startup_payload(self) -> dict[str, str]:
        path = self.coord / "startup.json"
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            with connect_broker_database(self.coord, readonly=True) as database:
                state = database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()["value"]
            if state in {"starting", "connecting", "initializing"}:
                raise OrchestrationError("Broker startup payload is unavailable")
            return {"task": "", "context_capsule": ""}
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OrchestrationError("Startup payload must be a private regular file")
        if metadata.st_size > MAX_BROKER_FRAME_BYTES:
            raise OrchestrationError("Startup payload exceeds the safety limit")
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise OrchestrationError("Cannot read broker startup payload") from error
        valid_fields = (
            {"task", "role_tasks"},
            {"task", "context_capsule", "role_tasks"},
        )
        if not isinstance(value, dict) or set(value) not in valid_fields:
            raise OrchestrationError("Broker startup payload is invalid")
        context_capsule = value.get("context_capsule", "")
        if (
            not isinstance(value["task"], str)
            or not isinstance(context_capsule, str)
            or len(context_capsule.encode("utf-8")) > MAX_CONTEXT_CAPSULE_BYTES
            or not isinstance(value["role_tasks"], dict)
        ):
            raise OrchestrationError("Broker startup payload is invalid")
        result = {"task": value["task"], "context_capsule": context_capsule}
        for role, body in value["role_tasks"].items():
            if role not in self.manifest["roles"] or not isinstance(body, str):
                raise OrchestrationError("Broker startup role payload is invalid")
            result[role] = body
        return result

    async def run(self) -> None:
        with self.dashboard:
            self.dashboard_active = True
            try:
                await self._run()
            finally:
                self.dashboard_active = False

    async def _run(self) -> None:
        socket_path = self.paths["socket"]
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        try:
            metadata = socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(metadata.st_mode):
                raise OrchestrationError("Broker socket path is not a socket")
            socket_path.unlink()
        previous_umask = os.umask(0o077)
        try:
            self.server = await asyncio.start_unix_server(
                self.handle_client,
                path=socket_path,
                limit=MAX_BROKER_FRAME_BYTES + 4,
            )
        finally:
            os.umask(previous_umask)
        os.chmod(socket_path, 0o600)
        with connect_broker_database(self.coord) as database:
            current_state = database.execute(
                "SELECT value FROM meta WHERE key='workflow_state'"
            ).fetchone()["value"]
            if current_state == "starting":
                current_state = "connecting"
                set_meta(database, "workflow_state", current_state)
            elif current_state in {"routing", "initializing"}:
                current_state = "uncertain"
                set_meta(database, "workflow_state", current_state)
                record_event(database, "workflow_uncertain", status=current_state)
            record_event(database, "broker_started", status=current_state)
        self.refresh_dashboard()
        async with self.server:
            await self.stopping.wait()
        await self.close_clients()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass

    def refresh_dashboard(self) -> None:
        """Refresh presentation after state writes without affecting broker behavior."""
        if not self.dashboard_active:
            return
        try:
            self.dashboard.refresh_from_store(self.coord)
        except Exception:
            # Presentation is best-effort; never turn a terminal failure into a
            # workflow transition or expose a raw terminal/database error.
            try:
                self.dashboard.render_unavailable()
            except Exception:
                pass

    async def close_clients(self) -> None:
        writers = [client.writer for client in self.clients.values()]
        writers.extend(observer.writer for observer in self.observers)
        for writer in writers:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    async def read_frame(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        prefix = await reader.readexactly(4)
        size = int.from_bytes(prefix, "big")
        if not 1 <= size <= MAX_BROKER_FRAME_BYTES:
            raise OrchestrationError("Broker frame size is invalid", "invalid_protocol")
        payload = await reader.readexactly(size)
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OrchestrationError(
                "Broker frame is not valid JSON", "invalid_protocol"
            ) from error
        return validate_client_message(value)

    async def send(self, client: Client, value: dict[str, Any]) -> None:
        async with client.send_lock:
            client.writer.write(encode_frame(value))
            await client.writer.drain()

    async def reply(
        self,
        client: Client,
        request_id: str,
        success: bool,
        *,
        status: str,
        error: str | None = None,
    ) -> None:
        await self.send(
            client,
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "response",
                "id": request_id,
                "success": success,
                "status": status,
                "error": error,
            },
        )

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client: Client | None = None
        try:
            self._verify_peer(writer)
            raw_hello = await self.read_raw_frame(reader)
            if raw_hello.get("type") == "control":
                await self.handle_control(reader, writer, raw_hello)
                return
            if raw_hello.get("type") == "observe":
                await self.handle_observer(reader, writer, raw_hello)
                return
            hello = validate_client_message(raw_hello)
            if hello["type"] != "hello":
                raise OrchestrationError(
                    "First broker frame must be hello", "invalid_protocol"
                )
            role = hello["role"]
            with connect_broker_database(self.coord) as database:
                row = database.execute(
                    "SELECT auth_token FROM roles WHERE role=?", (role,)
                ).fetchone()
                if row is None or not secrets.compare_digest(
                    row["auth_token"], hello["token"]
                ):
                    raise OrchestrationError(
                        "Worker authentication failed", "unauthorized"
                    )
                if role in self.clients:
                    raise OrchestrationError(
                        "Worker role is already connected", "conflict"
                    )
                database.execute(
                    "UPDATE roles SET connected=1,state='idle',updated_at=? WHERE role=?",
                    (utc_now(), role),
                )
                record_event(database, "worker_connected", role=role, status="idle")
                workflow_state = database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()["value"]
            client = Client(role, reader, writer)
            self.clients[role] = client
            await self.reply(client, hello["id"], True, status="connected")
            await self.maybe_start_workflow()
            if workflow_state != "connecting":
                await self.recover_role(client)
            self.refresh_dashboard()
            while True:
                message = await self.read_frame(reader)
                if message["role"] != role:
                    raise OrchestrationError(
                        "Worker cannot impersonate another role", "forbidden"
                    )
                if not secrets.compare_digest(message["token"], hello["token"]):
                    raise OrchestrationError(
                        "Worker authentication failed", "unauthorized"
                    )
                await self.handle_message(client, message)
        except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionError, OSError):
            pass
        except OrchestrationError as error:
            if client is not None:
                try:
                    await self.reply(
                        client,
                        secrets.token_hex(16),
                        False,
                        status=error.code,
                        error=bounded_message(error),
                    )
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
        finally:
            if client is not None and self.clients.get(client.role) is client:
                self.clients.pop(client.role, None)
                with connect_broker_database(self.coord) as database:
                    database.execute(
                        "UPDATE roles SET connected=0,state='disconnected',updated_at=? WHERE role=?",
                        (utc_now(), client.role),
                    )
                    record_event(
                        database,
                        "worker_disconnected",
                        role=client.role,
                        status="disconnected",
                    )
                self.refresh_dashboard()
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError):
                pass

    async def read_raw_frame(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        prefix = await reader.readexactly(4)
        size = int.from_bytes(prefix, "big")
        if not 1 <= size <= MAX_BROKER_FRAME_BYTES:
            raise OrchestrationError("Broker frame size is invalid", "invalid_protocol")
        try:
            value = json.loads(await reader.readexactly(size))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OrchestrationError(
                "Broker frame is not valid JSON", "invalid_protocol"
            ) from error
        if not isinstance(value, dict):
            raise OrchestrationError(
                "Broker frame must be an object", "invalid_protocol"
            )
        return value

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
            or role not in self.manifest["roles"]
            or action not in {"send", "abort"}
            or delivery not in {None, "steer", "follow-up"}
            or (action == "send" and (not isinstance(body, str) or not body.strip()))
            or (action == "abort" and body is not None)
        ):
            raise OrchestrationError(
                "Control message fields are invalid", "invalid_protocol"
            )
        resumed_round: int | None = None
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
            if command_count >= MAX_RPC_COMMANDS:
                raise OrchestrationError("Broker command registry is full", "rejected")
            if role not in self.clients:
                status = "uncertain"
            else:
                status = "accepted"
                if action == "send":
                    current_round = self.current_round(database)
                    await self.deliver(
                        role,
                        "operator_message",
                        current_round,
                        body.strip(),
                        trigger=True,
                    )
                    workflow_state = database.execute(
                        "SELECT value FROM meta WHERE key='workflow_state'"
                    ).fetchone()["value"]
                    if workflow_state == "needs_attention":
                        set_meta(database, "workflow_state", "active")
                        resumed_round = current_round
                else:
                    await self.send(
                        self.clients[role],
                        {
                            "version": BROKER_PROTOCOL_VERSION,
                            "type": "abort",
                            "id": command_id,
                        },
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
        await self.send_raw(
            writer,
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "response",
                "id": command_id,
                "success": status == "accepted",
                "status": status,
                "duplicate": False,
            },
        )
        self.refresh_dashboard()
        if resumed_round is not None:
            await self.broadcast_workflow("active", resumed_round)

    def current_round(self, database: Any) -> int:
        return int(
            database.execute("SELECT value FROM meta WHERE key='round'").fetchone()[
                "value"
            ]
        )

    def _verify_peer(self, writer: asyncio.StreamWriter) -> None:
        raw_socket = writer.get_extra_info("socket")
        if raw_socket is None:
            raise OrchestrationError("Cannot verify broker peer", "unauthorized")
        peer_uid: int | None = None
        if hasattr(socket, "SO_PEERCRED"):
            credentials = raw_socket.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12
            )
            peer_uid = int.from_bytes(credentials[4:8], byteorder="little")
        elif hasattr(raw_socket, "getpeereid"):
            peer_uid, _ = raw_socket.getpeereid()
        if peer_uid is not None and peer_uid != os.getuid():
            raise OrchestrationError(
                "Broker peer has a different owner", "unauthorized"
            )

    async def handle_message(self, client: Client, message: dict[str, Any]) -> None:
        try:
            if message["type"] == "lifecycle":
                await self.handle_lifecycle(client, message)
            elif message["type"] == "report":
                await self.handle_report(client, message)
            elif message["type"] == "ack":
                await self.handle_delivery_ack(client, message)
            else:
                raise OrchestrationError(
                    "Unsupported worker message", "invalid_protocol"
                )
        finally:
            self.refresh_dashboard()

    async def recover_role(self, client: Client) -> None:
        with connect_broker_database(self.coord) as database:
            assignment = database.execute(
                "SELECT id,round,kind,delivery_id,state FROM assignments "
                "WHERE role=? AND state IN ('delivering','accepted','uncertain') "
                "ORDER BY created_at DESC LIMIT 1",
                (client.role,),
            ).fetchone()
        if assignment is None:
            return
        if assignment["state"] in {"delivering", "uncertain"}:
            with connect_broker_database(self.coord) as database:
                if assignment["state"] == "delivering":
                    database.execute(
                        "UPDATE assignments SET state='uncertain',updated_at=? WHERE id=?",
                        (utc_now(), assignment["id"]),
                    )
                    record_event(
                        database,
                        "assignment_uncertain",
                        role=client.role,
                        round_number=assignment["round"],
                        assignment_id=assignment["id"],
                        delivery_id=assignment["delivery_id"],
                        status="uncertain",
                    )
                database.execute(
                    "UPDATE roles SET state='uncertain',updated_at=? WHERE role=?",
                    (utc_now(), client.role),
                )
                workflow_state = database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()["value"]
                if workflow_state != "uncertain":
                    set_meta(database, "workflow_state", "uncertain")
                    record_event(
                        database,
                        "workflow_uncertain",
                        role=client.role,
                        round_number=assignment["round"],
                        assignment_id=assignment["id"],
                        status="uncertain",
                    )
            await self.broadcast_workflow("uncertain", assignment["round"])
            return
        await self.send(
            client,
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "assignment",
                "id": assignment["delivery_id"],
                "assignment_id": assignment["id"],
                "round": assignment["round"],
                "kind": assignment["kind"],
                "content": self._assignment(client.role, assignment["round"]),
                "trigger": True,
            },
        )

    async def maybe_start_workflow(self) -> None:
        if set(self.clients) != set(self.manifest["roles"]):
            return
        with connect_broker_database(self.coord) as database:
            state = database.execute(
                "SELECT value FROM meta WHERE key='workflow_state'"
            ).fetchone()["value"]
            if state != "connecting":
                return
            set_meta(database, "workflow_state", "initializing")
            record_event(database, "workflow_started", status="initializing")
        for role in self.manifest["roles"]:
            baseline = self._baseline(role)
            await self.deliver(role, "baseline", 1, baseline, trigger=False)
        with connect_broker_database(self.coord) as database:
            set_meta(database, "workflow_state", "active")
            record_event(database, "workflow_active", status="active")
        await self.broadcast_workflow("active", 1)
        await self.assign(
            "implementer", "implementation", 1, self._assignment("implementer", 1)
        )
        if "probe" in self.clients:
            await self.assign("probe", "probe", 1, self._assignment("probe", 1))
        try:
            (self.coord / "startup.json").unlink()
        except FileNotFoundError:
            pass
        self.task_bodies = {"task": "", "context_capsule": ""}

    def _baseline(self, role: str) -> str:
        return render_worker_baseline(
            self.manifest["project"],
            role,
            self.task_bodies["task"],
            self.task_bodies.get("context_capsule", ""),
            self.task_bodies.get(role, ""),
        )

    def _assignment(self, role: str, round_number: int) -> str:
        instructions = {
            "implementer": "Implement and verify the task. Submit a concise implementation report as your final action.",
            "probe": "Investigate the highest-risk assumptions read-only. Submit a concise probe report as your final action.",
            "playwright": "Inspect the current worktree and run the authorized browser checks. Submit a concise Playwright verdict as your final action.",
            "django": "Inspect the current worktree for Django-specific correctness and operational risks. Submit a concise Django verdict as your final action.",
            "reviewer": "Inspect the current worktree and all supplied evidence independently. Submit an approval or changes-requested report as your final action.",
        }
        return (
            f"# Active assignment\n\nRound: {round_number}\n\n{instructions[role]}\n\n"
            "Do not wait, sleep, or poll after reporting; the tool ends this assignment."
        )

    async def assign(
        self, role: str, kind: str, round_number: int, content: str
    ) -> None:
        if role not in self.clients:
            return
        assignment_id = secrets.token_hex(16)
        delivery_id = secrets.token_hex(16)
        now = utc_now()
        with connect_broker_database(self.coord) as database:
            database.execute(
                "INSERT INTO assignments(id,role,round,kind,state,delivery_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    role,
                    round_number,
                    kind,
                    "delivering",
                    delivery_id,
                    now,
                    now,
                ),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=?,state='active',updated_at=? WHERE role=?",
                (assignment_id, now, role),
            )
            record_event(
                database,
                "assignment_created",
                role=role,
                round_number=round_number,
                assignment_id=assignment_id,
                delivery_id=delivery_id,
                status="delivering",
            )
        await self.send(
            self.clients[role],
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "assignment",
                "id": delivery_id,
                "assignment_id": assignment_id,
                "round": round_number,
                "kind": kind,
                "content": content,
                "trigger": True,
            },
        )

    async def deliver(
        self, role: str, kind: str, round_number: int, content: str, *, trigger: bool
    ) -> None:
        if role not in self.clients:
            return
        delivery_id = secrets.token_hex(16)
        await self.send(
            self.clients[role],
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "context",
                "id": delivery_id,
                "round": round_number,
                "kind": kind,
                "content": content,
                "trigger": trigger,
            },
        )
        with connect_broker_database(self.coord) as database:
            record_event(
                database,
                "context_delivered",
                role=role,
                round_number=round_number,
                delivery_id=delivery_id,
                status="delivered",
            )

    async def handle_delivery_ack(
        self, client: Client, message: dict[str, Any]
    ) -> None:
        if message["status"] not in {"accepted", "duplicate", "uncertain"}:
            raise OrchestrationError(
                "Delivery acknowledgement is invalid", "invalid_protocol"
            )
        uncertain_round: int | None = None
        with connect_broker_database(self.coord) as database:
            assignment = database.execute(
                "SELECT id,round FROM assignments WHERE delivery_id=? AND role=?",
                (message["delivery_id"], client.role),
            ).fetchone()
            if assignment is not None:
                state = (
                    "accepted"
                    if message["status"] in {"accepted", "duplicate"}
                    else "uncertain"
                )
                database.execute(
                    "UPDATE assignments SET state=?,updated_at=? WHERE id=?",
                    (state, utc_now(), assignment["id"]),
                )
                record_event(
                    database,
                    "assignment_delivery",
                    role=client.role,
                    round_number=assignment["round"],
                    assignment_id=assignment["id"],
                    delivery_id=message["delivery_id"],
                    status=state,
                )
                if state == "uncertain":
                    uncertain_round = assignment["round"]
                    database.execute(
                        "UPDATE roles SET state='uncertain',updated_at=? WHERE role=?",
                        (utc_now(), client.role),
                    )
                    set_meta(database, "workflow_state", "uncertain")
                    record_event(
                        database,
                        "workflow_uncertain",
                        role=client.role,
                        round_number=uncertain_round,
                        assignment_id=assignment["id"],
                        delivery_id=message["delivery_id"],
                        status="uncertain",
                    )
        if uncertain_round is not None:
            await self.broadcast_workflow("uncertain", uncertain_round)
        await self.reply(client, message["id"], True, status="recorded")

    async def handle_lifecycle(self, client: Client, message: dict[str, Any]) -> None:
        state = message["state"]
        if state not in {"idle", "active", "waiting", "uncertain"}:
            raise OrchestrationError(
                "Worker lifecycle state is invalid", "invalid_protocol"
            )
        usage = message["usage"]
        if usage is not None and not self._valid_usage(usage):
            raise OrchestrationError("Provider usage is invalid", "invalid_protocol")
        now = utc_now()
        with connect_broker_database(self.coord) as database:
            changes = ["state=?", "updated_at=?"]
            values: list[Any] = [state, now]
            if usage is not None:
                fields = {
                    "input_tokens": usage["input"],
                    "output_tokens": usage["output"],
                    "cache_read_tokens": usage["cacheRead"],
                    "cache_write_tokens": usage["cacheWrite"],
                    "reasoning_tokens": usage.get("reasoning"),
                    "cost_total": usage["cost"]["total"],
                    "context_tokens": usage.get("contextTokens"),
                    "context_window": usage.get("contextWindow"),
                    "context_percent": usage.get("contextPercent"),
                }
                for field, value in fields.items():
                    changes.append(f"{field}=?")
                    values.append(value)
            values.append(client.role)
            database.execute(
                f"UPDATE roles SET {','.join(changes)} WHERE role=?", values
            )
            record_event(database, "worker_lifecycle", role=client.role, status=state)
        workflow_transition: str | None = None
        round_number: int | None = None
        if state in {"active", "waiting", "uncertain"}:
            with connect_broker_database(self.coord) as database:
                active_assignment = database.execute(
                    "SELECT active_assignment_id FROM roles WHERE role=?",
                    (client.role,),
                ).fetchone()["active_assignment_id"]
                workflow_state = database.execute(
                    "SELECT value FROM meta WHERE key='workflow_state'"
                ).fetchone()["value"]
                if (
                    state == "uncertain"
                    and active_assignment
                    and workflow_state not in {"ready", "uncertain"}
                ):
                    workflow_transition = "uncertain"
                    round_number = self.current_round(database)
                    set_meta(database, "workflow_state", workflow_transition)
                    record_event(
                        database,
                        "workflow_uncertain",
                        role=client.role,
                        round_number=round_number,
                        assignment_id=active_assignment,
                        status=workflow_transition,
                    )
                elif (
                    state == "waiting"
                    and active_assignment
                    and workflow_state not in {"ready", "uncertain", "needs_attention"}
                ):
                    workflow_transition = "needs_attention"
                    round_number = self.current_round(database)
                    set_meta(database, "workflow_state", workflow_transition)
                    record_event(
                        database,
                        "workflow_needs_attention",
                        role=client.role,
                        round_number=round_number,
                        assignment_id=active_assignment,
                        status=workflow_transition,
                    )
                elif (
                    state == "active"
                    and active_assignment
                    and workflow_state == "needs_attention"
                ):
                    workflow_transition = "active"
                    round_number = self.current_round(database)
                    set_meta(database, "workflow_state", workflow_transition)
                    record_event(
                        database,
                        "workflow_resumed",
                        role=client.role,
                        round_number=round_number,
                        assignment_id=active_assignment,
                        status=workflow_transition,
                    )
        await self.broadcast(
            {
                "version": BROKER_PROTOCOL_VERSION,
                "type": "lifecycle",
                "session": self.manifest["session"],
                "role": client.role,
                "state": state,
            }
        )
        if workflow_transition is not None and round_number is not None:
            await self.broadcast_workflow(workflow_transition, round_number)
        await self.reply(client, message["id"], True, status="recorded")

    def _valid_usage(self, usage: object) -> bool:
        if not isinstance(usage, dict):
            return False
        required = {"input", "output", "cacheRead", "cacheWrite", "cost"}
        optional = {"reasoning", "contextTokens", "contextWindow", "contextPercent"}
        if not required.issubset(usage) or not set(usage).issubset(required | optional):
            return False
        for key in required - {"cost"}:
            if type(usage[key]) is not int or usage[key] < 0:
                return False
        if usage.get("reasoning") is not None and (
            type(usage["reasoning"]) is not int or usage["reasoning"] < 0
        ):
            return False
        cost = usage["cost"]
        if (
            not isinstance(cost, dict)
            or set(cost) != {"total"}
            or not isinstance(cost["total"], (int, float))
            or cost["total"] < 0
        ):
            return False
        for key in ("contextTokens", "contextWindow"):
            if usage.get(key) is not None and (
                type(usage[key]) is not int or usage[key] < 0
            ):
                return False
        if usage.get("contextPercent") is not None and not isinstance(
            usage["contextPercent"], (int, float)
        ):
            return False
        return True

    async def handle_report(self, client: Client, message: dict[str, Any]) -> None:
        report = validate_report(message["report"], client.role)
        assignment_id = message["assignment_id"]
        if not isinstance(assignment_id, str) or not RPC_TOKEN_PATTERN.fullmatch(
            assignment_id
        ):
            raise OrchestrationError("Assignment ID is invalid", "invalid_protocol")
        with connect_broker_database(self.coord) as database:
            assignment = database.execute(
                "SELECT id,round,kind,state FROM assignments WHERE id=? AND role=?",
                (assignment_id, client.role),
            ).fetchone()
            if assignment is None:
                raise OrchestrationError(
                    "Assignment is not active for this role", "conflict"
                )
            existing = database.execute(
                "SELECT id FROM reports WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if existing is not None:
                await self.reply(client, message["id"], True, status="duplicate")
                return
            report_id = secrets.token_hex(16)
            database.execute(
                "INSERT INTO reports(id,assignment_id,role,round,kind,verdict,summary_chars,"
                "changed_path_count,check_count,finding_count,risk_count,limitation_count,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report_id,
                    assignment_id,
                    client.role,
                    assignment["round"],
                    report["kind"],
                    report["verdict"],
                    len(report["summary"]),
                    len(report["changed_paths"]),
                    len(report["checks"]),
                    len(report["findings"]),
                    len(report["risks"]),
                    len(report["limitations"]),
                    utc_now(),
                ),
            )
            database.execute(
                "UPDATE assignments SET state='completed',updated_at=? WHERE id=?",
                (utc_now(), assignment_id),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=NULL,state='idle',updated_at=? WHERE role=?",
                (utc_now(), client.role),
            )
            record_event(
                database,
                "report_accepted",
                role=client.role,
                round_number=assignment["round"],
                assignment_id=assignment_id,
                status=report["verdict"] or "completed",
            )
        report_event = {
            "version": BROKER_PROTOCOL_VERSION,
            "type": "report",
            "session": self.manifest["session"],
            "id": report_id,
            "assignment_id": assignment_id,
            "role": client.role,
            "round": assignment["round"],
            "report": report,
        }
        self.recent_reports.append(report_event)
        if len(self.recent_reports) > MAX_OBSERVER_REPORTS:
            del self.recent_reports[: len(self.recent_reports) - MAX_OBSERVER_REPORTS]
        await self.broadcast(report_event)
        try:
            await self.route_report(client.role, assignment["round"], report)
        except Exception as error:
            with connect_broker_database(self.coord) as database:
                set_meta(database, "workflow_state", "uncertain")
                record_event(
                    database,
                    "workflow_uncertain",
                    role=client.role,
                    round_number=assignment["round"],
                    assignment_id=assignment_id,
                    status="uncertain",
                )
            await self.broadcast_workflow("uncertain", assignment["round"])
            raise OrchestrationError(
                "Report routing became uncertain", "broker_uncertain"
            ) from error
        await self.reply(client, message["id"], True, status="accepted")

    def _run_state_capsule(self, round_number: int) -> str:
        return render_run_state_capsule(self.recent_reports, round_number)

    async def _deliver_run_state(
        self, roles: list[str] | tuple[str, ...], round_number: int
    ) -> None:
        content = self._run_state_capsule(round_number)
        for recipient in dict.fromkeys(roles):
            if recipient in self.clients:
                await self.deliver(
                    recipient,
                    "run_state",
                    round_number,
                    content,
                    trigger=False,
                )

    async def route_report(
        self, role: str, round_number: int, report: dict[str, Any]
    ) -> None:
        if role == "probe":
            await self._deliver_run_state(("implementer", "reviewer"), round_number)
            return
        if role == "implementer":
            specialists = [
                name for name in ("playwright", "django") if name in self.clients
            ]
            await self._deliver_run_state(
                tuple(["reviewer", *specialists]), round_number
            )
            if specialists:
                for specialist in specialists:
                    await self.assign(
                        specialist,
                        specialist,
                        round_number,
                        self._assignment(specialist, round_number),
                    )
            else:
                await self.maybe_assign_reviewer(round_number)
            return
        if role in {"playwright", "django"}:
            await self._deliver_run_state(("implementer", "reviewer"), round_number)
            await self.maybe_assign_reviewer(round_number)
            return
        if role == "reviewer":
            if report["verdict"] == "approved":
                with connect_broker_database(self.coord) as database:
                    set_meta(database, "workflow_state", "ready")
                    record_event(
                        database,
                        "workflow_ready",
                        role=role,
                        round_number=round_number,
                        status="ready",
                    )
                await self.broadcast_workflow("ready", round_number)
                return
            await self._deliver_run_state(("implementer",), round_number)
            next_round = round_number + 1
            with connect_broker_database(self.coord) as database:
                set_meta(database, "round", str(next_round))
                set_meta(database, "workflow_state", "active")
            await self.broadcast_workflow("active", next_round)
            await self.assign(
                "implementer",
                "implementation",
                next_round,
                self._assignment("implementer", next_round),
            )

    async def maybe_assign_reviewer(self, round_number: int) -> None:
        specialists = [
            name for name in ("playwright", "django") if name in self.manifest["roles"]
        ]
        with connect_broker_database(self.coord) as database:
            implementation = database.execute(
                "SELECT 1 FROM reports WHERE role='implementer' AND round=?",
                (round_number,),
            ).fetchone()
            completed = {
                row["role"]
                for row in database.execute(
                    "SELECT role FROM reports WHERE round=? AND role IN ('playwright','django')",
                    (round_number,),
                )
            }
            reviewer_assignment = database.execute(
                "SELECT 1 FROM assignments WHERE role='reviewer' AND round=?",
                (round_number,),
            ).fetchone()
        if (
            implementation is not None
            and set(specialists).issubset(completed)
            and reviewer_assignment is None
        ):
            await self.assign(
                "reviewer",
                "review",
                round_number,
                self._assignment("reviewer", round_number),
            )


def initialize_broker_run(
    coord: Path,
    manifest: dict[str, Any],
    task: str,
    role_tasks: dict[str, str],
    *,
    context_capsule: str = "",
    soft_role_tokens: int = DEFAULT_SOFT_ROLE_TOKENS,
    soft_total_tokens: int = DEFAULT_SOFT_TOTAL_TOKENS,
) -> None:
    from .broker_store import initialize_broker_database

    tokens = {role: secrets.token_hex(16) for role in manifest["roles"]}
    control_token = secrets.token_hex(16)
    initialize_broker_database(
        coord,
        manifest,
        tokens,
        control_token,
        soft_role_tokens=soft_role_tokens,
        soft_total_tokens=soft_total_tokens,
    )
    secure_write(
        coord / "startup.json",
        json.dumps(
            {
                "task": task,
                "context_capsule": context_capsule,
                "role_tasks": role_tasks,
            },
            separators=(",", ":"),
        )
        + "\n",
    )
    for role, token in tokens.items():
        secure_write(coord / f"{role}.token", token + "\n")
    secure_write(coord / "control.token", control_token + "\n")


def _register_broker_signal_handlers(
    loop: asyncio.AbstractEventLoop, broker: Broker
) -> None:
    handlers = [
        (signal.SIGINT, broker.stopping.set),
        (signal.SIGTERM, broker.stopping.set),
    ]
    resize_signal = getattr(signal, "SIGWINCH", None)
    if resize_signal is not None:
        handlers.append((resize_signal, broker.refresh_dashboard))
    for signum, callback in handlers:
        try:
            loop.add_signal_handler(signum, callback)
        except (NotImplementedError, RuntimeError, ValueError):
            # Some event loops/platforms do not expose POSIX signal callbacks.
            # Broker state transitions still drive presentation in that case.
            continue


def broker_command(args: argparse.Namespace) -> int:
    runtime.STATE_ROOT = Path(args.state_root)
    coord = Path(args.coord)
    manifest = load_manifest(coord)
    broker = Broker(coord, manifest)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        _register_broker_signal_handlers(loop, broker)
        await broker.run()

    asyncio.run(run())
    return 0
