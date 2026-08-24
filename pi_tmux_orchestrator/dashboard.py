"""Dependency-free terminal presentation for the broker/status pane."""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .broker_store import public_broker_events, public_broker_snapshot

MAX_DASHBOARD_EVENTS = 8

SEMANTIC_ANSI = {
    "success": "32",
    "active": "36",
    "warning": "33",
    "error": "31",
    "muted": "2;37",
    "heading": "1",
}

_SUCCESS_STATES = {
    "accepted",
    "approved",
    "completed",
    "connected",
    "delivered",
    "idle",
    "pass",
    "passed",
    "ready",
    "recorded",
}
_ACTIVE_STATES = {
    "active",
    "connecting",
    "delivering",
    "initializing",
    "recovering",
    "restarting",
    "started",
    "streaming",
}
_WARNING_STATES = {"needs_attention", "waiting", "warning"}
_ERROR_STATES = {
    "aborted",
    "conflict",
    "disconnected",
    "error",
    "failed",
    "fail",
    "rejected",
    "uncertain",
}


@dataclass(frozen=True)
class Span:
    text: str
    semantic: str = "normal"


Line = list[Span]


def sanitize_terminal_text(value: object, *, fallback: str = "—") -> str:
    """Collapse dynamic text and remove bytes/code points with terminal effects."""
    if value is None:
        return fallback
    cleaned = []
    for character in str(value):
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            cleaned.append(" ")
        else:
            cleaned.append(character)
    text = " ".join("".join(cleaned).split())
    return text or fallback


def state_semantic(state: object) -> str:
    """Map workflow/lifecycle status to the dashboard's five-color vocabulary."""
    normalized = sanitize_terminal_text(state, fallback="unknown").lower()
    if normalized in _SUCCESS_STATES:
        return "success"
    if normalized in _ACTIVE_STATES:
        return "active"
    if normalized in _WARNING_STATES:
        return "warning"
    if normalized in _ERROR_STATES:
        return "error"
    return "muted"


def layout_for(width: int, height: int) -> str:
    if width >= 100 and height >= 22:
        return "full"
    if width >= 64 and height >= 12:
        return "compact"
    return "narrow"


def _display_width(text: str) -> int:
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def _truncate(text: str, width: int, *, unicode: bool) -> str:
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    ellipsis = "…" if unicode else "."
    available = max(0, width - _display_width(ellipsis))
    selected: list[str] = []
    used = 0
    for character in text:
        character_width = (
            0
            if unicodedata.combining(character)
            else 2
            if unicodedata.east_asian_width(character) in {"F", "W"}
            else 1
        )
        if used + character_width > available:
            break
        selected.append(character)
        used += character_width
    while selected and unicodedata.combining(selected[-1]):
        selected.pop()
    return "".join(selected) + ellipsis


def _cell(
    value: object,
    width: int,
    *,
    unicode: bool,
    align: str = "left",
) -> str:
    text = _truncate(sanitize_terminal_text(value), width, unicode=unicode)
    padding = max(0, width - _display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _fit_spans(spans: Line, width: int, *, unicode: bool) -> Line:
    if width <= 0:
        return []
    plain = "".join(span.text for span in spans)
    if _display_width(plain) <= width:
        return spans
    result: Line = []
    remaining = width
    for span in spans:
        if remaining <= 0:
            break
        span_width = _display_width(span.text)
        if span_width <= remaining:
            result.append(span)
            remaining -= span_width
            continue
        result.append(
            Span(_truncate(span.text, remaining, unicode=unicode), span.semantic)
        )
        break
    return result


def _render_line(spans: Line, width: int, *, color: bool, unicode: bool) -> str:
    spans = [
        Span(
            "".join(
                " "
                if unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                else character
                for character in span.text
            ),
            span.semantic,
        )
        for span in spans
    ]
    if not unicode:
        spans = [
            Span(span.text.encode("ascii", "replace").decode("ascii"), span.semantic)
            for span in spans
        ]
    fitted = _fit_spans(spans, width, unicode=unicode)
    if not color:
        return "".join(span.text for span in fitted)
    rendered = []
    for span in fitted:
        code = SEMANTIC_ANSI.get(span.semantic)
        if code:
            rendered.append(f"\x1b[{code}m{span.text}\x1b[0m")
        else:
            rendered.append(span.text)
    return "".join(rendered)


def _line(text: object = "", semantic: str = "normal") -> Line:
    return [Span(sanitize_terminal_text(text, fallback=""), semantic)]


def _format_tokens(value: object) -> str:
    if type(value) is not int or value < 0:
        return "—"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        number = f"{value / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{number}k"
    number = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{number}m"


def _format_role_tokens(role: dict[str, Any]) -> str:
    cumulative = _format_tokens(role.get("total_tokens"))
    latest = role.get("latest_assignment_usage")
    if not isinstance(latest, dict):
        return cumulative
    usage = latest.get("usage")
    if not isinstance(usage, dict):
        return f"{cumulative}/+—"
    assignment = _format_tokens(usage.get("operational_tokens"))
    return f"{cumulative}/+{assignment}"


def _format_context(role: dict[str, Any]) -> str:
    value = role.get("context_percent")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return "—"
    return f"{min(value, 999.9):.1f}%"


def _connection(role: dict[str, Any], *, unicode: bool, compact: bool = False) -> str:
    connected = role.get("connected") is True
    marker = (
        "●" if connected and unicode else "○" if unicode else "+" if connected else "-"
    )
    generation = role.get("generation")
    generation_text = (
        f"g{generation}" if type(generation) is int and generation > 0 else "g?"
    )
    if compact:
        return f"{marker}{generation_text}"
    return f"{marker} {'up' if connected else 'dn'} · {generation_text}"


def _assignment(role: dict[str, Any]) -> str:
    assignment = role.get("assignment")
    if not isinstance(assignment, dict):
        return "—"
    kind = sanitize_terminal_text(assignment.get("kind"))
    round_number = assignment.get("round")
    prefix = (
        f"r{round_number} " if type(round_number) is int and round_number > 0 else ""
    )
    return f"{prefix}{kind}"


def _ordered_roles(
    manifest: dict[str, Any], snapshot: dict[str, Any]
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    states = {
        role.get("role"): role
        for role in snapshot.get("roles", [])
        if isinstance(role, dict) and isinstance(role.get("role"), str)
    }
    values = []
    for name, config in manifest.get("roles", {}).items():
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        state = states.get(
            name,
            {
                "role": name,
                "state": "unknown",
                "connected": False,
                "generation": 1,
                "total_tokens": 0,
            },
        )
        values.append((name, config, state))
    return values


def _header_lines(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    unicode: bool,
    include_project: bool,
) -> list[Line]:
    workflow = snapshot.get("workflow", {})
    state = sanitize_terminal_text(workflow.get("state"), fallback="unknown")
    round_number = workflow.get("round")
    round_text = (
        str(round_number) if type(round_number) is int and round_number > 0 else "—"
    )
    marker = "●" if unicode else "*"
    session = sanitize_terminal_text(manifest.get("session"), fallback="unknown")
    lines: list[Line] = [
        [
            Span("PI TMUX ORCHESTRATOR", "heading"),
            Span("  /  SESSION ", "muted"),
            Span(session, "heading"),
        ]
    ]
    if include_project:
        lines.append(
            [
                Span("BROKER + STATUS", "muted"),
                Span("  ·  PROJECT ", "muted"),
                Span(sanitize_terminal_text(manifest.get("project"))),
            ]
        )
    semantic = state_semantic(state)
    state_line: Line = [
        Span(f"{marker} ", semantic),
        Span(state.upper(), semantic),
        Span("   ROUND ", "muted"),
        Span(round_text, "heading"),
    ]
    usage = snapshot.get("usage", {})
    if usage.get("soft_total_budget_exceeded") is True:
        state_line.extend([Span("   ", "normal"), Span("SOFT RUN BUDGET", "warning")])
    lines.append(state_line)
    return lines


def _transport_line(
    manifest: dict[str, Any], snapshot: dict[str, Any], *, compact: bool
) -> Line:
    transport = sanitize_terminal_text(manifest.get("transport"), fallback="unknown")
    protocol = sanitize_terminal_text(
        manifest.get("coordination"), fallback="broker-v1"
    )
    version = sanitize_terminal_text(manifest.get("protocol_version"), fallback="1")
    tokens = _format_tokens(snapshot.get("usage", {}).get("total_tokens"))
    if compact:
        return [
            Span(f"{transport} / {protocol} v{version}", "muted"),
            Span("  ·  ", "muted"),
            Span(f"{tokens} actual tokens"),
        ]
    return [
        Span("TRANSPORT ", "muted"),
        Span(transport.upper()),
        Span("   PROTOCOL ", "muted"),
        Span(f"{protocol.upper()} / V{version}"),
        Span("   ACTUAL USAGE ", "muted"),
        Span(f"{tokens} TOKENS"),
    ]


def _full_role_lines(
    manifest: dict[str, Any], snapshot: dict[str, Any], width: int, *, unicode: bool
) -> list[Line]:
    columns = {
        "role": 11,
        "link": 9,
        "state": 11,
        "work": 18,
        "think": 7,
        "tokens": 9,
        "context": 7,
    }
    gap = "  "
    fixed = sum(columns.values()) + len(gap) * 7
    columns["model"] = max(8, width - fixed)
    headings = [
        ("ROLE", "role"),
        ("LINK", "link"),
        ("STATE", "state"),
        ("ASSIGNMENT", "work"),
        ("MODEL", "model"),
        ("THINK", "think"),
        ("TOTAL/Δ", "tokens"),
        ("CTX", "context"),
    ]
    header = Span(
        gap.join(
            _cell(label, columns[key], unicode=unicode) for label, key in headings
        ),
        "muted",
    )
    divider_character = "─" if unicode else "-"
    lines: list[Line] = [[header], [Span(divider_character * width, "muted")]]
    for name, config, role in _ordered_roles(manifest, snapshot):
        connection_semantic = "success" if role.get("connected") is True else "error"
        lifecycle_semantic = state_semantic(role.get("state"))
        budget = role.get("soft_budget_exceeded") is True
        token_text = _format_role_tokens(role)
        if budget:
            token_text = f"! {token_text}"
        model = (
            f"{sanitize_terminal_text(config.get('provider'))}/"
            f"{sanitize_terminal_text(config.get('model'))}"
        )
        cells = [
            Span(_cell(name, columns["role"], unicode=unicode)),
            Span(gap),
            Span(
                _cell(
                    _connection(role, unicode=unicode),
                    columns["link"],
                    unicode=unicode,
                ),
                connection_semantic,
            ),
            Span(gap),
            Span(
                _cell(role.get("state"), columns["state"], unicode=unicode),
                lifecycle_semantic,
            ),
            Span(gap),
            Span(_cell(_assignment(role), columns["work"], unicode=unicode)),
            Span(gap),
            Span(_cell(model, columns["model"], unicode=unicode)),
            Span(gap),
            Span(_cell(config.get("thinking"), columns["think"], unicode=unicode)),
            Span(gap),
            Span(
                _cell(
                    token_text,
                    columns["tokens"],
                    unicode=unicode,
                    align="right",
                ),
                "warning" if budget else "normal",
            ),
            Span(gap),
            Span(
                _cell(
                    _format_context(role),
                    columns["context"],
                    unicode=unicode,
                    align="right",
                ),
                "warning"
                if isinstance(role.get("context_percent"), (int, float))
                and role.get("context_percent", 0) >= 80
                else "normal",
            ),
        ]
        lines.append(cells)
    return lines


def _compact_role_lines(
    manifest: dict[str, Any], snapshot: dict[str, Any], width: int, *, unicode: bool
) -> list[Line]:
    role_width = 11
    state_width = 11
    token_width = 8
    context_width = 6
    thinking_width = 7
    fixed = (
        4 + role_width + state_width + token_width + context_width + thinking_width + 6
    )
    model_width = max(6, width - fixed)
    lines: list[Line] = []
    for name, config, role in _ordered_roles(manifest, snapshot):
        connected = role.get("connected") is True
        budget = role.get("soft_budget_exceeded") is True
        model = (
            f"{sanitize_terminal_text(config.get('provider'))}/"
            f"{sanitize_terminal_text(config.get('model'))}"
        )
        token_text = _format_role_tokens(role)
        if budget:
            token_text = f"!{token_text}"
        lines.append(
            [
                Span(
                    _cell(
                        _connection(role, unicode=unicode, compact=True),
                        4,
                        unicode=unicode,
                    ),
                    "success" if connected else "error",
                ),
                Span(_cell(name, role_width, unicode=unicode)),
                Span(" "),
                Span(
                    _cell(role.get("state"), state_width, unicode=unicode),
                    state_semantic(role.get("state")),
                ),
                Span(" "),
                Span(
                    _cell(token_text, token_width, unicode=unicode, align="right"),
                    "warning" if budget else "normal",
                ),
                Span(" "),
                Span(
                    _cell(
                        _format_context(role),
                        context_width,
                        unicode=unicode,
                        align="right",
                    )
                ),
                Span(" "),
                Span(_cell(config.get("thinking"), thinking_width, unicode=unicode)),
                Span("  "),
                Span(_cell(model, model_width, unicode=unicode)),
            ]
        )
    return lines


def _event_lines(events: list[dict[str, Any]], *, unicode: bool) -> list[Line]:
    lines: list[Line] = []
    for event in events[-MAX_DASHBOARD_EVENTS:]:
        if not isinstance(event, dict):
            continue
        sequence = event.get("sequence")
        sequence_text = f"#{sequence:05d}" if type(sequence) is int else "#-----"
        timestamp = sanitize_terminal_text(event.get("timestamp"), fallback="--:--:--")
        time_text = (
            timestamp.split("T", 1)[-1][:8] if "T" in timestamp else timestamp[:8]
        )
        role = sanitize_terminal_text(event.get("role"), fallback="broker")
        name = sanitize_terminal_text(event.get("event"), fallback="event")
        status = sanitize_terminal_text(event.get("status"), fallback="unknown")
        round_number = event.get("round")
        round_text = f"r{round_number}" if type(round_number) is int else ""
        lines.append(
            [
                Span(f"{sequence_text}  {time_text}  ", "muted"),
                Span(_cell(role, 11, unicode=unicode)),
                Span("  "),
                Span(_cell(name, 24, unicode=unicode)),
                Span("  "),
                Span(status, state_semantic(status)),
                Span(f"  {round_text}" if round_text else "", "muted"),
            ]
        )
    return lines


def _guidance_lines(session: str, *, full: bool) -> list[Line]:
    if full:
        return [
            [
                Span("ACTIONS  ", "muted"),
                Span(f"attach: pi-tmux-agents attach {session}"),
                Span("   "),
                Span(f"status: pi-tmux-agents status {session}"),
            ],
            [
                Span("         ", "muted"),
                Span(f"stop: pi-tmux-agents stop {session} --yes"),
                Span("   "),
                Span("tmux: prefix + L return · prefix + z zoom", "muted"),
            ],
        ]
    return [
        [
            Span("COMMANDS  ", "muted"),
            Span(f"attach | status | stop {session} (--yes to stop)"),
        ]
    ]


def _full_layout(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    width: int,
    height: int,
    *,
    unicode: bool,
) -> list[Line]:
    lines = _header_lines(manifest, snapshot, unicode=unicode, include_project=True)
    lines.append(_transport_line(manifest, snapshot, compact=False))
    lines.extend([_line(), _line("ROLES", "heading")])
    lines.extend(_full_role_lines(manifest, snapshot, width, unicode=unicode))
    session = sanitize_terminal_text(manifest.get("session"), fallback="SESSION")
    footer = [_line(), *_guidance_lines(session, full=True)]
    remaining = height - len(lines) - len(footer)
    if remaining >= 3:
        lines.extend([_line(), _line("RECENT METADATA EVENTS", "heading")])
        event_capacity = remaining - 2
        lines.extend(_event_lines(events, unicode=unicode)[-event_capacity:])
    lines.extend(footer)
    return lines


def _compact_layout(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    width: int,
    height: int,
    *,
    unicode: bool,
) -> list[Line]:
    lines = _header_lines(manifest, snapshot, unicode=unicode, include_project=False)
    lines.append(_transport_line(manifest, snapshot, compact=True))
    lines.extend(
        [
            _line(),
            _line("ROLES  LINK/GEN · STATE · TOTAL/Δ · CTX · THINK · MODEL", "muted"),
        ]
    )
    lines.extend(_compact_role_lines(manifest, snapshot, width, unicode=unicode))
    session = sanitize_terminal_text(manifest.get("session"), fallback="SESSION")
    footer = [_line(), *_guidance_lines(session, full=False)]
    remaining = height - len(lines) - len(footer)
    if remaining >= 3:
        lines.extend([_line(), _line("RECENT METADATA", "heading")])
        lines.extend(_event_lines(events, unicode=unicode)[-(remaining - 2) :])
    lines.extend(footer)
    return lines


def _narrow_layout(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    width: int,
    height: int,
    *,
    unicode: bool,
) -> list[Line]:
    header = _header_lines(manifest, snapshot, unicode=unicode, include_project=False)
    header[1].extend(
        [Span("  ·  ", "muted"), *_transport_line(manifest, snapshot, compact=True)]
    )
    lines = header
    roles = _ordered_roles(manifest, snapshot)
    session = sanitize_terminal_text(manifest.get("session"), fallback="SESSION")
    footer = [_line(), *_guidance_lines(session, full=False)]
    available = max(0, height - len(lines) - len(footer))
    detail_rows = available >= len(roles) * 2
    shown = roles if detail_rows else roles[:available]
    if not detail_rows and len(shown) < len(roles) and available > 0:
        shown = roles[: max(0, available - 1)]
    for name, config, role in shown:
        connected = role.get("connected") is True
        marker = (
            "●"
            if connected and unicode
            else "○"
            if unicode
            else "+"
            if connected
            else "-"
        )
        token_text = _format_role_tokens(role)
        if role.get("soft_budget_exceeded") is True:
            token_text = f"!{token_text}"
        lines.append(
            [
                Span(f"{marker} ", "success" if connected else "error"),
                Span(name),
                Span("  "),
                Span(
                    sanitize_terminal_text(role.get("state")),
                    state_semantic(role.get("state")),
                ),
                Span(f"  {token_text}  {_format_context(role)}"),
            ]
        )
        if detail_rows:
            model = (
                f"{sanitize_terminal_text(config.get('provider'))}/"
                f"{sanitize_terminal_text(config.get('model'))}"
            )
            lines.append(
                [
                    Span("  ", "muted"),
                    Span(model),
                    Span(f" · think={sanitize_terminal_text(config.get('thinking'))}"),
                    Span(f" · g{role.get('generation', '?')}", "muted"),
                    Span(f" · {_assignment(role)}", "muted"),
                ]
            )
    if not detail_rows and len(shown) < len(roles) and available > 0:
        lines.append(_line(f"… +{len(roles) - len(shown)} roles", "muted"))
    remaining = height - len(lines) - len(footer)
    if remaining >= 3:
        lines.extend([_line(), _line("RECENT METADATA", "heading")])
        for event in events[-(remaining - 2) :]:
            if not isinstance(event, dict):
                continue
            sequence = event.get("sequence")
            sequence_text = f"#{sequence}" if type(sequence) is int else "#-"
            lines.append(
                [
                    Span(f"{sequence_text} ", "muted"),
                    Span(sanitize_terminal_text(event.get("event"), fallback="event")),
                    Span(" · ", "muted"),
                    Span(
                        sanitize_terminal_text(event.get("status"), fallback="unknown"),
                        state_semantic(event.get("status")),
                    ),
                    Span(" · ", "muted"),
                    Span(sanitize_terminal_text(event.get("role"), fallback="broker")),
                ]
            )
    lines.extend(footer)
    return lines


def render_dashboard(
    manifest: dict[str, Any],
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    width: int,
    height: int,
    color: bool,
    unicode: bool = True,
) -> str:
    """Render one bounded metadata-only dashboard frame."""
    safe_width = max(1, width - 1)
    safe_height = max(1, height)
    layout = layout_for(width, height)
    if layout == "full":
        lines = _full_layout(
            manifest,
            snapshot,
            events,
            safe_width,
            safe_height,
            unicode=unicode,
        )
    elif layout == "compact":
        lines = _compact_layout(
            manifest,
            snapshot,
            events,
            safe_width,
            safe_height,
            unicode=unicode,
        )
    else:
        lines = _narrow_layout(
            manifest,
            snapshot,
            events,
            safe_width,
            safe_height,
            unicode=unicode,
        )
    lines = lines[:safe_height]
    return "\n".join(
        _render_line(line, safe_width, color=color, unicode=unicode) for line in lines
    )


def _supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    return "utf" in encoding.lower()


class BrokerDashboard:
    """Event-driven terminal owner for one broker pane."""

    def __init__(
        self,
        manifest: dict[str, Any],
        *,
        stream: TextIO | None = None,
        environ: dict[str, str] | None = None,
        size_getter: Any = None,
    ) -> None:
        self.manifest = manifest
        self.stream = stream or sys.stdout
        self.environ = os.environ if environ is None else environ
        self.size_getter = size_getter or shutil.get_terminal_size
        term = self.environ.get("TERM", "")
        self.interactive = bool(self.stream.isatty()) and term.lower() != "dumb"
        self.color = self.interactive and "NO_COLOR" not in self.environ
        self.unicode = _supports_unicode(self.stream)
        self._entered = False
        self._last_frame: str | None = None
        self._last_plain_summary: str | None = None
        self._unavailable_displayed = False

    def __enter__(self) -> BrokerDashboard:
        self._entered = True
        if self.interactive:
            self.stream.write("\x1b[?25l")
            self.stream.flush()
        return self

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._entered and self.interactive:
            self.stream.write("\x1b[0m\x1b[?25h")
            self.stream.flush()
        self._entered = False

    def refresh_from_store(self, coord: Path) -> None:
        snapshot = public_broker_snapshot(coord)
        latest = snapshot.get("event_cursor", {}).get("latest", 0)
        if type(latest) is not int:
            latest = 0
        events = public_broker_events(
            coord,
            after=max(0, latest - MAX_DASHBOARD_EVENTS),
            limit=MAX_DASHBOARD_EVENTS,
        )["events"]
        self.refresh(snapshot, events)

    def refresh(self, snapshot: dict[str, Any], events: list[dict[str, Any]]) -> None:
        size = self.size_getter(fallback=(100, 30))
        self._unavailable_displayed = False
        frame = render_dashboard(
            self.manifest,
            snapshot,
            events,
            width=size.columns,
            height=size.lines,
            color=self.color,
            unicode=self.unicode,
        )
        if self.interactive:
            if frame == self._last_frame:
                return
            lines = frame.splitlines()
            output = ["\x1b[H"]
            for index, line in enumerate(lines):
                output.extend(["\x1b[2K", line])
                if index < len(lines) - 1:
                    output.append("\r\n")
            output.extend(["\x1b[J", "\x1b[0m"])
            self.stream.write("".join(output))
            self.stream.flush()
            self._last_frame = frame
            return
        if self._last_frame is None:
            self.stream.write(frame + "\n")
            self.stream.flush()
            self._last_plain_summary = self._plain_summary(
                snapshot, events, size.columns
            )
        else:
            summary = self._plain_summary(snapshot, events, size.columns)
            if summary != self._last_plain_summary:
                self.stream.write(summary + "\n")
                self.stream.flush()
            self._last_plain_summary = summary
        self._last_frame = frame

    def render_unavailable(self) -> None:
        if self._unavailable_displayed:
            return
        self._unavailable_displayed = True
        # The unavailable view replaces the cached frame on screen. Force the
        # next successful read to repaint even when broker metadata is unchanged.
        self._last_frame = None
        self._last_plain_summary = None
        session = sanitize_terminal_text(
            self.manifest.get("session"), fallback="unknown"
        )
        if self.interactive:
            message = (
                "\x1b[H\x1b[2KPI TMUX ORCHESTRATOR  /  SESSION "
                f"{session}\r\n\x1b[2KDashboard metadata temporarily unavailable."
                "\x1b[J\x1b[0m"
            )
        else:
            message = (
                f"PI TMUX ORCHESTRATOR / SESSION {session}\n"
                "Dashboard metadata temporarily unavailable.\n"
            )
        self.stream.write(message)
        self.stream.flush()

    def _plain_summary(
        self,
        snapshot: dict[str, Any],
        events: list[dict[str, Any]],
        width: int,
    ) -> str:
        workflow = snapshot.get("workflow", {})
        roles = snapshot.get("roles", [])
        role_states = ",".join(
            f"{sanitize_terminal_text(role.get('role'))}:"
            f"{sanitize_terminal_text(role.get('state'))}"
            for role in roles
            if isinstance(role, dict)
        )
        connected = sum(
            1
            for role in roles
            if isinstance(role, dict) and role.get("connected") is True
        )
        latest = events[-1].get("sequence") if events else None
        summary = (
            "dashboard update: "
            f"state={sanitize_terminal_text(workflow.get('state'))} "
            f"round={sanitize_terminal_text(workflow.get('round'))} "
            f"connected={connected}/{len(roles)} "
            f"tokens={_format_tokens(snapshot.get('usage', {}).get('total_tokens'))} "
            f"roles={role_states or 'none'} "
            f"event={sanitize_terminal_text(latest, fallback='none')}"
        )
        return _truncate(summary, max(1, width - 1), unicode=self.unicode)
