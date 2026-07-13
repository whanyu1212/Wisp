from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import pytest

from wisp.agent.execution import ToolExecutionEvent, ToolExecutionProtocolError, ToolExecutor
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.events import (
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolExecutionEnded,
    ToolResultReady,
    wisp_event_from_json,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


class NeverToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


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


def test_pure_loop_streams_without_application_dependencies() -> None:
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
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=messages,
            )
        ]

    events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    assert provider.calls[0].messages == messages


def test_pure_loop_forwards_executor_events_and_provider_results() -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd"},
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
    executor = RecordingToolExecutor()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)

    assert executor.calls == [call]
    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "message.completed",
        "tool.call",
        "tool.execution.started",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
        "tool.result",
        "turn.completed",
        "turn.started",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == "tool output"
    assert provider.calls[1].tool_results[0].output == "tool output"
    assert provider.calls[1].previous_response_id == "response-1"
    # The promoted exit_code reaches the event AND crosses the wire: the TUI
    # renderer only sees events after they are serialized (agent subprocess →
    # JSON → client), so the presentation signal must survive round-tripping.
    assert result.exit_code == 0
    assert wisp_event_from_json(result.model_dump_json()).exit_code == 0
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.exit_code == 0
    assert wisp_event_from_json(ended.model_dump_json()).exit_code == 0


def test_pure_loop_rejects_executor_without_terminal_result() -> None:
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
        with pytest.raises(ToolExecutionProtocolError, match="ended without a result"):
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
