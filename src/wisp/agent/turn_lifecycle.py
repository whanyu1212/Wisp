"""Track one invocation's turn lifecycle so each started turn ends exactly once."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from wisp.events import (
    ErrorEvent,
    FinishReason,
    MessageCompleted,
    RunOutcome,
    TurnCompleted,
    TurnStarted,
)

CANCELLED_RUN_MESSAGE = "Agent run cancelled"


@dataclass(slots=True)
class TurnLifecycle:
    """Know which turn is open and build the events that end it.

    `run_agent_loop` publishes turns through `start()` and `complete()`, which reject
    a second start or a second completion. The harness and session consume those
    events through `observe()`, and use `terminal_events()` for the turn-ending
    events they must publish themselves, such as after a hard cancellation or an
    exception the loop never saw. Terminal builders close the open turn, so a
    later terminal for the same turn is never produced.

    Attributes:
        open_turn (int | None): Turn that has started but not completed, if any.
        latest_turn (int): Most recently started turn, or 0 before any turn starts.
        had_tool_calls (bool): Whether the open or most recently completed turn's
            response requested tools. Reset when the next turn starts.
    """

    open_turn: int | None = None
    latest_turn: int = 0
    had_tool_calls: bool = False

    def start(self, turn: int) -> TurnStarted:
        """Open a turn and build its start event.

        Args:
            turn (int): Number of the new turn.

        Returns:
            TurnStarted: Event to publish for the new turn.

        Raises:
            RuntimeError: Another turn is still open.
        """
        if self.open_turn is not None:
            raise RuntimeError(f"Turn {turn} started while turn {self.open_turn} is still open")
        event = TurnStarted(turn=turn)
        self.observe(event)
        return event

    def complete(self, outcome: RunOutcome, finish_reason: FinishReason) -> TurnCompleted:
        """Close the open turn and build its completion event.

        Args:
            outcome (RunOutcome): How the turn ended.
            finish_reason (FinishReason): Provider or synthetic reason for the ending.

        Returns:
            TurnCompleted: Event to publish for the closed turn.

        Raises:
            RuntimeError: No turn is open, including when it was already completed.
        """
        if self.open_turn is None:
            raise RuntimeError("No open turn to complete")
        event = TurnCompleted(turn=self.open_turn, outcome=outcome, finish_reason=finish_reason)
        self.observe(event)
        return event

    def terminal_events(
        self,
        message: str,
        *,
        outcome: Literal["failed", "cancelled"],
    ) -> tuple[ErrorEvent | TurnCompleted, ...]:
        """Build the error that ends a run, closing the open turn if there is one.

        Args:
            message (str): Error text to publish.
            outcome (Literal["failed", "cancelled"]): Outcome for the open turn; its
                finish reason is "error" or "cancelled" respectively.

        Returns:
            tuple[ErrorEvent | TurnCompleted, ...]: The error, followed by a completion
                for the open turn only when one is open.

        Examples:
            >>> lifecycle = TurnLifecycle()
            >>> _ = lifecycle.start(1)
            >>> [event.type for event in lifecycle.cancelled_events()]
            ['error', 'turn.completed']
            >>> [event.type for event in lifecycle.cancelled_events()]
            ['error']
        """
        error = ErrorEvent(message=message)
        if self.open_turn is None:
            return (error,)
        finish_reason: FinishReason = "error" if outcome == "failed" else "cancelled"
        return (error, self.complete(outcome, finish_reason))

    def cancelled_events(self) -> tuple[ErrorEvent | TurnCompleted, ...]:
        """Build the standard cancellation terminal, closing the open turn if any.

        Returns:
            tuple[ErrorEvent | TurnCompleted, ...]: Cancellation error, followed by a
                cancelled completion only when a turn is open.
        """
        return self.terminal_events(CANCELLED_RUN_MESSAGE, outcome="cancelled")

    def observe(self, event: object) -> None:
        """Update turn state from an event published by this or another layer.

        Observation never raises, so a consumer can apply it to every event it
        forwards. Events that do not affect turn state are ignored.

        Args:
            event (object): Event being published.
        """
        if isinstance(event, TurnStarted):
            self.open_turn = event.turn
            self.latest_turn = event.turn
            self.had_tool_calls = False
        elif isinstance(event, MessageCompleted) and event.tool_calls:
            self.had_tool_calls = True
        elif isinstance(event, TurnCompleted):
            self.open_turn = None


__all__ = ["CANCELLED_RUN_MESSAGE", "TurnLifecycle"]
