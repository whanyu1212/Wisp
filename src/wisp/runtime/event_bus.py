"""Async event bus for Wisp runtime hooks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from inspect import isawaitable

from wisp.events import WispEvent

type EventHandlerResult = Awaitable[None] | None
type EventHandler = Callable[[WispEvent], EventHandlerResult]


class EventBus:
    """Small async event bus used by extensions and the agent core."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}

    def on(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type.

        Use "*" to observe every emitted event.
        """

        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event: WispEvent) -> None:
        """Emit an event to type-specific handlers and wildcard handlers."""

        handlers = [*self._handlers.get(event.type, ()), *self._handlers.get("*", ())]
        for handler in handlers:
            result = handler(event)
            if isawaitable(result):
                await result
