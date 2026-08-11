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

from wisp.tool_presentation import tool_result_status
from wisp.tui.tool_output import (
    _ERROR_TAIL_BYTES,
    _ERROR_TAIL_LINES,
    full_tool_result_for_display,
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
    rendered = render_error(
        "Command exited with code 2",
        exit_code=2,
        output_has_exit_status=True,
    )
    assert rendered == "exit 2"


def test_render_error_synthetic_restatement_does_not_reintroduce_negative_code() -> None:
    # The signal case is why this matters: the synthetic tail would restate the
    # raw negative code (`... code -15`), undoing the signal wording.
    rendered = render_error(
        "Command exited with code -15",
        exit_code=-15,
        output_has_exit_status=True,
    )
    assert "Command exited with code" not in rendered
    assert "-15" not in rendered
    assert rendered.startswith("killed by signal 15")


def test_render_error_strips_synthetic_prefix_but_keeps_command_output() -> None:
    rendered = render_error(
        "Command exited with code 2: assertion failed\ntrace detail",
        exit_code=2,
        output_has_exit_status=True,
    )

    assert rendered == "exit 2\nassertion failed\ntrace detail"
    assert rendered.count("exit 2") == 1
    assert "Command exited with code" not in rendered


def test_render_error_strips_negative_prefix_but_keeps_truncation_notice() -> None:
    rendered = render_error(
        "Command exited with code -9: [output truncated]",
        exit_code=-9,
        output_has_exit_status=True,
    )

    assert rendered.startswith("killed by signal 9")
    assert rendered.endswith("[output truncated]")
    assert "Command exited with code" not in rendered
    assert "exit -9" not in rendered


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
    mismatched_with_output = render_error(
        "Command exited with code 7: genuine output",
        exit_code=2,
        output_has_exit_status=True,
    )
    assert mismatched_with_output == ("exit 2\nCommand exited with code 7: genuine output")


def test_render_error_restatement_match_allows_only_trailing_newline() -> None:
    # The synthetic fallback is emitted verbatim (no padding), so the match is
    # exact except for a trailing newline. Output with surrounding whitespace is
    # genuine and preserved; a bare trailing newline is still the fallback.
    assert (
        render_error(
            "Command exited with code 2\n",
            exit_code=2,
            output_has_exit_status=True,
        )
        == "exit 2"
    )
    with_spaces = render_error(" Command exited with code 2 ", exit_code=2)
    assert with_spaces == "exit 2\n Command exited with code 2 "


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
    # do not. Managed timed-out/failed states also fail even without exit codes.
    # This single predicate drives both the glyph and the detail body.
    assert tool_result_failed(is_error=True, exit_code=None) is True
    assert tool_result_failed(is_error=False, exit_code=2) is True
    assert tool_result_failed(is_error=False, exit_code=None, process_state="timed_out") is True
    assert tool_result_failed(is_error=False, exit_code=None, process_state="failed") is True
    assert tool_result_failed(is_error=False, exit_code=0) is False
    assert tool_result_failed(is_error=False, exit_code=None) is False
    assert tool_result_failed(is_error=False, exit_code=None, process_state="cancelled") is False


def test_tool_result_status_maps_managed_process_states() -> None:
    assert tool_result_status(is_error=False, exit_code=None, process_state="timed_out") == "error"
    assert tool_result_status(is_error=False, exit_code=None, process_state="failed") == "error"
    assert (
        tool_result_status(is_error=False, exit_code=None, process_state="cancelled") == "cancelled"
    )
    assert tool_result_status(is_error=False, exit_code=0, process_state="completed") == "done"
    assert tool_result_status(is_error=False, exit_code=None, process_state="running") == "done"


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


def test_render_tool_result_treats_timed_out_process_as_failure() -> None:
    output = "Process proc-1 timed out"
    via_dispatch = render_tool_result(
        "bash",
        {},
        output,
        is_error=False,
        exit_code=None,
        process_state="timed_out",
    )

    assert via_dispatch == render_error(output, exit_code=None)


def test_render_tool_result_cancelled_process_stays_generic() -> None:
    output = "Process proc-1 cancelled"
    via_dispatch = render_tool_result(
        "bash",
        {},
        output,
        is_error=False,
        exit_code=None,
        process_state="cancelled",
    )

    assert via_dispatch == render_generic(output)


def test_render_tool_result_zero_exit_stays_generic() -> None:
    # exit 0 is success — it must not trigger the failure path.
    via_dispatch = render_tool_result("bash", {}, "ok\ndone", is_error=False, exit_code=0)
    assert via_dispatch == render_generic("ok\ndone")


def test_render_tool_result_zero_exit_strips_model_facing_status_prefix() -> None:
    via_dispatch = render_tool_result(
        "bash",
        {},
        "Command exited with code 0: ok\ndone",
        is_error=False,
        exit_code=0,
        output_has_exit_status=True,
    )

    assert via_dispatch == render_generic("ok\ndone")
    assert "Command exited with code" not in str(via_dispatch)


def test_render_tool_result_zero_exit_without_output_stays_empty() -> None:
    via_dispatch = render_tool_result(
        "bash",
        {},
        "Command exited with code 0",
        is_error=False,
        exit_code=0,
        output_has_exit_status=True,
    )

    assert via_dispatch == render_generic("")


def test_render_tool_result_shows_summary_in_place_of_output() -> None:
    # A read-type tool carries a promoted one-line summary; the dispatcher returns it
    # instead of a raw dump of the (much longer) output.
    output = "\n".join(f"line-{i}" for i in range(40))
    via_dispatch = render_tool_result(
        "read",
        {"path": "foo.py"},
        output,
        is_error=False,
        exit_code=None,
        summary="read 40 lines from foo.py",
    )
    assert via_dispatch == "read 40 lines from foo.py"


def test_render_tool_result_no_summary_falls_back_to_generic() -> None:
    # Without a summary (e.g. an unknown tool, or a covered tool that produced no
    # structured facts) the success path is unchanged: the generic preview.
    via_dispatch = render_tool_result(
        "read", {}, "raw\noutput", is_error=False, exit_code=None, summary=None
    )
    assert via_dispatch == render_generic("raw\noutput")


def test_render_tool_result_failed_read_uses_error_not_summary() -> None:
    # A failed read still renders the error, never the summary — a summary is only a
    # success affordance.
    result = render_tool_result(
        "read",
        {"path": "foo.py"},
        "read failed: file not found",
        is_error=True,
        exit_code=None,
        summary="read 40 lines from foo.py",
    )
    assert result == render_error("read failed: file not found", exit_code=None)
    assert "read 40 lines" not in result


def test_render_grep_result_keeps_summary_and_match_evidence() -> None:
    output = "src/a.py:1:TODO first\nsrc/b.py:2:TODO second\n"

    rendered = render_tool_result(
        "grep",
        {"pattern": "TODO"},
        output,
        is_error=False,
        exit_code=None,
        summary="grep: 2 matches",
    )

    assert rendered == "grep: 2 matches\nsrc/a.py:1:TODO first\nsrc/b.py:2:TODO second"
    assert full_tool_result_for_display("grep", output, None, summary="grep: 2 matches") == rendered


def test_render_long_grep_result_reports_hidden_match_lines() -> None:
    output = "\n".join(f"src/file.py:{index}:match" for index in range(20))

    rendered = render_tool_result(
        "grep",
        {"pattern": "match"},
        output,
        is_error=False,
        exit_code=None,
        summary="grep: 20 matches",
    )

    assert rendered.startswith("grep: 20 matches\nsrc/file.py:0:match")
    assert "src/file.py:7:match" in rendered
    assert "src/file.py:8:match" not in rendered
    assert "12 more lines" in rendered
    assert "bytes hidden" in rendered


def test_render_zero_match_grep_does_not_repeat_raw_empty_result() -> None:
    rendered = render_tool_result(
        "grep",
        {"pattern": "absent"},
        "No matches found",
        is_error=False,
        exit_code=None,
        summary="grep: no matches",
    )

    assert rendered == "grep: no matches"
    assert (
        full_tool_result_for_display(
            "grep",
            "No matches found",
            None,
            summary="grep: no matches",
        )
        == "grep: no matches"
    )


def test_successful_bash_result_shows_tail_and_expands_in_original_order() -> None:
    output = "\n".join(f"line-{index}" for index in range(40))

    rendered = render_tool_result("bash", {}, output, is_error=False, exit_code=0)
    full = full_tool_result_for_display("bash", output, 0)

    assert "line-39" in rendered
    assert "line-0" not in rendered
    assert "earlier lines" in rendered
    assert full.startswith("line-0\n")
    assert full.endswith("line-39")


def test_successful_extension_result_still_shows_head() -> None:
    output = "\n".join(f"line-{index}" for index in range(40))

    rendered = render_tool_result("extension", {}, output, is_error=False, exit_code=None)

    assert "line-0" in rendered
    assert "line-39" not in rendered
    assert "more lines" in rendered
