from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import Mock

import anyio
import pytest

import wisp.agent.loop.runner as agent_loop_module
from tests.agent.loop.support import (
    NeverToolExecutor,
    RaisingRequestBoundaryHook,
    RecordingToolExecutor,
    completed_stream,
)
from tests.support.agent_runtime import (
    assert_turn_terminals,
)
from wisp.agent.context_budget import observe_context
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
)
from wisp.events import (
    BillableTokenUsage,
    ContextEstimated,
    ContextOverflow,
    ErrorEvent,
    MessageCompleted,
    MessageStarted,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
    UsageCost,
    UsageCostRates,
    wisp_event_from_json,
)
from wisp.providers.base import (
    ContextOverflowError,
    ToolCallResult,
    ToolSpec,
)
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


class CacheAwareScriptedProvider(ScriptedProvider):
    """Scripted provider opting into the prompt-cache-key capability."""

    supports_prompt_cache_key = True


def test_delegates_context_budget_to_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="selected-model"),
                ProviderResponseCompleted(content="done"),
            ]
        ]
    )
    tools = (ToolSpec(name="read", description="Read", input_schema={"type": "object"}),)
    prefix = (Message(role="user", content="observed prefix"),)
    observation = observe_context(
        prefix,
        tools,
        provider=provider.name,
        model="selected-model",
        input_tokens=23,
    )
    messages = (
        *prefix,
        Message(role="assistant", content="prior reply", context_observation=observation),
        Message(role="user", content="trailing request"),
    )
    original_estimate_context_budget = agent_loop_module.estimate_context_budget
    budget_spy = Mock(wraps=original_estimate_context_budget)
    monkeypatch.setattr(agent_loop_module, "estimate_context_budget", budget_spy)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    tools=tools,
                    model="selected-model",
                    context_window=1_000,
                    context_reserve_tokens=100,
                ),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    budget_spy.assert_called_once_with(
        messages,
        tools,
        context_window=1_000,
        reserve_tokens=100,
        observation=observation,
        provider=provider.name,
        model="selected-model",
    )
    estimated = next(event for event in events if isinstance(event, ContextEstimated))
    assert estimated.budget == original_estimate_context_budget(
        messages,
        tools,
        context_window=1_000,
        reserve_tokens=100,
        observation=observation,
        provider=provider.name,
        model="selected-model",
    )


def test_streams_without_application_dependencies() -> None:
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


class RaisingCancellationToken:
    """Cancellation token whose is_cancelled() raises instead of returning a bool."""

    def is_cancelled(self) -> bool:
        raise RuntimeError("boom")


def test_does_not_mask_cancellation_token_errors() -> None:
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


def test_does_not_complete_unstarted_turn_with_offset() -> None:
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
    assert_turn_terminals(events)


def test_cost_estimator_gets_the_response_model() -> None:
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


def test_missing_usage_is_unpriced_but_response_is_kept() -> None:
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


def test_contains_cost_estimator_failures() -> None:
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


def test_works_with_provider_without_effort_parameter() -> None:
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


def test_forwards_effort_to_provider_that_supports_it() -> None:
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


def test_forwards_executor_events_and_provider_results() -> None:
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


def test_context_overflow_closes_started_message_before_retry() -> None:
    """A raised overflow must settle the public response before a retry turn."""

    class StartedOverflowProvider:
        name = "started-overflow"
        default_model = "test"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(
            self,
            messages: Sequence[Message],
            *,
            model: str | None = None,
            tools: Sequence[ToolSpec] = (),
            tool_results: Sequence[ToolCallResult] = (),
            previous_response_id: str | None = None,
            effort: str | None = None,
        ) -> AsyncIterator[ProviderEvent]:
            del messages, model, tools, tool_results, previous_response_id, effort
            self.calls += 1
            yield ProviderResponseStarted(
                model="test",
                response_id="failed-response" if self.calls == 1 else "recovered-response",
            )
            if self.calls == 1:
                raise ContextOverflowError("maximum context length exceeded")
            yield ProviderResponseCompleted(
                content="recovered",
                response_id="recovered-response",
            )

    class RecoverOverflow:
        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision:
            del snapshot
            return RequestBoundaryDecision(
                messages=(Message(role="user", content="compacted summary"),)
            )

    provider = StartedOverflowProvider()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    context_overflow_hook=RecoverOverflow(),
                ),
                messages=(Message(role="user", content="long prompt"),),
            )
        ]

    events = anyio.run(run)

    lifecycle_events = [
        event for event in events if isinstance(event, MessageStarted | MessageCompleted)
    ]
    assert [type(event) for event in lifecycle_events] == [
        MessageStarted,
        MessageCompleted,
        MessageStarted,
        MessageCompleted,
    ]
    failed_completion = lifecycle_events[1]
    assert isinstance(failed_completion, MessageCompleted)
    assert failed_completion.finish_reason == "error"
    assert failed_completion.response_id == "failed-response"
    assert [event.turn for event in events if isinstance(event, TurnCompleted)] == [1, 2]
    assert provider.calls == 2


@pytest.mark.parametrize("raised", [False, True])
def test_context_overflow_hook_retries_in_the_same_loop(raised: bool) -> None:
    """A hook may replace the failed request without constructing another loop."""

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="rejected"),
                ProviderTextDelta(delta="partial answer"),
                ContextOverflowError("context window exceeded")
                if raised
                else ProviderResponseFailed(
                    message="context window exceeded",
                    failure_kind="context_overflow",
                    response_id="rejected",
                ),
            ],
            completed_stream("recovered", response_id="recovered-response"),
        ]
    )

    class RecoverOverflow:
        def __init__(self) -> None:
            self.snapshots: list[ContextOverflowSnapshot] = []

        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision | None:
            self.snapshots.append(snapshot)
            return RequestBoundaryDecision(
                messages=(Message(role="user", content="compacted summary"),)
            )

    hook = RecoverOverflow()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    context_window=100,
                    context_overflow_hook=hook,
                ),
                messages=(Message(role="user", content="long prompt"),),
            )
        ]

    events = anyio.run(run)

    assert [event.turn for event in events if isinstance(event, TurnCompleted)] == [1, 2]
    assert any(isinstance(event, ContextOverflow) for event in events)
    assert hook.snapshots[0].had_streamed_delta is True
    assert hook.snapshots[0].continuation_messages == ()
    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "context.overflow",
        "turn.completed",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    failed_completion = next(event for event in events if isinstance(event, MessageCompleted))
    assert failed_completion.content == "partial answer"
    assert failed_completion.response_id == "rejected"
    assert failed_completion.finish_reason == "error"
    assert [
        (event.turn, event.outcome) for event in events if isinstance(event, TurnCompleted)
    ] == [
        (1, "failed"),
        (2, "completed"),
    ]
    assert len(provider.calls) == 2
    assert [(message.role, message.content) for message in provider.calls[1].messages] == [
        ("user", "compacted summary")
    ]
    assert provider.calls[1].previous_response_id is None
    assert provider.calls[1].tool_results == ()
    assert provider.calls[1].extra_messages == ()


@pytest.mark.parametrize("raised", [False, True])
@pytest.mark.parametrize("with_hook", [False, True])
def test_unrecovered_overflow_keeps_events_and_raises(
    raised: bool,
    with_hook: bool,
) -> None:
    events: list[agent_loop_module.AgentLoopEvent] = []
    snapshots: list[ContextOverflowSnapshot] = []

    class DeclineRecovery:
        async def recover_context_overflow(
            self,
            *,
            snapshot: ContextOverflowSnapshot,
        ) -> RequestBoundaryDecision | None:
            assert events[-1].type == "context.overflow"
            snapshots.append(snapshot)
            return None

    error = ContextOverflowError("context window exceeded")
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="rejected"),
                ProviderTextDelta(delta="partial"),
                error
                if raised
                else ProviderResponseFailed(message=str(error), response_id="rejected"),
            ]
        ]
    )

    async def run() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                context_overflow_hook=DeclineRecovery() if with_hook else None,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            events.append(event)

    overflow = pytest.raises(ContextOverflowError) if raised and not with_hook else nullcontext()
    with overflow:
        anyio.run(run)

    expected = [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "context.overflow",
        "error",
        "turn.completed",
    ]
    assert [event.type for event in events] == expected
    assert len(provider.calls) == 1
    completion = next(event for event in events if isinstance(event, MessageCompleted))
    assert (completion.content, completion.response_id, completion.finish_reason) == (
        "partial",
        "rejected",
        "error",
    )
    assert len(snapshots) == int(with_hook)
    if with_hook:
        assert snapshots[0].had_streamed_delta is True
        assert snapshots[0].continuation_messages == ()
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert (terminal.outcome, terminal.finish_reason) == ("failed", "error")


@pytest.mark.parametrize("had_tool_calls", [False, True])
def test_cancel_after_completed_turn_keeps_boundary_behavior(
    had_tool_calls: bool,
) -> None:
    class Token:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    token = Token()
    tool_call = ToolCall(call_id="call-1", name="test", arguments={})
    stream = (
        [
            ProviderResponseStarted(model="test"),
            ProviderToolCallCompleted(tool_call=tool_call),
            ProviderResponseCompleted(
                content="", tool_calls=(tool_call,), finish_reason="tool_calls"
            ),
        ]
        if had_tool_calls
        else completed_stream("done")
    )
    provider = ScriptedProvider([stream])

    async def run() -> list[agent_loop_module.AgentLoopEvent]:
        events: list[agent_loop_module.AgentLoopEvent] = []
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=RecordingToolExecutor(),
                cancellation_token=token,
                request_boundary_hook=RaisingRequestBoundaryHook(),
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            events.append(event)
            if isinstance(event, TurnCompleted):
                token.cancelled = True
        return events

    events = anyio.run(run)
    assert len(provider.calls) == 1
    assert [
        (event.turn, event.outcome) for event in events if isinstance(event, TurnCompleted)
    ] == [
        (1, "completed"),
    ]
    assert [event.message for event in events if isinstance(event, ErrorEvent)] == (
        ["Agent run cancelled"] if had_tool_calls else []
    )
    assert events[-1].type == ("error" if had_tool_calls else "turn.completed")
