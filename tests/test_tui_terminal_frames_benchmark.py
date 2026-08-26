from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
from typing import cast

import pytest

from benchmarks import tui_terminal_frames as terminal_frames_module
from benchmarks.tui_terminal_frames import (
    _MODE_QUERY,
    _SYNC_END,
    _SYNC_START,
    TerminalFrameConfig,
    _fixture_history_entry_capacity,
    _FrameCollector,
    _read_pty_process,
    _rotated_modes,
    _SequenceCounter,
    _validate_config,
    _validate_native_viewport,
    _wait_for_capability_state,
    run_native_benchmark,
    run_paired_benchmark,
)
from wisp.tui.diagnostics import TerminalWriteDiagnostic
from wisp.tui.textual_app import TextualTui

pytestmark = pytest.mark.benchmark


def test_sequence_counter_handles_fragmented_terminal_controls() -> None:
    counter = _SequenceCounter()
    payload = b"cells" + _MODE_QUERY + _SYNC_START + b"more cells" + _SYNC_END

    for byte in payload:
        counter.feed(bytes((byte,)))

    assert counter.query_count == 1
    assert counter.sync_begin_count == 1
    assert counter.sync_end_count == 1
    assert counter.max_sync_depth == 1
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
    assert counter.sync_balanced


def test_sequence_counter_rejects_end_before_begin() -> None:
    counter = _SequenceCounter()

    counter.feed(_SYNC_END + _SYNC_START)

    assert counter.sync_begin_count == 1
    assert counter.sync_end_count == 1
    assert not counter.sync_order_valid
    assert not counter.sync_balanced


def test_sequence_counter_tracks_nested_synchronization_depth() -> None:
    counter = _SequenceCounter()

    counter.feed(_SYNC_START + _SYNC_START + _SYNC_END + _SYNC_END)

    assert counter.sync_balanced
    assert counter.max_sync_depth == 2


def test_native_capability_wait_uses_configured_negotiation_window() -> None:
    class FakeApp:
        _sync_available = False

    app = FakeApp()

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()

        async def enable_support() -> None:
            await asyncio.sleep(0.3)
            app._sync_available = True

        task = asyncio.create_task(enable_support())
        await _wait_for_capability_state(
            cast(TextualTui, app),
            mode="native",
            timeout=0.5,
        )
        wait_elapsed = loop.time() - started
        await task
        return wait_elapsed

    elapsed = asyncio.run(scenario())

    assert app._sync_available
    assert elapsed >= 0.3
    assert elapsed < 0.5


def test_terminal_frame_collector_rejects_misordered_exact_pair() -> None:
    collector = _FrameCollector(collecting=True)
    collector.record_terminal_write(
        TerminalWriteDiagnostic(
            display_kind="other",
            sync_available=True,
            write_count=3,
            flush_count=1,
            payload_bytes=7,
            max_write_bytes=7,
            posix_write_count=1,
            windows_chunk_count=1,
            sync_begin_count=1,
            sync_end_count=1,
            sync_order_valid=False,
            writes_inside_sync=1,
            writes_outside_sync=0,
            observed_driver=True,
            out_of_band=False,
            out_of_band_kind=None,
        )
    )

    assert collector.exact_sync_pair_frame_count == 0
    assert collector.unbalanced_sync_frame_count == 1
    assert not collector.process_sync_balanced


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


def test_native_terminal_frame_benchmark_rejects_noninteractive_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NonInteractiveStream:
        def isatty(self) -> bool:
            return False

    class FakeSys:
        stdin = NonInteractiveStream()
        stdout = NonInteractiveStream()

    monkeypatch.setattr(terminal_frames_module, "sys", FakeSys())

    with pytest.raises(RuntimeError, match="requires an interactive terminal"):
        asyncio.run(run_native_benchmark(TerminalFrameConfig(runs=1)))


def test_native_terminal_frame_benchmark_rejects_mismatched_viewport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InteractiveStream:
        def fileno(self) -> int:
            return 101

    class FakeSys:
        stdout = InteractiveStream()

    monkeypatch.setattr(terminal_frames_module, "sys", FakeSys())
    monkeypatch.setattr(
        terminal_frames_module.os,
        "get_terminal_size",
        lambda descriptor: os.terminal_size((120, 40)),
    )

    with pytest.raises(
        RuntimeError,
        match=(
            r"native terminal viewport is 120x40; expected 100x24\. "
            r"Resize the terminal or pass --width 120 --height 40"
        ),
    ):
        _validate_native_viewport(TerminalFrameConfig())


def test_native_terminal_frame_report_records_validated_viewport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InteractiveStream:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 101

    class FakeSys:
        stdin = InteractiveStream()
        stdout = InteractiveStream()

    async def fake_workload(*_args: object, **kwargs: object) -> dict[str, object]:
        return {
            "mode": "native",
            "run": kwargs["run"],
            "order": kwargs["order"],
            "emulator_label": kwargs["emulator_label"],
            "capability_detected": True,
            "display_updates": {"layout": 1},
            "display_frame_cache_outcomes": {"updated": 1},
            "complete_layout_count": 1,
            "chops_update_count": 0,
            "emitted_spans": 0,
            "suppressed_spans": 0,
            "observed_driver_frames": 1,
            "terminal_payload_bytes": 7,
            "terminal_write_count": 3,
            "terminal_flush_count": 1,
            "exact_sync_pair_frame_count": 1,
            "unbalanced_sync_frame_count": 0,
            "writes_inside_sync": 1,
            "writes_outside_sync": 0,
            "out_of_band_writes": {},
            "diagnostic_process_sync_begin_count": 1,
            "diagnostic_process_sync_end_count": 1,
            "diagnostic_process_sync_balanced": True,
            "diagnostic_process_sync_max_depth": 1,
            "source_complete": True,
        }

    monkeypatch.setattr(terminal_frames_module, "sys", FakeSys())
    monkeypatch.setattr(
        terminal_frames_module.os,
        "get_terminal_size",
        lambda descriptor: os.terminal_size((100, 24)),
    )
    monkeypatch.setattr(terminal_frames_module, "_run_child_workload", fake_workload)

    report = asyncio.run(
        run_native_benchmark(
            TerminalFrameConfig(runs=1),
            emulator_label="test-terminal 1.0 / direct",
        )
    )
    payload = json.loads(report.to_json())

    assert payload["environment"]["terminal_columns"] == "100"
    assert payload["environment"]["terminal_lines"] == "24"
    assert payload["samples"][0]["emulator_label"] == "test-terminal 1.0 / direct"
    assert payload["samples"][0]["process_sync_balanced"] is True
    assert payload["samples"][0]["process_sync_max_depth"] == 1


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
    assert supported.process_sync_max_depth == 1
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
    assert unsupported.process_sync_max_depth == 0
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


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pseudo-terminals")
def test_terminal_frame_benchmark_opt_out_emits_no_synchronization_controls() -> None:
    report = run_paired_benchmark(
        TerminalFrameConfig(
            message_count=4,
            retained_history_entries=2,
            stream_chunks=2,
            stream_interval_seconds=0.02,
            viewport_width=80,
            viewport_height=16,
            runs=1,
            pending_tool_cards=0,
            negotiation_timeout_seconds=5,
            process_timeout_seconds=20,
            synchronized_output_enabled=False,
        ),
        emulator_label="pytest-pty-disabled",
    )

    assert len(report.samples) == 2
    for sample in report.samples:
        assert sample.capability_query_observed
        assert not sample.capability_detected
        assert sample.exact_sync_pair_frame_count == 0
        assert sample.unbalanced_sync_frame_count == 0
        assert sample.process_sync_begin_count == 0
        assert sample.process_sync_end_count == 0
        assert sample.process_sync_balanced
        assert sample.process_sync_max_depth == 0
        assert sample.source_complete
