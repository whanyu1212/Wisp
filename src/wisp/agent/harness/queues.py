"""Steering and follow-up message queues owned by one AgentHarness."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from uuid import uuid4

from wisp.events import QueueKind, QueueMode, QueueUpdated
from wisp.messages import Message


def queue_full_message(max_messages: int) -> str:
    """Return the error for a message that would exceed the queued-message limit.

    Args:
        max_messages (int): Maximum messages allowed across both queues.

    Returns:
        str: The rejection reported to whoever submitted the message.
    """
    return f"Agent queue is full (maximum {max_messages} pending messages)"


@dataclass(frozen=True, slots=True)
class QueuedMessages:
    """Detached snapshot of harness-owned queued user messages."""

    steering: tuple[Message, ...] = ()
    follow_up: tuple[Message, ...] = ()

    @property
    def count(self) -> int:
        """Return the total number of queued messages."""
        return len(self.steering) + len(self.follow_up)


class _QueueStore:
    """Hold both FIFO user-message queues, their shared limits, and one revision token.

    Every change replaces the revision token, so a holder of an older
    `QueueUpdated.token` can tell that the queues changed. Limits and drain modes
    come from the harness configuration, which can be replaced between runs, so
    callers pass the current values instead of the queues storing them.

    Queued messages are detached copies. Draining preserves entry identity:
    `next_batch()` returns the live entries, and `take_next()` removes one only
    while it is still at the head, so edits made during a drain stay visible.
    """

    def __init__(self) -> None:
        self._steering: deque[Message] = deque()
        self._follow_up: deque[Message] = deque()
        self._token = uuid4().hex

    @property
    def count(self) -> int:
        """Return the total number of queued messages without copying them."""
        return len(self._steering) + len(self._follow_up)

    @property
    def byte_size(self) -> int:
        """Return the serialized bytes retained across both queues."""
        return sum(_message_size(message) for message in (*self._steering, *self._follow_up))

    def has_messages(self, kind: QueueKind | None = None) -> bool:
        """Return whether one queue, or either queue when kind is None, is non-empty.

        Args:
            kind (QueueKind | None): Queue to inspect, or None for both.

        Returns:
            bool: True when at least one message is queued.
        """
        if kind is None:
            return bool(self._steering or self._follow_up)
        return bool(self._queue(kind))

    def snapshot(self) -> QueuedMessages:
        """Return detached copies of both queues, including nested message data."""
        return QueuedMessages(
            steering=tuple(message.model_copy(deep=True) for message in self._steering),
            follow_up=tuple(message.model_copy(deep=True) for message in self._follow_up),
        )

    def enqueue(
        self,
        kind: QueueKind,
        message: Message,
        *,
        max_messages: int,
        max_bytes: int,
    ) -> None:
        """Append a detached copy of a user message after checking the shared limits.

        Args:
            kind (QueueKind): Queue to append to.
            message (Message): User message to copy into the queue.
            max_messages (int): Maximum messages allowed across both queues.
            max_bytes (int): Maximum serialized bytes allowed across both queues.

        Raises:
            ValueError: The message is not a user message, or the kind is unknown.
            RuntimeError: Adding the message would exceed either limit.
        """
        if message.role != "user":
            raise ValueError("AgentHarness queues require a user message")
        if self.count >= max_messages:
            raise RuntimeError(queue_full_message(max_messages))
        if _message_size(message) > max_bytes - self.byte_size:
            raise RuntimeError(
                f"Agent queue byte limit exceeded (maximum {max_bytes} pending bytes)"
            )
        self._queue(kind).append(message.model_copy(deep=True))
        self.mark_changed()

    def pop_latest(self, kind: QueueKind) -> Message | None:
        """Remove and return the most recently queued message, if any.

        Args:
            kind (QueueKind): Queue to pop from.

        Returns:
            Message | None: The removed message, or None when the queue is empty.
        """
        queue = self._queue(kind)
        if not queue:
            return None
        self.mark_changed()
        return queue.pop()

    def clear(self, kind: QueueKind) -> tuple[Message, ...]:
        """Clear one queue.

        Args:
            kind (QueueKind): Queue to clear.

        Returns:
            tuple[Message, ...]: The removed messages in FIFO order.
        """
        queue = self._queue(kind)
        cleared = tuple(queue)
        queue.clear()
        self.mark_changed()
        return cleared

    def clear_all(self) -> QueuedMessages:
        """Clear both queues.

        Returns:
            QueuedMessages: Detached copies of the removed messages.
        """
        cleared = self.snapshot()
        self._steering.clear()
        self._follow_up.clear()
        self.mark_changed()
        return cleared

    def next_batch(self, kind: QueueKind, mode: QueueMode) -> tuple[Message, ...]:
        """Select the live entries the next drain of one queue will try to inject.

        Args:
            kind (QueueKind): Queue to drain.
            mode (QueueMode): `"one_at_a_time"` selects the head; `"all"` selects
                the whole current queue.

        Returns:
            tuple[Message, ...]: Queued entries themselves, not copies, in FIFO order.
        """
        batch = tuple(self._queue(kind))
        return batch[:1] if mode == "one_at_a_time" else batch

    def take_next(self, kind: QueueKind, expected: Message) -> Message | None:
        """Remove the queue head only if it is still the expected entry.

        Args:
            kind (QueueKind): Queue being drained.
            expected (Message): Entry selected by `next_batch()`.

        Returns:
            Message | None: The removed entry, or None when it was edited away or
                the queue was cleared since the batch was selected.
        """
        queue = self._queue(kind)
        if not queue or queue[0] is not expected:
            return None
        self.mark_changed()
        return queue.popleft()

    def mark_changed(self) -> None:
        """Replace the revision token, for example after a drain-mode change."""
        self._token = uuid4().hex

    def updated_event(self, *, steering_mode: QueueMode, follow_up_mode: QueueMode) -> QueueUpdated:
        """Describe the current queues and their revision.

        Args:
            steering_mode (QueueMode): Configured steering drain mode.
            follow_up_mode (QueueMode): Configured follow-up drain mode.

        Returns:
            QueueUpdated: User-visible contents, drain modes, and the revision token.
        """
        return QueueUpdated(
            steering=tuple(message.user_visible_content for message in self._steering),
            follow_up=tuple(message.user_visible_content for message in self._follow_up),
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
            token=self._token,
        )

    def _queue(self, kind: QueueKind) -> deque[Message]:
        if kind == "steering":
            return self._steering
        if kind == "follow_up":
            return self._follow_up
        raise ValueError(f"Unsupported queue kind: {kind!r}")


def _message_size(message: Message) -> int:
    return len(message.model_dump_json().encode("utf-8"))
