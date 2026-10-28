"""Bounded in-memory continuation state shared by provider adapters."""

from __future__ import annotations

from collections import OrderedDict

from wisp.messages import Message, NativeOutput


class ContinuationStore[T]:
    """Store provider-native continuation payloads by response ID.

    Operations are synchronous and contain no suspension points. The store is
    therefore safe under Wisp's current assumption that a provider instance is
    driven by one active run at a time; it does not coordinate concurrent runs.
    """

    def __init__(self, *, capacity: int = 128) -> None:
        if capacity <= 0:
            raise ValueError("continuation store capacity must be positive")
        self._capacity = capacity
        self._values: OrderedDict[str, T] = OrderedDict()

    def __len__(self) -> int:
        return len(self._values)

    def get(self, response_id: str | None, *, refresh: bool = False) -> T | None:
        """Return continuation state without consuming it.

        ``refresh`` updates the entry's recency for providers whose existing
        eviction behavior is access-ordered rather than insertion-ordered.
        """

        if response_id is None:
            return None
        try:
            value = self._values[response_id]
        except KeyError:
            return None
        if refresh:
            self._values.move_to_end(response_id)
        return value

    def consume(self, response_id: str | None) -> T | None:
        """Remove and return continuation state when an attempt completes."""

        if response_id is None:
            return None
        return self._values.pop(response_id, None)

    def remember(self, response_id: str, value: T) -> None:
        """Store state as the newest entry and evict the oldest if needed."""

        self._values.pop(response_id, None)
        self._values[response_id] = value
        while len(self._values) > self._capacity:
            self._values.popitem(last=False)

    def discard(self, response_id: str | None) -> None:
        """Remove continuation state without returning it, if present."""

        if response_id is not None:
            self._values.pop(response_id, None)


def replayable_native_items(
    message: Message,
    recorded: ContinuationStore[NativeOutput],
    *,
    provider: str,
    model: str,
) -> tuple[dict[str, object], ...] | None:
    """Return the output items that produced an assistant row, if replayable.

    Items saved on the row win over the in-memory record, so a new process
    replays the same items. Encrypted reasoning belongs to the model that
    produced it, so items from another provider or model are not replayed. A
    record without a message item cannot represent a row that has text, so
    that row keeps its portable form.

    Args:
        message (Message): Transcript row being rebuilt for a request.
        recorded (ContinuationStore[NativeOutput]): The provider's in-memory
            records by response ID; a hit refreshes the record's recency.
        provider (str): Name of the provider building the request.
        model (str): Model the request is sent to.

    Returns:
        tuple[dict[str, object], ...] | None: The response's items in their
        original order, or None to render the row in portable form.
    """

    if message.role != "assistant" or message.response_id is None:
        return None
    native = message.native_output or recorded.get(message.response_id, refresh=True)
    if native is None or native.provider != provider or native.model != model:
        return None
    if message.content and not any(item.get("type") == "message" for item in native.items):
        return None
    return native.items
