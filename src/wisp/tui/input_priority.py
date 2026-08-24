"""Bounded priority windows for latency-sensitive Textual input."""

from __future__ import annotations

from collections.abc import Callable
from time import perf_counter

from wisp.tui.diagnostics import InputEventCategory

type InputPriorityToken = tuple[InputEventCategory, float]


class InputPriorityPolicy:
    """Track input awaiting a visible frame without starving presentation work."""

    MAX_DEFERRAL_SECONDS = 0.1
    MAX_PENDING_INPUTS = 1_024

    def __init__(
        self,
        *,
        max_deferral_seconds: float = MAX_DEFERRAL_SECONDS,
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        if max_deferral_seconds <= 0:
            raise ValueError("input-priority deferral must be positive")
        self._max_deferral_seconds = max_deferral_seconds
        self._clock = clock
        self._pending: dict[InputPriorityToken, None] = {}
        self._deadline: float | None = None

    @property
    def pending_count(self) -> int:
        """Return input observations still awaiting a displayed frame."""

        self._expire(self._clock())
        return len(self._pending)

    def observe_input(self, token: InputPriorityToken, *, now: float | None = None) -> bool:
        """Open or extend the window for one newly routed input event."""

        observed_at = self._clock() if now is None else now
        self._expire(observed_at)
        if token in self._pending:
            return False
        if len(self._pending) >= self.MAX_PENDING_INPUTS:
            del self._pending[next(iter(self._pending))]
        self._pending[token] = None
        self._deadline = observed_at + self._max_deferral_seconds
        return True

    def cancel_input(self, token: InputPriorityToken) -> None:
        """Forget an observation whose handler failed before completing."""

        self._pending.pop(token, None)
        if not self._pending:
            self._deadline = None

    def frame_emitted(self, *, now: float | None = None) -> bool:
        """Close the active window after terminal-visible output was emitted."""

        displayed_at = self._clock() if now is None else now
        self._expire(displayed_at)
        if not self._pending:
            return False
        self._pending.clear()
        self._deadline = None
        return True

    def drain_delay(
        self,
        deferred_since: float | None,
        *,
        now: float | None = None,
    ) -> tuple[float, float | None]:
        """Return a bounded delay and the stable start of this drain's wait."""

        checked_at = self._clock() if now is None else now
        self._expire(checked_at)
        deadline = self._deadline
        if not self._pending or deadline is None:
            return 0.0, None
        started_at = checked_at if deferred_since is None else deferred_since
        drain_deadline = min(deadline, started_at + self._max_deferral_seconds)
        delay = max(0.0, drain_deadline - checked_at)
        return (delay, started_at) if delay > 0 else (0.0, None)

    def _expire(self, now: float) -> None:
        deadline = self._deadline
        if deadline is not None and now >= deadline:
            self._pending.clear()
            self._deadline = None


__all__ = ["InputPriorityPolicy", "InputPriorityToken"]
