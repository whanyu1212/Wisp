"""Stateful provider-neutral harness built on the pure agent loop."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

import anyio

from wisp.agent.execution import ToolExecutor
from wisp.agent.loop import AgentLoopConfig, AgentLoopEvent, run_agent_loop
from wisp.agent.messages import (
    Message,
    message_from_completion_event,
    provider_history_message,
)
from wisp.agent.transcript import plan_interrupted_tool_repairs
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    ToolExecutionEnded,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import Provider, ToolSpec


@dataclass(frozen=True, slots=True)
class AgentHarnessConfig:
    """Portable dependencies and limits for an `AgentHarness`."""

    provider: Provider
    tool_executor: ToolExecutor
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tool_iterations: int | None = None
    effort: str | None = None
    context_window: int | None = None
    context_pressure_threshold: float = 0.8


class SimpleCancellationToken:
    """Small cooperative cancellation token owned by one harness run."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._cancelled


def _cancelled_events(
    active_turn: int | None,
    *,
    active_turn_completed: bool,
) -> tuple[AgentLoopEvent, ...]:
    events: list[AgentLoopEvent] = [ErrorEvent(message="Agent run cancelled")]
    if active_turn is not None and not active_turn_completed:
        events.append(
            TurnCompleted(
                turn=active_turn,
                outcome="cancelled",
                finish_reason="cancelled",
            )
        )
    return tuple(events)


class AgentHarness:
    """Own an in-memory transcript and delegate execution to the pure loop."""

    def __init__(
        self,
        config: AgentHarnessConfig,
        *,
        messages: Sequence[Message] = (),
    ) -> None:
        self._config = config
        self._messages = list(messages)
        self._current_token: SimpleCancellationToken | None = None
        self._current_scope: anyio.CancelScope | None = None
        self._running = False

    @property
    def config(self) -> AgentHarnessConfig:
        """Return the current harness configuration."""
        return self._config

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable transcript snapshot."""
        return tuple(self._messages)

    @property
    def is_running(self) -> bool:
        """Return whether a prompt or continuation is active."""
        return self._running

    def replace_config(self, config: AgentHarnessConfig) -> None:
        """Replace provider/tool configuration between runs."""
        self._ensure_idle()
        self._config = config

    def append_message(self, message: Message) -> None:
        """Append restored or application-provided transcript state."""
        self._ensure_idle()
        self._messages.append(message)

    def replace_messages(self, messages: Sequence[Message]) -> None:
        """Replace the transcript between runs."""
        self._ensure_idle()
        self._messages = list(messages)

    def repair_interrupted_tool_calls(self) -> tuple[Message, ...]:
        """Repair logical ordering and return synthetic results needing persistence."""

        self._ensure_idle()
        plan = plan_interrupted_tool_repairs(self._messages)
        self._messages = list(plan.messages)
        return plan.repairs

    def cancel(self) -> bool:
        """Request cooperative cancellation for the active run."""
        if self._current_token is None:
            return False
        self._current_token.cancel()
        if self._current_scope is not None:
            self._current_scope.cancel()
        return True

    def prompt(self, content: str) -> AsyncGenerator[AgentLoopEvent, None]:
        """Append a user message and start a run."""
        return self.prompt_message(Message(role="user", content=content))

    def prompt_message(self, message: Message) -> AsyncGenerator[AgentLoopEvent, None]:
        """Append an existing user message and start a run."""
        if message.role != "user":
            raise ValueError("AgentHarness prompts require a user message")
        return self._run(prompt_message=message)

    def continue_(self) -> AsyncGenerator[AgentLoopEvent, None]:
        """Continue from the current transcript without adding a user message."""
        return self._run()

    async def _run(
        self,
        *,
        prompt_message: Message | None = None,
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        self.repair_interrupted_tool_calls()
        self._running = True
        token = SimpleCancellationToken()
        self._current_token = token
        if prompt_message is not None:
            self._messages.append(prompt_message)

        active_turn: int | None = None
        active_turn_completed = False
        run_finished = False
        config = AgentLoopConfig(
            provider=self._config.provider,
            tool_executor=self._config.tool_executor,
            model=self._config.model,
            tools=self._config.tools,
            max_tool_iterations=self._config.max_tool_iterations,
            cancellation_token=token,
            effort=self._config.effort,
            context_window=self._config.context_window,
            context_pressure_threshold=self._config.context_pressure_threshold,
        )
        provider_messages_list: list[Message] = []
        for message in self._messages:
            provider_message = provider_history_message(message)
            if provider_message is not None:
                provider_messages_list.append(provider_message)
        provider_messages = tuple(provider_messages_list)
        loop_events = run_agent_loop(config, messages=provider_messages)
        try:
            while True:
                if token.is_cancelled() and not run_finished:
                    for cancellation_event in _cancelled_events(
                        active_turn,
                        active_turn_completed=active_turn_completed,
                    ):
                        yield cancellation_event
                    break

                scope = anyio.CancelScope()
                self._current_scope = scope
                event: AgentLoopEvent | None = None
                stream_ended = False
                with scope:
                    try:
                        event = await anext(loop_events)
                    except StopAsyncIteration:
                        stream_ended = True
                if self._current_scope is scope:
                    self._current_scope = None

                if scope.cancel_called:
                    if not run_finished:
                        for cancellation_event in _cancelled_events(
                            active_turn,
                            active_turn_completed=active_turn_completed,
                        ):
                            yield cancellation_event
                    break
                if stream_ended:
                    break
                assert event is not None

                if isinstance(event, TurnStarted):
                    active_turn = event.turn
                    active_turn_completed = False
                    run_finished = False
                if isinstance(event, MessageCompleted):
                    self._messages.append(message_from_completion_event(event))
                    run_finished = not event.tool_calls
                elif isinstance(event, ToolExecutionEnded):
                    # ToolResultReady copies this terminal payload; retain it now so
                    # closing the stream at this visible boundary cannot lose output.
                    self._messages.append(message_from_completion_event(event))
                elif isinstance(event, TurnCompleted):
                    active_turn_completed = True
                    run_finished = run_finished or event.outcome != "completed"
                yield event
        finally:
            self._current_scope = None
            with anyio.CancelScope(shield=True):
                await loop_events.aclose()
            if self._current_token is token:
                self._current_token = None
            self._running = False

    def _ensure_idle(self) -> None:
        if self._running:
            raise RuntimeError("AgentHarness is already running")


__all__ = ["AgentHarness", "AgentHarnessConfig", "SimpleCancellationToken"]
