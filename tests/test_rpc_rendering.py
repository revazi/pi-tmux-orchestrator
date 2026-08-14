from __future__ import annotations

import unittest

from pi_tmux_orchestrator.rpc_supervisor import (
    MAX_RPC_DISPLAY_CHARS,
    _bounded_rpc_display,
    _rpc_json_display,
    _rpc_tool_result_display,
)


class RpcPaneRenderingTests(unittest.TestCase):
    def test_tool_inputs_are_pretty_printed_without_terminal_control_bytes(
        self,
    ) -> None:
        rendered = _rpc_json_display(
            {"path": "src/example.py", "pattern": "PRIVATE\x1b[2J_CANARY"}
        )
        self.assertIn('"path": "src/example.py"', rendered)
        self.assertIn(r"PRIVATE\u001b[2J_CANARY", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_text_tool_results_preserve_visible_output(self) -> None:
        rendered = _rpc_tool_result_display(
            {
                "content": [
                    {"type": "text", "text": "line one\nline two"},
                    {"type": "image", "data": "not-rendered"},
                ],
                "details": {"private": "details are not preferred over text"},
            }
        )
        self.assertEqual(rendered, "line one\nline two\n[image result]")
        self.assertNotIn("details are not preferred", rendered)

    def test_non_text_tool_results_fall_back_to_details(self) -> None:
        rendered = _rpc_tool_result_display(
            {"content": [], "details": {"status": "passed", "count": 2}}
        )
        self.assertIn('"status": "passed"', rendered)
        self.assertIn('"count": 2', rendered)

    def test_rpc_pane_output_is_bounded(self) -> None:
        rendered = _bounded_rpc_display("x" * (MAX_RPC_DISPLAY_CHARS + 100))
        self.assertLessEqual(len(rendered), MAX_RPC_DISPLAY_CHARS + 40)
        self.assertTrue(rendered.endswith("… [RPC pane output truncated]"))


if __name__ == "__main__":
    unittest.main()
