from __future__ import annotations

import shlex
import sys
from collections import deque
from collections.abc import AsyncIterator, Iterable
from decimal import Decimal
from pathlib import Path
from typing import cast

import anyio
import pytest

import wisp.agent.loop as agent_loop_module
from wisp.agent.execution import (
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    BillableTokenUsage,
    ContextEstimated,
    MessageCompleted,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
    UsageCost,
    UsageCostRates,
    wisp_event_from_json,
)
from wisp.providers.base import ProviderProtocolError, ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.tools import shell as shell_module
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.builtin import BashTool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError


class NeverToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class CacheAwareScriptedProvider(ScriptedProvider):
    """Scripted provider opting into the prompt-cache-key capability."""

    supports_prompt_cache_key = True


class RecordingToolExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        arguments = dict(tool_call.arguments)
        yield ToolApprovalRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=arguments,
            safety="command",
        )
        yield ToolApprovalResolved(
            call_id=tool_call.call_id,
            name=tool_call.name,
            approved=True,
        )
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="tool output",
            is_error=False,
            exit_code=0,
            process_id="proc-1",
            process_state="completed",
            stdout="tool stdout\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_dropped_bytes=0,
            stderr_dropped_bytes=0,
        )


class MissingResultExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolApprovalRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=dict(tool_call.arguments),
            safety="command",
        )


class MismatchedResultExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id="different-call",
            name=tool_call.name,
            output="wrong",
            is_error=False,
        )


class ExtraEventExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="done",
            is_error=False,
        )
        yield ToolApprovalResolved(
            call_id=tool_call.call_id,
            name=tool_call.name,
            approved=True,
        )


class ScriptedToolExecutor:
    def __init__(self, events: tuple[object, ...]) -> None:
        self.events = events

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        del tool_call
        for event in self.events:
            yield cast(ToolExecutionEvent, event)


def test_agent_loop_config_preserves_legacy_positional_field_order() -> None:
    config = AgentLoopConfig(
        ScriptedProvider([]),
        NeverToolExecutor(),
        None,
        (),
        None,
        None,
        None,
        None,
        16_384,
        0.8,
        0,
        0,
        None,
        True,
    )

    assert config.defer_context_overflow_errors is True
    assert config.prompt_cache_key is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_tool_iterations", -1, "max_tool_iterations"),
        ("max_tool_iterations", True, "max_tool_iterations"),
        ("context_window", 0, "context_window"),
        ("context_window", True, "context_window"),
        ("context_reserve_tokens", -1, "context_reserve_tokens"),
        ("context_reserve_tokens", False, "context_reserve_tokens"),
        ("context_pressure_threshold", 0, "context_pressure_threshold"),
        ("context_pressure_threshold", 1.1, "context_pressure_threshold"),
        ("context_pressure_threshold", 10**1000, "context_pressure_threshold"),
        ("context_pressure_threshold", float("nan"), "context_pressure_threshold"),
        ("context_pressure_threshold", float("inf"), "context_pressure_threshold"),
        ("turn_offset", -1, "turn_offset"),
        ("turn_offset", True, "turn_offset"),
        ("tool_iteration_offset", -1, "tool_iteration_offset"),
        ("tool_iteration_offset", False, "tool_iteration_offset"),
    ],
)
def test_agent_loop_config_rejects_invalid_runtime_limits(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentLoopConfig(
            provider=ScriptedProvider([]),
            tool_executor=NeverToolExecutor(),
            **cast(dict[str, object], {field: value}),
        )


def test_agent_loop_config_accepts_runtime_limit_boundaries() -> None:
    config = AgentLoopConfig(
        provider=ScriptedProvider([]),
        tool_executor=NeverToolExecutor(),
        max_tool_iterations=0,
        context_window=1,
        context_reserve_tokens=1,
        context_pressure_threshold=1,
        turn_offset=0,
        tool_iteration_offset=0,
    )

    assert config.max_tool_iterations == 0
    assert config.context_pressure_threshold == 1


def test_pure_loop_streams_without_application_dependencies() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(
                    content="hello",
                    usage=ProviderUsage(
                        input_tokens=12,
                        output_tokens=7,
                        total_tokens=19,
                    ),
                ),
            ]
        ]
    )
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.usage is not None
    assert completed.usage.total_tokens == 19
    assert provider.calls[0].messages == messages


class RecordingRequestBoundaryHook:
    """Records each boundary snapshot and replays scripted decisions in order."""

    def __init__(self, decisions: Iterable[RequestBoundaryDecision]) -> None:
        self._decisions = deque(decisions)
        self.snapshots: list[RequestBoundarySnapshot] = []

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        self.snapshots.append(snapshot)
        if not self._decisions:
            return RequestBoundaryDecision(stop=True)
        return self._decisions.popleft()


def _completed_stream(content: str) -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="test"),
        ProviderTextDelta(delta=content),
        ProviderResponseCompleted(content=content),
    ]


def test_request_boundary_hook_not_configured_matches_default_behavior() -> None:
    """No hook configured must produce the exact same events as before hooks existed."""

    provider = ScriptedProvider([_completed_stream("hi")])
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    assert len(provider.calls) == 1


def test_request_boundary_hook_can_stop_after_tool_round() -> None:
    """A hook may still stop the run at the tool-round boundary."""

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(
                    tool_call=ToolCall(call_id="call-1", name="noop", arguments={})
                ),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(ToolCall(call_id="call-1", name="noop", arguments={}),),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    executor = RecordingToolExecutor()
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(stop=True)])
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    request_boundary_hook=hook,
                ),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    assert [event.type for event in events].count("turn.completed") == 1
    assert len(provider.calls) == 1
    assert hook.snapshots[0].had_tool_calls is True


def test_request_boundary_hook_rejects_injection_after_tool_round() -> None:
    """`messages`/`extra_messages` are unsupported immediately after a tool round.

    Regression for #363 review: rebuilding that continuation would replay the
    round's assistant tool-call/tool-result messages through each provider's
    plain-message converter, which flattens them to ordinary text instead of
    the structured function-call/output pairs a provider expects (confirmed
    against `_messages_to_response_input`/`_messages_to_anthropic`/
    `_messages_to_google`, none of which special-case `Message.tool_calls`/
    `tool_call_id`). The loop must raise loudly rather than silently corrupt
    history.
    """

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(
                    tool_call=ToolCall(call_id="call-1", name="noop", arguments={})
                ),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(ToolCall(call_id="call-1", name="noop", arguments={}),),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    executor = RecordingToolExecutor()
    injected = Message(role="user", content="steered")
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(extra_messages=(injected,))])
    messages = (Message(role="user", content="hi"),)
    collected: list[object] = []

    async def run() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=executor,
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            collected.append(event)

    with pytest.raises(RequestBoundaryUnsupportedError):
        anyio.run(run)

    assert len(provider.calls) == 1
    # The turn already completed successfully (exactly one TurnCompleted)
    # before the hook raised; the loop must not emit a second, contradictory
    # TurnCompleted for the same turn once the exception propagates.
    assert [event.type for event in collected].count("turn.completed") == 1
    assert [event.type for event in collected].count("error") == 1


def test_request_boundary_hook_fires_after_clean_turn_and_can_continue() -> None:
    """A hook can turn a would-be-final turn (no tool calls) into a follow-up."""

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(stop=False)])
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    request_boundary_hook=hook,
                ),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    assert [event.type for event in events].count("turn.started") == 2
    assert len(provider.calls) == 2
    assert hook.snapshots[0].had_tool_calls is False
    # Regression for #363 review: the first turn's own completed answer must
    # be visible to the follow-up request -- every provider ignores
    # previous_response_id whenever tool_results is empty (true here), so
    # `messages` alone must already carry it.
    second_call = provider.calls[1]
    assert second_call.tool_results == ()
    assert [(m.role, m.content) for m in second_call.messages] == [
        ("user", "hi"),
        ("assistant", "first"),
    ]


class RaisingRequestBoundaryHook:
    """A hook whose `before_next_request` always raises."""

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        raise RuntimeError("boundary hook failed")


def test_request_boundary_hook_failure_does_not_double_complete_the_turn() -> None:
    """A hook failure after a clean turn must not emit a second TurnCompleted.

    Regression for #363 review: the turn's one terminal TurnCompleted(outcome=
    "completed") is yielded before the hook is invoked. If the hook then
    raises, the outer exception handler previously still saw `turn_started`
    as true and emitted a second, contradictory TurnCompleted(outcome=
    "failed") for the same turn.
    """

    provider = ScriptedProvider([_completed_stream("first")])
    hook = RaisingRequestBoundaryHook()
    messages = (Message(role="user", content="hi"),)
    collected: list[object] = []

    async def run() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            collected.append(event)

    with pytest.raises(RuntimeError, match="boundary hook failed"):
        anyio.run(run)

    assert [event.type for event in collected].count("turn.completed") == 1
    assert [event.type for event in collected].count("error") == 1


def test_request_boundary_hook_clears_stale_pending_tool_results() -> None:
    """A tool round's results must not leak into a later, unrelated request."""

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(
                    tool_call=ToolCall(call_id="call-1", name="noop", arguments={})
                ),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(ToolCall(call_id="call-1", name="noop", arguments={}),),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="no more tools"),
                ProviderResponseCompleted(content="no more tools", response_id="resp-2"),
            ],
            _completed_stream("final"),
        ]
    )
    executor = RecordingToolExecutor()
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(stop=False), RequestBoundaryDecision(stop=False)]
    )
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    request_boundary_hook=hook,
                ),
                messages=messages,
            )
        ]

    anyio.run(run)

    assert len(provider.calls) == 3
    # Call 1 legitimately carries call-1's result. Call 2 (after the clean
    # turn that consumed it) must not still be sending it.
    assert provider.calls[1].tool_results == (
        ToolCallResult(call_id="call-1", output="tool output", is_error=False),
    )
    assert provider.calls[2].tool_results == ()
    # A plain continuation (no injected content) never needs `messages`
    # rebuilt: every provider natively continues from previous_response_id,
    # which still correctly points at the just-completed clean turn.
    assert provider.calls[2].messages == messages
    assert provider.calls[2].previous_response_id == "resp-2"


def test_request_boundary_hook_rejects_injection_after_earlier_tool_round() -> None:
    """`messages`/`extra_messages` stay unsupported at a *later* clean-turn
    boundary once this run has had a tool round, not just immediately after
    one.

    Regression for #363 review: an earlier version only rejected injection
    at the boundary immediately following a tool round, but a subsequent
    no-tool-calls boundary in the same run still unconditionally folded
    `state.continuation_messages` -- including that earlier round's
    assistant tool-call/tool-result messages -- into `messages`, which
    would corrupt history the same way once sent through a real provider's
    plain-message converter.
    """

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(
                    tool_call=ToolCall(call_id="call-1", name="noop", arguments={})
                ),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(ToolCall(call_id="call-1", name="noop", arguments={}),),
                    finish_reason="tool_calls",
                ),
            ],
            _completed_stream("no more tools"),
        ]
    )
    executor = RecordingToolExecutor()
    injected = Message(role="user", content="steered")
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(stop=False), RequestBoundaryDecision(extra_messages=(injected,))]
    )
    messages = (Message(role="user", content="hi"),)
    collected: list[object] = []

    async def run() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=executor,
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            collected.append(event)

    with pytest.raises(RequestBoundaryUnsupportedError):
        anyio.run(run)

    assert len(provider.calls) == 2


def test_request_boundary_hook_can_replace_base_messages() -> None:
    """A hook can replace the loop's base history (e.g. after compaction)."""

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
    replacement = (Message(role="user", content="compacted summary"),)
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(messages=replacement)])
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    request_boundary_hook=hook,
                ),
                messages=messages,
            )
        ]

    anyio.run(run)

    assert provider.calls[0].messages == messages
    assert provider.calls[1].messages == replacement
    # A full replacement discards accumulated continuation state entirely --
    # the point of compaction is that the old history stops being resent.
    assert provider.calls[1].tool_results == ()
    assert provider.calls[1].previous_response_id is None


class RaisingCancellationToken:
    """Cancellation token whose is_cancelled() raises instead of returning a bool."""

    def is_cancelled(self) -> bool:
        raise RuntimeError("boom")


def test_pure_loop_does_not_mask_cancellation_token_errors() -> None:
    """A cancellation-token failure before the first turn must not raise UnboundLocalError.

    Regression test for #359: `turn` was previously only bound inside the loop body
    after the first cancellation check, so an exception raised by that very check
    (before `turn = state.begin_turn()` executed) hit `if turn > 0:` in the outer
    `except` and raised UnboundLocalError, masking the original failure.
    """

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="boom"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=ScriptedProvider([]),
                    tool_executor=NeverToolExecutor(),
                    cancellation_token=RaisingCancellationToken(),
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == ["error"]


def test_pure_loop_does_not_complete_unstarted_turn_with_nonzero_offset() -> None:
    """A nonzero turn_offset must not make an unstarted turn look completed.

    Regression test for a follow-up to #359: using `turn > 0` to decide whether to
    emit a failure `TurnCompleted` is wrong once `turn` is pre-seeded to
    `config.turn_offset` before the loop starts. With a nonzero offset and a
    cancellation-token failure on the very first check, no `TurnStarted` is ever
    emitted for this invocation, so no matching `TurnCompleted` must be emitted
    either -- only the explicit `turn_started` flag can tell the two cases apart.
    """

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="boom"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=ScriptedProvider([]),
                    tool_executor=NeverToolExecutor(),
                    cancellation_token=RaisingCancellationToken(),
                    turn_offset=5,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == ["error"]


@pytest.mark.parametrize(
    ("started_id", "terminal_id", "tool_call_id"),
    [
        ("response-1", None, None),
        (None, "response-1", None),
        (None, None, "response-1"),
        ("response-1", "response-1", "response-1"),
    ],
)
def test_pure_loop_resolves_response_id_for_messages_and_tool_continuation(
    started_id: str | None,
    terminal_id: str | None,
    tool_call_id: str | None,
) -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd"},
        response_id=tool_call_id,
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id=started_id),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    response_id=terminal_id,
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)

    completed = [event for event in events if isinstance(event, MessageCompleted)]
    assert completed[0].response_id == "response-1"
    assert provider.calls[1].previous_response_id == "response-1"


@pytest.mark.parametrize(
    "provider_events",
    [
        [
            ProviderResponseStarted(model="test", response_id="started-id"),
            ProviderResponseCompleted(content="done", response_id="terminal-id"),
        ],
        [
            ProviderResponseStarted(model="test", response_id="started-id"),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-1",
                    name="bash",
                    arguments={"command": "pwd"},
                    response_id="tool-id",
                )
            ),
            ProviderResponseCompleted(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        name="bash",
                        arguments={"command": "pwd"},
                        response_id="tool-id",
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ],
        [
            ProviderResponseStarted(model="test"),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-1", name="bash", arguments={}, response_id="tool-id"
                )
            ),
            ProviderResponseCompleted(
                content="",
                tool_calls=(
                    ToolCall(call_id="call-1", name="bash", arguments={}, response_id="tool-id"),
                ),
                response_id="terminal-id",
                finish_reason="tool_calls",
            ),
        ],
        [
            ProviderResponseStarted(model="test"),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-1", name="bash", arguments={}, response_id="response-1"
                )
            ),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-2", name="bash", arguments={}, response_id="response-2"
                )
            ),
            ProviderResponseCompleted(
                content="",
                tool_calls=(
                    ToolCall(call_id="call-1", name="bash", arguments={}, response_id="response-1"),
                    ToolCall(call_id="call-2", name="bash", arguments={}, response_id="response-2"),
                ),
                finish_reason="tool_calls",
            ),
        ],
    ],
)
def test_pure_loop_rejects_conflicting_provider_response_ids(
    provider_events: list[ProviderEvent],
) -> None:
    provider = ScriptedProvider([provider_events])

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ProviderProtocolError, match="conflicting response ids"):
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert not any(isinstance(event, MessageCompleted) for event in events)
    assert not any(event.type == "tool.call" for event in events)


def test_pure_loop_validates_failed_terminal_response_id() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderResponseFailed(message="upstream failed", response_id="response-1"),
            ],
            [
                ProviderResponseStarted(model="test", response_id="started-id"),
                ProviderResponseFailed(message="upstream failed", response_id="terminal-id"),
            ],
        ]
    )

    async def run_failed() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run_failed)
    assert [event.type for event in events[-3:]] == [
        "message.completed",
        "error",
        "turn.completed",
    ]

    async def run_invalid() -> None:
        with pytest.raises(ProviderProtocolError, match="conflicting response ids"):
            async for _ in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            ):
                pass

    anyio.run(run_invalid)


def test_failed_response_does_not_execute_streamed_tool_calls() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseFailed(message="truncated"),
            ]
        ]
    )
    executor = RecordingToolExecutor()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)

    assert executor.calls == []
    assert not any(event.type in {"tool.call", "tool.execution.started"} for event in events)


def test_failed_response_id_does_not_leak_into_a_later_run() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="failed-response"),
                ProviderResponseFailed(message="upstream failed", response_id="failed-response"),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def fail() -> None:
        async for _ in run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
            messages=(Message(role="user", content="first"),),
        ):
            pass

    async def succeed() -> None:
        async for _ in run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
            messages=(Message(role="user", content="second"),),
        ):
            pass

    anyio.run(fail)
    anyio.run(succeed)

    assert provider.calls[1].previous_response_id is None


def test_pure_loop_recovers_empty_completion_from_streamed_text() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="complete "),
                ProviderTextDelta(delta="streamed response"),
                ProviderResponseCompleted(content=""),
            ]
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.content == "complete streamed response"


def test_pure_loop_passes_the_provider_response_model_to_cost_estimator() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="resolved-model"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                ),
            ]
        ]
    )
    calls: list[tuple[str, str | None, str | None]] = []

    def estimate(
        provider_name: str,
        requested_model: str | None,
        response_model: str | None,
        usage: object,
    ) -> UsageCost:
        del usage
        calls.append((provider_name, requested_model, response_model))
        return UsageCost(
            provider=provider_name,
            requested_model=requested_model,
            model=response_model,
            billable=BillableTokenUsage(
                input_tokens=10,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=5,
            ),
            rates=UsageCostRates(
                input_usd_per_million=Decimal("1"),
                output_usd_per_million=Decimal("2"),
            ),
            estimated_usd=Decimal("0.00002"),
        )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    model="requested-model",
                    cost_estimator=estimate,
                ),
                messages=(Message(role="user", content="hello"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert calls == [("scripted", "requested-model", "resolved-model")]
    assert completed.cost is not None
    assert completed.cost.estimated_usd == Decimal("0.00002")


def test_pure_loop_marks_missing_usage_unpriced_without_losing_the_response() -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="model"), ProviderResponseCompleted(content="done")]]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hello"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.usage is None
    assert completed.cost is not None
    assert completed.cost.unavailable_reason == "usage_incomplete"
    assert events[-1].type == "turn.completed"


def test_pure_loop_contains_cost_estimator_failures() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                ),
            ]
        ]
    )

    def fail_estimate(*args: object) -> UsageCost:
        del args
        raise RuntimeError("pricing lookup failed")

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    cost_estimator=fail_estimate,
                ),
                messages=(Message(role="user", content="hello"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.content == "done"
    assert completed.cost is not None
    assert completed.cost.unavailable_reason == "estimation_failed"
    assert not any(event.type == "error" for event in events)


class _LegacyProviderWithoutEffortParameter:
    """A `Provider` implemented against the pre-`effort` `stream()` signature.

    `Provider` is a structural `typing.Protocol` with no runtime enforcement,
    so a third-party provider written before `effort` was added is still a
    perfectly valid implementation. `run_agent_loop` must not unconditionally
    pass `effort=` to every provider on every turn, or a provider like this
    one breaks on its very first call.
    """

    name = "legacy"
    default_model: str | None = "legacy"
    supports_prompt_cache_key = False

    async def stream(
        self,
        messages: object,
        *,
        model: str | None = None,
        tools: object = (),
        tool_results: object = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[object]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderTextDelta(delta="hello")
        yield ProviderResponseCompleted(content="hello")


def test_pure_loop_does_not_break_a_provider_without_an_effort_parameter() -> None:
    # Regression test for a real Codex finding: config.effort defaults to
    # None, but the loop previously passed effort=None unconditionally on
    # every call, which raised TypeError against any Provider implemented
    # before this keyword existed.
    provider = _LegacyProviderWithoutEffortParameter()
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(  # type: ignore[arg-type]
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    prompt_cache_key="wisp:session-1",
                ),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]


def test_pure_loop_forwards_effort_to_a_provider_that_supports_it() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    effort="high",
                ),
                messages=messages,
            )
        ]

    anyio.run(run)

    assert provider.calls[0].effort == "high"


def test_pure_loop_forwards_executor_events_and_provider_results() -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd"},
        response_id="response-1",
    )
    provider = CacheAwareScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderThinkingDelta(delta="reasoning" * 100),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    response_id="response-1",
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )
    executor = RecordingToolExecutor()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    prompt_cache_key="wisp:session-1",
                ),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)

    assert executor.calls == [call]
    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "tool.call",
        "tool.execution.started",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
        "tool.result",
        "turn.completed",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == "tool output"
    assert provider.calls[1].tool_results[0].output == "tool output"
    assert provider.calls[1].previous_response_id == "response-1"
    assert [call.prompt_cache_key for call in provider.calls] == [
        "wisp:session-1",
        "wisp:session-1",
    ]
    estimates = [event for event in events if isinstance(event, ContextEstimated)]
    assert len(estimates) == 2
    assert estimates[1].budget.estimate.total_tokens > estimates[0].budget.estimate.total_tokens
    assert estimates[1].budget.estimate.message_tokens < len("reasoning" * 100) // 4
    assert all("reasoning" not in message.content for message in provider.calls[1].messages)
    # The promoted exit_code reaches the event AND crosses the wire: the TUI
    # renderer only sees events after they are serialized (agent subprocess →
    # JSON → client), so the presentation signal must survive round-tripping.
    assert result.exit_code == 0
    assert result.process_id == "proc-1"
    assert result.process_state == "completed"
    assert result.stdout == "tool stdout\n"
    result_round_tripped = wisp_event_from_json(result.model_dump_json())
    assert isinstance(result_round_tripped, ToolResultReady)
    assert result_round_tripped.exit_code == 0
    assert result_round_tripped.process_id == "proc-1"
    assert result_round_tripped.process_state == "completed"
    assert result_round_tripped.stdout == "tool stdout\n"
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.exit_code == 0
    ended_round_tripped = wisp_event_from_json(ended.model_dump_json())
    assert isinstance(ended_round_tripped, ToolExecutionEnded)
    assert ended_round_tripped.exit_code == 0
    assert ended_round_tripped.process_id == "proc-1"
    assert ended_round_tripped.process_state == "completed"
    assert ended_round_tripped.stdout == "tool stdout\n"


def test_tool_result_projection_preserves_the_complete_wire_payload() -> None:
    ended = ToolExecutionEnded(
        call_id="call-1",
        name="bash",
        output="Command exited with code 2: output",
        is_error=True,
        exit_code=2,
        output_has_exit_status=True,
        before_text="before\n",
        created=True,
        summary="summary",
        truncated=True,
        process_id="proc-1",
        process_state="failed",
        process_error="process failed",
        stdout="stdout\n",
        stderr="stderr\n",
        stdout_truncated=True,
        stderr_truncated=True,
        stdout_dropped_bytes=11,
        stderr_dropped_bytes=12,
    )

    result = ToolResultReady.from_execution_ended(ended)
    expected_payload = {
        "call_id": "call-1",
        "name": "bash",
        "output": "Command exited with code 2: output",
        "is_error": True,
        "failure_code": None,
        "retryable": False,
        "recovery_hint": None,
        "exit_code": 2,
        "output_has_exit_status": True,
        "before_text": "before\n",
        "created": True,
        "summary": "summary",
        "truncated": True,
        "process_id": "proc-1",
        "process_state": "failed",
        "process_error": "process failed",
        "stdout": "stdout\n",
        "stderr": "stderr\n",
        "stdout_truncated": True,
        "stderr_truncated": True,
        "stdout_dropped_bytes": 11,
        "stderr_dropped_bytes": 12,
    }

    envelope_fields = {"type", "schema_version", "timestamp"}
    assert ended.model_dump(exclude=envelope_fields) == expected_payload
    assert result.model_dump(exclude=envelope_fields) == expected_payload
    assert result.type == "tool.result"
    assert result.timestamp >= ended.timestamp
    assert wisp_event_from_json(ended.model_dump_json()) == ended
    assert wisp_event_from_json(result.model_dump_json()) == result


def test_completion_and_continuation_snapshot_projection_is_single_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd", "options": {"cwd": "before"}},
        parse_error="example parse error",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )

    estimated_messages: list[tuple[Message, ...]] = []
    original_estimate_context = agent_loop_module.estimate_context

    def capture_estimate(messages: tuple[Message, ...], tools: tuple[ToolSpec, ...]) -> object:
        estimated_messages.append(messages)
        return original_estimate_context(messages, tools)

    monkeypatch.setattr(agent_loop_module, "estimate_context", capture_estimate)

    async def run() -> list[object]:
        events: list[object] = []
        loop = run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
            messages=(Message(role="user", content="run pwd"),),
        )
        async for event in loop:
            events.append(event)
            if isinstance(event, MessageCompleted) and event.tool_calls:
                event.tool_calls[0].arguments["command"] = "malicious"
                options = cast(dict[str, object], event.tool_calls[0].arguments["options"])
                options["cwd"] = "after"
        return events

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    expected_snapshot = ToolCallSnapshot(
        call_id="call-1",
        name="bash",
        arguments={"command": "malicious", "options": {"cwd": "after"}},
        parse_error="example parse error",
    )
    assert completed.tool_calls == (expected_snapshot,)
    continuation = next(message for message in estimated_messages[1] if message.role == "assistant")
    assert continuation.tool_calls == (
        ToolCallSnapshot(
            call_id="call-1",
            name="bash",
            arguments={"command": "pwd", "options": {"cwd": "before"}},
            parse_error="example parse error",
        ),
    )


def test_execution_end_immediately_precedes_projected_tool_result() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)
    ended_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolExecutionEnded)
    )
    ended = events[ended_index]
    result = events[ended_index + 1]

    assert isinstance(ended, ToolExecutionEnded)
    assert isinstance(result, ToolResultReady)
    envelope_fields = {"type", "schema_version", "timestamp"}
    assert result.model_dump(exclude=envelope_fields) == ended.model_dump(exclude=envelope_fields)


def _run_bash_loop(
    tmp_path: Path,
    *,
    command: str,
    timeout: int | None = None,
    bash_tool: BashTool | None = None,
) -> tuple[ScriptedProvider, list[object]]:
    arguments: dict[str, object] = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments=arguments,
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(bash_tool or BashTool())
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run verification"),),
            )
        ]

    return provider, anyio.run(run)


@pytest.mark.parametrize("exit_code", [0, 3])
def test_pure_loop_exposes_bash_exit_code_to_provider(
    tmp_path: Path,
    exit_code: int,
) -> None:
    python = shlex.quote(sys.executable)
    command = f"{python} -c \"import sys; print('evidence'); sys.exit({exit_code})\""
    provider, events = _run_bash_loop(tmp_path, command=command)

    expected = f"Command exited with code {exit_code}: evidence"
    assert provider.calls[1].tool_results[0].output == expected
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == expected
    assert result.exit_code == exit_code
    assert result.output_has_exit_status is True


def test_pure_loop_exposes_bash_timeout_as_inconclusive_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def time_out(*_args: object, **_kwargs: object) -> object:
        raise ToolError(
            "Command timed out after 30 seconds",
            failure_code="timeout",
            retryable=True,
            recovery_hint="The result is inconclusive; retry with a suitable timeout.",
        )

    monkeypatch.setattr(shell_module, "_run_shell", time_out)

    provider, events = _run_bash_loop(
        tmp_path,
        command="slow check",
        timeout=30,
        bash_tool=BashTool(None),
    )

    tool_result = provider.calls[1].tool_results[0]
    assert tool_result.output.startswith("Command timed out after 30 seconds")
    assert "Recovery:" in tool_result.output
    assert tool_result.is_error is True
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == tool_result.output
    assert result.is_error is True
    assert result.failure_code == "timeout"
    assert result.retryable is True
    assert result.exit_code is None


def test_pure_loop_rejects_executor_with_unresolved_approval() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ToolExecutionProtocolError, match="unresolved approval"):
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=MissingResultExecutor()),
                messages=(Message(role="user", content="run pwd"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events[-2:]] == ["error", "turn.completed"]
    assert events[-1].outcome == "failed"


@pytest.mark.parametrize(
    ("executor", "error"),
    [
        (MismatchedResultExecutor(), "does not match the requested call"),
        (ExtraEventExecutor(), "emitted an event after the result"),
    ],
)
def test_pure_loop_rejects_malformed_terminal_results(
    executor: ToolExecutor,
    error: str,
) -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    async def run() -> None:
        with pytest.raises(ToolExecutionProtocolError, match=error):
            async for _ in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            ):
                pass

    anyio.run(run)


def _approval_request(*, arguments: dict[str, object] | None = None) -> ToolApprovalRequested:
    return ToolApprovalRequested(
        call_id="call-1",
        name="bash",
        arguments=arguments or {"command": "pwd"},
        safety="command",
    )


def _approval_resolution(*, approved: bool = True) -> ToolApprovalResolved:
    return ToolApprovalResolved(
        call_id="call-1",
        name="bash",
        approved=approved,
    )


def _terminal_result(*, is_error: bool = False) -> ToolExecutionEnded:
    return ToolExecutionEnded(
        call_id="call-1",
        name="bash",
        output="denied" if is_error else "done",
        is_error=is_error,
    )


@pytest.mark.parametrize(
    ("events", "error"),
    [
        (
            (),
            "ended without a result",
        ),
        (
            (_approval_resolution(), _terminal_result()),
            "resolved approval before requesting it",
        ),
        (
            (_approval_request(), _approval_request(), _terminal_result()),
            "requested approval more than once",
        ),
        (
            (
                _approval_request(),
                _approval_resolution(),
                _approval_resolution(),
                _terminal_result(),
            ),
            "resolved approval more than once",
        ),
        (
            (_approval_request(),),
            "ended with an unresolved approval",
        ),
        (
            (_approval_request(), _terminal_result()),
            "ended with an unresolved approval",
        ),
        (
            (_approval_request(arguments={"command": "whoami"}), _terminal_result()),
            "approval arguments do not match",
        ),
        (
            (_approval_request(), _approval_resolution(approved=False), _terminal_result()),
            "reported success after approval was denied",
        ),
        (
            (object(),),
            "unsupported event type",
        ),
    ],
)
def test_pure_loop_rejects_malformed_approval_lifecycle(
    events: tuple[object, ...],
    error: str,
) -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    async def run() -> list[object]:
        emitted: list[object] = []
        with pytest.raises(ToolExecutionProtocolError, match=error):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=ScriptedToolExecutor(events),
                ),
                messages=(Message(role="user", content="run pwd"),),
            ):
                emitted.append(event)
        return emitted

    emitted = anyio.run(run)

    assert not any(isinstance(event, ToolResultReady) for event in emitted)
    assert [event.type for event in emitted[-2:]] == ["error", "turn.completed"]
    assert emitted[-1].outcome == "failed"
    assert len(provider.calls) == 1


def test_pure_loop_rejects_type_changing_nested_approval_arguments() -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd", "options": [1]},
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    executor = ScriptedToolExecutor(
        (
            _approval_request(arguments={"command": "pwd", "options": [True]}),
            _terminal_result(),
        )
    )

    async def run() -> None:
        with pytest.raises(ToolExecutionProtocolError, match="approval arguments do not match"):
            async for _ in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            ):
                pass

    anyio.run(run)


def test_pure_loop_accepts_denied_approval_with_error_result() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="noted"),
            ],
        ]
    )
    executor = ScriptedToolExecutor(
        (
            _approval_request(),
            _approval_resolution(approved=False),
            _terminal_result(is_error=True),
        )
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    emitted = anyio.run(run)

    result = next(event for event in emitted if isinstance(event, ToolResultReady))
    assert result.is_error is True
    assert provider.calls[1].tool_results == (
        ToolCallResult(call_id="call-1", output="denied", is_error=True),
    )


class WriteSnapshotExecutor:
    """Emits a terminal result carrying a pre-write snapshot, like the write tool."""

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="Wrote 4 bytes to f.txt",
            is_error=False,
            before_text="old\n",
            created=False,
        )


def test_pure_loop_forwards_before_text_across_the_wire() -> None:
    # The write tool's pre-write snapshot AND its create flag must reach
    # ToolResultReady AND survive serialization: the TUI renderer only sees events
    # after the agent subprocess serializes them to JSON, so a field that doesn't
    # round-trip renders no diff — the exact failure that retired the opaque `data`
    # field. created rides alongside before_text to disambiguate a None snapshot.
    call = ToolCall(
        call_id="call-1",
        name="write",
        arguments={"path": "f.txt", "content": "new\n"},
        response_id="response-1",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    response_id="response-1",
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=WriteSnapshotExecutor()),
                messages=(Message(role="user", content="write f.txt"),),
            )
        ]

    events = anyio.run(run)

    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.before_text == "old\n"
    assert result.created is False
    round_tripped = wisp_event_from_json(result.model_dump_json())
    assert round_tripped.before_text == "old\n"
    assert round_tripped.created is False
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.before_text == "old\n"
    assert ended.created is False
    assert wisp_event_from_json(ended.model_dump_json()).before_text == "old\n"


class SummaryExecutor:
    """Emits a terminal result carrying a one-line summary, like a read-type tool."""

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="line 1\nline 2\nline 3\n",
            is_error=False,
            summary="read 3 lines from f.txt",
            truncated=True,
        )


def test_pure_loop_forwards_summary_across_the_wire() -> None:
    # A read-type tool's one-line summary AND its truncation flag must reach
    # ToolResultReady AND survive serialization — the renderer shows the summary in
    # place of the raw output, and the card shows a "truncated" marker on expand, so a
    # field that doesn't round-trip would silently drop either signal.
    call = ToolCall(
        call_id="call-1",
        name="read",
        arguments={"path": "f.txt"},
        response_id="response-1",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    response_id="response-1",
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=SummaryExecutor()),
                messages=(Message(role="user", content="read f.txt"),),
            )
        ]

    events = anyio.run(run)

    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.summary == "read 3 lines from f.txt"
    assert result.truncated is True
    round_tripped = wisp_event_from_json(result.model_dump_json())
    assert round_tripped.summary == "read 3 lines from f.txt"
    assert round_tripped.truncated is True
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.summary == "read 3 lines from f.txt"
    assert ended.truncated is True
    round_tripped_ended = wisp_event_from_json(ended.model_dump_json())
    assert round_tripped_ended.summary == "read 3 lines from f.txt"
    assert round_tripped_ended.truncated is True
