"""Provider cursor, pending request inputs, and live transcript for one loop run."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from wisp.events import ToolResultReady
from wisp.messages import Message
from wisp.providers.base import ToolCallResult


@dataclass(slots=True)
class ContinuationState:
    """Provider cursor, pending request data, and the live continuation transcript."""

    pending_tool_results: tuple[ToolCallResult, ...] = ()
    pending_extra_messages: tuple[Message, ...] = ()
    previous_response_id: str | None = None
    continuation_messages: list[Message] = field(default_factory=list)

    def record_response(self, message: Message, *, response_id: str | None) -> None:
        """Append a successful response and consume queued extra messages.

        Args:
            message (Message): Assistant transcript row with isolated tool snapshots.
            response_id (str | None): New provider cursor. None retains any prior cursor.
        """
        # Public response IDs remain upstream-observed values. Stateless
        # adapters may safely retain their existing local replay key when a
        # later clean response has no new upstream ID, so do not erase a
        # usable cursor in that case.
        if response_id is not None:
            self.previous_response_id = response_id
        self.pending_extra_messages = ()
        self.continuation_messages.append(message)

    def record_tool_result(self, result_event: ToolResultReady) -> None:
        """Append a tool's provider-visible output to the continuation transcript.

        Args:
            result_event (ToolResultReady): Result containing the call identity and output.
        """
        self.continuation_messages.append(
            Message(
                role="tool",
                content=result_event.output,
                tool_call_id=result_event.call_id,
                tool_name=result_event.name,
                is_error=result_event.is_error,
            )
        )

    def complete_tool_round(self, results: Sequence[ToolCallResult]) -> None:
        """Store batch results for the next provider request.

        Args:
            results (Sequence[ToolCallResult]): Ordered results, including any partial
                results collected before cancellation.
        """
        self.pending_tool_results = tuple(results)

    def consume_pending_tool_results(self) -> None:
        """Clear tool results once the request carrying them has been sent.

        Without this, a completed round's results stay in
        `pending_tool_results` and leak into a later, unrelated request --
        e.g. once the loop continues past a turn that had no tool calls of
        its own. `previous_response_id` is left untouched: it still points
        at the just-completed turn, and every provider natively continues
        from it with an empty `tool_results` -- no `messages` rebuild needed.
        """

        self.pending_tool_results = ()

    def queue_extra_messages(self, messages: Sequence[Message]) -> None:
        """Queue user messages for exactly the next continued request.

        Appends to the transcript and replaces the pending extra-message tuple.
        The boundary decision validator is responsible for checking message roles.

        Args:
            messages (Sequence[Message]): Validated plain user messages to inject.
        """

        queued = tuple(messages)
        self.continuation_messages.extend(queued)
        self.pending_extra_messages = queued

    def clear_native_continuation(self) -> None:
        """Discard the provider cursor and data not yet consumed by a request."""

        self.previous_response_id = None
        self.pending_tool_results = ()
        self.pending_extra_messages = ()

    def replace_context(self) -> None:
        """Atomically discard all state made obsolete by a base replacement."""

        self.clear_native_continuation()
        self.continuation_messages.clear()

    def fold_clean(self, messages: Sequence[Message]) -> Sequence[Message]:
        """Fold the just-finished turn's own answer into `messages` and reset.

        Only called when nothing in `continuation_messages` is tool-shaped (see
        `_apply_continuation` in `boundary_transitions`) -- folding a plain
        completed-turn answer and resetting `previous_response_id` is always safe
        there, since every provider's plain-message converter round-trips ordinary
        text messages correctly.

        Args:
            messages (Sequence[Message]): Portable base history to extend.

        Returns:
            Sequence[Message]: Base history followed by the live continuation. The
                continuation transcript, cursor, and pending inputs are cleared.

        Examples:
            >>> state = ContinuationState()
            >>> state.record_response(Message(role="assistant", content="Done"), response_id="r1")
            >>> folded = state.fold_clean((Message(role="user", content="Hello"),))
            >>> [message.content for message in folded]
            ['Hello', 'Done']
            >>> state.previous_response_id is None
            True
        """

        folded = (*messages, *self.continuation_messages)
        self.continuation_messages.clear()
        self.clear_native_continuation()
        return folded

    def snapshot(self) -> tuple[Message, ...]:
        """Copy the continuation transcript for a caller or hook.

        Returns:
            tuple[Message, ...]: Deep copies in transcript order. Mutating nested tool
                arguments in a snapshot cannot change the live continuation.
        """

        # `Message`/`ToolCallSnapshot` are frozen, but a `ToolCallSnapshot`'s
        # arguments contain a mutable dict. Never expose the loop's live state.
        return tuple(message.model_copy(deep=True) for message in self.continuation_messages)
