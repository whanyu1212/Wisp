"""Measure Textual synchronized-output framing through a real terminal driver."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import textual

from benchmarks.support import environment
from benchmarks.tui_long_session import append_benchmark_messages
from benchmarks.tui_stream_hotpaths import _newest_history_entries
from wisp.events import ToolCallRequested
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.diagnostics import (
    DisplayUpdateDiagnostic,
    InputLatencyDiagnostic,
    MarkdownDrainDiagnostic,
    TerminalWriteDiagnostic,
)
from wisp.tui.state import TuiExitReason
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui

type CapabilityMode = Literal["supported", "unsupported", "native"]

_MODE_QUERY = b"\x1b[?2026$p"
_MODE_SUPPORTED = b"\x1b[?2026;1$y"
_SYNC_START = b"\x1b[?2026h"
_SYNC_END = b"\x1b[?2026l"
_OBSERVED_SEQUENCES = (_MODE_QUERY, _SYNC_START, _SYNC_END)
_SEQUENCE_TAIL_BYTES = max(len(sequence) for sequence in _OBSERVED_SEQUENCES) - 1


@dataclass(frozen=True)
class TerminalFrameConfig:
    """Configuration shared by paired PTY and native terminal runs."""

    message_count: int = 20
    retained_history_entries: int = 10
    stream_chunks: int = 12
    stream_interval_seconds: float = 0.03
    viewport_width: int = 100
    viewport_height: int = 24
    runs: int = 3
    pending_tool_cards: int = 2
    negotiation_timeout_seconds: float = 5.0
    process_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class TerminalFrameSample:
    """Privacy-safe counts from one terminal-backed streaming workload."""

    mode: CapabilityMode
    run: int
    order: int
    emulator_label: str | None
    capability_query_observed: bool | None
    capability_response_supplied: bool
    capability_detected: bool
    display_updates: dict[str, int]
    display_frame_cache_outcomes: dict[str, int]
    complete_layout_count: int
    chops_update_count: int
    emitted_spans: int
    suppressed_spans: int
    observed_driver_frames: int
    terminal_payload_bytes: int
    terminal_write_count: int
    terminal_flush_count: int
    exact_sync_pair_frame_count: int
    unbalanced_sync_frame_count: int
    writes_inside_sync: int
    writes_outside_sync: int
    out_of_band_writes: dict[str, int]
    process_sync_begin_count: int | None
    process_sync_end_count: int | None
    process_sync_balanced: bool | None
    source_complete: bool


@dataclass(frozen=True)
class TerminalFrameReport:
    """Serializable terminal-frame evidence without terminal payload text."""

    config: TerminalFrameConfig
    environment: dict[str, str]
    samples: tuple[TerminalFrameSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass
class _FrameCollector:
    collecting: bool = False
    display_updates: dict[str, int] = field(default_factory=dict)
    display_frame_cache_outcomes: dict[str, int] = field(default_factory=dict)
    complete_layout_count: int = 0
    chops_update_count: int = 0
    emitted_spans: int = 0
    suppressed_spans: int = 0
    observed_driver_frames: int = 0
    terminal_payload_bytes: int = 0
    terminal_write_count: int = 0
    terminal_flush_count: int = 0
    exact_sync_pair_frame_count: int = 0
    unbalanced_sync_frame_count: int = 0
    writes_inside_sync: int = 0
    writes_outside_sync: int = 0
    capability_detected: bool = False
    out_of_band_writes: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.display_updates.clear()
        self.display_frame_cache_outcomes.clear()
        self.complete_layout_count = 0
        self.chops_update_count = 0
        self.emitted_spans = 0
        self.suppressed_spans = 0
        self.observed_driver_frames = 0
        self.terminal_payload_bytes = 0
        self.terminal_write_count = 0
        self.terminal_flush_count = 0
        self.exact_sync_pair_frame_count = 0
        self.unbalanced_sync_frame_count = 0
        self.writes_inside_sync = 0
        self.writes_outside_sync = 0
        self.capability_detected = False
        self.out_of_band_writes.clear()

    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        return

    def record_input_latency(self, _diagnostic: InputLatencyDiagnostic) -> None:
        return

    def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None:
        if not self.collecting:
            return
        self.display_updates[diagnostic.kind] = self.display_updates.get(diagnostic.kind, 0) + 1
        self.display_frame_cache_outcomes[diagnostic.frame_cache] = (
            self.display_frame_cache_outcomes.get(diagnostic.frame_cache, 0) + 1
        )
        if diagnostic.kind == "layout":
            self.complete_layout_count += 1
        elif diagnostic.kind == "chops":
            self.chops_update_count += 1
        self.emitted_spans += diagnostic.emitted_spans
        self.suppressed_spans += diagnostic.suppressed_spans

    def record_terminal_write(self, diagnostic: TerminalWriteDiagnostic) -> None:
        if not self.collecting:
            return
        if diagnostic.out_of_band:
            kind = diagnostic.out_of_band_kind or "other"
            self.out_of_band_writes[kind] = (
                self.out_of_band_writes.get(kind, 0) + diagnostic.write_count
            )
            return
        self.capability_detected = self.capability_detected or diagnostic.sync_available
        if diagnostic.observed_driver:
            self.observed_driver_frames += 1
        self.terminal_payload_bytes += diagnostic.payload_bytes
        self.terminal_write_count += diagnostic.write_count
        self.terminal_flush_count += diagnostic.flush_count
        self.writes_inside_sync += diagnostic.writes_inside_sync
        self.writes_outside_sync += diagnostic.writes_outside_sync
        has_sync_controls = bool(diagnostic.sync_begin_count or diagnostic.sync_end_count)
        if (
            diagnostic.sync_begin_count == 1
            and diagnostic.sync_end_count == 1
            and diagnostic.sync_order_valid
        ):
            self.exact_sync_pair_frame_count += 1
        elif has_sync_controls:
            self.unbalanced_sync_frame_count += 1


class _SequenceCounter:
    """Count selected terminal controls without retaining terminal output."""

    def __init__(self) -> None:
        self.query_count = 0
        self.sync_begin_count = 0
        self.sync_end_count = 0
        self.sync_order_valid = True
        self._sync_depth = 0
        self._tail = b""

    @property
    def sync_balanced(self) -> bool:
        return (
            self.sync_order_valid
            and self._sync_depth == 0
            and self.sync_begin_count == self.sync_end_count
        )

    def feed(self, data: bytes) -> None:
        prefix_size = len(self._tail)
        combined = self._tail + data
        self.query_count += len(_new_sequence_offsets(combined, _MODE_QUERY, prefix_size))
        sync_controls = [
            *(
                (offset, "begin")
                for offset in _new_sequence_offsets(combined, _SYNC_START, prefix_size)
            ),
            *(
                (offset, "end")
                for offset in _new_sequence_offsets(combined, _SYNC_END, prefix_size)
            ),
        ]
        for _offset, kind in sorted(sync_controls):
            if kind == "begin":
                self.sync_begin_count += 1
                self._sync_depth += 1
            else:
                self.sync_end_count += 1
                if self._sync_depth == 0:
                    self.sync_order_valid = False
                else:
                    self._sync_depth -= 1
        self._tail = combined[-_SEQUENCE_TAIL_BYTES:]


def _new_sequence_offsets(data: bytes, sequence: bytes, prefix_size: int) -> list[int]:
    offsets = []
    start = 0
    while True:
        index = data.find(sequence, start)
        if index < 0:
            return offsets
        if index + len(sequence) > prefix_size:
            offsets.append(index)
        start = index + len(sequence)


def _fixture_history_entry_capacity(message_count: int) -> int:
    """Return rendered entries produced by the shared five-message fixture cycle."""

    return message_count - message_count // 5


def _validate_config(config: TerminalFrameConfig) -> None:
    positive_integers = (
        config.message_count,
        config.retained_history_entries,
        config.stream_chunks,
        config.viewport_width,
        config.viewport_height,
        config.runs,
    )
    if any(value < 1 for value in positive_integers):
        raise ValueError("message, history, chunk, viewport, and run values must be positive")
    history_capacity = _fixture_history_entry_capacity(config.message_count)
    if config.retained_history_entries > history_capacity:
        raise ValueError(
            f"retained history must not exceed the fixture's {history_capacity} rendered entries"
        )
    if config.stream_interval_seconds <= 0:
        raise ValueError("stream interval must be positive")
    if config.pending_tool_cards < 0:
        raise ValueError("pending tool cards must not be negative")
    if config.negotiation_timeout_seconds <= 0 or config.process_timeout_seconds <= 0:
        raise ValueError("terminal negotiation and process timeouts must be positive")


def _rotated_modes(run: int) -> tuple[CapabilityMode, CapabilityMode]:
    modes: tuple[CapabilityMode, CapabilityMode] = ("unsupported", "supported")
    return modes if run % 2 else tuple(reversed(modes))


async def _wait_for_controller(control_fd: int | None, timeout: float) -> None:
    if control_fd is None:
        return
    try:
        signal = await asyncio.wait_for(asyncio.to_thread(os.read, control_fd, 1), timeout=timeout)
    finally:
        os.close(control_fd)
    if signal != b"1":
        raise RuntimeError("terminal capability controller closed before negotiation completed")


async def _wait_for_capability_state(
    app: TextualTui,
    *,
    mode: CapabilityMode,
    timeout: float,
) -> None:
    if mode in ("supported", "native"):
        deadline = asyncio.get_running_loop().time() + timeout
        while not bool(getattr(app, "_sync_available", False)):
            if asyncio.get_running_loop().time() >= deadline:
                if mode == "supported":
                    raise TimeoutError("Textual did not accept the synchronized-output response")
                return
            await asyncio.sleep(0.01)
    else:
        await asyncio.sleep(0.05)
        if bool(getattr(app, "_sync_available", False)):
            raise RuntimeError("unsupported terminal unexpectedly enabled synchronized output")


async def _run_child_workload(
    config: TerminalFrameConfig,
    *,
    mode: CapabilityMode,
    run: int,
    order: int,
    emulator_label: str | None,
    control_fd: int | None,
) -> dict[str, object]:
    collector = _FrameCollector()
    app, renderer = create_textual_tui(diagnostics=collector)
    assert isinstance(renderer, TextualTuiRenderer)
    source_complete = False

    with tempfile.TemporaryDirectory(prefix="wisp-tui-terminal-frames-") as directory:
        session = JsonlSessionStore(Path(directory)).create()
        await append_benchmark_messages(session, config.message_count)
        entries = _newest_history_entries(
            session,
            retained_history_entries=config.retained_history_entries,
        )
        chunks = tuple(
            f"## Terminal frame {index}\n\n- measured item {index}\n\n"
            for index in range(config.stream_chunks)
        )

        async def workload() -> TuiExitReason:
            nonlocal source_complete
            await _wait_for_controller(control_fd, config.negotiation_timeout_seconds)
            await _wait_for_capability_state(
                app,
                mode=mode,
                timeout=config.negotiation_timeout_seconds,
            )
            renderer.replace_history_entries(entries, session_label="Terminal frame benchmark")
            await app.wait_for_history_render()
            renderer.running()
            for index in range(config.pending_tool_cards):
                renderer.event(
                    ToolCallRequested(
                        call_id=f"terminal-frame-pending-{index}",
                        name="read",
                        arguments={"path": f"terminal-frame-{index}.txt"},
                    )
                )
            await asyncio.sleep(config.stream_interval_seconds * 2)
            collector.reset()
            collector.collecting = True
            try:
                full_update = app.screen._compositor.render_full_update()
                app._displayed_screen = None
                app._display(app.screen, full_update)
                await asyncio.sleep(config.stream_interval_seconds * 2)
                for chunk in chunks:
                    renderer.token_delta(chunk)
                    await asyncio.sleep(config.stream_interval_seconds)
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                await asyncio.sleep(config.stream_interval_seconds * 2)
                completed = app.stream_widget_for_completed_message()
                source_complete = completed is not None and completed.source == "".join(chunks)
            finally:
                collector.collecting = False
                renderer.cancelled()
                app.hide_working_indicator()
            return TuiExitReason.exited

        await app.run_shell(workload)

    return {
        "mode": mode,
        "run": run,
        "order": order,
        "emulator_label": emulator_label,
        "capability_detected": collector.capability_detected,
        "display_updates": dict(sorted(collector.display_updates.items())),
        "display_frame_cache_outcomes": dict(
            sorted(collector.display_frame_cache_outcomes.items())
        ),
        "complete_layout_count": collector.complete_layout_count,
        "chops_update_count": collector.chops_update_count,
        "emitted_spans": collector.emitted_spans,
        "suppressed_spans": collector.suppressed_spans,
        "observed_driver_frames": collector.observed_driver_frames,
        "terminal_payload_bytes": collector.terminal_payload_bytes,
        "terminal_write_count": collector.terminal_write_count,
        "terminal_flush_count": collector.terminal_flush_count,
        "exact_sync_pair_frame_count": collector.exact_sync_pair_frame_count,
        "unbalanced_sync_frame_count": collector.unbalanced_sync_frame_count,
        "writes_inside_sync": collector.writes_inside_sync,
        "writes_outside_sync": collector.writes_outside_sync,
        "out_of_band_writes": dict(sorted(collector.out_of_band_writes.items())),
        "source_complete": source_complete,
    }


def _set_terminal_size(fd: int, *, width: int, height: int) -> None:
    if os.name != "posix":
        raise RuntimeError("terminal sizing requires a POSIX pseudo-terminal")
    import fcntl
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _read_pty_process(
    process: subprocess.Popen[bytes],
    *,
    master_fd: int,
    control_fd: int,
    mode: CapabilityMode,
    negotiation_timeout: float,
    process_timeout: float,
) -> _SequenceCounter:
    counter = _SequenceCounter()
    started = time.monotonic()
    negotiation_deadline = started + negotiation_timeout
    process_deadline = started + process_timeout
    negotiation_signalled = False
    try:
        while True:
            now = time.monotonic()
            if now >= process_deadline:
                raise TimeoutError(f"{mode} terminal benchmark exceeded {process_timeout:.1f}s")
            readable, _, _ = select.select([master_fd], [], [], min(0.05, process_deadline - now))
            if readable:
                try:
                    data = os.read(master_fd, 65_536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                counter.feed(data)
                if counter.query_count and not negotiation_signalled:
                    if mode == "supported":
                        os.write(master_fd, _MODE_SUPPORTED)
                    os.write(control_fd, b"1")
                    negotiation_signalled = True
            if not negotiation_signalled and time.monotonic() >= negotiation_deadline:
                raise TimeoutError("Textual did not emit the synchronized-output capability query")
            if process.poll() is not None and not readable:
                break
        process.wait(timeout=max(0.1, process_deadline - time.monotonic()))
    except BaseException:
        _stop_process(process)
        raise
    return counter


def _close_descriptors(*descriptors: int) -> None:
    for descriptor in descriptors:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _run_pty_sample(
    config: TerminalFrameConfig,
    *,
    mode: CapabilityMode,
    run: int,
    order: int,
    emulator_label: str | None,
) -> TerminalFrameSample:
    if mode not in ("supported", "unsupported"):
        raise ValueError("PTY samples require supported or unsupported mode")
    master_fd, slave_fd = os.openpty()
    control_read_fd, control_write_fd = os.pipe()
    _set_terminal_size(
        slave_fd,
        width=config.viewport_width,
        height=config.viewport_height,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="wisp-tui-terminal-report-") as directory:
            report_path = Path(directory) / "child.json"
            command = [
                sys.executable,
                "-m",
                "benchmarks.tui_terminal_frames",
                "--child",
                "--child-mode",
                mode,
                "--child-report",
                str(report_path),
                "--control-fd",
                str(control_read_fd),
                "--run",
                str(run),
                "--order",
                str(order),
                "--messages",
                str(config.message_count),
                "--retained-history",
                str(config.retained_history_entries),
                "--stream-chunks",
                str(config.stream_chunks),
                "--stream-interval-seconds",
                str(config.stream_interval_seconds),
                "--width",
                str(config.viewport_width),
                "--height",
                str(config.viewport_height),
                "--pending-tool-cards",
                str(config.pending_tool_cards),
                "--negotiation-timeout-seconds",
                str(config.negotiation_timeout_seconds),
                "--process-timeout-seconds",
                str(config.process_timeout_seconds),
            ]
            if emulator_label is not None:
                command.extend(("--emulator-label", emulator_label))
            child_environment = os.environ.copy()
            child_environment["TERM"] = "xterm-256color"
            child_environment.pop("TERM_PROGRAM", None)
            process = subprocess.Popen(
                command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                pass_fds=(control_read_fd,),
                env=child_environment,
                cwd=Path(__file__).resolve().parents[1],
            )
            os.close(slave_fd)
            slave_fd = -1
            os.close(control_read_fd)
            control_read_fd = -1
            try:
                counter = _read_pty_process(
                    process,
                    master_fd=master_fd,
                    control_fd=control_write_fd,
                    mode=mode,
                    negotiation_timeout=config.negotiation_timeout_seconds,
                    process_timeout=config.process_timeout_seconds,
                )
            except BaseException as error:
                raise RuntimeError(f"{mode} terminal benchmark failed: {error}") from error
            if process.returncode != 0:
                raise RuntimeError(
                    f"{mode} terminal benchmark child exited with {process.returncode}"
                )
            child = json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        if process is not None:
            _stop_process(process)
        _close_descriptors(master_fd, slave_fd, control_read_fd, control_write_fd)
    return TerminalFrameSample(
        **child,
        capability_query_observed=counter.query_count > 0,
        capability_response_supplied=mode == "supported",
        process_sync_begin_count=counter.sync_begin_count,
        process_sync_end_count=counter.sync_end_count,
        process_sync_balanced=counter.sync_balanced,
    )


def run_paired_benchmark(
    config: TerminalFrameConfig | None = None,
    *,
    emulator_label: str | None = None,
) -> TerminalFrameReport:
    """Run supported and unsupported terminal modes under POSIX PTYs."""

    selected = config or TerminalFrameConfig()
    _validate_config(selected)
    if os.name != "posix":
        raise RuntimeError("paired terminal-frame measurement requires a POSIX pseudo-terminal")
    samples: list[TerminalFrameSample] = []
    for run in range(1, selected.runs + 1):
        for order, mode in enumerate(_rotated_modes(run), start=1):
            samples.append(
                _run_pty_sample(
                    selected,
                    mode=mode,
                    run=run,
                    order=order,
                    emulator_label=emulator_label,
                )
            )
    report_environment = environment()
    report_environment["textual"] = textual.__version__
    return TerminalFrameReport(
        config=selected,
        environment=report_environment,
        samples=tuple(samples),
    )


async def run_native_benchmark(
    config: TerminalFrameConfig | None = None,
    *,
    emulator_label: str | None = None,
) -> TerminalFrameReport:
    """Run the workload in the caller's real terminal for manual observation."""

    selected = config or TerminalFrameConfig(runs=1)
    _validate_config(selected)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("native terminal-frame measurement requires an interactive terminal")
    samples = []
    for run in range(1, selected.runs + 1):
        child = await _run_child_workload(
            selected,
            mode="native",
            run=run,
            order=1,
            emulator_label=emulator_label,
            control_fd=None,
        )
        samples.append(
            TerminalFrameSample(
                **child,
                capability_query_observed=None,
                capability_response_supplied=False,
                process_sync_begin_count=None,
                process_sync_end_count=None,
                process_sync_balanced=None,
            )
        )
    report_environment = environment()
    report_environment["textual"] = textual.__version__
    report_environment["term"] = os.environ.get("TERM", "")
    report_environment["term_program"] = os.environ.get("TERM_PROGRAM", "")
    return TerminalFrameReport(
        config=selected,
        environment=report_environment,
        samples=tuple(samples),
    )


def _config_from_args(parsed: argparse.Namespace) -> TerminalFrameConfig:
    return TerminalFrameConfig(
        message_count=parsed.messages,
        retained_history_entries=parsed.retained_history,
        stream_chunks=parsed.stream_chunks,
        stream_interval_seconds=parsed.stream_interval_seconds,
        viewport_width=parsed.width,
        viewport_height=parsed.height,
        runs=parsed.runs,
        pending_tool_cards=parsed.pending_tool_cards,
        negotiation_timeout_seconds=parsed.negotiation_timeout_seconds,
        process_timeout_seconds=parsed.process_timeout_seconds,
    )


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("paired", "native"), default="paired")
    parser.add_argument("--runs", type=int, default=TerminalFrameConfig.runs)
    parser.add_argument("--messages", type=int, default=TerminalFrameConfig.message_count)
    parser.add_argument(
        "--retained-history",
        type=int,
        default=TerminalFrameConfig.retained_history_entries,
    )
    parser.add_argument("--stream-chunks", type=int, default=TerminalFrameConfig.stream_chunks)
    parser.add_argument(
        "--stream-interval-seconds",
        type=float,
        default=TerminalFrameConfig.stream_interval_seconds,
    )
    parser.add_argument("--width", type=int, default=TerminalFrameConfig.viewport_width)
    parser.add_argument("--height", type=int, default=TerminalFrameConfig.viewport_height)
    parser.add_argument(
        "--pending-tool-cards",
        type=int,
        default=TerminalFrameConfig.pending_tool_cards,
    )
    parser.add_argument(
        "--negotiation-timeout-seconds",
        type=float,
        default=TerminalFrameConfig.negotiation_timeout_seconds,
    )
    parser.add_argument(
        "--process-timeout-seconds",
        type=float,
        default=TerminalFrameConfig.process_timeout_seconds,
    )
    parser.add_argument("--emulator-label")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--child-mode",
        choices=("supported", "unsupported"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--child-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--control-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--run", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--order", type=int, default=1, help=argparse.SUPPRESS)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    config = _config_from_args(parsed)
    _validate_config(config)
    if parsed.child:
        if parsed.child_mode is None or parsed.child_report is None or parsed.control_fd is None:
            raise ValueError("child mode requires mode, report path, and control descriptor")
        child = await _run_child_workload(
            config,
            mode=parsed.child_mode,
            run=parsed.run,
            order=parsed.order,
            emulator_label=parsed.emulator_label,
            control_fd=parsed.control_fd,
        )
        parsed.child_report.write_text(
            f"{json.dumps(child, sort_keys=True)}\n",
            encoding="utf-8",
        )
        return
    if parsed.mode == "native":
        report = await run_native_benchmark(config, emulator_label=parsed.emulator_label)
    else:
        report = await asyncio.to_thread(
            run_paired_benchmark,
            config,
            emulator_label=parsed.emulator_label,
        )
    payload = report.to_json()
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")
    print(payload)


def main() -> None:
    asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
