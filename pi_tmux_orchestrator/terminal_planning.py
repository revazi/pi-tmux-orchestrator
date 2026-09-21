"""Confirmed terminal adapter for the shared Pi dynamic-planning start path."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
from typing import Any

from . import runtime
from .models import CommandResult, OrchestrationError
from .tmux import command_path, read_text_argument

TERMINAL_PROTOCOL_VERSION = 1
TERMINAL_TIMEOUT_SECONDS = 120.0
MAX_RPC_LINE_BYTES = 512 * 1024


def _optional_text(args: Any, name: str, label: str) -> str | None:
    text = getattr(args, name, None)
    file_name = getattr(args, f"{name}_file", None)
    if text is None and file_name is None:
        return None
    return read_text_argument(text, file_name, label)


def _model_overrides(args: Any) -> dict[str, dict[str, str]] | None:
    overrides: dict[str, dict[str, str]] = {}
    for role in ("implementer", "reviewer", "probe", "playwright", "django"):
        value = {
            field: getattr(args, f"{role}_{field}")
            for field in ("provider", "model", "thinking")
            if getattr(args, f"{role}_{field}") is not None
        }
        if value:
            overrides[role] = value
    return overrides or None


def _budget_overrides(args: Any) -> dict[str, Any] | None:
    value: dict[str, Any] = {}
    enforcement = getattr(args, "budget_enforcement", None)
    if enforcement is not None:
        value["enforcement"] = enforcement
    for level, scope, metric, threshold in getattr(args, "budget_override", []):
        value.setdefault(level, {}).setdefault(scope, {})[metric] = threshold
    return value or None


def _worker_skills(args: Any) -> dict[str, list[str]] | None:
    value: dict[str, list[str]] = {}
    for role, path in getattr(args, "worker_skill", []):
        value.setdefault(role, []).append(path)
    return value or None


def _decision_model(args: Any) -> dict[str, str] | None:
    provider = getattr(args, "decision_provider", None)
    model = getattr(args, "decision_model", None)
    thinking = getattr(args, "decision_thinking", None)
    if (provider is None) != (model is None):
        raise OrchestrationError(
            "--decision-provider and --decision-model must be supplied together",
            "invalid_arguments",
        )
    if provider is None:
        if thinking is not None:
            raise OrchestrationError(
                "--decision-thinking requires an exact decision provider/model",
                "invalid_arguments",
            )
        return None
    return {
        "provider": provider,
        "model": model,
        **({"thinking": thinking} if thinking is not None else {}),
    }


def _terminal_input(args: Any, project: Path) -> dict[str, Any]:
    if getattr(args, "custom_role", []) or getattr(args, "role_registry", None):
        raise OrchestrationError(
            "Dynamic terminal planning accepts only exact-project allowlisted custom roles from the configured registry",
            "invalid_arguments",
        )
    task = read_text_argument(args.task, args.task_file, "task")
    context_capsule = _optional_text(args, "context_capsule", "context-capsule")
    input_value: dict[str, Any] = {
        "action": "start",
        "project": str(project),
        "task": task,
        "dynamicPlan": True,
        "previewOnly": bool(args.dry_run),
        "approveProject": bool(args.approve_project),
        "rpcWorkers": bool(args.rpc_workers),
        "withProbe": args.with_probe,
        "withPlaywright": args.with_playwright,
        "withDjangoExpert": args.with_django_expert,
        "probeTask": _optional_text(args, "probe_task", "probe-task"),
        "playwrightTask": _optional_text(args, "playwright_task", "playwright-task"),
        "djangoTask": _optional_text(args, "django_task", "django-task"),
        "renderedContextCapsule": context_capsule,
        "workspaceCapsule": getattr(args, "workspace_capsule", None),
        "workspaceRelevantPaths": getattr(args, "workspace_relevant_path", []),
        "implementationFlow": getattr(args, "implementation_flow", None),
        "forceSpecialists": getattr(args, "force_specialist", []),
        "profile": getattr(args, "profile", None),
        "maxRepairRounds": getattr(args, "max_repair_rounds", None),
        "workerContext": dict(getattr(args, "worker_context", None) or []),
        "workerSkills": _worker_skills(args),
        "budgetOverrides": _budget_overrides(args),
        "modelOverrides": _model_overrides(args),
        "decisionModel": _decision_model(args),
        "projectCustomRoles": getattr(args, "project_custom_roles", None),
        "requiredCustomRoleIds": getattr(args, "project_custom_role", None),
        "session": getattr(args, "session", None),
    }
    return {key: value for key, value in input_value.items() if value is not None}


def _confirmation_value(args: Any, title: object) -> bool:
    if title == "Authorize preflight decision call?":
        return bool(args.authorize_planning)
    if title == "Use static/manual start instead?":
        return bool(args.allow_static_fallback)
    if title == "Child project trust bypass":
        return bool(args.approve_project)
    if title == "Start tmux orchestration?":
        return bool(args.yes)
    return False


def _write_rpc(process: subprocess.Popen[str], value: dict[str, Any]) -> None:
    if process.stdin is None:
        raise OrchestrationError("Dynamic planning RPC input is unavailable")
    process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _read_terminal_result(
    process: subprocess.Popen[str],
    output_path: Path,
    args: Any,
) -> dict[str, Any]:
    if process.stdout is None:
        raise OrchestrationError("Dynamic planning RPC output is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + TERMINAL_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if output_path.exists() and output_path.stat().st_size > 0:
                return json.loads(output_path.read_text(encoding="utf-8"))
            if process.poll() is not None:
                break
            for key, _ in selector.select(timeout=0.25):
                line = key.fileobj.readline()
                if not line:
                    continue
                if len(line.encode("utf-8")) > MAX_RPC_LINE_BYTES:
                    raise OrchestrationError("Dynamic planning RPC frame is oversized")
                try:
                    event = json.loads(line)
                except ValueError as error:
                    raise OrchestrationError(
                        "Dynamic planning RPC emitted invalid JSON"
                    ) from error
                if (
                    event.get("type") == "extension_ui_request"
                    and event.get("method") == "confirm"
                ):
                    _write_rpc(
                        process,
                        {
                            "type": "extension_ui_response",
                            "id": event.get("id"),
                            "confirmed": _confirmation_value(args, event.get("title")),
                        },
                    )
        raise OrchestrationError("Dynamic planning process did not complete safely")
    finally:
        selector.close()


def _run_terminal_planner(
    args: Any, project: Path, request: dict[str, Any]
) -> dict[str, Any]:
    pi = command_path("pi")
    extension = runtime.PACKAGE_ROOT / "extensions" / "tmux-orchestrator.js"
    if not extension.is_file():
        raise OrchestrationError("Bundled dynamic-planning extension is unavailable")
    with tempfile.TemporaryDirectory(prefix="pi-tmux-planning-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        request_path = root / "request.json"
        output_path = root / "result.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        request_path.chmod(0o600)
        output_path.write_text("", encoding="utf-8")
        output_path.chmod(0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "PI_TMUX_ORCHESTRATOR_TERMINAL_REQUEST": str(request_path),
                "PI_TMUX_ORCHESTRATOR_TERMINAL_OUTPUT": str(output_path),
            }
        )
        process = subprocess.Popen(
            [
                pi,
                "--mode",
                "rpc",
                "--no-session",
                "--no-tools",
                "--no-extensions",
                "--extension",
                str(extension),
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--no-approve",
            ],
            cwd=str(project),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            _write_rpc(
                process,
                {
                    "id": "terminal-start",
                    "type": "prompt",
                    "message": "/or-terminal-start",
                },
            )
            return _read_terminal_result(process, output_path, args)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def terminal_dynamic_start(args: Any) -> CommandResult:
    if not args.authorize_planning:
        raise OrchestrationError(
            "--dynamic-plan requires --authorize-planning before the provider call",
            "planning_confirmation_required",
        )
    if not args.dry_run and not args.yes:
        raise OrchestrationError(
            "Launching an accepted dynamic plan requires --yes",
            "start_confirmation_required",
        )
    if args.attach:
        raise OrchestrationError(
            "Dynamic terminal planning does not support --attach; attach after launch",
            "invalid_arguments",
        )
    if args.skip_model_check:
        raise OrchestrationError(
            "Dynamic terminal planning cannot skip model checks",
            "invalid_arguments",
        )
    project = Path(args.project).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise OrchestrationError("Dynamic planning project must be a directory")
    request = {
        "version": TERMINAL_PROTOCOL_VERSION,
        "previewOnly": bool(args.dry_run),
        "input": _terminal_input(args, project),
    }
    result = _run_terminal_planner(args, project, request)
    if (
        not isinstance(result, dict)
        or result.get("version") != TERMINAL_PROTOCOL_VERSION
        or type(result.get("success")) is not bool
    ):
        raise OrchestrationError("Dynamic planning returned an invalid result")
    if not result["success"]:
        error = result.get("error")
        code = (
            error.get("code") if isinstance(error, dict) else "dynamic_planning_failed"
        )
        message = (
            error.get("message")
            if isinstance(error, dict)
            else "Dynamic planning failed before launch"
        )
        raise OrchestrationError(str(message), str(code))
    envelope = result.get("envelope")
    if (
        not isinstance(envelope, dict)
        or envelope.get("command") != "start"
        or envelope.get("success") is not True
        or not isinstance(envelope.get("data"), dict)
    ):
        raise OrchestrationError("Dynamic planning start envelope is invalid")
    return CommandResult(data=envelope["data"])
