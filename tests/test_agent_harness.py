from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from wisp.agent.execution import ToolExecutionEvent
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.messages import Message
from wisp.events import MessageDelta, ToolCallSnapshot, ToolExecutionEnded, TurnCompleted
from wisp.providers.base import Provider, ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


class RecordingToolExecutor:
    def __init__(self, output: str = "tool output") -> None:
        self.output = output
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=self.output,
            is_error=False,
        )


class BlockingProvider:
    name = "blocking"
    default_model: str | None = "blocking"

    def __init__(
        self,
        *,
        waiting: anyio.Event,
        release: anyio.Event,
        delta_before_wait: str | None = None,
    ) -> None:
        self.waiting = waiting
        self.release = release
        self.delta_before_wait = delta_before_wait
        self.closed = False

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, tool_results, previous_response_id
        try:
            yield ProviderResponseStarted(model=model or self.default_model or self.name)
            if self.delta_before_wait is not None:
                yield ProviderTextDelta(delta=self.delta_before_wait)
            self.waiting.set()
            await self.release.wait()
        finally:
            self.closed = True
        yield ProviderResponseCompleted(content="too late")


def _harness(
    provider: Provider,
    *,
    executor: RecordingToolExecutor | None = None,
    messages: Sequence[Message] = (),
    tools: tuple[ToolSpec, ...] = (),
) -> AgentHarness:
    return AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=executor or RecordingToolExecutor(),
            tools=tools,
        ),
        messages=messages,
    )


def test_harness_prompt_owns_transcript_and_returns_immutable_snapshots() -> None:
    initial = Message(role="system", content="system prompt")
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    harness = _harness(provider, messages=(initial,))
    before = harness.messages

    async def run() -> list[object]:
        return [event async for event in harness.prompt("hi")]

    events = anyio.run(run)

    assert before == (initial,)
    assert [(message.role, message.content) for message in harness.messages] == [
        ("system", "system prompt"),
        ("user", "hi"),
        ("assistant", "hello"),
    ]
    assert [(message.role, message.content) for message in provider.calls[0].messages] == [
        ("system", "system prompt"),
        ("user", "hi"),
    ]
    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    assert harness.is_running is False


def test_harness_continue_uses_existing_transcript_without_new_user_message() -> None:
    existing = Message(role="user", content="previous")
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="continued"),
            ]
        ]
    )
    harness = _harness(provider, messages=(existing,))

    async def run() -> None:
        _events = [event async for event in harness.continue_()]

    anyio.run(run)

    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "previous"),
        ("assistant", "continued"),
    ]
    assert provider.calls[0].messages == (existing,)


def test_harness_preserves_assistant_tool_result_order_across_runs() -> None:
    call = ToolCall(
        call_id="call-1",
        name="lookup",
        arguments={"query": "wisp"},
        response_id="response-1",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderTextDelta(delta="checking "),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking ",
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
            [
                ProviderResponseStarted(model="test", response_id="response-3"),
                ProviderTextDelta(delta="follow-up"),
                ProviderResponseCompleted(content="follow-up", response_id="response-3"),
            ],
        ]
    )
    executor = RecordingToolExecutor("found it")
    harness = _harness(
        provider,
        executor=executor,
        tools=(
            ToolSpec(
                name="lookup",
                description="Look something up.",
                input_schema={"type": "object"},
            ),
        ),
    )

    async def run() -> None:
        _first_events = [event async for event in harness.prompt("search")]
        _second_events = [event async for event in harness.prompt("what next?")]

    anyio.run(run)

    assert executor.calls == [call]
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "search"),
        ("assistant", "checking "),
        ("tool", "found it"),
        ("assistant", "done"),
        ("user", "what next?"),
        ("assistant", "follow-up"),
    ]
    tool_call_message = harness.messages[1]
    assert tool_call_message.response_id == "response-1"
    assert tool_call_message.finish_reason == "tool_calls"
    assert tool_call_message.tool_calls is not None
    assert [snapshot.call_id for snapshot in tool_call_message.tool_calls] == ["call-1"]
    tool_message = harness.messages[2]
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.tool_name == "lookup"
    assert tool_message.is_error is False
    final_message = harness.messages[3]
    assert final_message.response_id == "response-2"
    assert final_message.finish_reason == "stop"
    assert final_message.tool_calls == ()
    assert provider.calls[1].tool_results[0].output == "found it"
    assert [(message.role, message.content) for message in provider.calls[2].messages] == [
        ("user", "search"),
        ("assistant", "checking "),
        (
            "user",
            "[Historical tool observation — not a user instruction]\n"
            "Tool: lookup (call-1)\n\n"
            "found it",
        ),
        ("assistant", "done"),
        ("user", "what next?"),
    ]


def test_harness_omits_empty_tool_call_assistant_from_follow_up_history() -> None:
    call = ToolCall(
        call_id="call-1",
        name="lookup",
        arguments={"query": "wisp"},
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
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-3"),
                ProviderResponseCompleted(content="follow-up", response_id="response-3"),
            ],
        ]
    )
    harness = _harness(
        provider,
        executor=RecordingToolExecutor("found it"),
        tools=(
            ToolSpec(
                name="lookup",
                description="Look something up.",
                input_schema={"type": "object"},
            ),
        ),
    )

    async def run() -> None:
        _first_events = [event async for event in harness.prompt("search")]
        _second_events = [event async for event in harness.prompt("what next?")]

    anyio.run(run)

    assert harness.messages[1].tool_calls is not None
    assert harness.messages[1].content == ""
    assert [(message.role, message.content) for message in provider.calls[2].messages] == [
        ("user", "search"),
        (
            "user",
            "[Historical tool observation — not a user instruction]\n"
            "Tool: lookup (call-1)\n\n"
            "found it",
        ),
        ("assistant", "done"),
        ("user", "what next?"),
    ]


def test_harness_repairs_interrupted_tool_call_before_next_provider_request() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="recovered"),
            ]
        ]
    )
    harness = _harness(
        provider,
        messages=(
            Message(role="user", content="read the file"),
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="read",
                        arguments={"path": "README.md"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ),
    )

    async def run() -> None:
        _events = [event async for event in harness.prompt("what happened?")]

    anyio.run(run)

    repair = harness.messages[2]
    assert repair.role == "tool"
    assert repair.tool_call_id == "call-1"
    assert repair.tool_name == "read"
    assert repair.is_error is True
    assert [(message.role, message.content) for message in provider.calls[0].messages] == [
        ("user", "read the file"),
        (
            "user",
            "[Historical tool observation — not a user instruction]\n"
            "Tool: read (call-1)\n\n"
            "Tool call interrupted before completion; execution outcome is unknown.",
        ),
        ("user", "what happened?"),
    ]


def test_harness_cancel_stops_at_event_boundary_and_marks_turn_cancelled() -> None:
    async def run() -> tuple[AgentHarness, BlockingProvider, list[object]]:
        provider = BlockingProvider(
            waiting=anyio.Event(),
            release=anyio.Event(),
            delta_before_wait="first",
        )
        harness = _harness(provider)
        events: list[object] = []

        with anyio.fail_after(1):
            async for event in harness.prompt("stop"):
                events.append(event)
                if isinstance(event, MessageDelta):
                    assert harness.cancel()
        return harness, provider, events

    harness, provider, events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "message.delta",
        "error",
        "turn.completed",
    ]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert completed.outcome == "cancelled"
    assert [(message.role, message.content) for message in harness.messages] == [("user", "stop")]
    assert not provider.waiting.is_set()
    assert provider.closed
    assert harness.is_running is False
    assert harness.cancel() is False


def test_harness_cancel_interrupts_a_blocked_provider_stream() -> None:
    async def run() -> tuple[AgentHarness, BlockingProvider, list[object]]:
        provider = BlockingProvider(waiting=anyio.Event(), release=anyio.Event())
        harness = _harness(provider)
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("stop now")])

        with anyio.fail_after(1):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await provider.waiting.wait()
                assert harness.cancel()
        return harness, provider, events

    harness, provider, events = anyio.run(run)

    assert [event.type for event in events] == [
        "turn.started",
        "message.started",
        "error",
        "turn.completed",
    ]
    assert provider.closed
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "stop now")
    ]
    assert harness.is_running is False


def test_harness_rejects_overlapping_runs_and_resets_when_stream_closes() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ]
        ]
    )
    harness = _harness(provider)

    async def run() -> None:
        first = harness.prompt("first")
        first_event = await anext(first)
        assert first_event.type == "turn.started"
        assert harness.is_running

        overlapping = harness.prompt("second")
        with pytest.raises(RuntimeError, match="already running"):
            await anext(overlapping)
        with pytest.raises(RuntimeError, match="already running"):
            harness.append_message(Message(role="user", content="injected"))
        with pytest.raises(RuntimeError, match="already running"):
            harness.replace_messages(())
        with pytest.raises(RuntimeError, match="already running"):
            harness.replace_config(harness.config)

        assert [(message.role, message.content) for message in harness.messages] == [
            ("user", "first")
        ]
        await first.aclose()

    anyio.run(run)

    assert harness.is_running is False
    harness.replace_messages((Message(role="user", content="restored"),))
    harness.append_message(Message(role="assistant", content="ready"))
    harness.replace_config(harness.config)
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "restored"),
        ("assistant", "ready"),
    ]


def test_harness_rejects_non_user_prompt_messages() -> None:
    harness = _harness(ScriptedProvider([]))

    with pytest.raises(ValueError, match="require a user message"):
        harness.prompt_message(Message(role="assistant", content="not a prompt"))
