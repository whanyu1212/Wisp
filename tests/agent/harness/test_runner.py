from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Sequence

import anyio
import pytest

import wisp.agent.harness.runner as agent_harness_module
from tests.agent.harness.support import (
    RecordingToolExecutor,
    append_nested_path,
    build_harness,
    message_with_nested_arguments,
)
from tests.support.agent_runtime import (
    assert_turn_terminals,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, AgentLoopEvent
from wisp.agent.messages import Message
from wisp.events import (
    MessageCompleted,
    QueueMessageInjected,
    QueueUpdated,
    ToolCallSnapshot,
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import (
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


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
    harness = build_harness(
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


@pytest.mark.parametrize("entry_point", ["constructor", "append", "replace"])
def test_harness_transcript_inputs_and_snapshots_are_detached(entry_point: str) -> None:
    original = message_with_nested_arguments(role="assistant")
    expected = original.model_copy(deep=True)
    if entry_point == "constructor":
        harness = build_harness(ScriptedProvider([]), messages=(original,))
    else:
        harness = build_harness(ScriptedProvider([]))
        if entry_point == "append":
            harness.append_message(original)
        else:
            harness.replace_messages((original,))
    append_nested_path(original)
    assert harness.messages == (expected,)
    snapshot = harness.messages
    append_nested_path(snapshot[0])
    assert harness.messages == (expected,)
    assert harness.messages[0].tool_calls is not None
    assert harness.messages[0].tool_calls[0].provider_call_id == "native-call"


def test_harness_prompt_message_snapshots_input_without_starting_the_run() -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
    )
    harness = build_harness(provider)
    original = message_with_nested_arguments(role="user")
    expected = original.model_copy(deep=True)
    events = harness.prompt_message(original)
    append_nested_path(original)
    assert harness.messages == ()
    assert not harness.is_running

    async def run() -> None:
        async for _ in events:
            pass

    anyio.run(run)
    assert provider.calls[0].messages == (expected,)
    assert harness.messages[0] == expected
    append_nested_path(provider.calls[0].messages[0])
    assert harness.messages[0] == expected


@pytest.mark.parametrize("close_at_completion", [False, True])
def test_harness_retains_detached_completion_before_exposing_it(close_at_completion: bool) -> None:
    call = ToolCall(call_id="call", name="read", arguments={"paths": ["original.txt"]})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="", tool_calls=(call,), finish_reason="tool_calls"
                ),
            ],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")],
        ]
    )
    harness = build_harness(
        provider,
        tools=(ToolSpec(name="read", description="Read", input_schema={"type": "object"}),),
    )

    async def run() -> None:
        events = harness.prompt("read")
        async for event in events:
            if isinstance(event, MessageCompleted) and event.tool_calls:
                expected = harness.messages[-1]
                paths = event.tool_calls[0].arguments["paths"]
                assert isinstance(paths, list)
                paths.append("changed.txt")
                assert harness.messages[-1] == expected
                if close_at_completion:
                    await events.aclose()
                    break
        retained = next(message for message in harness.messages if message.tool_calls)
        assert retained.tool_calls is not None
        assert retained.tool_calls[0].arguments == {"paths": ["original.txt"]}
        assert not harness.is_running

    anyio.run(run)


def test_harness_requeued_message_is_not_part_of_the_old_drain_snapshot() -> None:
    provider = ScriptedProvider(
        [
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content=str(index))]
            for index in range(3)
        ]
    )
    harness = build_harness(provider)
    harness.set_steering_mode("all")
    harness.steer("first")
    harness.steer("second")

    async def run() -> None:
        injected: list[str] = []
        boundaries: list[tuple[str, ...]] = []
        async for event in harness.prompt("start"):
            if isinstance(event, QueueMessageInjected):
                injected.append(event.content)
                if event.content == "first":
                    removed = harness.pop_latest_steering()
                    assert removed is not None
                    harness.steer_message(removed)
            elif isinstance(event, QueueUpdated):
                boundaries.append(event.steering)
        assert injected == ["first", "second"]
        assert boundaries == [("second",), ()]

    anyio.run(run)


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
    harness = build_harness(provider, messages=(initial,))
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
    harness = build_harness(provider, messages=(existing,))

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
    harness = build_harness(
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
    harness = build_harness(
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
    harness = build_harness(
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


@pytest.mark.parametrize("field", ["turn_offset", "tool_iteration_offset"])
@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_harness_invalid_offsets_leave_transcript_unchanged_and_allow_retry(
    field: str, value: object
) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
    )
    interrupted = Message(
        role="assistant",
        content="",
        tool_calls=(ToolCallSnapshot(call_id="pending", name="read", arguments={}),),
    )
    harness = build_harness(provider, messages=(interrupted,))
    harness.steer("queued")
    before = harness.messages
    queued = harness.queued_messages

    async def run() -> None:
        with pytest.raises(ValueError, match=field):
            await anext(harness.prompt("invalid", **{field: value}))  # type: ignore[arg-type]
        assert harness.messages == before
        assert harness.queued_messages == queued
        assert not harness.is_running
        assert not harness.cancel()
        assert provider.calls == []
        harness.clear_queues()
        events = [event async for event in harness.prompt("retry")]
        assert isinstance(events[-1], TurnCompleted)
        assert events[-1].outcome == "completed"
        assert not harness.is_running

    anyio.run(run)


def test_harness_history_preparation_failure_releases_run_state() -> None:
    class FailingReplayProvider(ScriptedProvider):
        fail_preparation = True

        def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
            if self.fail_preparation:
                raise RuntimeError("history preparation failed")
            return True

    provider = FailingReplayProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
    )
    harness = build_harness(provider)
    harness.follow_up("queued")
    queued = harness.queued_messages

    async def run() -> None:
        with pytest.raises(RuntimeError, match="history preparation failed"):
            await anext(harness.prompt("accepted"))
        assert not harness.is_running
        assert not harness.cancel()
        assert [message.content for message in harness.messages] == ["accepted"]
        assert harness.queued_messages == queued
        provider.fail_preparation = False
        harness.clear_queues()
        events = [event async for event in harness.continue_()]
        assert isinstance(events[-1], TurnCompleted)
        assert events[-1].outcome == "completed"
        assert not harness.is_running

    anyio.run(run)


@pytest.mark.parametrize("exit_mode", ["close", "error"])
def test_harness_cleanup_failure_releases_run_state(
    monkeypatch: pytest.MonkeyPatch, exit_mode: str
) -> None:
    async def failing_loop(
        config: AgentLoopConfig, *, messages: Sequence[Message]
    ) -> AsyncGenerator[AgentLoopEvent, None]:
        try:
            yield TurnStarted(turn=1)
            raise ValueError("execution failed")
        finally:
            raise RuntimeError("cleanup failed")

    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")]]
    )
    harness = build_harness(provider)

    async def run() -> None:
        with monkeypatch.context() as patch:
            patch.setattr(agent_harness_module, "run_agent_loop", failing_loop)
            events = harness.prompt("first")
            assert isinstance(await anext(events), TurnStarted)
            with pytest.raises(RuntimeError, match="cleanup failed") as raised:
                if exit_mode == "close":
                    await events.aclose()
                else:
                    await anext(events)
            if exit_mode == "error":
                assert isinstance(raised.value.__context__, ValueError)
            assert not harness.is_running
            assert not harness.cancel()
        retry = [event async for event in harness.prompt("retry")]
        assert isinstance(retry[-1], TurnCompleted)
        assert retry[-1].outcome == "completed"

    anyio.run(run)


def test_harness_rejects_overlapping_runs_and_resets_when_stream_closes() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ]
        ]
    )
    harness = build_harness(provider)

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


def test_harness_does_not_retain_empty_failed_completion() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseFailed(message="provider failed"),
            ]
        ]
    )
    harness = build_harness(provider)

    async def run() -> list[object]:
        return [event async for event in harness.prompt("initial")]

    events = anyio.run(run)

    assert any(event.type == "message.completed" and event.content == "" for event in events)
    assert [(message.role, message.content) for message in harness.messages] == [
        ("user", "initial")
    ]


@pytest.mark.parametrize("entry_point", ["prompt", "prompt_message", "continue_"])
@pytest.mark.parametrize("tool_iteration_offset", [1, 2])
def test_harness_invocation_offsets_preserve_turn_numbers_and_tool_limit(
    entry_point: str, tool_iteration_offset: int
) -> None:
    call = ToolCall(call_id="call-1", name="lookup", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking", tool_calls=(call,), finish_reason="tool_calls"
                ),
            ],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")],
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
            max_tool_iterations=2,
        ),
        messages=(Message(role="user", content="existing task"),),
    )

    async def run() -> list[object]:
        offsets = {"turn_offset": 7, "tool_iteration_offset": tool_iteration_offset}
        if entry_point == "prompt":
            stream = harness.prompt("next task", **offsets)
        elif entry_point == "prompt_message":
            stream = harness.prompt_message(Message(role="user", content="next task"), **offsets)
        else:
            stream = harness.continue_(**offsets)
        events: list[object] = []
        if tool_iteration_offset == 2:
            with pytest.raises(RuntimeError, match="Maximum tool iterations exceeded: 2"):
                async for event in stream:
                    events.append(event)
        else:
            events.extend([event async for event in stream])
        return events

    events = anyio.run(run)
    exhausted = tool_iteration_offset == 2
    assert executor.calls == ([] if exhausted else [call])
    assert [event.turn for event in events if isinstance(event, TurnStarted)] == (
        [8] if exhausted else [8, 9]
    )
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.outcome == ("failed" if exhausted else "completed")
    assert not harness.is_running
    assert not harness.cancel()
    assert_turn_terminals(events)


def test_harness_rejects_non_user_prompt_messages() -> None:
    harness = build_harness(ScriptedProvider([]))

    with pytest.raises(ValueError, match="require a user message"):
        harness.prompt_message(Message(role="assistant", content="not a prompt"))
