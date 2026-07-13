"""Tests for the coding-layer tool executor's result promotion (issue #74).

The executor promotes a narrow ``exit_code`` scalar from a tool's structured
``ToolResult.data`` onto the event, gated to shell-like tools. Gating agent-side
(rather than in the renderer) is what keeps a custom tool that stashes an
unrelated ``exit_code`` from being styled as a failure.
"""

from __future__ import annotations

from wisp.coding.tool_execution import (
    _promote_before_text,
    _promote_created,
    _promote_exit_code,
)


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


def test_promote_before_text_extracts_for_write_tool() -> None:
    assert _promote_before_text("write", {"before_text": "old\n"}) == "old\n"
    assert _promote_before_text("write", {"before_text": ""}) == ""


def test_promote_before_text_ignores_unrecognized_tools() -> None:
    # A custom tool that stashes a "before_text" must not feed the diff renderer;
    # only the write tool's snapshot is promoted onto the event.
    assert _promote_before_text("edit", {"before_text": "old\n"}) is None
    assert _promote_before_text("custom", {"before_text": "old\n"}) is None


def test_promote_before_text_none_when_absent_or_non_str() -> None:
    assert _promote_before_text("write", {}) is None
    assert _promote_before_text("write", {"before_text": 123}) is None
    assert _promote_before_text("write", {"before_text": None}) is None


def test_promote_created_extracts_for_write_tool() -> None:
    assert _promote_created("write", {"created": True}) is True
    assert _promote_created("write", {"created": False}) is False


def test_promote_created_ignores_unrecognized_tools() -> None:
    # Only write-like tools report a create; anything else is treated as "not a
    # create" so a stray "created" key can't drive create-style rendering.
    assert _promote_created("edit", {"created": True}) is False
    assert _promote_created("custom", {"created": True}) is False


def test_promote_created_false_when_absent_or_non_bool() -> None:
    # Conservative default: a missing or odd value reads as "overwrote", which never
    # fabricates a create-style pure-addition diff.
    assert _promote_created("write", {}) is False
    assert _promote_created("write", {"created": "yes"}) is False
    assert _promote_created("write", {"created": None}) is False
