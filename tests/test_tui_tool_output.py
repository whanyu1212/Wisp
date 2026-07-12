"""Unit tests for the tool-aware result renderer (issue #74, PR A).

These exercise the pure rendering functions in isolation — no Textual, no event
pipeline — so each rendering decision (exit-status line, tail vs. head, empty
output, hidden-content metadata) is pinned independently of the widget layer.
"""

from __future__ import annotations

from wisp.tui.tool_output import (
    _ERROR_TAIL_BYTES,
    _ERROR_TAIL_LINES,
    render_error,
    render_generic,
    render_tool_result,
)


def test_render_error_leads_with_exit_code_when_present() -> None:
    rendered = render_error("boom", data={"exit_code": 2})
    assert rendered.splitlines()[0] == "exit 2"
    assert "boom" in rendered


def test_render_error_suppresses_zero_exit_code() -> None:
    # A tool can report is_error with exit 0 (failed for a non-exit reason);
    # "exit 0" reads as success and is noise, so it is dropped.
    rendered = render_error("failed anyway", data={"exit_code": 0})
    assert "exit 0" not in rendered
    assert rendered == "failed anyway"


def test_render_error_omits_exit_line_for_non_int_or_missing() -> None:
    # Synthetic error paths (approval-denied, raised exceptions, custom tools)
    # carry no exit_code; a custom tool may put a non-int there. Neither prints.
    assert render_error("denied", data={}) == "denied"
    assert render_error("weird", data={"exit_code": "boom"}) == "weird"


def test_render_error_shows_the_tail_not_the_head() -> None:
    # The core fix: failures surface at the END of output. A long output must
    # show its last lines, not its first — the pre-#74 behavior showed the head
    # and lost the actual error.
    output = "\n".join(f"line-{i}" for i in range(40))
    rendered = render_error(output, data={})
    assert "line-39" in rendered  # the tail is shown
    assert "line-0" not in rendered  # the head is dropped


def test_render_error_marks_hidden_earlier_lines() -> None:
    # Truncation stays honest: dropped lines are counted in a leading marker,
    # alongside the dropped byte count.
    output = "\n".join(f"line-{i}" for i in range(40))
    rendered = render_error(output, data={})
    hidden = 40 - _ERROR_TAIL_LINES
    assert f"... {hidden} earlier lines" in rendered
    assert "bytes hidden" in rendered


def test_render_error_marks_byte_only_truncation_of_single_line() -> None:
    # A single line longer than the byte budget is clipped from the front so the
    # tail survives; without a byte marker it would look complete. No line was
    # dropped, so the marker reports only bytes.
    output = "x" * (_ERROR_TAIL_BYTES + 500)
    rendered = render_error(output, data={})
    assert "earlier line" not in rendered  # no whole line was dropped
    assert "bytes hidden" in rendered
    assert rendered.rstrip().endswith("x")  # the tail is what survives


def test_render_error_short_output_has_no_hidden_marker() -> None:
    rendered = render_error("only one line", data={})
    assert "earlier" not in rendered
    assert rendered == "only one line"


def test_render_error_empty_output_with_exit_shows_only_exit() -> None:
    # A bare non-zero exit with no output still names the failure.
    assert render_error("", data={"exit_code": 1}) == "exit 1"


def test_render_error_completely_empty_is_no_output() -> None:
    assert render_error("   \n  ", data={}) == "(no output)"


def test_render_generic_matches_widget_default_bounds() -> None:
    # The generic (success / unknown-tool) path must be byte-for-byte identical
    # to the pre-#74 preview; it delegates to the shared helper at its defaults.
    from wisp.tui.widgets import _preview_tool_output

    output = "\n".join(f"line-{i}" for i in range(20))
    assert render_generic(output) == _preview_tool_output(output)


def test_render_tool_result_routes_error_to_error_renderer() -> None:
    output = "\n".join(f"line-{i}" for i in range(40))
    via_dispatch = render_tool_result("bash", {}, output, is_error=True, data={"exit_code": 3})
    assert via_dispatch == render_error(output, data={"exit_code": 3})


def test_render_tool_result_routes_success_to_generic() -> None:
    via_dispatch = render_tool_result("bash", {}, "ok\ndone", is_error=False, data={})
    assert via_dispatch == render_generic("ok\ndone")


def test_render_tool_result_treats_nonzero_exit_as_failure() -> None:
    # A bash command that ran fine but exited nonzero is NOT is_error (that stays
    # a normal model-visible result), yet its card should render as a failure and
    # surface the exit status. The dispatcher routes on exit_code, not is_error.
    output = "\n".join(f"line-{i}" for i in range(40))
    via_dispatch = render_tool_result("bash", {}, output, is_error=False, data={"exit_code": 2})
    assert via_dispatch == render_error(output, data={"exit_code": 2})
    assert "exit 2" in via_dispatch
    assert "line-39" in via_dispatch  # the tail (the failure) is shown


def test_render_tool_result_zero_exit_stays_generic() -> None:
    # exit 0 is success — it must not trigger the failure path.
    via_dispatch = render_tool_result("bash", {}, "ok\ndone", is_error=False, data={"exit_code": 0})
    assert via_dispatch == render_generic("ok\ndone")
