"""Report acceptance, bounded run state, and assignment routing for the broker."""

from __future__ import annotations

import math
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .broker_observers import MAX_OBSERVER_REPORTS
from .broker_store import (
    connect_broker_database,
    public_specialist_activations,
    record_event,
    record_specialist_activation,
    retained_forced_specialists,
    retained_implementation_flow,
    set_meta,
    utc_now,
)
from .constants import BROKER_PROTOCOL_VERSION, RPC_TOKEN_PATTERN
from .context_capsules import render_run_state_capsule
from .models import OrchestrationError
from .protocol import validate_report
from .specialist_activation import decide_specialist

if TYPE_CHECKING:
    from .broker import Client


class BrokerWorkflowSupport:
    """Workflow operations mixed into the existing single-writer broker.

    The host owns transport, assignment delivery, lifecycle/recovery, and the
    in-memory collections below. Report acceptance commits metadata before
    routing; report and capsule bodies remain in memory, never in the database.
    """

    coord: Path
    manifest: dict[str, Any]
    clients: dict[str, Client]
    recent_reports: list[dict[str, Any]]
    latest_reports: dict[str, dict[str, Any]]
    role_run_state: dict[str, str]
    pending_run_state: dict[str, int]

    def _valid_usage(
        self,
        usage: object,
        *,
        require_provider_calls: bool = False,
        allow_peak_context: bool = False,
    ) -> bool:
        if not isinstance(usage, dict):
            return False
        required = {"input", "output", "cacheRead", "cacheWrite", "cost"}
        if require_provider_calls:
            required.add("providerCalls")
        optional = {
            "providerCalls",
            "reasoning",
            "contextTokens",
            "contextWindow",
            "contextPercent",
        }
        if allow_peak_context:
            optional.add("peakContextTokens")
        if not required.issubset(usage) or not set(usage).issubset(required | optional):
            return False
        for key in required - {"cost"}:
            if type(usage[key]) is not int or usage[key] < 0:
                return False
        if usage.get("providerCalls") is not None and (
            type(usage["providerCalls"]) is not int or usage["providerCalls"] < 0
        ):
            return False
        if usage.get("reasoning") is not None and (
            type(usage["reasoning"]) is not int or usage["reasoning"] < 0
        ):
            return False
        cost = usage["cost"]
        if (
            not isinstance(cost, dict)
            or set(cost) != {"total"}
            or type(cost["total"]) not in {int, float}
            or not math.isfinite(cost["total"])
            or cost["total"] < 0
        ):
            return False
        for key in ("contextTokens", "contextWindow", "peakContextTokens"):
            if usage.get(key) is not None and (
                type(usage[key]) is not int or usage[key] < 0
            ):
                return False
        percent = usage.get("contextPercent")
        if percent is not None and (
            type(percent) not in {int, float}
            or not math.isfinite(percent)
            or percent < 0
        ):
            return False
        return True

    def _valid_report_usage(self, usage: object) -> bool:
        return (
            isinstance(usage, dict)
            and set(usage) == {"cumulative", "assignment"}
            and self._valid_usage(usage["cumulative"], require_provider_calls=True)
            and self._valid_usage(
                usage["assignment"],
                require_provider_calls=True,
                allow_peak_context=True,
            )
        )

    async def handle_report(self, client: Client, message: dict[str, Any]) -> None:
        report = validate_report(message["report"], client.role)
        report_usage = message.get("usage")
        if report_usage is not None and not self._valid_report_usage(report_usage):
            raise OrchestrationError(
                "Assignment provider usage is invalid", "invalid_protocol"
            )
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
            if assignment["kind"] != report["kind"]:
                raise OrchestrationError(
                    "Report kind does not match the active assignment", "forbidden"
                )
            existing = database.execute(
                "SELECT id FROM reports WHERE assignment_id=?", (assignment_id,)
            ).fetchone()
            if existing is not None:
                await self.reply(client, message["id"], True, status="duplicate")
                return
            report_id = secrets.token_hex(16)
            assignment_usage = (
                report_usage["assignment"] if report_usage is not None else {}
            )
            database.execute(
                "INSERT INTO reports(id,assignment_id,role,round,kind,verdict,summary_chars,"
                "changed_path_count,check_count,finding_count,risk_count,limitation_count,"
                "provider_calls,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,"
                "reasoning_tokens,cost_total,context_tokens,context_window,context_percent,"
                "peak_context_tokens,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    assignment_usage.get("providerCalls"),
                    assignment_usage.get("input"),
                    assignment_usage.get("output"),
                    assignment_usage.get("cacheRead"),
                    assignment_usage.get("cacheWrite"),
                    assignment_usage.get("reasoning"),
                    assignment_usage.get("cost", {}).get("total"),
                    assignment_usage.get("contextTokens"),
                    assignment_usage.get("contextWindow"),
                    assignment_usage.get("contextPercent"),
                    assignment_usage.get("peakContextTokens"),
                    utc_now(),
                ),
            )
            if report_usage is not None:
                cumulative = report_usage["cumulative"]
                database.execute(
                    "UPDATE roles SET provider_calls=?,input_tokens=?,output_tokens=?,"
                    "cache_read_tokens=?,cache_write_tokens=?,reasoning_tokens=?,cost_total=?,"
                    "context_tokens=?,context_window=?,context_percent=? WHERE role=?",
                    (
                        cumulative["providerCalls"],
                        cumulative["input"],
                        cumulative["output"],
                        cumulative["cacheRead"],
                        cumulative["cacheWrite"],
                        cumulative.get("reasoning"),
                        cumulative["cost"]["total"],
                        cumulative.get("contextTokens"),
                        cumulative.get("contextWindow"),
                        cumulative.get("contextPercent"),
                        client.role,
                    ),
                )
            database.execute(
                "UPDATE assignments SET state='completed',updated_at=? WHERE id=?",
                (utc_now(), assignment_id),
            )
            database.execute(
                "UPDATE roles SET active_assignment_id=NULL,state='idle',activity=NULL,"
                "activity_at=NULL,updated_at=? WHERE role=?",
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
            "usage": report_usage["assignment"] if report_usage is not None else None,
        }
        self._remember_report(report_event)
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

    def _remember_report(self, report_event: dict[str, Any]) -> None:
        role = report_event["role"]
        current = self.latest_reports.get(role)
        if current is None or report_event["round"] >= current["round"]:
            self.latest_reports[role] = report_event
        self.recent_reports.append(report_event)
        if len(self.recent_reports) > MAX_OBSERVER_REPORTS:
            del self.recent_reports[: len(self.recent_reports) - MAX_OBSERVER_REPORTS]

    def _run_state_capsule(self, round_number: int) -> str:
        with connect_broker_database(self.coord, readonly=True) as database:
            activations = public_specialist_activations(
                database, round_number=round_number
            )
        return render_run_state_capsule(
            list(self.latest_reports.values()),
            round_number,
            specialist_activations=activations,
        )

    def _role_has_active_assignment(self, role: str) -> bool:
        with connect_broker_database(self.coord, readonly=True) as database:
            row = database.execute(
                "SELECT active_assignment_id FROM roles WHERE role=?", (role,)
            ).fetchone()
        return row is not None and row["active_assignment_id"] is not None

    def _materialize_pending_run_state(
        self, role: str, round_number: int
    ) -> str | None:
        if role in self.pending_run_state:
            self.pending_run_state.pop(role, None)
            self.role_run_state[role] = self._run_state_capsule(round_number)
        return self.role_run_state.get(role)

    async def _flush_pending_run_state(self, role: str, round_number: int) -> None:
        if role not in self.pending_run_state:
            return
        content = self._materialize_pending_run_state(role, round_number)
        if content is not None:
            await self.deliver(role, "run_state", round_number, content, trigger=False)

    async def _deliver_run_state(
        self, roles: list[str] | tuple[str, ...], round_number: int
    ) -> None:
        content = self._run_state_capsule(round_number)
        for recipient in dict.fromkeys(roles):
            if recipient not in self.clients:
                continue
            if recipient in self.pending_run_state or self._role_has_active_assignment(
                recipient
            ):
                self.pending_run_state[recipient] = round_number
                continue
            self.role_run_state[recipient] = content
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
            await self.maybe_assign_reviewer(round_number)
            return
        if role == "implementer" and report["kind"] == "plan":
            await self._deliver_run_state(("implementer",), round_number)
            with connect_broker_database(self.coord, readonly=True) as database:
                implementation_flow = retained_implementation_flow(database)
            if implementation_flow == "phased":
                await self.assign(
                    "implementer",
                    "implementation",
                    round_number,
                    self._assignment("implementer", round_number, "implementation"),
                )
            return
        if role == "implementer":
            configured = [
                name
                for name in ("probe", "playwright", "django")
                if name in self.manifest["roles"]
            ]
            activated: list[str] = []
            with connect_broker_database(self.coord) as database:
                existing = {
                    value["role"]: value
                    for value in public_specialist_activations(
                        database, round_number=round_number
                    )
                }
                forced = retained_forced_specialists(
                    database, set(self.manifest["roles"])
                )
                for specialist in configured:
                    if specialist in existing:
                        continue
                    decision = decide_specialist(
                        specialist,
                        report["changed_paths"],
                        forced=specialist in forced,
                    )
                    record_specialist_activation(
                        database,
                        round_number=round_number,
                        **decision,
                    )
                    if decision["decision"] == "run":
                        activated.append(specialist)
            await self._deliver_run_state(tuple(["reviewer", *activated]), round_number)
            for specialist in activated:
                await self.assign(
                    specialist,
                    specialist,
                    round_number,
                    self._assignment(specialist, round_number),
                )
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
            name
            for name in ("probe", "playwright", "django")
            if name in self.manifest["roles"]
        ]
        with connect_broker_database(self.coord) as database:
            implementation = database.execute(
                "SELECT 1 FROM reports WHERE role='implementer' AND round=? "
                "AND kind='implementation'",
                (round_number,),
            ).fetchone()
            completed = {
                row["role"]
                for row in database.execute(
                    "SELECT role FROM reports WHERE round=? "
                    "AND role IN ('probe','playwright','django')",
                    (round_number,),
                )
            }
            activations = {
                value["role"]: value
                for value in public_specialist_activations(
                    database, round_number=round_number
                )
            }
            reviewer_assignment = database.execute(
                "SELECT 1 FROM assignments WHERE role='reviewer' AND round=?",
                (round_number,),
            ).fetchone()
        specialists_ready = all(
            specialist in activations
            and (
                activations[specialist]["decision"] == "skipped"
                or specialist in completed
            )
            for specialist in specialists
        )
        if (
            implementation is not None
            and specialists_ready
            and reviewer_assignment is None
        ):
            await self.assign(
                "reviewer",
                "review",
                round_number,
                self._assignment("reviewer", round_number),
            )
