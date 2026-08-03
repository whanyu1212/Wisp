"""Bound the transcript widgets while retaining already visited entries."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TypeVar

TUI_TRANSCRIPT_WINDOW_SIZE = 300
TUI_TRANSCRIPT_WINDOW_SHIFT = 75

Entry = TypeVar("Entry")


@dataclass
class TranscriptWindow[Entry]:
    """A movable bounded view over chronological transcript entries.

    The caller owns durable paging and entry rendering. This controller only retains
    entries that have already been seen by the UI and selects the bounded slice that
    should be mounted at a given point in scrollback navigation.
    """

    capacity: int = TUI_TRANSCRIPT_WINDOW_SIZE
    shift: int = TUI_TRANSCRIPT_WINDOW_SHIFT
    _entries: list[Entry] = field(default_factory=list, init=False, repr=False)
    _start: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        if not 1 <= self.shift <= self.capacity:
            raise ValueError("shift must be between one and capacity")

    @property
    def entries(self) -> tuple[Entry, ...]:
        """Return every entry retained during this UI session."""

        return tuple(self._entries)

    @property
    def visible(self) -> tuple[Entry, ...]:
        """Return the current bounded chronological mount target."""

        return tuple(self._entries[self._start : self._start + self.capacity])

    @property
    def is_at_oldest(self) -> bool:
        return self._start == 0

    @property
    def is_at_latest(self) -> bool:
        return self._start + self.capacity >= len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._start = 0

    def replace(self, entries: Iterable[Entry]) -> None:
        self._entries = list(entries)
        self.show_latest()

    def prepend(self, entries: Iterable[Entry]) -> None:
        added = list(entries)
        if not added:
            return
        self._entries[0:0] = added
        self._start = min(
            self._start + len(added),
            max(0, len(self._entries) - self.capacity),
        )

    def append(self, entries: Iterable[Entry], *, follow_tail: bool) -> None:
        added = list(entries)
        if not added:
            return
        self._entries.extend(added)
        if follow_tail:
            self.show_latest()

    def show_latest(self) -> bool:
        target = max(0, len(self._entries) - self.capacity)
        changed = target != self._start
        self._start = target
        return changed

    def shift_older(self) -> bool:
        target = max(0, self._start - self.shift)
        changed = target != self._start
        self._start = target
        return changed

    def shift_newer(self) -> bool:
        target = min(
            max(0, len(self._entries) - self.capacity),
            self._start + self.shift,
        )
        changed = target != self._start
        self._start = target
        return changed


__all__ = [
    "TUI_TRANSCRIPT_WINDOW_SHIFT",
    "TUI_TRANSCRIPT_WINDOW_SIZE",
    "TranscriptWindow",
]
