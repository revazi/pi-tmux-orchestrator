"""Pi RPC worker process supervision and lifecycle correlation."""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from .constants import (
    BROKER_READ_ONLY_TOOLS,
    MAX_JSON_ITEMS,
    MAX_RPC_COMMANDS,
    MAX_RPC_PROMPT_BYTES,
    RPC_TERMINAL_COMMAND_STATUSES,
    RPC_TOKEN_PATTERN,
)
from .models import OrchestrationError
from .output import bounded_message, human_print
from .rpc_protocol import (
    read_rpc_mailbox_request,
    rpc_acknowledge,
    strict_rpc_reader,
    write_rpc_record,
)
from .rpc_store import (
    deterministic_rpc_token,
    initialize_rpc_registry,
    record_rpc_event,
    rpc_command_map,
    rpc_role_paths,
    save_rpc_registry,
    save_rpc_state,
    transition_rpc_command,
    unlink_private_regular,
)
from .storage import ensure_private_directory, read_regular_file
from .tmux import command_path


def run_rpc_agent(
    coord: Path,
    manifest: dict[str, Any],
    role_name: str,
    role: dict[str, Any],
) -> int:
    paths = rpc_role_paths(coord, role_name, create=True)
    brokered = manifest.get("version") == 3
    if brokered:
        initial_message = None
    else:
        prompt_path = Path(role["prompt_path"])
        prompt_bytes = read_regular_file(
            prompt_path, "role prompt", MAX_RPC_PROMPT_BYTES
        )
        try:
            initial_message = prompt_bytes.decode("utf-8").strip() + (
                "\n\nFollow the attached role instructions and begin."
            )
        except UnicodeDecodeError as error:
            raise OrchestrationError("Role prompt is not valid UTF-8") from error
    ensure_private_directory(Path(role["session_dir"]), parents=True)
    command = [
        command_path("pi"),
        "--mode",
        "rpc",
        "--session-dir",
        role["session_dir"],
        "--name",
        f"{Path(manifest['project']).name} {role_name}",
    ]
    if brokered:
        command.extend(["--session-id", role["session_id"]])
    command.extend(
        [
            "--provider",
            role["provider"],
            "--model",
            role["model"],
            "--thinking",
            role["thinking"],
        ]
    )
    if manifest["approve_project"]:
        command.append("--approve")
    if role.get("tools"):
        command.extend(
            ["--tools", BROKER_READ_ONLY_TOOLS if brokered else role["tools"]]
        )
    elif brokered:
        command.extend(
            ["--tools", "read,bash,edit,write,grep,find,ls,orchestrator_report"]
        )
    if brokered:
        from . import runtime
        from .broker_store import broker_paths
        from .prompts import role_system_prompt
        from .storage import require_regular_file, secure_write

        token_path = coord / f"{role_name}.token"
        require_regular_file(token_path, "worker broker token", nonempty=True)
        token = token_path.read_text(encoding="utf-8").strip()
        system_prompt_path = coord / f"{role_name}.system.md"
        secure_write(
            system_prompt_path,
            role_system_prompt(Path(manifest["project"]), role_name),
        )
        command.extend(
            [
                "--extension",
                str(runtime.WORKER_EXTENSION_PATH),
                "--append-system-prompt",
                str(system_prompt_path),
            ]
        )
    environment = os.environ.copy()
    environment.pop("PI_TMUX_CONTROLLER", None)
    environment.pop("PI_TMUX_CONTROLLER_HOME", None)
    environment["PI_SKIP_VERSION_CHECK"] = "1"
    environment["PI_TELEMETRY"] = "0"
    if brokered:
        environment["PI_TMUX_ORCHESTRATOR_ROLE"] = role_name
        environment["PI_TMUX_ORCHESTRATOR_TOKEN"] = token
        environment["PI_TMUX_ORCHESTRATOR_SOCKET"] = str(broker_paths(coord)["socket"])

    previous_umask = os.umask(0o077)
    try:
        try:
            child = subprocess.Popen(
                command,
                cwd=manifest["project"],
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as error:
            raise OrchestrationError("Cannot start Pi RPC worker") from error
    finally:
        os.umask(previous_umask)
    if child.stdin is None or child.stdout is None or child.stderr is None:
        child.kill()
        child.wait(timeout=3)
        raise OrchestrationError("Cannot create Pi RPC pipes")
    state: dict[str, Any] = {
        "version": 1,
        "role": role_name,
        "pid": child.pid,
        "status": "starting",
        "is_streaming": False,
        "steering_count": 0,
        "follow_up_count": 0,
        "session_id": None,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    try:
        registry = initialize_rpc_registry(coord, paths, role_name, child.pid)
    except BaseException:
        child.terminate()
        child.wait(timeout=3)
        raise

    def persist_state(**changes: Any) -> None:
        state.update(changes)
        state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save_rpc_state(paths["state"], state, role_name)

    records: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=64)
    stdout_thread = threading.Thread(
        target=strict_rpc_reader,
        args=(child.stdout, "stdout", records),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=strict_rpc_reader,
        args=(child.stderr, "stderr", records),
        daemon=True,
    )
    try:
        stdout_thread.start()
        stderr_thread.start()
    except BaseException:
        child.terminate()
        child.wait(timeout=3)
        try:
            record_rpc_event(paths, registry, role_name, "supervisor_failed")
        except OrchestrationError:
            pass
        raise
    pending: dict[str, tuple[str, str, str | None, Path]] = {}
    eof_channels: set[str] = set()
    fatal = False
    run_outcome: str | None = None
    abort_requested = False

    def process_mailbox() -> None:
        for request_path in sorted(paths["inbox"].glob("*.json"))[:MAX_JSON_ITEMS]:
            token = request_path.stem
            if not RPC_TOKEN_PATTERN.fullmatch(token):
                continue
            ack_path = paths["acks"] / f"{token}.json"
            command_name = "prompt"
            delivery_value: str | None = "steer"
            try:
                request = read_rpc_mailbox_request(request_path, token)
                command_name = request["type"]
                delivery_value = request.get("delivery")
                known = rpc_command_map(registry).get(token)
                rpc_id = f"orchestrator-mailbox-{token}"
                if known is not None:
                    if (
                        known["command"] != command_name
                        or known["delivery"] != delivery_value
                    ):
                        rpc_acknowledge(
                            ack_path,
                            token,
                            command_name,
                            False,
                            status="conflict",
                            duplicate=True,
                            event_sequence=known["event_sequence"],
                        )
                    elif rpc_id not in pending:
                        status = known["status"]
                        rpc_acknowledge(
                            ack_path,
                            token,
                            command_name,
                            status not in {"rejected", "uncertain"},
                            status=status,
                            duplicate=True,
                            event_sequence=known["event_sequence"],
                        )
                    continue
                if len(registry["commands"]) >= MAX_RPC_COMMANDS:
                    rpc_acknowledge(
                        ack_path,
                        token,
                        command_name,
                        False,
                        status="rejected",
                    )
                    continue
                record_rpc_event(
                    paths,
                    registry,
                    role_name,
                    "command_received",
                    command_id=token,
                    command=command_name,
                    delivery=delivery_value,
                )
                if request["type"] == "prompt":
                    behavior = "steer" if request["delivery"] == "steer" else "followUp"
                    rpc_value = {
                        "id": rpc_id,
                        "type": "prompt",
                        "message": request["message"],
                        "streamingBehavior": behavior,
                    }
                else:
                    rpc_value = {"id": rpc_id, "type": "abort"}
                try:
                    write_rpc_record(child.stdin, rpc_value)
                except (BrokenPipeError, OSError):
                    event = transition_rpc_command(
                        paths,
                        registry,
                        role_name,
                        token,
                        "uncertain",
                    )
                    rpc_acknowledge(
                        ack_path,
                        token,
                        command_name,
                        False,
                        status="uncertain",
                        event_sequence=event["sequence"],
                    )
                    continue
                pending[rpc_id] = (token, command_name, delivery_value, ack_path)
            except OrchestrationError:
                if not ack_path.exists() and not ack_path.is_symlink():
                    rpc_acknowledge(
                        ack_path,
                        token,
                        command_name,
                        False,
                        status="rejected",
                    )
            finally:
                unlink_private_regular(request_path, "RPC mailbox request")

    def transition_active_commands(status: str) -> None:
        commands = rpc_command_map(registry)
        for command_id in list(registry["active_command_ids"]):
            command = commands[command_id]
            if status == "started" and command["status"] != "accepted":
                continue
            if status != "started" and command["status"] not in {"accepted", "started"}:
                continue
            transition_rpc_command(
                paths,
                registry,
                role_name,
                command_id,
                status,
            )

    try:
        persist_state()
        human_print(
            f"Pi RPC supervisor: {role_name} · {role['provider']}/{role['model']} · "
            f"thinking={role['thinking']}"
        )
        if brokered:
            initial_token = None
            initial_rpc_id = None
            write_rpc_record(
                child.stdin,
                {"id": "orchestrator-state", "type": "get_state"},
            )
        else:
            initial_token = deterministic_rpc_token(
                "initial",
                registry["worker_id"],
                str(registry["generation"]),
            )
            record_rpc_event(
                paths,
                registry,
                role_name,
                "command_received",
                command_id=initial_token,
                command="prompt",
                delivery="steer",
            )
            initial_rpc_id = f"orchestrator-initial-{initial_token}"
            write_rpc_record(
                child.stdin,
                {
                    "id": initial_rpc_id,
                    "type": "prompt",
                    "message": initial_message,
                },
            )
        while True:
            if not brokered:
                process_mailbox()
            try:
                channel, payload = records.get(timeout=0.1)
            except queue.Empty:
                if child.poll() is not None and eof_channels == {"stdout", "stderr"}:
                    break
                continue
            if channel == "stderr":
                if payload:
                    try:
                        stderr_line = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        stderr_line = "non-UTF-8 stderr"
                    human_print(f"[pi stderr] {bounded_message(stderr_line, 400)}")
                continue
            if channel in {"protocol_error", "reader_error"}:
                human_print(f"[rpc error] {bounded_message(payload, 240)}")
                fatal = True
                break
            if channel in {"stdout_eof", "stderr_eof"}:
                eof_channels.add(channel.removesuffix("_eof"))
                if (
                    child.poll() is not None
                    and eof_channels == {"stdout", "stderr"}
                    and records.empty()
                ):
                    break
                continue
            if channel != "stdout" or not payload:
                continue
            try:
                event = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                human_print("[rpc error] invalid JSON record from Pi")
                fatal = True
                break
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                human_print("[rpc error] invalid event from Pi")
                fatal = True
                break
            event_type = event["type"]
            if event_type == "response":
                rpc_id = event.get("id")
                if initial_rpc_id is not None and rpc_id == initial_rpc_id:
                    initial_status = (
                        "accepted" if event.get("success") is True else "rejected"
                    )
                    transition_rpc_command(
                        paths,
                        registry,
                        role_name,
                        initial_token,
                        initial_status,
                    )
                    if initial_status == "rejected":
                        human_print("[rpc error] Pi rejected the initial role prompt")
                        persist_state(status="error", is_streaming=False)
                        fatal = True
                        break
                    write_rpc_record(
                        child.stdin,
                        {"id": "orchestrator-state", "type": "get_state"},
                    )
                elif rpc_id == "orchestrator-state" and event.get("success") is True:
                    data = event.get("data")
                    if isinstance(data, dict):
                        session_id = data.get("sessionId")
                        if not isinstance(session_id, str):
                            session_id = None
                        persist_state(session_id=session_id)
                        registry["session_id"] = session_id
                        if data.get("isStreaming") is True:
                            registry["status"] = "streaming"
                        elif registry["status"] not in {"settled", "error", "exited"}:
                            registry["status"] = "idle"
                        registry["updated_at"] = dt.datetime.now(
                            dt.timezone.utc
                        ).isoformat()
                        save_rpc_registry(paths["registry"], registry, role_name)
                elif isinstance(rpc_id, str) and rpc_id in pending:
                    token, command_name, _delivery, ack_path = pending.pop(rpc_id)
                    accepted = event.get("success") is True
                    transition = transition_rpc_command(
                        paths,
                        registry,
                        role_name,
                        token,
                        "accepted" if accepted else "rejected",
                    )
                    status = transition["status"]
                    if accepted and command_name == "abort":
                        abort_requested = bool(registry["active_command_ids"])
                        transition = transition_rpc_command(
                            paths,
                            registry,
                            role_name,
                            token,
                            "completed",
                        )
                        status = "completed"
                    rpc_acknowledge(
                        ack_path,
                        token,
                        command_name,
                        accepted,
                        status=status,
                        event_sequence=transition["sequence"],
                    )
                continue
            if event_type == "agent_start":
                run_outcome = None
                transition_active_commands("started")
                record_rpc_event(paths, registry, role_name, "agent_started")
                persist_state(status="streaming", is_streaming=True)
                human_print("\n[agent working]")
            elif event_type == "agent_settled":
                outcome = "aborted" if abort_requested else (run_outcome or "completed")
                transition_active_commands(outcome)
                record_rpc_event(
                    paths,
                    registry,
                    role_name,
                    {
                        "completed": "agent_completed",
                        "failed": "agent_failed",
                        "aborted": "agent_aborted",
                    }[outcome],
                )
                abort_requested = False
                run_outcome = None
                persist_state(status="settled", is_streaming=False)
                human_print(f"\n[agent {outcome}]")
            elif event_type == "queue_update":
                steering = event.get("steering")
                follow_up = event.get("followUp")
                steering_count = len(steering) if isinstance(steering, list) else 0
                follow_up_count = len(follow_up) if isinstance(follow_up, list) else 0
                persist_state(
                    steering_count=steering_count,
                    follow_up_count=follow_up_count,
                )
                human_print(
                    f"\n[queue steering={steering_count} follow-up={follow_up_count}]"
                )
            elif event_type == "message_update":
                update = event.get("assistantMessageEvent")
                if isinstance(update, dict) and update.get("type") == "text_delta":
                    delta = update.get("delta")
                    if isinstance(delta, str):
                        print(delta, end="", flush=True)
                elif isinstance(update, dict) and update.get("type") == "error":
                    run_outcome = (
                        "aborted" if update.get("reason") == "aborted" else "failed"
                    )
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    stop_reasons = {
                        message.get("stopReason")
                        for message in messages
                        if isinstance(message, dict)
                        and message.get("role") == "assistant"
                    }
                    if "aborted" in stop_reasons:
                        run_outcome = "aborted"
                    elif "error" in stop_reasons:
                        run_outcome = "failed"
            elif event_type == "auto_retry_end" and event.get("success") is False:
                run_outcome = "failed"
            elif event_type == "tool_execution_start":
                human_print(f"\n[tool {bounded_message(event.get('toolName'), 80)}]")
            elif event_type == "extension_ui_request":
                method = event.get("method")
                request_id = event.get("id")
                if method in {"select", "confirm", "input", "editor"} and isinstance(
                    request_id, str
                ):
                    write_rpc_record(
                        child.stdin,
                        {
                            "type": "extension_ui_response",
                            "id": request_id,
                            "cancelled": True,
                        },
                    )
                    human_print(f"\n[headless UI request cancelled: {method}]")
            elif event_type == "extension_error":
                human_print("\n[extension error]")
    except KeyboardInterrupt:
        try:
            write_rpc_record(child.stdin, {"type": "abort"})
        except (BrokenPipeError, OSError):
            pass
    except (BrokenPipeError, OSError):
        human_print("[rpc error] Pi RPC transport closed unexpectedly")
        fatal = True
    finally:
        for token, command_name, _delivery, ack_path in pending.values():
            if not ack_path.exists() and not ack_path.is_symlink():
                try:
                    command = rpc_command_map(registry).get(token)
                    if (
                        command
                        and command["status"] not in RPC_TERMINAL_COMMAND_STATUSES
                    ):
                        transition = transition_rpc_command(
                            paths,
                            registry,
                            role_name,
                            token,
                            "uncertain",
                        )
                        sequence = transition["sequence"]
                    else:
                        sequence = command["event_sequence"] if command else None
                    rpc_acknowledge(
                        ack_path,
                        token,
                        command_name,
                        False,
                        status="uncertain",
                        event_sequence=sequence,
                    )
                except OrchestrationError:
                    pass
        supervisor_failed = fatal or child.poll() not in {None, 0}
        for command in list(registry["commands"]):
            if command["status"] in RPC_TERMINAL_COMMAND_STATUSES:
                continue
            try:
                transition_rpc_command(
                    paths,
                    registry,
                    role_name,
                    command["id"],
                    (
                        "failed"
                        if supervisor_failed
                        and command["status"] in {"accepted", "started"}
                        else "uncertain"
                    ),
                )
            except OrchestrationError:
                pass
        try:
            record_rpc_event(
                paths,
                registry,
                role_name,
                "supervisor_failed" if supervisor_failed else "supervisor_exited",
            )
        except OrchestrationError:
            pass
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        try:
            persist_state(
                status=(
                    "error" if fatal or child.returncode not in {None, 0} else "exited"
                ),
                is_streaming=False,
                steering_count=0,
                follow_up_count=0,
            )
        except OrchestrationError:
            pass
    if fatal:
        return 1
    return child.returncode or 0
