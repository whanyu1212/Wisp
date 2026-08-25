from __future__ import annotations

import errno
import json
import os
import subprocess
from typing import cast

import pytest

from benchmarks.tui_terminal_frames import (
    _MODE_QUERY,
    _SYNC_END,
    _SYNC_START,
    TerminalFrameConfig,
    _fixture_history_entry_capacity,
    _read_pty_process,
    _rotated_modes,
    _SequenceCounter,
    _validate_config,
    run_paired_benchmark,
)

pytestmark = pytest.mark.benchmark


def test_sequence_counter_handles_fragmented_terminal_controls() -> None:
    counter = _SequenceCounter()
    payload = b"cells" + _MODE_QUERY + _SYNC_START + b"more cells" + _SYNC_END

    for byte in payload:
        counter.feed(bytes((byte,)))

    assert counter.query_count == 1
    assert counter.sync_begin_count == 1
    assert counter.sync_end_count == 1
    assert not hasattr(counter, "payload")
    assert not hasattr(counter, "output")


def test_sequence_counter_does_not_recount_complete_tail_sequences() -> None:
    counter = _SequenceCounter()

    counter.feed(_SYNC_START)
    counter.feed(b"payload")
    counter.feed(_SYNC_END)
    counter.feed(b"more")

    assert counter.sync_begin_count == 1
    assert counter.sync_end_count == 1


def test_terminal_frame_fixture_capacity_accounts_for_collapsed_tool_pairs() -> None:
    assert _fixture_history_entry_capacity(4) == 4
    assert _fixture_history_entry_capacity(5) == 4
    assert _fixture_history_entry_capacity(10) == 8


def test_terminal_frame_mode_order_rotates_between_runs() -> None:
    assert _rotated_modes(1) == ("unsupported", "supported")
    assert _rotated_modes(2) == ("supported", "unsupported")
    assert _rotated_modes(3) == ("unsupported", "supported")


def test_terminal_frame_linux_pty_eio_is_treated_as_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CompletedProcess:
        returncode = 0

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            return 0

    def raise_eio(_descriptor: int, _size: int) -> bytes:
        raise OSError(errno.EIO, "PTY closed")

    monkeypatch.setattr(
        "benchmarks.tui_terminal_frames.select.select",
        lambda *_args: ([101], [], []),
    )
    monkeypatch.setattr("benchmarks.tui_terminal_frames.os.read", raise_eio)

    counter = _read_pty_process(
        cast(subprocess.Popen[bytes], CompletedProcess()),
        master_fd=101,
        control_fd=102,
        mode="unsupported",
        negotiation_timeout=1,
        process_timeout=1,
    )

    assert counter.query_count == 0
    assert counter.sync_begin_count == 0
    assert counter.sync_end_count == 0


def test_terminal_frame_negotiation_timeout_terminates_child() -> None:
    class StalledProcess:
        returncode: int | None = None
        terminated = False
        killed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            assert self.returncode is not None
            return self.returncode

    process = StalledProcess()
    terminal_read_fd, terminal_write_fd = os.pipe()
    control_read_fd, control_write_fd = os.pipe()
    try:
        with pytest.raises(TimeoutError, match="capability query"):
            _read_pty_process(
                cast(subprocess.Popen[bytes], process),
                master_fd=terminal_read_fd,
                control_fd=control_write_fd,
                mode="unsupported",
                negotiation_timeout=0.01,
                process_timeout=1,
            )
    finally:
        for descriptor in (
            terminal_read_fd,
            terminal_write_fd,
            control_read_fd,
            control_write_fd,
        ):
            os.close(descriptor)

    assert process.terminated
    assert not process.killed


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (TerminalFrameConfig(runs=0), "positive"),
        (
            TerminalFrameConfig(message_count=5, retained_history_entries=5),
            "fixture's 4 rendered entries",
        ),
        (TerminalFrameConfig(stream_interval_seconds=0), "stream interval"),
        (TerminalFrameConfig(pending_tool_cards=-1), "must not be negative"),
        (TerminalFrameConfig(process_timeout_seconds=0), "timeouts must be positive"),
    ],
)
def test_terminal_frame_config_rejects_invalid_values(
    config: TerminalFrameConfig,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_config(config)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pseudo-terminals")
def test_terminal_frame_benchmark_observes_supported_and_unsupported_modes() -> None:
    report = run_paired_benchmark(
        TerminalFrameConfig(
            message_count=4,
            retained_history_entries=2,
            stream_chunks=3,
            stream_interval_seconds=0.02,
            viewport_width=80,
            viewport_height=16,
            runs=1,
            pending_tool_cards=1,
            negotiation_timeout_seconds=5,
            process_timeout_seconds=20,
        ),
        emulator_label="pytest-pty",
    )

    assert len(report.samples) == 2
    by_mode = {sample.mode: sample for sample in report.samples}
    supported = by_mode["supported"]
    unsupported = by_mode["unsupported"]

    assert supported.capability_query_observed
    assert supported.capability_response_supplied
    assert supported.capability_detected
    assert supported.observed_driver_frames >= 1
    assert supported.exact_sync_pair_frame_count == supported.observed_driver_frames
    assert supported.unbalanced_sync_frame_count == 0
    assert supported.writes_inside_sync >= supported.observed_driver_frames
    assert supported.writes_outside_sync == 0
    assert supported.process_sync_begin_count is not None
    assert supported.process_sync_begin_count > 0
    assert supported.process_sync_begin_count == supported.process_sync_end_count
    assert supported.process_sync_balanced
    assert supported.source_complete

    assert unsupported.capability_query_observed
    assert not unsupported.capability_response_supplied
    assert not unsupported.capability_detected
    assert unsupported.observed_driver_frames >= 1
    assert unsupported.exact_sync_pair_frame_count == 0
    assert unsupported.unbalanced_sync_frame_count == 0
    assert unsupported.writes_inside_sync == 0
    assert unsupported.writes_outside_sync >= unsupported.observed_driver_frames
    assert unsupported.process_sync_begin_count == 0
    assert unsupported.process_sync_end_count == 0
    assert unsupported.process_sync_balanced
    assert unsupported.source_complete

    assert supported.display_updates
    assert unsupported.display_updates
    assert supported.complete_layout_count >= 1
    assert unsupported.complete_layout_count >= 1
    assert supported.chops_update_count >= 1
    assert unsupported.chops_update_count >= 1
    assert supported.display_frame_cache_outcomes
    assert unsupported.display_frame_cache_outcomes
    assert supported.terminal_payload_bytes > 0
    assert unsupported.terminal_payload_bytes > 0

    payload = report.to_json()
    decoded = json.loads(payload)
    assert decoded["environment"]["textual"]
    assert decoded["samples"][0]["emulator_label"] == "pytest-pty"
    assert "\\u001b" not in payload
    assert "\\x1b" not in payload
    assert "\x1b" not in payload
    assert "Terminal frame 0" not in payload
    assert "measured item" not in payload
