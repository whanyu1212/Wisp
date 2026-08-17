from __future__ import annotations

import pytest

from wisp.tui.process_lifecycle import (
    PROCESS_OUTPUT_MAX_BYTES,
    ProcessLifecycle,
    historical_process_observation,
    process_call_identity,
)

pytestmark = pytest.mark.tui


def test_process_call_identity_accepts_only_bash_poll_and_cancel() -> None:
    poll = process_call_identity(
        "bash",
        {"operation": "poll", "process_id": "proc-1", "wait_seconds": 2},
    )
    cancel = process_call_identity("bash", {"operation": "cancel", "process_id": "proc-1"})

    assert poll is not None
    assert (poll.process_id, poll.operation) == ("proc-1", "poll")
    assert cancel is not None
    assert (cancel.process_id, cancel.operation) == ("proc-1", "cancel")
    assert process_call_identity("bash", {"operation": "start", "command": "pytest"}) is None
    assert process_call_identity("read", {"operation": "poll", "process_id": "proc-1"}) is None
    assert process_call_identity("bash", {"operation": "poll", "process_id": ""}) is None


def test_process_lifecycle_accumulates_incremental_output_and_poll_count() -> None:
    lifecycle = ProcessLifecycle("proc-1")

    lifecycle.begin("poll")
    lifecycle.observe(operation="poll", state="running", stdout="first chunk\n")
    lifecycle.begin("poll")
    presentation = lifecycle.observe(
        operation="poll",
        state="completed",
        stdout="second chunk\n",
    )

    assert presentation.poll_count == 2
    assert presentation.display_state == "completed"
    assert presentation.full_output == "first chunk\nsecond chunk"
    assert "first chunk" in presentation.detail
    assert "second chunk" in presentation.detail
    assert presentation.terminal is True


def test_failed_tool_result_overrides_nominal_completed_process_state() -> None:
    lifecycle = ProcessLifecycle("proc-1")
    lifecycle.begin("poll")

    presentation = lifecycle.observe(
        operation="poll",
        state="completed",
        failed=True,
    )

    assert presentation.display_state == "failed"
    assert presentation.terminal is True


def test_process_lifecycle_does_not_duplicate_failure_reason_from_fallback() -> None:
    lifecycle = ProcessLifecycle("proc-1")
    lifecycle.begin("poll")

    presentation = lifecycle.observe(
        operation="poll",
        state="failed",
        fallback_output="cleanup failed",
        failure_reason="cleanup failed",
        failed=True,
    )

    assert presentation.full_output == "cleanup failed"


def test_process_lifecycle_bounds_accumulated_output_and_reports_omission() -> None:
    lifecycle = ProcessLifecycle("proc-1")
    lifecycle.begin("poll")
    lifecycle.observe(operation="poll", state="running", stdout="a" * PROCESS_OUTPUT_MAX_BYTES)
    lifecycle.begin("poll")
    presentation = lifecycle.observe(
        operation="poll",
        state="running",
        stdout="diagnostic tail",
    )

    assert presentation.ui_dropped_bytes > 0
    assert len(presentation.full_output.encode("utf-8")) < PROCESS_OUTPUT_MAX_BYTES + 200
    assert "earlier process-output bytes omitted by TUI" in presentation.full_output
    assert presentation.full_output.endswith("diagnostic tail")


def test_historical_process_envelope_requires_the_expected_process_id() -> None:
    state, output = historical_process_observation(
        "proc-1",
        "Process proc-1 is still running\nstdout:\nprogress\n",
    )
    mismatched_state, mismatched_output = historical_process_observation(
        "proc-1",
        "Process proc-other completed with exit code 0\nstdout:\ndone\n",
    )
    malformed_state, malformed_output = historical_process_observation(
        "proc-1",
        "Process proc-1 completed with exit code unknown",
    )

    assert state == "running"
    assert output == "progress\n"
    assert mismatched_state is None
    assert mismatched_output.startswith("Process proc-other completed")
    assert malformed_state is None
    assert malformed_output == "Process proc-1 completed with exit code unknown"


@pytest.mark.parametrize(
    ("body", "expected_output"),
    [
        ("", "cleanup failed"),
        ("\nstdout:\npartial output\n", "cleanup failed\npartial output\n"),
    ],
)
def test_historical_failed_process_preserves_header_reason(
    body: str,
    expected_output: str,
) -> None:
    state, output = historical_process_observation(
        "proc-1",
        f"Process proc-1 failed: cleanup failed{body}",
    )

    assert state == "failed"
    assert output == expected_output


def test_denied_poll_does_not_claim_that_the_process_was_cancelled() -> None:
    lifecycle = ProcessLifecycle("proc-1")
    lifecycle.begin("poll")

    presentation = lifecycle.deny("poll")

    assert presentation.display_state == "poll_denied"
    assert presentation.terminal is False


def test_denied_poll_preserves_reason() -> None:
    lifecycle = ProcessLifecycle("proc-1")
    lifecycle.begin("poll")

    presentation = lifecycle.deny("poll", "not now")

    assert presentation.display_state == "poll_denied"
    assert presentation.detail == "not now"
    assert presentation.full_output == "not now"
