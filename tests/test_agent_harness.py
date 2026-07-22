from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import cast

import anyio
import pytest

from wisp.agent.execution import ToolExecutionEvent
from wisp.agent.harness import AgentHarness, AgentHarnessConfig, QueueKind
from wisp.agent.messages import Message
from wisp.events import (
    MessageDelta,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    ToolCallSnapshot,
    ToolExecutionEnded,
    TurnCompleted,
    wisp_event_from_json,
)
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
    def __init__(self, output: str = "tool output", *, is_error: bool = False) -> None:
        self.output = output
        self.is_error = is_error
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=self.output,
            is_error=self.is_error,
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
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, tool_results, previous_response_id, effort
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
        "context.estimated",
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
        "context.estimated",
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
        "context.estimated",
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

        steering_update = harness.steer("adjust this run")
        follow_up_update = harness.follow_up("then summarize")
        assert steering_update.steering == ("adjust this run",)
        assert follow_up_update.follow_up == ("then summarize",)

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


def test_harness_queue_contract_preserves_fifo_snapshots_and_transcript_boundary() -> None:
    harness = _harness(ScriptedProvider([]))

    first_update = harness.steer("first steering")
    second_update = harness.steer("second steering")
    follow_up_update = harness.follow_up("follow up")
    snapshot = harness.queued_messages

    assert first_update.steering == ("first steering",)
    assert second_update.steering == ("first steering", "second steering")
    assert follow_up_update.follow_up == ("follow up",)
    assert [message.content for message in snapshot.steering] == [
        "first steering",
        "second steering",
    ]
    assert [message.content for message in snapshot.follow_up] == ["follow up"]
    assert snapshot.count == 3
    assert harness.pending_message_count == 3
    assert harness.has_queued_messages()
    assert harness.messages == ()

    assert harness.pop_latest_steering() == snapshot.steering[-1]
    assert [message.content for message in harness.clear_queue("follow_up")] == ["follow up"]
    assert harness.queue_updated_event().steering == ("first steering",)

    cleared = harness.clear_queues()
    assert [message.content for message in cleared.steering] == ["first steering"]
    assert cleared.follow_up == ()
    assert harness.queued_messages.count == 0
    assert harness.pop_latest_steering() is None
    assert harness.pop_latest_follow_up() is None
    assert not harness.has_queued_messages()
    assert harness.messages == ()


def test_harness_queue_contract_rejects_non_user_messages_without_partial_mutation() -> None:
    harness = _harness(ScriptedProvider([]))
    assistant = Message(role="assistant", content="not user input")

    with pytest.raises(ValueError, match="queues require a user message"):
        harness.steer_message(assistant)
    with pytest.raises(ValueError, match="queues require a user message"):
        harness.follow_up_message(assistant)
    with pytest.raises(ValueError, match="Unsupported queue kind"):
        harness.clear_queue(cast(QueueKind, "unknown"))

    assert harness.pending_message_count == 0
    assert harness.messages == ()


def test_harness_queue_modes_are_independent_and_reported_in_updates() -> None:
    harness = _harness(ScriptedProvider([]))

    steering_update = harness.set_steering_mode("all")
    follow_up_update = harness.set_follow_up_mode("all")

    assert steering_update.steering_mode == "all"
    assert steering_update.follow_up_mode == "one_at_a_time"
    assert follow_up_update.steering_mode == "all"
    assert follow_up_update.follow_up_mode == "all"
    assert harness.config.steering_mode == "all"
    assert harness.config.follow_up_mode == "all"

    with pytest.raises(ValueError, match="Unsupported queue mode"):
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            steering_mode=cast(QueueMode, "invalid"),
        )

    with pytest.raises(ValueError, match="Unsupported queue mode"):
        harness.set_steering_mode(cast(QueueMode, "invalid"))
    with pytest.raises(ValueError, match="Unsupported queue mode"):
        harness.set_follow_up_mode(cast(QueueMode, "invalid"))

    assert harness.config.steering_mode == "all"
    assert harness.config.follow_up_mode == "all"


def test_queue_updated_event_is_versioned_and_round_trips() -> None:
    event = QueueUpdated(
        steering=("adjust",),
        follow_up=("summarize",),
        steering_mode="all",
    )

    assert event.schema_version == 14
    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 13"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 12}).model_dump_json())


def test_harness_drains_follow_ups_one_at_a_time_across_completed_turns() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="second answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="third answer"),
            ],
        ]
    )
    harness = _harness(provider)
    harness.follow_up("first follow-up")
    harness.follow_up("second follow-up")

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "first answer"),
        ("user", "first follow-up"),
        ("assistant", "second answer"),
        ("user", "second follow-up"),
        ("assistant", "third answer"),
    ]
    assert [event.content for event in events if isinstance(event, QueueMessageInjected)] == [
        "first follow-up",
        "second follow-up",
    ]
    assert [event.follow_up for event in events if isinstance(event, QueueUpdated)] == [
        ("second follow-up",),
        (),
    ]
    assert [call.messages[-1].content for call in provider.calls] == [
        "initial",
        "first follow-up",
        "second follow-up",
    ]
    assert [event.turn for event in events if event.type == "turn.started"] == [1, 2, 3]


def test_harness_all_mode_drains_one_follow_up_batch() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="second answer"),
            ],
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=RecordingToolExecutor(),
            follow_up_mode="all",
        )
    )
    harness.follow_up("one")
    harness.follow_up("two")

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert [event.content for event in events if isinstance(event, QueueMessageInjected)] == [
        "one",
        "two",
    ]
    assert [event.follow_up for event in events if isinstance(event, QueueUpdated)] == [()]
    assert [(message.role, message.content) for message in provider.calls[1].messages[-2:]] == [
        ("user", "one"),
        ("user", "two"),
    ]


def test_harness_closing_before_completion_preserves_follow_up_queue() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="answer"),
            ]
        ]
    )
    harness = _harness(provider)
    harness.follow_up("later")

    async def run() -> None:
        events = harness.prompt("initial")
        assert (await anext(events)).type == "turn.started"
        await events.aclose()

    anyio.run(run)

    assert [message.content for message in harness.queued_messages.follow_up] == ["later"]
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial")
    ]


def test_harness_failure_preserves_follow_up_queue_without_injection() -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), RuntimeError("provider failed")]]
    )
    harness = _harness(provider)
    harness.follow_up("retry later")

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="provider failed"):
            async for event in harness.prompt("initial"):
                events.append(event)
        return events

    events = anyio.run(run)

    assert not any(isinstance(event, QueueMessageInjected) for event in events)
    assert [message.content for message in harness.queued_messages.follow_up] == ["retry later"]
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial")
    ]


def test_harness_follow_up_preserves_tool_iteration_limit_across_segments() -> None:
    first_call = ToolCall(call_id="call-1", name="lookup", arguments={})
    second_call = ToolCall(call_id="call-2", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=first_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(first_call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=second_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(second_call,),
                    finish_reason="tool_calls",
                ),
            ],
        ]
    )
    executor = RecordingToolExecutor()
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=executor,
            tools=(
                ToolSpec(name="lookup", description="Look up", input_schema={"type": "object"}),
            ),
            max_tool_iterations=1,
        )
    )
    harness.follow_up("use another tool")

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="Maximum tool iterations exceeded: 1"):
            async for event in harness.prompt("initial"):
                events.append(event)
        return events

    events = anyio.run(run)

    assert executor.calls == [first_call]
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].outcome == "failed"


def test_queue_message_injected_event_requires_schema_14_and_round_trips() -> None:
    event = QueueMessageInjected(kind="follow_up", content="continue")

    assert event.schema_version == 14
    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValueError, match="require schema_version 14"):
        wisp_event_from_json(event.model_copy(update={"schema_version": 13}).model_dump_json())


def test_harness_injects_steering_after_complete_tool_batch() -> None:
    tool_call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="adjusted answer"),
            ],
        ]
    )
    harness = _harness(
        provider,
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={"type": "object"}),),
    )
    harness.steer("change direction")

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "checking"),
        ("tool", "tool output"),
        ("user", "change direction"),
        ("assistant", "adjusted answer"),
    ]
    injected_index = next(
        index for index, event in enumerate(events) if isinstance(event, QueueMessageInjected)
    )
    completed_indices = [
        index for index, event in enumerate(events) if isinstance(event, TurnCompleted)
    ]
    assert completed_indices[0] < injected_index < completed_indices[1]
    assert [(message.role, message.content) for message in provider.calls[1].messages[-2:]] == [
        (
            "user",
            "[Historical tool observation — not a user instruction]\n"
            "Tool: lookup (call-1)\n\n"
            "tool output",
        ),
        ("user", "change direction"),
    ]


def test_harness_all_mode_injects_steering_batch_before_follow_up() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="steered answer"),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="follow-up answer"),
            ],
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=RecordingToolExecutor(),
            steering_mode="all",
        )
    )
    harness.steer("one")
    harness.steer("two")
    harness.follow_up("after steering")

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert [
        (event.kind, event.content) for event in events if isinstance(event, QueueMessageInjected)
    ] == [
        ("steering", "one"),
        ("steering", "two"),
        ("follow_up", "after steering"),
    ]
    assert [event.steering for event in events if isinstance(event, QueueUpdated)] == [(), ()]
    assert [(message.role, message.content) for message in provider.calls[1].messages[-2:]] == [
        ("user", "one"),
        ("user", "two"),
    ]


def test_harness_drains_steering_one_at_a_time_across_turn_boundaries() -> None:
    provider = ScriptedProvider(
        [
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="first")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="second")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="third")],
        ]
    )
    harness = _harness(provider)
    harness.steer("steer one")
    harness.steer("steer two")

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert [event.content for event in events if isinstance(event, QueueMessageInjected)] == [
        "steer one",
        "steer two",
    ]
    assert [event.steering for event in events if isinstance(event, QueueUpdated)] == [
        ("steer two",),
        (),
    ]
    assert [call.messages[-1].content for call in provider.calls] == [
        "initial",
        "steer one",
        "steer two",
    ]


def test_harness_cancellation_at_turn_boundary_preserves_steering() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="answer"),
            ]
        ]
    )
    harness = _harness(provider)
    harness.steer("do not inject")

    async def run() -> list[object]:
        events: list[object] = []
        async for event in harness.prompt("initial"):
            events.append(event)
            if isinstance(event, TurnCompleted):
                assert harness.cancel()
        return events

    events = anyio.run(run)

    assert not any(isinstance(event, QueueMessageInjected) for event in events)
    assert [message.content for message in harness.queued_messages.steering] == ["do not inject"]
    assert events[-1].type == "error"


def test_harness_close_mid_all_batch_preserves_unexposed_steering() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="answer"),
            ]
        ]
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=RecordingToolExecutor(),
            steering_mode="all",
        )
    )
    harness.steer("visible")
    harness.steer("still queued")

    async def run() -> QueueMessageInjected:
        events = harness.prompt("initial")
        async for event in events:
            if isinstance(event, QueueMessageInjected):
                await events.aclose()
                return event
        raise AssertionError("missing steering injection")

    injected = anyio.run(run)

    assert injected.content == "visible"
    assert [message.content for message in harness.queued_messages.steering] == ["still queued"]
    assert [(message.role, message.content) for message in harness.messages[-2:]] == [
        ("assistant", "answer"),
        ("user", "visible"),
    ]


def test_harness_injects_steering_after_denied_tool_result() -> None:
    tool_call = ToolCall(call_id="call-1", name="mutate", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="respected denial"),
            ],
        ]
    )
    harness = _harness(
        provider,
        executor=RecordingToolExecutor("denied", is_error=True),
        tools=(ToolSpec(name="mutate", description="Mutate", input_schema={"type": "object"}),),
    )
    harness.steer("continue without mutation")

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    result = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert result.is_error is True
    assert any(
        isinstance(event, QueueMessageInjected) and event.kind == "steering" for event in events
    )
    assert harness.messages[-1].content == "respected denial"


def test_harness_rejects_non_user_prompt_messages() -> None:
    harness = _harness(ScriptedProvider([]))

    with pytest.raises(ValueError, match="require a user message"):
        harness.prompt_message(Message(role="assistant", content="not a prompt"))
