"""Bound mounted transcript widgets and retained scrollback metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TypeVar

TUI_TRANSCRIPT_WINDOW_SIZE = 60
TUI_TRANSCRIPT_WINDOW_SHIFT = 40
TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT = 1_200

Entry = TypeVar("Entry")


@dataclass
class TranscriptWindow[Entry]:
    """A bounded retained history with a movable mounted-entry view.

    The caller owns durable paging and entry rendering. When older pages push retention
    over its bound, the newest entries are evicted so the reader's current older view
    remains available. ``latest_is_retained`` then tells the caller to reload the
    durable latest page before jumping to the transcript tail.
    """

    capacity: int = TUI_TRANSCRIPT_WINDOW_SIZE
    shift: int = TUI_TRANSCRIPT_WINDOW_SHIFT
    retained_capacity: int = TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT
    _entries: list[Entry] = field(default_factory=list, init=False, repr=False)
    _start: int = field(default=0, init=False, repr=False)
    _latest_is_retained: bool = field(default=True, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be positive")
        if not 1 <= self.shift <= self.capacity:
            raise ValueError("shift must be between one and capacity")
        if self.retained_capacity < self.capacity:
            raise ValueError("retained_capacity must be at least capacity")

    @property
    def entries(self) -> tuple[Entry, ...]:
        """Return the bounded set of entries retained by this UI session."""

        return tuple(self._entries)

    @property
    def retained_count(self) -> int:
        """Return the number of retained entries without copying them."""

        return len(self._entries)

    @property
    def visible(self) -> tuple[Entry, ...]:
        """Return the current bounded chronological mount target."""

        return tuple(self._entries[self._start : self._start + self.capacity])

    @property
    def visible_append_capacity(self) -> int:
        """Return how many appended entries fit in the current mounted slice."""

        return self.capacity - len(self.visible)

    @property
    def is_at_oldest(self) -> bool:
        return self._start == 0

    @property
    def is_at_latest(self) -> bool:
        return self._start + self.capacity >= len(self._entries)

    @property
    def latest_is_retained(self) -> bool:
        """Whether the current durable transcript tail is in retained entries."""

        return self._latest_is_retained

    def clear(self) -> None:
        self._entries.clear()
        self._start = 0
        self._latest_is_retained = True

    def replace(self, entries: Iterable[Entry]) -> None:
        self._entries = list(entries)
        if len(self._entries) > self.retained_capacity:
            del self._entries[: len(self._entries) - self.retained_capacity]
        self._latest_is_retained = True
        self.show_latest()

    def prepend(self, entries: Iterable[Entry]) -> tuple[Entry, ...]:
        """Retain an older page and return entries evicted from the newest edge."""

        added = list(entries)
        if not added:
            return ()
        self._entries[0:0] = added
        self._start += len(added)
        evicted: tuple[Entry, ...] = ()
        if len(self._entries) > self.retained_capacity:
            overflow = len(self._entries) - self.retained_capacity
            evicted = tuple(self._entries[-overflow:])
            del self._entries[-overflow:]
            self._latest_is_retained = False
        self._start = min(
            self._start,
            max(0, len(self._entries) - self.capacity),
        )
        return evicted

    def append(self, entries: Iterable[Entry], *, follow_tail: bool) -> tuple[Entry, ...]:
        """Retain newer entries and return entries evicted from the oldest edge."""

        added = list(entries)
        if not added:
            return ()
        self._entries.extend(added)
        evicted: tuple[Entry, ...] = ()
        if len(self._entries) > self.retained_capacity:
            overflow = len(self._entries) - self.retained_capacity
            evicted = tuple(self._entries[:overflow])
            del self._entries[:overflow]
            self._start = max(0, self._start - overflow)
        if follow_tail:
            self.show_latest()
        return evicted

    def show_latest(self) -> bool:
        target = max(0, len(self._entries) - self.capacity)
        changed = target != self._start
        self._start = target
        return changed

    def show_oldest(self) -> bool:
        """Move the mounted window directly to the oldest retained entries."""

        changed = self._start != 0
        self._start = 0
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
    "TUI_TRANSCRIPT_RETAINED_ENTRY_LIMIT",
    "TUI_TRANSCRIPT_WINDOW_SIZE",
    "TranscriptWindow",
]
