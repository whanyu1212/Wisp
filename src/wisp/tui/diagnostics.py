"""Privacy-safe, opt-in diagnostics for Textual rendering hotpaths.

The production TUI does not retain diagnostics. Benchmark and test callers may
supply a sink that receives numeric timing and update-shape metadata only. Sink
failures are isolated so observation can never change rendering behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

type DisplayUpdateKind = Literal["layout", "chops", "other", "none"]
type InputEventCategory = Literal[
    "typing",
    "cursor",
    "navigation",
    "wheel",
    "submission",
    "approval",
    "cancellation",
    "paste",
]
type DisplayFrameCacheOutcome = Literal[
    "updated",
    "retained",
    "unavailable",
    "fail-open",
]
type TerminalWriteClass = Literal[
    "payload",
    "sync_begin",
    "sync_end",
    "osc52",
    "mode_query",
    "bell",
    "other",
]


@dataclass(frozen=True, slots=True)
class MarkdownDrainDiagnostic:
    """One attempted coalesced Markdown write without source content."""

    render_seconds: float
    appended_chars: int
    appended_bytes: int
    resulting_source_chars: int
    processed_source_chars: int
    reused_source_chars: int
    incremental: bool
    succeeded: bool


@dataclass(frozen=True, slots=True)
class InputLatencyDiagnostic:
    """One interactive event through the first subsequently emitted display frame."""

    category: InputEventCategory
    handler_seconds: float
    queued_seconds: float
    display_seconds: float
    total_seconds: float
    display_kind: DisplayUpdateKind


@dataclass(frozen=True, slots=True)
class DisplayUpdateDiagnostic:
    """One call at the Textual display boundary without terminal cell content."""

    kind: DisplayUpdateKind
    input_spans: int
    emitted_spans: int
    suppressed_spans: int
    frame_cache: DisplayFrameCacheOutcome
    fail_open: bool
    history_prepend_suppressed: bool
    history_prepend_unsettled: bool


@dataclass(frozen=True, slots=True)
class TerminalWriteDiagnostic:
    """One logical display frame or out-of-band driver write, without payload text."""

    display_kind: DisplayUpdateKind
    sync_available: bool
    write_count: int
    flush_count: int
    payload_bytes: int
    max_write_bytes: int
    posix_write_count: int
    windows_chunk_count: int
    sync_begin_count: int
    sync_end_count: int
    sync_order_valid: bool
    writes_inside_sync: int
    writes_outside_sync: int
    observed_driver: bool
    out_of_band: bool
    out_of_band_kind: TerminalWriteClass | None


class TuiDiagnosticsSink(Protocol):
    """Synchronous observer used only by local benchmarks and focused tests."""

    def record_markdown_drain(self, diagnostic: MarkdownDrainDiagnostic) -> None: ...

    def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None: ...

    def record_input_latency(self, diagnostic: InputLatencyDiagnostic) -> None: ...


def record_markdown_drain(
    sink: TuiDiagnosticsSink | None,
    diagnostic: MarkdownDrainDiagnostic,
) -> None:
    """Publish a Markdown sample without letting an observer affect the TUI."""

    if sink is None:
        return
    try:
        sink.record_markdown_drain(diagnostic)
    except Exception:
        return


def record_input_latency(
    sink: TuiDiagnosticsSink | None,
    diagnostic: InputLatencyDiagnostic,
) -> None:
    """Publish an input latency sample without letting an observer affect the TUI."""

    if sink is None:
        return
    try:
        sink.record_input_latency(diagnostic)
    except Exception:
        return


def record_display_update(
    sink: TuiDiagnosticsSink | None,
    diagnostic: DisplayUpdateDiagnostic,
) -> None:
    """Publish a display sample without letting an observer affect the TUI."""

    if sink is None:
        return
    try:
        sink.record_display_update(diagnostic)
    except Exception:
        return


def record_terminal_write(
    sink: TuiDiagnosticsSink | None,
    diagnostic: TerminalWriteDiagnostic,
) -> None:
    """Publish a terminal-write sample if the sink opted into the method."""

    if sink is None:
        return
    recorder = getattr(sink, "record_terminal_write", None)
    if recorder is None:
        return
    try:
        recorder(diagnostic)
    except Exception:
        return


__all__ = [
    "DisplayFrameCacheOutcome",
    "DisplayUpdateDiagnostic",
    "DisplayUpdateKind",
    "InputEventCategory",
    "InputLatencyDiagnostic",
    "MarkdownDrainDiagnostic",
    "TerminalWriteClass",
    "TerminalWriteDiagnostic",
    "TuiDiagnosticsSink",
    "record_display_update",
    "record_input_latency",
    "record_markdown_drain",
    "record_terminal_write",
]
