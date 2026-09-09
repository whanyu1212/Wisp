"""Coordinate request boundaries and transcript transitions within one harness run."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    ContextOverflowHook,
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
)
from wisp.providers.base import Provider, prepare_provider_history


@dataclass(frozen=True, slots=True)
class HarnessBoundaryContext:
    """Harness state available to session-owned request-boundary preparation."""

    snapshot: RequestBoundarySnapshot
    messages: tuple[Message, ...]
    active_from: int
    injected_messages: tuple[Message, ...]
    stop_by_default: bool


class HarnessBoundaryPreparer(Protocol):
    """Prepare compaction/rebase decisions without owning queues or the loop."""

    async def prepare_boundary(
        self, *, context: HarnessBoundaryContext
    ) -> RequestBoundaryDecision | None:
        """Prepare a session-owned decision before the next provider request.

        Args:
            context (HarnessBoundaryContext): Transcript snapshot and queue
                effects associated with the completed turn.

        Returns:
            RequestBoundaryDecision | None: A complete decision, or None to use
            the harness's normal append, replacement, or stop behavior.
        """
        ...


@dataclass(frozen=True, slots=True)
class _ArmedRequestBoundary:
    """Queue effects exposed before the loop invokes its boundary hook."""

    turn: int
    had_tool_calls: bool
    injected_messages: tuple[Message, ...]
    stop_by_default: bool


@dataclass(slots=True)
class _HarnessBoundaryCoordinator:
    """Coordinate one run's request decisions and delayed transcript updates.

    The runner supplies transcript snapshots and owns every transcript mutation.
    It arms this coordinator after emitting queue effects, then consumes the
    prepared replacement when the loop starts the next turn.
    """

    get_messages: Callable[[], tuple[Message, ...]]
    provider: Provider
    effort: str | None
    active_from: int
    boundary_preparer: HarnessBoundaryPreparer | None
    context_overflow_hook: ContextOverflowHook | None
    armed_boundary: _ArmedRequestBoundary | None = None
    pending_transcript_transition: tuple[RequestBoundaryDecision, tuple[Message, ...]] | None = None

    def arm(
        self,
        *,
        turn: int,
        had_tool_calls: bool,
        injected_messages: Sequence[Message],
        stop_by_default: bool,
    ) -> None:
        self.armed_boundary = _ArmedRequestBoundary(
            turn=turn,
            had_tool_calls=had_tool_calls,
            injected_messages=tuple(injected_messages),
            stop_by_default=stop_by_default,
        )

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        boundary = self.armed_boundary
        if boundary is None:
            raise RuntimeError("AgentHarness received an unarmed request boundary")
        if snapshot.turn != boundary.turn or snapshot.had_tool_calls != boundary.had_tool_calls:
            raise RuntimeError("AgentHarness request boundary did not match its completed turn")
        self.armed_boundary = None

        if self.boundary_preparer is not None:
            decision = await self.boundary_preparer.prepare_boundary(
                context=HarnessBoundaryContext(
                    snapshot=snapshot,
                    messages=tuple(
                        message.model_copy(deep=True) for message in self.get_messages()
                    ),
                    active_from=self.active_from,
                    injected_messages=boundary.injected_messages,
                    stop_by_default=boundary.stop_by_default,
                )
            )
            if decision is not None:
                self._remember_transcript_transition(decision, snapshot.continuation_messages)
                return decision

        if boundary.injected_messages:
            if snapshot.can_append_user_messages:
                return RequestBoundaryDecision(extra_messages=boundary.injected_messages)
            # Cursor-less structured history cannot be flattened into
            # extras. Replace from the complete normalized transcript,
            # retaining assistant/tool pairs atomically.
            return RequestBoundaryDecision(
                messages=prepare_provider_history(
                    self.get_messages(),
                    provider=self.provider,
                    effort=self.effort,
                    active_from=self.active_from,
                )
            )
        return RequestBoundaryDecision(stop=boundary.stop_by_default)

    async def recover_context_overflow(
        self, *, snapshot: ContextOverflowSnapshot
    ) -> RequestBoundaryDecision | None:
        if self.context_overflow_hook is None:
            raise RuntimeError("AgentHarness received context overflow without a recovery hook")
        decision = await self.context_overflow_hook.recover_context_overflow(snapshot=snapshot)
        if decision is not None:
            self._remember_transcript_transition(decision, snapshot.continuation_messages)
        return decision

    def take_transcript_replacement(self) -> tuple[Message, ...] | None:
        """Consume the accepted decision's replacement without changing the transcript.

        Returns:
            tuple[Message, ...] | None: Replacement rows for the runner to apply,
            or None if no replacement is pending or the decision stopped the run.
            An empty tuple is a valid replacement that clears the transcript.
        """
        pending = self.pending_transcript_transition
        self.pending_transcript_transition = None
        if pending is None:
            return None
        decision, continuation_messages = pending
        if decision.stop:
            return None
        if decision.messages is not None:
            return (*decision.messages, *decision.extra_messages)
        if decision.context_rebase is not None:
            return (
                *decision.context_rebase.base_messages,
                *continuation_messages,
                *decision.extra_messages,
            )
        return None

    def _remember_transcript_transition(
        self,
        decision: RequestBoundaryDecision,
        continuation_messages: Sequence[Message],
    ) -> None:
        if decision.messages is not None or decision.context_rebase is not None:
            self.pending_transcript_transition = (decision, tuple(continuation_messages))
