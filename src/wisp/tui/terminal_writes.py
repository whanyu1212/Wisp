"""Privacy-safe observation of Textual driver writes.

The production TUI never installs this observer. Benchmarks and focused tests
attach it only when a diagnostics sink is present. Observation never emits CSI
sequences, never retains write text, and cannot change driver behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from math import ceil
from typing import cast

from rich.console import Console, RenderableType
from textual._compositor import ChopsUpdate, CompositorUpdate, LayoutUpdate

from wisp.tui.diagnostics import (
    DisplayUpdateKind,
    TerminalWriteClass,
    TerminalWriteDiagnostic,
    TuiDiagnosticsSink,
    record_terminal_write,
)

# Textual 8.2.8 writes these exact sequences from App._begin_update/_end_update
# and the Linux driver mode query. Duplicated here to avoid importing Textual
# private modules.
_SYNC_START = "\x1b[?2026h"
_SYNC_END = "\x1b[?2026l"
_OSC52_PREFIX = "\x1b]52;"
_MODE_QUERY = "\x1b[?2026$p"
_BELL = "\x07"
_WINDOWS_WRITE_CHUNK_SIZE = 8192


def classify_terminal_write(data: str, *, in_display: bool) -> TerminalWriteClass:
    """Classify a driver write without retaining its contents."""

    if data == _SYNC_START:
        return "sync_begin"
    if data == _SYNC_END:
        return "sync_end"
    if data.startswith(_OSC52_PREFIX):
        return "osc52"
    if data == _MODE_QUERY:
        return "mode_query"
    if data == _BELL:
        return "bell"
    return "payload" if in_display else "other"


def payload_bytes_for_renderable(renderable: object, console: Console) -> int:
    """Return UTF-8 size of one prepared display payload, excluding CSI 2026."""

    if renderable is None:
        return 0
    try:
        if isinstance(renderable, CompositorUpdate):
            sequence = renderable.render_segments(console)
        else:
            segments = console.render(cast(RenderableType, renderable))
            sequence = console._render_buffer(segments)
    except Exception:
        return 0
    return len(sequence.encode("utf-8"))


def windows_chunk_count(payload_bytes: int) -> int:
    """Return how many 8 KiB writes Textual would emit on Windows."""

    if payload_bytes <= 0:
        return 0
    return ceil(payload_bytes / _WINDOWS_WRITE_CHUNK_SIZE)


class TerminalWriteObserver:
    """Count driver writes for one diagnostics-enabled Textual app."""

    def __init__(self, sink: TuiDiagnosticsSink) -> None:
        self._sink = sink
        self._driver: object | None = None
        self._original_write: Callable[[str], None] | None = None
        self._original_flush: Callable[[], None] | None = None
        self._wrapped_write: Callable[[str], None] | None = None
        self._wrapped_flush: Callable[[], None] | None = None
        self._in_display = False
        self._reset_frame()

    def attach(self, driver: object | None) -> None:
        """Wrap ``driver.write`` / ``flush`` if this is a new driver."""

        if driver is None or driver is self._driver:
            return
        self.detach()
        write = getattr(driver, "write", None)
        flush = getattr(driver, "flush", None)
        if not callable(write):
            return
        self._driver = driver
        self._original_write = cast(Callable[[str], None], write)
        self._original_flush = cast(Callable[[], None], flush) if callable(flush) else None
        # Bound methods are recreated on access, so restore must compare the
        # exact wrapper objects assigned onto the driver.
        self._wrapped_write = self._write
        driver.write = self._wrapped_write  # type: ignore[attr-defined]
        if self._original_flush is not None:
            self._wrapped_flush = self._flush
            driver.flush = self._wrapped_flush  # type: ignore[attr-defined]

    def detach(self) -> None:
        """Restore the original driver methods if this observer still owns them."""

        driver = self._driver
        if driver is None:
            return
        if (
            self._original_write is not None
            and self._wrapped_write is not None
            and getattr(driver, "write", None) is self._wrapped_write
        ):
            driver.write = self._original_write  # type: ignore[attr-defined]
        if (
            self._original_flush is not None
            and self._wrapped_flush is not None
            and getattr(driver, "flush", None) is self._wrapped_flush
        ):
            driver.flush = self._original_flush  # type: ignore[attr-defined]
        self._driver = None
        self._original_write = None
        self._original_flush = None
        self._wrapped_write = None
        self._wrapped_flush = None
        self._in_display = False

    def begin_frame(self, driver: object | None) -> None:
        """Start attributing subsequent driver writes to one ``_display`` call."""

        self.attach(driver)
        self._reset_frame()
        self._in_display = True

    def finish_frame(
        self,
        renderable: object,
        *,
        sync_available: bool,
        console: Console,
    ) -> None:
        """Publish one frame sample after ``super()._display`` returns."""

        self._in_display = False
        observed_driver = self._write_count > 0 or self._flush_count > 0
        if renderable is None and not observed_driver:
            return
        payload_bytes = payload_bytes_for_renderable(renderable, console)
        posix_writes = 1 if payload_bytes else 0
        write_count = self._write_count if observed_driver else posix_writes
        record_terminal_write(
            self._sink,
            TerminalWriteDiagnostic(
                display_kind=_display_kind(renderable),
                sync_available=sync_available,
                write_count=write_count,
                flush_count=self._flush_count,
                payload_bytes=payload_bytes,
                max_write_bytes=self._max_payload_write_bytes if observed_driver else payload_bytes,
                posix_write_count=posix_writes,
                windows_chunk_count=windows_chunk_count(payload_bytes),
                sync_begin_count=self._sync_begin_count,
                sync_end_count=self._sync_end_count,
                writes_inside_sync=self._writes_inside_sync,
                writes_outside_sync=self._writes_outside_sync,
                observed_driver=observed_driver,
                out_of_band=False,
                out_of_band_kind=None,
            ),
        )

    def _reset_frame(self) -> None:
        self._write_count = 0
        self._flush_count = 0
        self._sync_begin_count = 0
        self._sync_end_count = 0
        self._writes_inside_sync = 0
        self._writes_outside_sync = 0
        self._sync_depth = 0
        self._max_payload_write_bytes = 0

    def _write(self, data: str) -> None:
        original = self._original_write
        if original is None:
            return
        try:
            self._note_write(data)
        except Exception:
            pass
        original(data)

    def _flush(self) -> None:
        original = self._original_flush
        if self._in_display:
            self._flush_count += 1
        if original is not None:
            original()

    def _note_write(self, data: str) -> None:
        kind = classify_terminal_write(data, in_display=self._in_display)
        if not self._in_display:
            self._record_out_of_band(kind)
            return
        self._write_count += 1
        if kind == "sync_begin":
            self._sync_begin_count += 1
            self._sync_depth += 1
            return
        if kind == "sync_end":
            self._sync_end_count += 1
            self._sync_depth = max(0, self._sync_depth - 1)
            return
        if self._sync_depth > 0:
            self._writes_inside_sync += 1
        else:
            self._writes_outside_sync += 1
        if kind == "payload":
            self._max_payload_write_bytes = max(
                self._max_payload_write_bytes,
                len(data.encode("utf-8")),
            )

    def _record_out_of_band(self, kind: TerminalWriteClass) -> None:
        record_terminal_write(
            self._sink,
            TerminalWriteDiagnostic(
                display_kind="none",
                sync_available=False,
                write_count=1,
                flush_count=0,
                payload_bytes=0,
                max_write_bytes=0,
                posix_write_count=0,
                windows_chunk_count=0,
                sync_begin_count=1 if kind == "sync_begin" else 0,
                sync_end_count=1 if kind == "sync_end" else 0,
                writes_inside_sync=0,
                writes_outside_sync=1,
                observed_driver=True,
                out_of_band=True,
                out_of_band_kind=kind,
            ),
        )


def _display_kind(renderable: object) -> DisplayUpdateKind:
    if renderable is None:
        return "none"
    if isinstance(renderable, LayoutUpdate):
        return "layout"
    if isinstance(renderable, ChopsUpdate):
        return "chops"
    return "other"


__all__ = [
    "TerminalWriteObserver",
    "classify_terminal_write",
    "payload_bytes_for_renderable",
    "windows_chunk_count",
]
