from __future__ import annotations

import copy
import io
import os
import unittest

from pi_tmux_orchestrator.dashboard import (
    BrokerDashboard,
    SEMANTIC_ANSI,
    layout_for,
    render_dashboard,
    sanitize_terminal_text,
    state_semantic,
)


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool, encoding: str = "utf-8") -> None:
        super().__init__()
        self.tty = tty
        self.output_encoding = encoding

    @property
    def encoding(self) -> str:
        return self.output_encoding

    def isatty(self) -> bool:
        return self.tty


class DashboardFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "session": "pi-dashboard-test",
            "project": "/work/example",
            "transport": "tui",
            "coordination": "broker-v1",
            "protocol_version": 1,
            "roles": {
                "implementer": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "thinking": "high",
                },
                "reviewer": {
                    "provider": "google",
                    "model": "gemini-3.1-pro-preview",
                    "thinking": "medium",
                },
            },
        }
        self.snapshot = {
            "workflow": {"state": "active", "round": 2},
            "usage": {
                "total_tokens": 12_345,
                "soft_total_budget_exceeded": False,
            },
            "roles": [
                {
                    "role": "implementer",
                    "state": "active",
                    "connected": True,
                    "generation": 2,
                    "total_tokens": 12_000,
                    "soft_budget_exceeded": False,
                    "context_percent": 47.2,
                    "latest_assignment_usage": {
                        "assignment_id": "a" * 32,
                        "round": 1,
                        "kind": "implementation",
                        "usage": {"operational_tokens": 1_000},
                    },
                    "assignment": {
                        "kind": "implementation",
                        "round": 2,
                        "state": "accepted",
                    },
                },
                {
                    "role": "reviewer",
                    "state": "waiting",
                    "connected": True,
                    "generation": 1,
                    "total_tokens": 345,
                    "soft_budget_exceeded": True,
                    "context_percent": 82.5,
                    "latest_assignment_usage": {
                        "assignment_id": "b" * 32,
                        "round": 1,
                        "kind": "review",
                        "usage": {"operational_tokens": 345},
                    },
                    "assignment": {"kind": "review", "round": 2, "state": "accepted"},
                },
            ],
            "event_cursor": {"latest": 7},
        }
        self.events = [
            {
                "sequence": 7,
                "timestamp": "2026-08-15T12:34:56+00:00",
                "event": "worker_lifecycle",
                "role": "reviewer",
                "round": 2,
                "status": "waiting",
            }
        ]


class DashboardRenderingTests(DashboardFixture):
    def test_repair_limit_is_visibly_incomplete(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["workflow"].update(
            {
                "state": "needs_attention",
                "continuation": {"pause_reason": "repair_round_limit"},
            }
        )
        rendered = render_dashboard(
            self.manifest, snapshot, self.events, width=180, height=30, color=False
        )
        self.assertIn("REPAIR LIMIT · INCOMPLETE", rendered)
        self.assertIn("NEEDS_ATTENTION", rendered)

    def test_full_layout_has_deliberate_visual_hierarchy_and_role_metadata(
        self,
    ) -> None:
        rendered = render_dashboard(
            self.manifest,
            self.snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
        )
        self.assertTrue(rendered.startswith("PI TMUX ORCHESTRATOR  /  SESSION"))
        self.assertLess(rendered.index("● ACTIVE   ROUND 2"), rendered.index("ROLES"))
        self.assertLess(
            rendered.index("ROLES"), rendered.index("RECENT METADATA EVENTS")
        )
        self.assertLess(
            rendered.index("RECENT METADATA EVENTS"), rendered.index("ACTIONS")
        )
        self.assertIn("NOW", rendered)
        self.assertIn("→", rendered)
        self.assertIn("⚡ implementer", rendered)
        self.assertIn("👀 reviewer waiting", rendered)
        self.assertIn("TRANSPORT TUI", rendered)
        self.assertIn("PROTOCOL BROKER-V1 / V1", rendered)
        self.assertIn("anthropic/claude-sonnet-4-6", rendered)
        self.assertIn("google/gemini-3.1-pro-preview", rendered)
        self.assertIn("r2 implementation", rendered)
        self.assertIn("● up · g2", rendered)
        self.assertIn("TOTAL/Δ", rendered)
        self.assertIn("12k/+1k", rendered)
        self.assertIn("47.2%", rendered)
        self.assertIn("! 345", rendered)
        self.assertIn("#00007", rendered)
        self.assertIn("💫 worker_lifecycle", rendered)
        self.assertIn("worker_lifecycle", rendered)
        self.assertIn("pi-tmux-agents stop pi-dashboard-test --yes", rendered)
        self.assertIn("prefix + L return", rendered)

    def test_dashboard_uses_manifest_launch_models_not_snapshot_claims(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["roles"][0]["provider"] = "openai"
        snapshot["roles"][0]["model"] = "gpt-5.6"
        rendered = render_dashboard(
            self.manifest,
            snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
        )
        self.assertIn("anthropic/claude-sonnet-4-6", rendered)
        self.assertIn("google/gemini-3.1-pro-preview", rendered)
        self.assertNotIn("gpt-5.6", rendered)
        self.assertNotIn("runtime_identity", rendered)

    def test_now_flow_shows_active_to_waiting_handoff_without_bodies(self) -> None:
        rendered = render_dashboard(
            self.manifest,
            self.snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
        )
        self.assertIn("NOW", rendered)
        self.assertIn("⚡ implementer", rendered)
        self.assertIn("→", rendered)
        self.assertIn("👀 reviewer waiting", rendered)
        compact = render_dashboard(
            self.manifest,
            self.snapshot,
            self.events,
            width=80,
            height=18,
            color=False,
        )
        self.assertIn("NOW", compact)
        self.assertNotIn("a" * 32, rendered)
        self.assertNotIn("PRIVATE_", rendered)

    def test_live_worker_activity_pulses_in_every_layout(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["roles"][0].update({"activity": "streaming", "activity_sequence": 1})
        first = []
        for width, height in ((180, 30), (80, 18), (45, 14)):
            with self.subTest(width=width):
                rendered = render_dashboard(
                    self.manifest,
                    snapshot,
                    self.events,
                    width=width,
                    height=height,
                    color=False,
                )
                self.assertIn("streaming", rendered)
                first.append(rendered)
        snapshot["roles"][0]["activity_sequence"] = 2
        second = render_dashboard(
            self.manifest,
            snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
        )
        self.assertIn("streaming", second)
        self.assertNotEqual(first[0], second)

    def test_live_progress_bar_moves_with_activity_sequence(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["roles"][0].update({"activity": "streaming", "activity_sequence": 0})
        frames = []
        for sequence in range(8):
            snapshot["roles"][0]["activity_sequence"] = sequence
            rendered = render_dashboard(
                self.manifest,
                snapshot,
                self.events,
                width=180,
                height=30,
                color=False,
            )
            self.assertIn("streaming", rendered)
            self.assertRegex(rendered, r"LIVE [▰▱]{12}")
            self.assertIn("NOW", rendered)
            frames.append(rendered)
        self.assertGreaterEqual(len(set(frames)), 6)
        ascii_frame = render_dashboard(
            self.manifest,
            snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
            unicode=False,
        )
        self.assertRegex(ascii_frame, r"LIVE [#-]{12}")
        self.assertNotIn("▰", ascii_frame)
        idle = render_dashboard(
            self.manifest,
            self.snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
        )
        self.assertIn("LIVE", idle)
        self.assertNotIn("▰", idle.split("NOW", 1)[0])
        self.assertNotIn("PRIVATE_", "\n".join(frames))

    def test_assignment_guardrail_markers_are_visible_in_every_layout(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["roles"][0]["assignment_guardrails"] = [
            {
                "assignment_id": "c" * 32,
                "level": "hard",
                "metric": "provider_calls",
                "observed": 6,
                "threshold": 6,
            }
        ]
        for width, height in ((180, 30), (80, 18), (45, 14)):
            with self.subTest(width=width):
                rendered = render_dashboard(
                    self.manifest,
                    snapshot,
                    self.events,
                    width=width,
                    height=height,
                    color=False,
                )
                self.assertIn("G!", rendered)
                self.assertNotIn("c" * 32, rendered)

    def test_state_and_color_semantics_are_bounded_and_consistent(self) -> None:
        expectations = {
            "ready": "success",
            "active": "active",
            "recovering": "active",
            "restarting": "active",
            "needs_attention": "warning",
            "uncertain": "error",
            "starting": "muted",
            "unrecognized": "muted",
        }
        for state, semantic in expectations.items():
            with self.subTest(state=state):
                self.assertEqual(state_semantic(state), semantic)
        self.assertEqual(
            set(SEMANTIC_ANSI),
            {"success", "active", "warning", "error", "muted", "heading"},
        )
        rendered = render_dashboard(
            self.manifest,
            self.snapshot,
            self.events,
            width=180,
            height=30,
            color=True,
        )
        self.assertIn("\x1b[36mACTIVE\x1b[0m", rendered)
        self.assertIn("\x1b[33mwaiting", rendered)
        self.assertIn("\x1b[32m● up · g2", rendered)

    def test_full_compact_and_narrow_layouts_are_height_bounded(self) -> None:
        cases = [
            (140, 26, "full", "RECENT METADATA EVENTS"),
            (80, 18, "compact", "RECENT METADATA"),
            (45, 14, "narrow", "think=high"),
            (32, 5, "narrow", "+2 roles"),
            (20, 3, "narrow", "PI TMUX"),
        ]
        for width, height, expected_layout, marker in cases:
            with self.subTest(width=width, height=height):
                self.assertEqual(layout_for(width, height), expected_layout)
                rendered = render_dashboard(
                    self.manifest,
                    self.snapshot,
                    self.events,
                    width=width,
                    height=height,
                    color=False,
                )
                lines = rendered.splitlines()
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(len(line) <= width - 1 for line in lines))
                self.assertIn(marker, rendered)

    def test_recent_event_rail_is_bounded_to_the_latest_metadata(self) -> None:
        events = [
            {
                "sequence": sequence,
                "timestamp": "2026-08-15T12:34:56+00:00",
                "event": "worker_lifecycle",
                "role": "reviewer",
                "round": 2,
                "status": "waiting",
            }
            for sequence in range(1, 21)
        ]
        rendered = render_dashboard(
            self.manifest,
            self.snapshot,
            events,
            width=180,
            height=40,
            color=False,
        )
        self.assertIn("#00013", rendered)
        self.assertIn("#00020", rendered)
        self.assertNotIn("#00012", rendered)

    def test_dynamic_values_truncate_without_wrapping(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["roles"]["implementer"]["model"] = "model-" + "x" * 200
        rendered = render_dashboard(
            manifest,
            self.snapshot,
            self.events,
            width=100,
            height=22,
            color=False,
        )
        self.assertIn("…", rendered)
        self.assertNotIn("x" * 100, rendered)
        self.assertTrue(all(len(line) <= 99 for line in rendered.splitlines()))

    def test_control_bytes_are_sanitized_and_payload_fields_are_never_rendered(
        self,
    ) -> None:
        manifest = copy.deepcopy(self.manifest)
        manifest["session"] = "safe\x1b[2J\nSESSION"
        manifest["roles"]["implementer"]["model"] = "model\r\nINJECTED"
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["task"] = "PRIVATE_TASK_BODY_CANARY"
        snapshot["prompt"] = "PRIVATE_PROMPT_BODY_CANARY"
        snapshot["report"] = "PRIVATE_REPORT_BODY_CANARY"
        snapshot["roles"][0]["assignment_id"] = "PRIVATE_ASSIGNMENT_ID_CANARY"
        events = copy.deepcopy(self.events)
        events[0]["message"] = "PRIVATE_MESSAGE_BODY_CANARY"
        events[0]["delivery_id"] = "PRIVATE_DELIVERY_ID_CANARY"
        events[0]["event"] = "worker\x1b[31m_lifecycle\nforged"
        rendered = render_dashboard(
            manifest,
            snapshot,
            events,
            width=180,
            height=30,
            color=False,
        )
        self.assertNotIn("\x1b", rendered)
        self.assertIn("safe [2J SESSION", rendered)
        self.assertIn("model INJECTED", rendered)
        self.assertIn("worker [31m_lifecycle", rendered)
        for canary in (
            "PRIVATE_TASK_BODY_CANARY",
            "PRIVATE_PROMPT_BODY_CANARY",
            "PRIVATE_REPORT_BODY_CANARY",
            "PRIVATE_ASSIGNMENT_ID_CANARY",
            "PRIVATE_MESSAGE_BODY_CANARY",
            "PRIVATE_DELIVERY_ID_CANARY",
        ):
            self.assertNotIn(canary, rendered)
        self.assertEqual(sanitize_terminal_text("a\x00\x1b\nb"), "a b")


class DashboardTerminalModeTests(DashboardFixture):
    @staticmethod
    def size_getter(*, fallback: tuple[int, int]) -> os.terminal_size:
        del fallback
        return os.terminal_size((120, 26))

    def test_no_color_tty_repaints_in_place_and_restores_cursor_on_error(self) -> None:
        stream = FakeStream(tty=True)
        dashboard = BrokerDashboard(
            self.manifest,
            stream=stream,
            environ={"TERM": "tmux-256color", "NO_COLOR": "1"},
            size_getter=self.size_getter,
        )
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with dashboard:
                dashboard.refresh(self.snapshot, self.events)
                dashboard.refresh(self.snapshot, self.events)
                raise RuntimeError("synthetic")
        output = stream.getvalue()
        self.assertIn("\x1b[?25l", output)
        self.assertEqual(output.count("\x1b[H"), 1)
        self.assertIn("\x1b[?25h", output)
        self.assertNotIn("\x1b[32m", output)
        self.assertNotIn("\x1b[36m", output)

    def test_presentation_tick_animates_active_without_metadata_reads(self) -> None:
        stream = FakeStream(tty=True)
        dashboard = BrokerDashboard(
            self.manifest,
            stream=stream,
            environ={"TERM": "tmux-256color", "NO_COLOR": "1"},
            size_getter=self.size_getter,
        )
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["roles"][0].pop("activity", None)
        reads = []
        dashboard.refresh_from_store = lambda coord: reads.append(coord)
        with dashboard:
            dashboard.refresh(snapshot, self.events)
            before = stream.getvalue().count("\x1b[H")
            dashboard.presentation_tick()
            dashboard.presentation_tick()
        output = stream.getvalue()
        self.assertGreater(output.count("\x1b[H"), before)
        self.assertEqual(dashboard._presentation_frame, 2)
        self.assertIn("active", output)
        self.assertIn("LIVE", output)
        self.assertEqual(reads, [])

    def test_presentation_tick_animates_loading_but_not_static_or_plain(self) -> None:
        for tty in (True, False):
            with self.subTest(tty=tty):
                stream = FakeStream(tty=tty)
                dashboard = BrokerDashboard(
                    self.manifest,
                    stream=stream,
                    environ={"TERM": "xterm-256color", "NO_COLOR": "1"},
                    size_getter=self.size_getter,
                )
                loading = copy.deepcopy(self.snapshot)
                loading["workflow"]["state"] = "starting"
                for role in loading["roles"]:
                    role["state"] = "disconnected"
                    role.pop("activity", None)
                first_loading = render_dashboard(
                    self.manifest,
                    loading,
                    self.events,
                    width=180,
                    height=30,
                    color=False,
                    presentation_frame=0,
                )
                second_loading = render_dashboard(
                    self.manifest,
                    loading,
                    self.events,
                    width=180,
                    height=30,
                    color=False,
                    presentation_frame=1,
                )
                self.assertNotEqual(first_loading, second_loading)
                self.assertIn("LOADING", first_loading)
                with dashboard:
                    dashboard.refresh(loading, self.events)
                    initial = stream.getvalue().count("\x1b[H")
                    dashboard.presentation_tick()
                    self.assertEqual(
                        stream.getvalue().count("\x1b[H"), initial + int(tty)
                    )
                    static = copy.deepcopy(self.snapshot)
                    static["workflow"]["state"] = "ready"
                    for role in static["roles"]:
                        role["state"] = "waiting"
                    dashboard.refresh(static, self.events)
                    before = stream.getvalue()
                    dashboard.presentation_tick()
                    self.assertEqual(stream.getvalue(), before)

    def test_presentation_frames_move_in_all_adaptive_layouts_and_ascii(self) -> None:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["roles"][0].pop("activity", None)
        for width, height in ((180, 30), (80, 18), (45, 14)):
            first = render_dashboard(
                self.manifest,
                snapshot,
                self.events,
                width=width,
                height=height,
                color=False,
                presentation_frame=0,
            )
            second = render_dashboard(
                self.manifest,
                snapshot,
                self.events,
                width=width,
                height=height,
                color=False,
                presentation_frame=1,
            )
            with self.subTest(width=width):
                self.assertNotEqual(first, second)
                if width == 45:
                    # The active role animates, but a waiting role remains static.
                    self.assertEqual(
                        first.splitlines()[4].split("LIVE", 1)[0],
                        second.splitlines()[4].split("LIVE", 1)[0],
                    )
                self.assertLessEqual(len(second.splitlines()), height)
                self.assertTrue(
                    all(len(line) <= width - 1 for line in second.splitlines())
                )
        ascii_frame = render_dashboard(
            self.manifest,
            snapshot,
            self.events,
            width=180,
            height=30,
            color=False,
            unicode=False,
            presentation_frame=1,
        )
        ascii_frame.encode("ascii")

    def test_size_change_repaints_new_layout_while_same_size_is_suppressed(
        self,
    ) -> None:
        stream = FakeStream(tty=True)
        size = [os.terminal_size((120, 26))]

        def current_size(*, fallback: tuple[int, int]) -> os.terminal_size:
            del fallback
            return size[0]

        dashboard = BrokerDashboard(
            self.manifest,
            stream=stream,
            environ={"TERM": "tmux-256color", "NO_COLOR": "1"},
            size_getter=current_size,
        )
        with dashboard:
            dashboard.refresh(self.snapshot, self.events)
            dashboard.refresh(self.snapshot, self.events)
            size[0] = os.terminal_size((80, 18))
            dashboard.refresh(self.snapshot, self.events)
        output = stream.getvalue()
        self.assertEqual(output.count("\x1b[H"), 2)
        full_frame, compact_frame = output.split("\x1b[H")[1:]
        self.assertIn("RECENT METADATA EVENTS", full_frame)
        self.assertIn("ROLES LINK/GEN", compact_frame)
        self.assertNotIn("ASSIGNMENT", compact_frame)

    def test_unavailable_view_forces_unchanged_frame_to_repaint_on_recovery(
        self,
    ) -> None:
        for tty, term in ((True, "tmux-256color"), (False, "xterm-256color")):
            with self.subTest(tty=tty):
                stream = FakeStream(tty=tty)
                dashboard = BrokerDashboard(
                    self.manifest,
                    stream=stream,
                    environ={"TERM": term},
                    size_getter=self.size_getter,
                )
                with dashboard:
                    dashboard.refresh(self.snapshot, self.events)
                    dashboard.render_unavailable()
                    dashboard.render_unavailable()
                    dashboard.refresh(self.snapshot, self.events)
                output = stream.getvalue()
                self.assertEqual(
                    output.count("Dashboard metadata temporarily unavailable."), 1
                )
                self.assertEqual(output.count("PI TMUX ORCHESTRATOR"), 3)
                if tty:
                    self.assertEqual(output.count("\x1b[H"), 3)
                else:
                    self.assertNotIn("\x1b", output)
                    self.assertNotIn("dashboard update:", output)
                self.assertGreater(
                    output.rfind("PI TMUX ORCHESTRATOR"),
                    output.index("Dashboard metadata temporarily unavailable."),
                )

    def test_non_tty_and_term_dumb_use_plain_bounded_updates(self) -> None:
        for tty, term in ((False, "xterm-256color"), (True, "dumb")):
            with self.subTest(tty=tty, term=term):
                stream = FakeStream(tty=tty)
                dashboard = BrokerDashboard(
                    self.manifest,
                    stream=stream,
                    environ={"TERM": term},
                    size_getter=self.size_getter,
                )
                with dashboard:
                    dashboard.refresh(self.snapshot, self.events)
                    updated = copy.deepcopy(self.snapshot)
                    updated["workflow"]["state"] = "ready"
                    dashboard.refresh(updated, self.events)
                    dashboard.refresh(updated, self.events)
                output = stream.getvalue()
                self.assertNotIn("\x1b", output)
                self.assertEqual(output.count("PI TMUX ORCHESTRATOR"), 1)
                self.assertEqual(output.count("dashboard update:"), 1)
                self.assertIn("state=ready", output)

    def test_ascii_stream_uses_safe_glyph_fallbacks(self) -> None:
        stream = FakeStream(tty=False, encoding="ascii")
        dashboard = BrokerDashboard(
            self.manifest,
            stream=stream,
            environ={"TERM": "dumb"},
            size_getter=self.size_getter,
        )
        with dashboard:
            dashboard.refresh(self.snapshot, self.events)
        output = stream.getvalue()
        self.assertNotIn("●", output)
        self.assertNotIn("—", output)
        output.encode("ascii")
