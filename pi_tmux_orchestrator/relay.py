"""Relay support for Pi tmux orchestration."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import runtime
from .constants import RPC_TRANSPORT
from .models import OrchestrationError
from .commands import coordination_files, send_keys
from .rpc import deterministic_rpc_token, rpc_control_request
from .storage import (
    absolute_path,
    ensure_private_directory,
    load_manifest,
    manifest_transport,
    require_regular_file,
    secure_write,
    validate_coordination_directory,
)
from .tmux import exact_window_target, session_exists, tmux


def relay_send(
    manifest: dict[str, Any],
    role: str,
    message: str,
    *,
    command_id: str | None = None,
) -> bool:
    role_config_value = manifest["roles"].get(role)
    if not role_config_value:
        return False
    try:
        if manifest_transport(manifest) == RPC_TRANSPORT:
            rpc_control_request(
                Path(manifest["coord"]),
                manifest,
                role,
                "prompt",
                message=message,
                delivery="steer",
                command_id=(
                    command_id
                    or deterministic_rpc_token(
                        "relay",
                        str(
                            manifest.get(
                                "session", manifest.get("coord", "orchestration")
                            )
                        ),
                        role,
                        message,
                    )
                ),
                timeout=5.0,
            )
        else:
            send_keys(role_config_value["pane_id"], message)
    except (OrchestrationError, subprocess.CalledProcessError):
        return False
    return True


def seen_path(seen_dir: Path, token: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", token)
    return seen_dir / safe


def mark_seen(seen_dir: Path, token: str) -> None:
    secure_write(seen_path(seen_dir, token), "")


def is_seen(seen_dir: Path, token: str) -> bool:
    path = seen_path(seen_dir, token)
    if not path.exists() and not path.is_symlink():
        return False
    require_regular_file(path, "relay delivery state")
    return True


def report_first_line(path: Path) -> str:
    metadata = require_regular_file(
        path, f"coordination report {path.name}", nonempty=True
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OrchestrationError("Cannot safely inspect coordination report") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise OrchestrationError("Coordination report changed while opening")
        first_line = os.read(descriptor, 257).split(b"\n", 1)[0]
        if len(first_line) > 256:
            raise OrchestrationError("Coordination report first line is too long")
        try:
            return first_line.decode("utf-8").rstrip("\r")
        except UnicodeDecodeError as error:
            raise OrchestrationError(
                "Coordination report first line is not valid UTF-8"
            ) from error
    finally:
        os.close(descriptor)


def ready_report_is_valid(
    marker: Path,
    report: Path,
    allowed_first_lines: frozenset[str] | None = None,
) -> bool:
    try:
        require_regular_file(marker, f"coordination marker {marker.name}")
        require_regular_file(
            report, f"coordination report {report.name}", nonempty=True
        )
        if (
            allowed_first_lines is not None
            and report_first_line(report) not in allowed_first_lines
        ):
            return False
    except OrchestrationError:
        return False
    return True


def deliver_marker(
    manifest: dict[str, Any],
    seen_dir: Path,
    token: str,
    recipients: dict[str, str],
) -> None:
    enabled = {
        role: message
        for role, message in recipients.items()
        if role in manifest["roles"]
    }
    for role, message in enabled.items():
        recipient_token = f"{token}--{role}"
        if is_seen(seen_dir, recipient_token):
            continue
        command_id = deterministic_rpc_token(
            "relay-marker",
            str(manifest.get("session", manifest.get("coord", "orchestration"))),
            token,
            role,
        )
        delivered = (
            relay_send(manifest, role, message, command_id=command_id)
            if manifest_transport(manifest) == RPC_TRANSPORT
            else relay_send(manifest, role, message)
        )
        if delivered:
            mark_seen(seen_dir, recipient_token)
    if enabled and all(is_seen(seen_dir, f"{token}--{role}") for role in enabled):
        mark_seen(seen_dir, token)


def relay_once(coord: Path, manifest: dict[str, Any], seen_dir: Path) -> None:
    coord = validate_coordination_directory(coord)
    seen_dir = ensure_private_directory(seen_dir, parents=True)
    for marker in sorted(coord.glob("handoff-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"handoff-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"handoff-{round_number}.md"
        if not ready_report_is_valid(marker, report):
            continue
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {
                "reviewer": (
                    f"Coordination notice: implementer handoff round {round_number} is ready at "
                    f"{report}. Review it now and write review-{round_number}.md plus "
                    f"review-{round_number}.ready."
                ),
                "playwright": (
                    f"Coordination notice: implementer handoff round {round_number} is ready at "
                    f"{report}. Run the browser test now and write playwright-{round_number}.md "
                    f"plus playwright-{round_number}.ready."
                ),
                "django": (
                    f"Coordination notice: implementer handoff round {round_number} is ready at "
                    f"{report}. Run the Django expert review now and write "
                    f"django-review-{round_number}.md plus django-review-{round_number}.ready."
                ),
            },
        )

    for marker in sorted(coord.glob("playwright-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"playwright-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"playwright-{round_number}.md"
        if not ready_report_is_valid(marker, report, frozenset({"PASS", "FAIL"})):
            continue
        message = (
            f"Coordination notice: Playwright report round {round_number} is ready at "
            f"{report}. Evaluate the evidence and failures."
        )
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {"implementer": message, "reviewer": message},
        )

    for marker in sorted(coord.glob("django-review-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"django-review-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"django-review-{round_number}.md"
        if not ready_report_is_valid(
            marker,
            report,
            frozenset({"ADVISORY_APPROVED", "ISSUES_FOUND"}),
        ):
            continue
        message = (
            f"Coordination notice: Django expert review round {round_number} is ready at "
            f"{report}. Evaluate the findings and best-practice recommendations within "
            "authorized scope."
        )
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {"implementer": message, "reviewer": message},
        )

    for marker in sorted(coord.glob("review-*.ready")):
        token = marker.name
        if is_seen(seen_dir, token):
            continue
        match = re.fullmatch(r"review-(\d+)\.ready", marker.name)
        if not match:
            continue
        round_number = match.group(1)
        report = coord / f"review-{round_number}.md"
        if not ready_report_is_valid(
            marker,
            report,
            frozenset({"APPROVED", "CHANGES_REQUESTED"}),
        ):
            continue
        deliver_marker(
            manifest,
            seen_dir,
            token,
            {
                "implementer": (
                    f"Coordination notice: reviewer response round {round_number} is ready at "
                    f"{report}. Read it now; address CHANGES_REQUESTED or write "
                    "implementation-ready.md if APPROVED."
                )
            },
        )

    probe_marker = coord / "probe.ready"
    if (
        (probe_marker.exists() or probe_marker.is_symlink())
        and not is_seen(seen_dir, probe_marker.name)
        and ready_report_is_valid(probe_marker, coord / "probe.md")
    ):
        message = (
            f"Coordination notice: the independent probe is ready at {coord}/probe.md. "
            "Use valid evidence while preserving its stated limitations."
        )
        deliver_marker(
            manifest,
            seen_dir,
            probe_marker.name,
            {"implementer": message, "reviewer": message},
        )

    ready = coord / "implementation-ready.md"
    if (
        (ready.exists() or ready.is_symlink())
        and not is_seen(seen_dir, ready.name)
        and ready_report_is_valid(ready, ready)
    ):
        deliver_marker(
            manifest,
            seen_dir,
            ready.name,
            {
                "reviewer": (
                    f"Coordination notice: {ready} exists. Confirm the latest round is approved "
                    "and remain available for final questions."
                )
            },
        )


def render_monitor(coord: Path, manifest: dict[str, Any]) -> None:
    session = manifest["session"]
    print("\033[H\033[2J", end="")
    print("Pi + tmux agent orchestration")
    print(f"Session: {session}")
    print(f"Project: {manifest['project']}")
    print(f"Transport: {manifest_transport(manifest)}")
    print(f"Coordination: {coord}\n")
    result = tmux(
        [
            "list-panes",
            "-t",
            exact_window_target(session, manifest["window"]),
            "-F",
            "pane #{pane_index} | #{pane_title} | cmd=#{pane_current_command} | "
            "pid=#{pane_pid} | dead=#{pane_dead} | #{pane_width}x#{pane_height}",
        ],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        print("tmux session ended")
        return
    print(result.stdout.rstrip())
    print("\nCoordination files:")
    files = coordination_files(coord)
    if not files:
        print("  waiting for agent status...")
    for path, metadata in files:
        print(f"  {path.name:<30} {metadata.st_size:>7} bytes")
    print(
        "\nRelay: handoff → reviewer/playwright/django; specialist reports → "
        "implementer/reviewer; review → implementer; probe → both"
    )
    print(f"Attach/switch: pi-tmux-agents attach {session}")
    print(f"Status:        pi-tmux-agents status {session}")
    print(f"Stop:          pi-tmux-agents stop {session} --yes")
    sys.stdout.flush()


def relay_command(args: argparse.Namespace) -> int:
    runtime.STATE_ROOT = Path(args.state_root)
    coord = absolute_path(Path(args.coord))
    manifest = load_manifest(coord)
    seen_dir = ensure_private_directory(coord / ".relay-seen", parents=True)
    try:
        while session_exists(manifest["session"]):
            relay_once(coord, manifest, seen_dir)
            render_monitor(coord, manifest)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0
    return 0
