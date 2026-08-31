from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import cast

import anyio
import pytest

import wisp.agent.harness as agent_harness_module
from tests.agent_runtime import (
    assert_settled_tool_calls,
    assert_tool_result_pairing,
    assert_turn_terminals,
)
from wisp.agent.execution import (
    PreparedToolExecution,
    RequestBoundaryDecision,
    RequestBoundaryUnsupportedError,
    RequestContextRebase,
    ToolExecutionEvent,
    ToolExecutor,
    ToolPreparationEvent,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig, QueuedMessages, QueueKind
from wisp.agent.messages import Message
from wisp.events import (
    ErrorEvent,
    MessageDelta,
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
    wisp_event_from_json,
)
from wisp.providers.base import Provider, ToolCallResult, ToolSpec, prepare_provider_history
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
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


class BlockingPreparedExecutor:
    def __init__(self, all_started: anyio.Event) -> None:
        self._all_started = all_started
        self.started_call_ids: set[str] = set()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            self.started_call_ids.add(tool_call.call_id)
            if len(self.started_call_ids) == 2:
                self._all_started.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=run,
        )

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        async for event in self.prepare(tool_call):
            if isinstance(event, PreparedToolExecution):
                yield await event.run()
            else:
                yield event


class FailingAndBlockingPreparedExecutor:
    def __init__(self, failed: anyio.Event) -> None:
        self._failed = failed
        self._second_started = anyio.Event()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            if tool_call.call_id == "call-1":
                await self._second_started.wait()
                self._failed.set()
                raise RuntimeError("executor failed during cancellation")
            self._second_started.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=run,
        )

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        async for event in self.prepare(tool_call):
            if isinstance(event, PreparedToolExecution):
                yield await event.run()
            else:
                yield event


class ImmediatePreparedExecutor:
    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            return ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output=f"output-{tool_call.call_id}",
                is_error=False,
            )

        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=run,
        )

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        async for event in self.prepare(tool_call):
            if isinstance(event, PreparedToolExecution):
                yield await event.run()
            else:
                yield event


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
    executor: ToolExecutor | None = None,
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


@pytest.mark.parametrize(
    ("support", "effort", "expects_native"),
    [(True, None, True), (False, None, False), (None, None, False), (True, "high", False)],
)
def test_harness_requires_explicit_configuration_support_for_native_history(
    support: bool | None,
    effort: str | None,
    expects_native: bool,
) -> None:
    class CapabilityProvider(ScriptedProvider):
        if support is not None:

            def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
                return support and effort is None

    provider = CapabilityProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
    )
    messages = (
        Message(role="user", content="search"),
        Message(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"query": "wisp"}),
            ),
        ),
        Message(
            role="tool",
            content="found it",
            tool_call_id="call-1",
            tool_name="lookup",
            is_error=False,
        ),
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=RecordingToolExecutor(),
            effort=effort,
        ),
        messages=messages,
    )

    async def run() -> None:
        _events = [event async for event in harness.prompt("what next?")]

    anyio.run(run)

    replayed = provider.calls[0].messages
    if expects_native:
        assert [message.role for message in replayed] == [
            "user",
            "assistant",
            "tool",
            "user",
        ]
        assert replayed[1].tool_calls is not None
    else:
        assert [message.role for message in replayed] == ["user", "assistant", "user"]
        assert json.loads(replayed[1].content)["type"] == "wisp.portable_tool_exchange"


def test_harness_continue_treats_completed_tool_turn_as_history() -> None:
    class OpaqueProvider(ScriptedProvider):
        def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
            del effort
            return False

    provider = OpaqueProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="continued")]]
    )
    harness = _harness(
        provider,
        messages=(
            Message(role="user", content="search"),
            Message(
                role="assistant",
                content="checking",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id="call-1",
                        name="lookup",
                        arguments={"query": "wisp"},
                    ),
                ),
            ),
            Message(
                role="tool",
                content="found it",
                tool_call_id="call-1",
                tool_name="lookup",
            ),
            Message(role="assistant", content="done"),
        ),
    )

    async def run() -> None:
        _events = [event async for event in harness.continue_()]

    anyio.run(run)

    replayed = provider.calls[0].messages
    assert [message.role for message in replayed] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert json.loads(replayed[1].content)["type"] == "wisp.portable_tool_exchange"
    assert replayed[2].content == "done"


def test_harness_rebases_active_boundary_after_transcript_replacement() -> None:
    tool_call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="first"),
            ],
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
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )

    class ReplacingPreparer:
        def __init__(self) -> None:
            self.boundaries: list[int] = []

        async def prepare_boundary(
            self, *, context: agent_harness_module.HarnessBoundaryContext
        ) -> RequestBoundaryDecision | None:
            self.boundaries.append(context.active_from)
            if len(self.boundaries) == 1:
                return RequestBoundaryDecision(
                    messages=(Message(role="user", content="compressed history"),)
                )
            if len(self.boundaries) == 2:
                return RequestBoundaryDecision(
                    messages=prepare_provider_history(
                        context.messages,
                        provider=provider,
                        effort=None,
                        active_from=context.active_from,
                    )
                )
            return None

    preparer = ReplacingPreparer()
    harness = _harness(
        provider,
        messages=(
            Message(role="user", content="old question one"),
            Message(role="assistant", content="old answer one"),
            Message(role="user", content="old question two"),
            Message(role="assistant", content="old answer two"),
        ),
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={}),),
    )

    async def run() -> None:
        _events = [event async for event in harness.continue_(boundary_preparer=preparer)]

    anyio.run(run)

    assert preparer.boundaries[:2] == [4, 1]
    assert [message.role for message in provider.calls[2].messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert provider.calls[2].messages[1].tool_calls is not None
    assert provider.calls[2].messages[2].tool_call_id == "call-1"


def test_agent_harness_config_preserves_legacy_positional_field_order() -> None:
    config = AgentHarnessConfig(
        ScriptedProvider([]),
        RecordingToolExecutor(),
        None,
        (),
        None,
        None,
        None,
        16_384,
        0.8,
        None,
        "all",
        "all",
        0,
    )

    assert config.max_pending_queue_messages == 0
    assert config.prompt_cache_key is None


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
    replayed = provider.calls[2].messages
    assert [(message.role, message.content) for message in replayed[::2]] == [
        ("user", "search"),
        ("assistant", "done"),
    ]
    assert (replayed[-1].role, replayed[-1].content) == ("user", "what next?")
    portable_exchange = replayed[1]
    assert portable_exchange.role == "assistant"
    assert portable_exchange.tool_calls is None
    payload = json.loads(portable_exchange.content)
    assert payload["type"] == "wisp.portable_tool_exchange"
    assert payload["assistant_content"] == "checking "
    assert payload["calls"][0]["result"]["output"] == "found it"


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
    replayed = provider.calls[2].messages
    assert [message.role for message in replayed] == ["user", "assistant", "assistant", "user"]
    payload = json.loads(replayed[1].content)
    assert payload["type"] == "wisp.portable_tool_exchange"
    assert payload["assistant_content"] == ""
    assert payload["calls"][0]["result"]["output"] == "found it"


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
    replayed = provider.calls[0].messages
    assert [message.role for message in replayed] == ["user", "assistant", "user"]
    payload = json.loads(replayed[1].content)
    assert payload["type"] == "wisp.portable_tool_exchange"
    result = payload["calls"][0]["result"]
    assert result["is_error"] is True
    assert result["output"] == (
        "Tool call interrupted before completion; execution outcome is unknown."
    )


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
    assert_turn_terminals(events)
    assert_tool_result_pairing(events)


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


def test_harness_queue_capacity_is_shared_and_recovers_after_removal() -> None:
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            max_pending_queue_messages=2,
        )
    )

    harness.steer("first")
    harness.follow_up("second")

    with pytest.raises(RuntimeError, match="maximum 2 pending messages"):
        harness.steer("overflow")

    assert harness.queue_updated_event().steering == ("first",)
    assert harness.queue_updated_event().follow_up == ("second",)
    assert harness.pop_latest_follow_up() is not None

    recovered = harness.steer("replacement")

    assert recovered.steering == ("first", "replacement")
    assert recovered.follow_up == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tool_iterations", -1),
        ("context_window", 0),
        ("context_reserve_tokens", True),
        ("context_pressure_threshold", float("inf")),
    ],
)
def test_agent_harness_config_rejects_invalid_shared_runtime_limits(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            **cast(dict[str, object], {field: value}),
        )


def test_agent_harness_config_accepts_runtime_limit_boundaries() -> None:
    config = AgentHarnessConfig(
        provider=ScriptedProvider([]),
        tool_executor=RecordingToolExecutor(),
        max_tool_iterations=0,
        context_window=1,
        context_reserve_tokens=1,
        context_pressure_threshold=1,
    )

    assert config.max_tool_iterations == 0


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
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            steering_mode=cast(QueueMode, ["all"]),
        )
    for field in ("max_pending_queue_messages", "max_pending_queue_bytes"):
        for invalid_limit in (-1, True):
            with pytest.raises(ValueError, match="non-negative integer"):
                AgentHarnessConfig(
                    provider=ScriptedProvider([]),
                    tool_executor=RecordingToolExecutor(),
                    **cast(dict[str, object], {field: invalid_limit}),
                )

    with pytest.raises(ValueError, match="Unsupported queue mode"):
        harness.set_steering_mode(cast(QueueMode, "invalid"))
    with pytest.raises(ValueError, match="Unsupported queue mode"):
        harness.set_follow_up_mode(cast(QueueMode, "invalid"))

    assert harness.config.steering_mode == "all"
    assert harness.config.follow_up_mode == "all"


def test_harness_queue_byte_limit_rejects_before_mutation() -> None:
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            max_pending_queue_bytes=1,
        )
    )

    with pytest.raises(RuntimeError, match="queue byte limit exceeded"):
        harness.steer("oversized")

    assert harness.queued_messages == QueuedMessages()
    assert harness.pending_message_bytes == 0


def test_queue_updated_event_is_versioned_and_round_trips() -> None:
    event = QueueUpdated(
        steering=("adjust",),
        follow_up=("summarize",),
        steering_mode="all",
    )

    assert event.schema_version == 35
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


@pytest.mark.parametrize("kind", ["steering", "follow_up"])
@pytest.mark.parametrize("mutation", ["pop", "clear"])
def test_harness_all_mode_tolerates_queue_edits_during_drain(
    kind: QueueKind,
    mutation: str,
) -> None:
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
            steering_mode="all" if kind == "steering" else "one_at_a_time",
            follow_up_mode="all" if kind == "follow_up" else "one_at_a_time",
        )
    )
    enqueue = harness.steer if kind == "steering" else harness.follow_up
    for content in ("one", "two", "three"):
        enqueue(content)

    async def run() -> list[object]:
        events: list[object] = []
        mutated = False
        async for event in harness.prompt("initial"):
            events.append(event)
            if isinstance(event, QueueMessageInjected) and not mutated:
                mutated = True
                if mutation == "pop":
                    removed = (
                        harness.pop_latest_steering()
                        if kind == "steering"
                        else harness.pop_latest_follow_up()
                    )
                    assert removed is not None
                    assert removed.content == "three"
                else:
                    assert [message.content for message in harness.clear_queue(kind)] == [
                        "two",
                        "three",
                    ]
        return events

    events = anyio.run(run)

    expected = ["one", "two"] if mutation == "pop" else ["one"]
    assert [
        event.content for event in events if isinstance(event, QueueMessageInjected)
    ] == expected
    assert harness.queued_messages.steering == ()
    assert harness.queued_messages.follow_up == ()
    queue_updates = [event for event in events if isinstance(event, QueueUpdated)]
    assert queue_updates[-1].steering == ()
    assert queue_updates[-1].follow_up == ()


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


def test_harness_closing_after_tool_execution_end_preserves_tool_output() -> None:
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
            ]
        ]
    )
    harness = _harness(
        provider,
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={}),),
    )

    async def run() -> None:
        events = harness.prompt("initial")
        async for event in events:
            if isinstance(event, ToolExecutionEnded):
                await events.aclose()
                return
        raise AssertionError("missing tool execution end")

    anyio.run(run)

    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "checking"),
        ("tool", "tool output"),
    ]


def test_harness_cancellation_drains_prepared_batch_results_in_source_order() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=calls,
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ]
        ]
    )

    async def run() -> tuple[AgentHarness, list[object]]:
        all_started = anyio.Event()
        harness = _harness(
            provider,
            executor=BlockingPreparedExecutor(all_started),
        )
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await all_started.wait()
                assert harness.cancel()
        return harness, events

    harness, events = anyio.run(run)

    terminals = [event for event in events if isinstance(event, ToolExecutionEnded)]
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in terminals] == ["call-1", "call-2"]
    assert [event.call_id for event in results] == ["call-1", "call-2"]
    assert all(event.is_error and event.retryable for event in results)
    assert all(event.process_state == "cancelled" for event in results)
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"
    assert [(message.role, message.tool_call_id) for message in harness.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
        ("tool", "call-2"),
    ]
    assert_turn_terminals(events)
    assert_settled_tool_calls(events, ("call-1", "call-2"))


def test_harness_cancellation_settles_batch_before_sibling_executor_error() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=calls,
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ]
        ]
    )

    async def run() -> list[object]:
        failed = anyio.Event()
        harness = _harness(
            provider,
            executor=FailingAndBlockingPreparedExecutor(failed),
        )
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("initial")])

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await failed.wait()
                assert harness.cancel()
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in results] == ["call-1", "call-2"]
    assert all(event.process_state == "cancelled" for event in results)
    assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
        "Agent run cancelled"
    ]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"


def test_harness_cancel_after_prepared_terminal_finishes_batch_then_cancels_turn() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=calls,
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ]
        ]
    )
    harness = _harness(provider, executor=ImmediatePreparedExecutor())

    async def run() -> list[object]:
        emitted: list[object] = []
        async for event in harness.prompt("initial"):
            emitted.append(event)
            if isinstance(event, ToolExecutionEnded) and event.call_id == "call-1":
                assert harness.cancel()
        return emitted

    events = anyio.run(run)

    assert [event.call_id for event in events if isinstance(event, ToolExecutionEnded)] == [
        "call-1",
        "call-2",
    ]
    assert [event.call_id for event in events if isinstance(event, ToolResultReady)] == [
        "call-1",
        "call-2",
    ]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"
    assert [(message.role, message.tool_call_id) for message in harness.messages] == [
        ("user", None),
        ("assistant", None),
        ("tool", "call-1"),
        ("tool", "call-2"),
    ]


def test_harness_cancel_after_tool_execution_end_preserves_tool_output() -> None:
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
            ]
        ]
    )
    harness = _harness(
        provider,
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={}),),
    )

    async def run() -> list[object]:
        emitted: list[object] = []
        async for event in harness.prompt("initial"):
            emitted.append(event)
            if isinstance(event, ToolExecutionEnded):
                assert harness.cancel()
        return emitted

    events = anyio.run(run)

    assert [event.type for event in events[-2:]] == ["error", "turn.completed"]
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "checking"),
        ("tool", "tool output"),
    ]
    tool_message = harness.messages[-1]
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.tool_name == "lookup"
    assert tool_message.is_error is False


def test_harness_does_not_retain_empty_failed_completion() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseFailed(message="provider failed"),
            ]
        ]
    )
    harness = _harness(provider)

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert any(event.type == "message.completed" and event.content == "" for event in events)
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


def test_harness_uses_one_primary_loop_for_tool_steering_and_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue boundaries continue the same loop rather than reconstructing offsets."""

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
                ProviderResponseStarted(model="test", response_id="steered-response"),
                ProviderResponseCompleted(content="steered", response_id="steered-response"),
            ],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="followed")],
        ]
    )
    harness = _harness(
        provider,
        tools=(ToolSpec(name="lookup", description="Look up", input_schema={"type": "object"}),),
    )
    harness.steer("change direction")
    harness.follow_up("finish this")
    real_run_agent_loop = agent_harness_module.run_agent_loop
    loop_calls = 0

    def recording_run_agent_loop(*args: object, **kwargs: object) -> object:
        nonlocal loop_calls
        loop_calls += 1
        return real_run_agent_loop(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_harness_module, "run_agent_loop", recording_run_agent_loop)

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert loop_calls == 1
    assert [event.content for event in events if isinstance(event, QueueMessageInjected)] == [
        "change direction",
        "finish this",
    ]
    assert provider.calls[1].messages[-1].content == "change direction"
    assert [message.content for message in provider.calls[2].extra_messages] == ["finish this"]


def test_harness_rejects_a_stale_rebase_without_mutating_its_transcript() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderResponseCompleted(content="answer", response_id="response-1"),
            ]
        ]
    )
    harness = _harness(provider)

    class StalePreparer:
        async def prepare_boundary(self, *, context: object) -> RequestBoundaryDecision | None:
            del context
            return RequestBoundaryDecision(
                context_rebase=RequestContextRebase(
                    base_messages=(Message(role="user", content="bad summary"),),
                    expected_continuation_messages=(),
                )
            )

    async def run() -> None:
        with pytest.raises(
            RequestBoundaryUnsupportedError,
            match="expected continuation does not match",
        ):
            async for _event in harness.prompt("initial", boundary_preparer=StalePreparer()):
                pass

    anyio.run(run)

    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial"),
        ("assistant", "answer"),
    ]


def test_harness_drains_steering_before_first_provider_request() -> None:
    harness = _harness(ScriptedProvider([]))
    harness.append_message(Message(role="user", content="initial"))
    harness.steer("preflight steering")

    events = harness.drain_steering()

    assert isinstance(events[0], QueueMessageInjected)
    assert events[0].content == "preflight steering"
    assert isinstance(events[-1], QueueUpdated)
    assert harness.messages[-1].content == "preflight steering"
    assert harness.queued_messages.steering == ()


def test_queue_message_injected_event_requires_schema_14_and_round_trips() -> None:
    event = QueueMessageInjected(kind="follow_up", content="continue")

    assert event.schema_version == 35
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
    replayed = provider.calls[1].messages[-3:]
    assert [message.role for message in replayed] == ["assistant", "tool", "user"]
    assert replayed[0].tool_calls is not None
    assert replayed[0].tool_calls[0].call_id == "call-1"
    assert replayed[1].content == "tool output"
    assert replayed[2].content == "change direction"


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


def test_harness_close_mid_all_follow_up_batch_preserves_unexposed_messages() -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="answer")]]
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=RecordingToolExecutor(),
            follow_up_mode="all",
        )
    )
    harness.follow_up("visible")
    harness.follow_up("still queued")

    async def run() -> QueueMessageInjected:
        events = harness.prompt("initial")
        async for event in events:
            if isinstance(event, QueueMessageInjected):
                await events.aclose()
                return event
        raise AssertionError("missing follow-up injection")

    injected = anyio.run(run)

    assert injected.kind == "follow_up"
    assert injected.content == "visible"
    assert [message.content for message in harness.queued_messages.follow_up] == ["still queued"]
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
