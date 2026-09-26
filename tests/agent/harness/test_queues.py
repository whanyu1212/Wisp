from __future__ import annotations

from typing import cast

import anyio
import pytest

import wisp.agent.harness.runner as agent_harness_module
from tests.agent.harness.support import (
    RecordingToolExecutor,
    append_nested_path,
    build_harness,
    message_with_nested_arguments,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig, QueuedMessages, QueueKind
from wisp.agent.messages import Message
from wisp.events import (
    QueueMessageInjected,
    QueueMode,
    QueueUpdated,
    ToolExecutionEnded,
    TurnCompleted,
    wisp_event_from_json,
)
from wisp.providers.base import (
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


@pytest.mark.parametrize("kind", ["steering", "follow_up"])
def test_queue_inputs_and_snapshots_are_detached(kind: QueueKind) -> None:
    original = message_with_nested_arguments(role="user")
    expected = original.model_copy(deep=True)
    size = len(expected.model_dump_json().encode("utf-8"))
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=ScriptedProvider([]),
            tool_executor=RecordingToolExecutor(),
            max_pending_queue_bytes=size,
        )
    )
    enqueue = harness.steer_message if kind == "steering" else harness.follow_up_message
    enqueue(original)
    append_nested_path(original)
    snapshot = harness.queued_messages
    messages = snapshot.steering if kind == "steering" else snapshot.follow_up
    assert messages == (expected,)
    append_nested_path(messages[0])
    assert harness.pending_message_bytes == size
    assert harness.pending_message_count == 1
    with pytest.raises(RuntimeError, match="byte limit exceeded"):
        enqueue(expected)
    assert harness.clear_queue(kind) == (expected,)
    assert harness.pending_message_count == 0
    enqueue(expected)
    assert harness.pending_message_bytes == size


def test_queue_accounting_builds_no_message_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_harness(ScriptedProvider([]))
    harness.steer("pending")
    size = harness.pending_message_bytes

    def unexpected_snapshot(self: AgentHarness) -> QueuedMessages:
        raise AssertionError("queue accounting must not copy messages")

    monkeypatch.setattr(AgentHarness, "queued_messages", property(unexpected_snapshot))
    assert harness.pending_message_count == 1
    assert harness.pending_message_bytes == size
    assert harness.has_queued_messages()


def test_queue_keeps_fifo_snapshots_and_transcript_boundary() -> None:
    harness = build_harness(ScriptedProvider([]))

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


def test_queue_rejects_non_user_messages_without_partial_change() -> None:
    harness = build_harness(ScriptedProvider([]))
    assistant = Message(role="assistant", content="not user input")

    with pytest.raises(ValueError, match="queues require a user message"):
        harness.steer_message(assistant)
    with pytest.raises(ValueError, match="queues require a user message"):
        harness.follow_up_message(assistant)
    with pytest.raises(ValueError, match="Unsupported queue kind"):
        harness.clear_queue(cast(QueueKind, "unknown"))

    assert harness.pending_message_count == 0
    assert harness.messages == ()


def test_queue_capacity_is_shared_and_frees_on_removal() -> None:
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


def test_queue_modes_are_independent_and_reported() -> None:
    harness = build_harness(ScriptedProvider([]))

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


def test_queue_byte_limit_rejects_before_mutation() -> None:
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


def test_queue_updated_event_round_trips() -> None:
    event = QueueUpdated(
        steering=("adjust",),
        follow_up=("summarize",),
        steering_mode="all",
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_drains_follow_ups_one_per_completed_turn() -> None:
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
    harness = build_harness(provider)
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


def test_all_mode_drains_follow_ups_as_one_batch() -> None:
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
def test_all_mode_tolerates_queue_edits_during_drain(
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


def test_close_before_completion_keeps_follow_up_queue() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="answer"),
            ]
        ]
    )
    harness = build_harness(provider)
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


def test_failure_keeps_follow_up_queue_uninjected() -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), RuntimeError("provider failed")]]
    )
    harness = build_harness(provider)
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


def test_follow_up_keeps_tool_iteration_limit_across_segments() -> None:
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


def test_one_loop_serves_tools_steering_and_follow_ups(
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
    harness = build_harness(
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


def test_drains_steering_before_first_provider_request() -> None:
    harness = build_harness(ScriptedProvider([]))
    harness.append_message(Message(role="user", content="initial"))
    harness.steer("preflight steering")

    events = harness.drain_steering()

    assert isinstance(events[0], QueueMessageInjected)
    assert events[0].content == "preflight steering"
    assert isinstance(events[-1], QueueUpdated)
    assert harness.messages[-1].content == "preflight steering"
    assert harness.queued_messages.steering == ()


def test_queue_message_injected_event_round_trips() -> None:
    event = QueueMessageInjected(kind="follow_up", content="continue")

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_injects_steering_after_complete_tool_batch() -> None:
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
    harness = build_harness(
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


def test_all_mode_injects_steering_before_follow_up() -> None:
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


def test_drains_steering_one_per_turn_boundary() -> None:
    provider = ScriptedProvider(
        [
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="first")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="second")],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="third")],
        ]
    )
    harness = build_harness(provider)
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


def test_cancel_at_turn_boundary_keeps_steering() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="answer"),
            ]
        ]
    )
    harness = build_harness(provider)
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


def test_close_mid_batch_keeps_unexposed_steering() -> None:
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


def test_close_mid_follow_up_batch_keeps_unexposed_messages() -> None:
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


def test_injects_steering_after_denied_tool_result() -> None:
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
    harness = build_harness(
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
