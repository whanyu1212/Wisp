"""One app-owned clock for lightweight animated TUI presentation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import floor
from time import monotonic
from typing import Protocol

from textual.timer import Timer


class PresentationClockSubscriber(Protocol):
    """A mounted presentation object that needs a periodic monotonic timestamp."""

    def presentation_clock_tick(self, now: float) -> None: ...


@dataclass
class _Subscription:
    interval: float
    next_tick: float


class PresentationClock:
    """Coalesce widget animation timers behind one lazily active app interval."""

    INTERVAL = 0.08

    def __init__(
        self,
        set_interval: Callable[[float, Callable[[], None]], Timer],
    ) -> None:
        self._set_interval = set_interval
        self._subscribers: dict[PresentationClockSubscriber, _Subscription] = {}
        self._timer: Timer | None = None

    @property
    def subscriber_count(self) -> int:
        """Return the number of mounted presentations sharing this clock."""

        return len(self._subscribers)

    @property
    def is_running(self) -> bool:
        """Return whether the shared Textual interval is currently active."""

        return self._timer is not None

    def subscribe(
        self,
        subscriber: PresentationClockSubscriber,
        *,
        interval: float = INTERVAL,
    ) -> None:
        """Register one subscriber at its cadence and start the shared interval."""

        if subscriber in self._subscribers:
            return
        if interval <= 0:
            raise ValueError("presentation clock interval must be positive")
        self._subscribers[subscriber] = _Subscription(
            interval=interval,
            next_tick=monotonic() + interval,
        )
        if self._timer is None:
            self._timer = self._set_interval(self.INTERVAL, self._tick)

    def unsubscribe(self, subscriber: PresentationClockSubscriber) -> None:
        """Remove one subscriber and stop the interval when the set becomes empty."""

        self._subscribers.pop(subscriber, None)
        if not self._subscribers and self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        """Notify a stable snapshot so callbacks may unsubscribe safely."""

        now = monotonic()
        for subscriber, subscription in tuple(self._subscribers.items()):
            if now < subscription.next_tick:
                continue
            skipped = floor((now - subscription.next_tick) / subscription.interval) + 1
            subscription.next_tick += skipped * subscription.interval
            subscriber.presentation_clock_tick(now)


__all__ = ["PresentationClock", "PresentationClockSubscriber"]
