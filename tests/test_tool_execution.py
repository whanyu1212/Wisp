"""Tests for the coding-layer tool executor's result promotion (issue #74).

The executor promotes a narrow ``exit_code`` scalar from a tool's structured
``ToolResult.data`` onto the event, gated to shell-like tools. Gating agent-side
(rather than in the renderer) is what keeps a custom tool that stashes an
unrelated ``exit_code`` from being styled as a failure.
"""

from __future__ import annotations

from wisp.coding.tool_execution import _promote_exit_code


def test_promote_exit_code_extracts_for_recognized_shell_tool() -> None:
    assert _promote_exit_code("bash", {"exit_code": 2}) == 2
    assert _promote_exit_code("bash", {"exit_code": 0}) == 0


def test_promote_exit_code_ignores_unrecognized_tools() -> None:
    # A custom tool may legitimately carry an integer "exit_code" that has no
    # process-exit meaning; it must not reach the event or drive failure styling.
    assert _promote_exit_code("custom", {"exit_code": 7}) is None
    assert _promote_exit_code("search", {"exit_code": 1}) is None


def test_promote_exit_code_none_when_absent_or_non_int() -> None:
    assert _promote_exit_code("bash", {}) is None
    assert _promote_exit_code("bash", {"exit_code": "boom"}) is None
    assert _promote_exit_code("bash", {"exit_code": None}) is None
