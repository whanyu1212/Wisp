"""Unit tests for the tool-aware result renderer (issue #74, PR A).

These exercise the pure rendering functions in isolation — no Textual, no event
pipeline — so each rendering decision (exit-status line, tail vs. head, empty
output, hidden-content metadata) is pinned independently of the widget layer.

The renderer receives a promoted ``exit_code: int | None`` scalar, not the raw
``ToolResult.data`` mapping: the executor extracts it agent-side for shell-like
tools only, so a card is never spuriously reddened by an unrelated tool's data.
"""

from __future__ import annotations

import signal

from wisp.tui.tool_output import (
    _ERROR_TAIL_BYTES,
    _ERROR_TAIL_LINES,
    render_error,
    render_generic,
    render_tool_result,
    tool_result_failed,
)


def test_render_error_leads_with_exit_code_when_present() -> None:
    rendered = render_error("boom", exit_code=2)
    assert rendered.splitlines()[0] == "exit 2"
    assert "boom" in rendered


def test_render_error_suppresses_zero_exit_code() -> None:
    # A tool can report is_error with exit 0 (failed for a non-exit reason);
    # "exit 0" reads as success and is noise, so it is dropped.
    rendered = render_error("failed anyway", exit_code=0)
    assert "exit 0" not in rendered
    assert rendered == "failed anyway"


def test_render_error_omits_exit_line_when_none() -> None:
    # Synthetic error paths (approval-denied, raised exceptions) and tools with
    # no exit-code semantics promote exit_code=None, so no exit line prints.
    assert render_error("denied", exit_code=None) == "denied"


def test_render_error_renders_signal_termination_not_negative_exit() -> None:
    # asyncio reports a negative code when the process was killed by a signal
    # (including Wisp's own SIGKILL on output-budget exhaustion). "exit -9" is
    # nonsensical; render it as the signal. The signal *number* is
    # platform-invariant; its symbolic name (SIGKILL) is only present where the
    # platform defines it, so assert the number always and the name when known.
    rendered = render_error("boom", exit_code=-9)
    first = rendered.splitlines()[0]
    assert "exit -9" not in rendered
    assert first.startswith("killed by signal 9")
    try:
        name = signal.Signals(9).name
    except ValueError:
        assert first == "killed by signal 9"
    else:
        assert first == f"killed by signal 9 ({name})"


def test_render_error_unknown_signal_number_falls_back() -> None:
    # A signal number with no Signals enum member still renders cleanly.
    rendered = render_error("boom", exit_code=-999)
    assert rendered.splitlines()[0] == "killed by signal 999"


def test_render_error_suppresses_synthetic_exit_restatement() -> None:
    # A shell command with no stdout/stderr has output "Command exited with code
    # N" (a restatement of the exit code). With a status line already shown, that
    # tail is pure duplication and must be dropped.
    rendered = render_error("Command exited with code 2", exit_code=2)
    assert rendered == "exit 2"


def test_render_error_synthetic_restatement_does_not_reintroduce_negative_code() -> None:
    # The signal case is why this matters: the synthetic tail would restate the
    # raw negative code (`... code -15`), undoing the signal wording.
    rendered = render_error("Command exited with code -15", exit_code=-15)
    assert "Command exited with code" not in rendered
    assert "-15" not in rendered
    assert rendered.startswith("killed by signal 15")


def test_render_error_keeps_real_output_alongside_exit_line() -> None:
    # Suppression is narrow: real command output is never dropped, even when a
    # status line is present.
    rendered = render_error("error: file not found", exit_code=2)
    assert rendered == "exit 2\nerror: file not found"
    # Output that merely starts with the synthetic phrase but carries more is real.
    multi = render_error("Command exited with code 2\nstderr detail", exit_code=2)
    assert "stderr detail" in multi


def test_render_error_keeps_genuine_output_resembling_the_fallback() -> None:
    # A command whose real output is literally "Command exited with code 7" while
    # it exits 2 must be preserved — suppression requires the restated number to
    # match the promoted exit code, so a mismatched number is genuine output.
    rendered = render_error("Command exited with code 7", exit_code=2)
    assert "Command exited with code 7" in rendered
    assert rendered == "exit 2\nCommand exited with code 7"


def test_render_error_keeps_restatement_when_no_status_line() -> None:
    # With no promoted exit code there is no status line, so the synthetic
    # restatement is the only signal and must be kept.
    assert render_error("Command exited with code 2", exit_code=None) == (
        "Command exited with code 2"
    )


def test_render_error_shows_the_tail_not_the_head() -> None:
    # The core fix: failures surface at the END of output. A long output must
    # show its last lines, not its first — the pre-#74 behavior showed the head
    # and lost the actual error.
    output = "\n".join(f"line-{i}" for i in range(40))
    rendered = render_error(output, exit_code=None)
    assert "line-39" in rendered  # the tail is shown
    assert "line-0" not in rendered  # the head is dropped


def test_render_error_marks_hidden_earlier_lines() -> None:
    # Truncation stays honest: dropped lines are counted in a leading marker,
    # alongside the dropped byte count.
    output = "\n".join(f"line-{i}" for i in range(40))
    rendered = render_error(output, exit_code=None)
    hidden = 40 - _ERROR_TAIL_LINES
    assert f"... {hidden} earlier lines" in rendered
    assert "bytes hidden" in rendered


def test_render_error_marks_byte_only_truncation_of_single_line() -> None:
    # A single line longer than the byte budget is clipped from the front so the
    # tail survives; without a byte marker it would look complete. No line was
    # dropped, so the marker reports only bytes.
    output = "x" * (_ERROR_TAIL_BYTES + 500)
    rendered = render_error(output, exit_code=None)
    assert "earlier line" not in rendered  # no whole line was dropped
    assert "bytes hidden" in rendered
    assert rendered.rstrip().endswith("x")  # the tail is what survives


def test_render_error_byte_trim_on_newline_boundary_has_no_blank_line() -> None:
    # When the byte trim lands exactly on a newline, the separator must not
    # survive as a spurious blank first line, and the counts stay honest: the
    # first (dropped) line is reported as a hidden line, not swallowed as bytes.
    output = "head\n" + "x" * (_ERROR_TAIL_BYTES - 1)
    rendered = render_error(output, exit_code=None)
    body = rendered.split("\n", 1)[1] if rendered.startswith("...") else rendered
    assert not body.startswith("\n")  # no leading blank line
    assert "head" not in rendered  # the dropped head line is gone
    assert "1 earlier line" in rendered  # and counted as a hidden line


def test_render_error_keeps_complete_line_when_window_starts_at_boundary() -> None:
    # When the byte window happens to begin right after a newline, the first line
    # in it is COMPLETE and must be kept — only a mid-line window's partial
    # remnant should be dropped. Here only the short head line falls outside the
    # window; the full middle line must survive.
    middle = "a" * 1000
    output = "head\n" + middle + "\n" + "b" * 999
    rendered = render_error(output, exit_code=None)
    assert middle in rendered  # the complete middle line is preserved
    assert "head" not in rendered  # only the out-of-window head line is dropped
    assert "1 earlier line" in rendered  # exactly one line hidden, not two


def test_render_error_short_output_has_no_hidden_marker() -> None:
    rendered = render_error("only one line", exit_code=None)
    assert "earlier" not in rendered
    assert rendered == "only one line"


def test_render_error_empty_output_with_exit_shows_only_exit() -> None:
    # A bare non-zero exit with no output still names the failure.
    assert render_error("", exit_code=1) == "exit 1"


def test_render_error_completely_empty_is_no_output() -> None:
    assert render_error("   \n  ", exit_code=None) == "(no output)"


def test_tool_result_failed_judgment() -> None:
    # is_error always fails; a nonzero exit fails without is_error; zero and None
    # do not. This single predicate drives both the glyph and the detail body.
    assert tool_result_failed(is_error=True, exit_code=None) is True
    assert tool_result_failed(is_error=False, exit_code=2) is True
    assert tool_result_failed(is_error=False, exit_code=0) is False
    assert tool_result_failed(is_error=False, exit_code=None) is False


def test_render_generic_matches_widget_default_bounds() -> None:
    # The generic (success / unknown-tool) path must be byte-for-byte identical
    # to the pre-#74 preview; it delegates to the shared helper at its defaults.
    from wisp.tui.widgets import _preview_tool_output

    output = "\n".join(f"line-{i}" for i in range(20))
    assert render_generic(output) == _preview_tool_output(output)


def test_render_tool_result_routes_error_to_error_renderer() -> None:
    output = "\n".join(f"line-{i}" for i in range(40))
    via_dispatch = render_tool_result("bash", {}, output, is_error=True, exit_code=3)
    assert via_dispatch == render_error(output, exit_code=3)


def test_render_tool_result_routes_success_to_generic() -> None:
    via_dispatch = render_tool_result("bash", {}, "ok\ndone", is_error=False, exit_code=None)
    assert via_dispatch == render_generic("ok\ndone")


def test_render_tool_result_treats_nonzero_exit_as_failure() -> None:
    # A bash command that ran fine but exited nonzero is NOT is_error (that stays
    # a normal model-visible result), yet its card should render as a failure and
    # surface the exit status. The dispatcher routes on exit_code, not is_error.
    output = "\n".join(f"line-{i}" for i in range(40))
    via_dispatch = render_tool_result("bash", {}, output, is_error=False, exit_code=2)
    assert via_dispatch == render_error(output, exit_code=2)
    assert "exit 2" in via_dispatch
    assert "line-39" in via_dispatch  # the tail (the failure) is shown


def test_render_tool_result_zero_exit_stays_generic() -> None:
    # exit 0 is success — it must not trigger the failure path.
    via_dispatch = render_tool_result("bash", {}, "ok\ndone", is_error=False, exit_code=0)
    assert via_dispatch == render_generic("ok\ndone")
