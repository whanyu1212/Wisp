from __future__ import annotations

from typing import cast

import anyio
import pytest

from tests.agent.loop.support import (
    NeverToolExecutor,
    RecordingRequestBoundaryHook,
    RecordingToolExecutor,
    completed_stream,
)
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    RequestBoundaryDecision,
)
from wisp.events import (
    MessageCompleted,
    ToolResultReady,
    TurnCompleted,
)
from wisp.providers.base import (
    ProviderProtocolError,
    ToolCallResult,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderFinishReason,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


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


@pytest.mark.parametrize(
    ("finish_reason", "calls", "error"),
    [
        ("tool_calls", (), "requires at least one tool call"),
        (
            "stop",
            (ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"}),),
            "cannot include tool calls",
        ),
    ],
)
def test_pure_loop_rejects_inconsistent_finish_reason_tool_call_combinations(
    finish_reason: ProviderFinishReason,
    calls: tuple[ToolCall, ...],
    error: str,
) -> None:
    provider_events: list[ProviderEvent] = [ProviderResponseStarted(model="test")]
    provider_events.extend(ProviderToolCallCompleted(tool_call=call) for call in calls)
    provider_events.append(
        ProviderResponseCompleted(
            content="",
            tool_calls=calls,
            finish_reason=finish_reason,
        )
    )
    provider = ScriptedProvider([provider_events])
    executor = RecordingToolExecutor()
    hook = RecordingRequestBoundaryHook([])

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ProviderProtocolError, match=error):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    request_boundary_hook=hook,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert executor.calls == []
    assert hook.snapshots == []
    assert not any(isinstance(event, MessageCompleted) for event in events)
    assert not any(event.type.startswith("tool.") for event in events)


def test_pure_loop_fails_truncated_tool_batch_in_band_without_execution() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"}),
        ToolCall(
            call_id="call-2",
            name="bash",
            arguments={"command": "echo incomplete"},
            parse_error="truncated arguments",
        ),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="truncated-response"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=calls,
                    response_id="truncated-response",
                    finish_reason="length",
                ),
            ],
            completed_stream("recovered", response_id="recovered-response"),
        ]
    )
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(stop=False)])

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    request_boundary_hook=hook,
                ),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)

    completed = [event for event in events if isinstance(event, MessageCompleted)]
    assert completed[0].finish_reason == "length"
    assert [call.call_id for call in completed[0].tool_calls] == ["call-1", "call-2"]
    tool_events = [event for event in events if event.type.startswith("tool.")]
    assert [(event.type, event.call_id) for event in tool_events] == [
        ("tool.call", "call-1"),
        ("tool.execution.ended", "call-1"),
        ("tool.result", "call-1"),
        ("tool.call", "call-2"),
        ("tool.execution.ended", "call-2"),
        ("tool.result", "call-2"),
    ]
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert all(result.is_error for result in results)
    assert all(result.failure_code == "invalid_arguments" for result in results)
    assert all(result.retryable for result in results)
    assert all(
        result.recovery_hint == "Re-issue the tool call with complete arguments."
        for result in results
    )
    assert provider.calls[1].previous_response_id == "truncated-response"
    assert provider.calls[1].tool_results == (
        ToolCallResult(
            call_id="call-1",
            output=results[0].output,
            is_error=True,
        ),
        ToolCallResult(
            call_id="call-2",
            output=results[1].output,
            is_error=True,
        ),
    )
    assert hook.snapshots[0].had_tool_calls is True
    assert [message.role for message in hook.snapshots[0].continuation_messages] == [
        "assistant",
        "tool",
        "tool",
    ]


def test_truncated_tool_batch_honors_cancellation_after_synthetic_result() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="length",
                ),
            ]
        ]
    )

    class MutableCancellationToken:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    token = MutableCancellationToken()

    async def run() -> list[object]:
        events: list[object] = []
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                cancellation_token=token,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            events.append(event)
            if isinstance(event, ToolResultReady):
                token.cancelled = True
        return events

    events = anyio.run(run)

    assert [event.type for event in events[-3:]] == [
        "tool.result",
        "error",
        "turn.completed",
    ]
    terminal = cast(TurnCompleted, events[-1])
    assert terminal.outcome == "cancelled"
    assert terminal.finish_reason == "cancelled"
    assert len(provider.calls) == 1


def test_truncated_tool_batches_count_toward_the_tool_iteration_limit() -> None:
    first = ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"})
    second = ToolCall(call_id="call-2", name="read", arguments={"path": "two.txt"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=first),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(first,),
                    finish_reason="length",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=second),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(second,),
                    finish_reason="length",
                ),
            ],
        ]
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="Maximum tool iterations exceeded: 1"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    max_tool_iterations=1,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in results] == ["call-1"]
    assert len(provider.calls) == 2


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
