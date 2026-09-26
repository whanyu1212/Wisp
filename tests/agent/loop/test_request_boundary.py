from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence

import anyio
import pytest

from tests.agent.loop.support import (
    NeverToolExecutor,
    RaisingRequestBoundaryHook,
    RecordingRequestBoundaryHook,
    RecordingToolExecutor,
    completed_stream,
)
from tests.support.agent_runtime import (
    assert_turn_terminals,
)
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
    RequestContextRebase,
)
from wisp.events import (
    MessageCompleted,
    ToolCallSnapshot,
)
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


class OpaqueReplayScriptedProvider(ScriptedProvider):
    """Scripted provider that cannot fresh-replay opaque tool-turn state."""

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        return effort is None


def test_request_boundary_hook_not_configured_matches_default_behavior() -> None:
    """No hook configured must produce the exact same events as before hooks existed."""

    provider = ScriptedProvider([completed_stream("hi")])
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


def test_request_boundary_snapshot_mutation_does_not_leak_to_next_boundary() -> None:
    """A snapshot mutation at one boundary must not appear in a later one.

    If `continuation_messages` were shared (not deep-copied) with
    `state.continuation_messages`, mutating a snapshot's tool-call arguments
    at the first boundary would corrupt the loop's own history, and that
    corruption would still be visible in a *second* boundary's snapshot
    later in the same run.
    """

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(
                    tool_call=ToolCall(
                        call_id="call-1", name="noop", arguments={"path": "original"}
                    )
                ),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(
                        ToolCall(call_id="call-1", name="noop", arguments={"path": "original"}),
                    ),
                    response_id="tool-response",
                    finish_reason="tool_calls",
                ),
            ],
            completed_stream("no more tools"),
        ]
    )
    executor = RecordingToolExecutor()

    class MutateThenRecordHook:
        def __init__(self) -> None:
            self.calls = 0
            self.second_boundary_arguments: dict[str, object] | None = None

        async def before_next_request(
            self, *, snapshot: RequestBoundarySnapshot
        ) -> RequestBoundaryDecision:
            self.calls += 1
            tool_call_message = next(
                (message for message in snapshot.continuation_messages if message.tool_calls),
                None,
            )
            if self.calls == 1:
                assert tool_call_message is not None
                tool_call_message.tool_calls[0].arguments["path"] = "corrupted-by-hook"
                return RequestBoundaryDecision(stop=False)
            self.second_boundary_arguments = (
                dict(tool_call_message.tool_calls[0].arguments)
                if tool_call_message is not None
                else None
            )
            return RequestBoundaryDecision(stop=True)

    hook = MutateThenRecordHook()
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

    assert hook.calls == 2
    assert hook.second_boundary_arguments == {"path": "original"}


def test_request_boundary_hook_stop_wins_over_unused_message_edits() -> None:
    """`stop=True` is honored even when combined with `messages`/`extra_messages`.

    Regression for #363 review: the loop previously raised
    RequestBoundaryUnsupportedError before checking `decision.stop`, even
    though no provider request is made when stopping -- so the supplied
    history can't cause the corruption that validation exists to prevent.
    `stop` must always be honored regardless, per `RequestBoundaryDecision`'s
    documented contract.
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
    unused_replacement = (Message(role="user", content="compacted summary"),)
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(stop=True, messages=unused_replacement)]
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

    events = anyio.run(run)

    assert [event.type for event in events].count("turn.completed") == 1
    assert len(provider.calls) == 1


def test_request_boundary_hook_injects_steering_after_tool_round() -> None:
    """A capable provider can receive steering injected right after a tool round.

    `ScriptedProvider` declares `supports_continuation_messages = True`, so
    the loop delivers `extra_messages` through the provider's native
    `tool_results`/`previous_response_id` continuation rather than
    rejecting it -- the second request carries the tool round's own
    `tool_results` (untouched, not flattened) plus the injected steering
    message via `extra_messages`.
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
                    response_id="tool-response",
                    finish_reason="tool_calls",
                ),
            ],
            completed_stream("adjusted answer"),
        ]
    )
    executor = RecordingToolExecutor()
    injected = Message(role="user", content="steered")
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(extra_messages=(injected,))])
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

    assert [event.type for event in events].count("turn.completed") == 2
    assert len(provider.calls) == 2
    second_call = provider.calls[1]
    # tool_results is not flattened away by the injection -- both channels
    # are used together on the same request.
    assert second_call.tool_results == (
        ToolCallResult(call_id="call-1", output="tool output", is_error=False),
    )
    assert second_call.extra_messages == (injected,)
    assert second_call.previous_response_id is not None
    # messages stays the original, never-mutated base -- the whole point of
    # extra_messages is that it never needs a `messages` rebuild.
    assert second_call.messages == messages


def test_request_boundary_hook_rejects_tool_shaped_extra_message_with_no_history() -> None:
    """A hook's own `extra_messages` must not carry tool-shaped content either.

    Regression for #363 review: earlier validation only checked the loop's
    own accumulated `state.continuation_messages` for tool-shaped content.
    A hook can independently hand the loop a tool-shaped message even at
    the very first no-tool-calls boundary, before any loop-generated tool
    round has ever happened -- the same plain-message-converter flattening
    applies regardless of where the content came from, so this must be
    rejected too, not just content that originated from a real tool round.
    """

    provider = ScriptedProvider([completed_stream("first")])
    tool_shaped = Message(
        role="assistant",
        content="",
        tool_calls=(ToolCallSnapshot(call_id="fake-1", name="noop", arguments={}),),
    )
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(extra_messages=(tool_shaped,))])
    messages = (Message(role="user", content="hi"),)

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError):
        anyio.run(run)

    assert len(provider.calls) == 1


def test_request_boundary_hook_replaces_context_with_active_tool_exchange() -> None:
    """A full replacement may retain the active structured tool pair."""

    provider = ScriptedProvider([completed_stream("first"), completed_stream("second")])
    replacement = (
        Message(role="user", content="compacted summary"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCallSnapshot(call_id="call-1", name="noop", arguments={}),),
        ),
        Message(role="tool", content="tool output", tool_call_id="call-1", tool_name="noop"),
    )
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

    events = anyio.run(run)

    assert [event.type for event in events].count("turn.completed") == 2
    assert provider.calls[1].messages == replacement
    assert provider.calls[1].tool_results == ()
    assert provider.calls[1].previous_response_id is None


def test_request_boundary_hook_rejects_orphaned_tool_replacement() -> None:
    """A fresh replacement cannot inject a tool result without its call."""

    provider = ScriptedProvider([completed_stream("first")])
    hook = RecordingRequestBoundaryHook(
        [
            RequestBoundaryDecision(
                messages=(
                    Message(role="user", content="compacted summary"),
                    Message(role="tool", content="tool output", tool_call_id="call-1"),
                )
            )
        ]
    )

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError, match="unpaired structured tool exchange"):
        anyio.run(run)
    assert len(provider.calls) == 1


def test_request_boundary_hook_rejects_interleaved_or_mismatched_tool_replacement() -> None:
    """Structured replacements preserve the native assistant/result adjacency."""

    provider = ScriptedProvider([completed_stream("first")])
    hook = RecordingRequestBoundaryHook(
        [
            RequestBoundaryDecision(
                messages=(
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=(
                            ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),
                        ),
                    ),
                    Message(role="user", content="interleaved"),
                    Message(
                        role="tool",
                        content="tool output",
                        tool_call_id="call-1",
                        tool_name="other",
                    ),
                )
            )
        ]
    )

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError, match="unpaired structured tool exchange"):
        anyio.run(run)
    assert len(provider.calls) == 1


def test_request_boundary_hook_rejects_opaque_structured_tool_replacement() -> None:
    """Providers may guard configurations with unrepresentable native blocks."""

    provider = OpaqueReplayScriptedProvider([completed_stream("first")])
    hook = RecordingRequestBoundaryHook(
        [
            RequestBoundaryDecision(
                messages=(
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=(
                            ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),
                        ),
                    ),
                    Message(
                        role="tool",
                        content="tool output",
                        tool_call_id="call-1",
                        tool_name="lookup",
                    ),
                )
            )
        ]
    )

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
                effort="high",
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError, match="cannot fresh-replay"):
        anyio.run(run)
    assert len(provider.calls) == 1


def test_request_boundary_hook_rejects_mismatched_tool_name_in_replacement() -> None:
    """A tool result must retain the provider-visible name of its matching call."""

    provider = ScriptedProvider([completed_stream("first")])
    hook = RecordingRequestBoundaryHook(
        [
            RequestBoundaryDecision(
                messages=(
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=(
                            ToolCallSnapshot(call_id="call-1", name="lookup", arguments={}),
                        ),
                    ),
                    Message(
                        role="tool",
                        content="tool output",
                        tool_call_id="call-1",
                        tool_name="other",
                    ),
                )
            )
        ]
    )

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError, match="unpaired structured tool exchange"):
        anyio.run(run)
    assert len(provider.calls) == 1


def test_request_boundary_hook_fires_after_clean_turn_and_can_continue() -> None:
    """A hook can turn a would-be-final turn (no tool calls) into a follow-up.

    `ScriptedProvider` is `ContinuationMessageProvider`-capable, so the
    follow-up relies on the provider's own native continuation
    (`previous_response_id`) to carry the first turn's answer forward --
    `messages` never needs to be touched at all.
    """

    provider = ScriptedProvider([completed_stream("first"), completed_stream("second")])
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
    second_call = provider.calls[1]
    assert second_call.tool_results == ()
    assert second_call.extra_messages == ()
    assert second_call.messages == messages
    assert second_call.previous_response_id is not None


class _LegacyProviderWithoutContinuationMessages:
    """A minimal `Provider` implementation with no ContinuationMessageProvider support."""

    name = "legacy"
    default_model: str | None = "legacy"

    def __init__(self, streams: Iterable[Iterable[ProviderEvent | BaseException]]) -> None:
        self._streams = deque(tuple(stream) for stream in streams)
        self.calls: list[tuple[Message, ...]] = []

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
        self.calls.append(tuple(messages))
        for item in self._streams.popleft():
            await anyio.sleep(0)
            if isinstance(item, BaseException):
                raise item
            yield item


def test_request_boundary_hook_folds_clean_continuation_for_incapable_provider() -> None:
    """A provider without ContinuationMessageProvider support keeps today's fold fallback."""

    provider = _LegacyProviderWithoutContinuationMessages(
        [completed_stream("first"), completed_stream("second")]
    )
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

    anyio.run(run)

    assert len(provider.calls) == 2
    assert [(m.role, m.content) for m in provider.calls[1]] == [
        ("user", "hi"),
        ("assistant", "first"),
    ]


class _PromptCacheOnlyProvider:
    """Legacy optional provider proving prompt-cache and append capabilities differ."""

    name = "prompt-cache-only"
    default_model: str | None = "prompt-cache-only"
    supports_prompt_cache_key = True

    def __init__(self, streams: Iterable[Iterable[ProviderEvent]]) -> None:
        self._streams = deque(tuple(stream) for stream in streams)
        self.calls: list[tuple[tuple[Message, ...], str | None]] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append((tuple(messages), prompt_cache_key))
        for event in self._streams.popleft():
            yield event


def test_request_boundary_keeps_prompt_cache_capability_independent() -> None:
    """A prompt-cache-only provider never receives the new optional keyword."""

    provider = _PromptCacheOnlyProvider([completed_stream("first"), completed_stream("second")])
    injected = Message(role="user", content="follow up")
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(extra_messages=(injected,))])

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                prompt_cache_key="session-key",
                request_boundary_hook=hook,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            pass

    anyio.run(run)

    assert [prompt_cache_key for _messages, prompt_cache_key in provider.calls] == [
        "session-key",
        "session-key",
    ]
    assert [(message.role, message.content) for message in provider.calls[1][0]] == [
        ("user", "hi"),
        ("assistant", "first"),
        ("user", "follow up"),
    ]


def test_request_boundary_combines_prompt_cache_and_native_append() -> None:
    """A provider opting into both features receives each independently."""

    provider = ScriptedProvider([completed_stream("first"), completed_stream("second")])
    provider.supports_prompt_cache_key = True
    injected = Message(role="user", content="follow up")
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(extra_messages=(injected,))])

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                prompt_cache_key="session-key",
                request_boundary_hook=hook,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            pass

    anyio.run(run)

    assert [call.prompt_cache_key for call in provider.calls] == ["session-key", "session-key"]
    assert provider.calls[1].extra_messages == (injected,)
    assert [(message.role, message.content) for message in provider.calls[1].messages] == [
        ("user", "hi"),
    ]


def test_request_boundary_replacement_folds_extras_and_discards_old_state() -> None:
    """Replacement plus extras is fresh and later snapshots cannot see old context."""

    provider = ScriptedProvider([completed_stream("first"), completed_stream("second")])
    replacement = (Message(role="user", content="compacted summary"),)
    injected = Message(role="user", content="steered")
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(messages=replacement, extra_messages=(injected,))]
    )

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=(Message(role="user", content="original"),),
        ):
            pass

    anyio.run(run)

    assert provider.calls[1].messages == (*replacement, injected)
    assert provider.calls[1].extra_messages == ()
    assert provider.calls[1].previous_response_id is None
    assert [
        (message.role, message.content) for message in hook.snapshots[1].continuation_messages
    ] == [
        ("assistant", "second"),
    ]


def test_request_boundary_folds_idless_clean_response_before_appending() -> None:
    """The loop does not invent a public response ID for a clean response."""

    provider = ScriptedProvider(
        [completed_stream("first", response_id=None), completed_stream("second")]
    )
    injected = Message(role="user", content="follow up")
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(extra_messages=(injected,))])

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

    completions = [event for event in events if isinstance(event, MessageCompleted)]
    assert completions[0].response_id is None
    assert provider.calls[1].previous_response_id is None
    assert provider.calls[1].extra_messages == ()
    assert [(message.role, message.content) for message in provider.calls[1].messages] == [
        ("user", "hi"),
        ("assistant", "first"),
        ("user", "follow up"),
    ]


@pytest.mark.parametrize("had_tool_calls", [False, True])
def test_request_boundary_hook_failure_does_not_double_complete_the_turn(
    had_tool_calls: bool,
) -> None:
    """A hook failure after a completed turn must not emit a second TurnCompleted.

    Regression for #363 review: the turn's one terminal TurnCompleted(outcome=
    "completed") is yielded before the hook is invoked. If the hook then
    raises, the outer exception handler previously still saw `turn_started`
    as true and emitted a second, contradictory TurnCompleted(outcome=
    "failed") for the same turn.
    """

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
        else completed_stream("first")
    )
    provider = ScriptedProvider([stream])
    hook = RaisingRequestBoundaryHook()
    messages = (Message(role="user", content="hi"),)
    collected: list[object] = []

    async def run() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=RecordingToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            collected.append(event)

    with pytest.raises(RuntimeError, match="boundary hook failed"):
        anyio.run(run)

    assert [event.type for event in collected].count("turn.completed") == 1
    assert [event.type for event in collected].count("error") == 1
    assert_turn_terminals(collected)


def test_request_boundary_hook_stops_clean_turn_after_earlier_tool_round() -> None:
    """A clean turn that follows an earlier tool round can only stop, not continue.

    Regression for #363 review: no provider-native mechanism carries a tool
    round forward past a *later* no-tool-calls boundary -- the
    `previous_response_id`-anchored replay tail that would carry it is only
    loaded when `tool_results` is non-empty (confirmed for
    anthropic.py/google.py/openai_compatible.py's `_create_stream`), and this
    boundary always sends an empty `tool_results`. A plain "just continue"
    decision here would silently sample from only the original base
    `messages`, missing the tool round and the turn that followed it. Only
    `stop=True` is supported; the loop must still clear
    `pending_tool_results` so nothing stale would leak if this boundary
    were ever reached with a decision that could continue.
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
            completed_stream("no more tools"),
        ]
    )
    executor = RecordingToolExecutor()
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(stop=False), RequestBoundaryDecision(stop=True)]
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

    events = anyio.run(run)

    assert len(provider.calls) == 2
    assert [event.type for event in events].count("turn.completed") == 2
    # Call 1 legitimately carries call-1's result.
    assert provider.calls[1].tool_results == (
        ToolCallResult(call_id="call-1", output="tool output", is_error=False),
    )


def test_request_boundary_hook_continues_plain_after_earlier_tool_round() -> None:
    """A capable provider can continue past a clean turn that followed a tool round.

    The provider's own `previous_response_id` continuation already carries
    the tool round forward (the whole reason the loop's earlier blanket
    rejection existed was because *incapable* providers can't do this
    safely -- `ScriptedProvider` here declares
    `supports_continuation_messages`, so a plain "just continue" decision
    is honored, not rejected).
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
            completed_stream("no more tools"),
            completed_stream("final"),
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

    events = anyio.run(run)

    assert [event.type for event in events].count("turn.completed") == 3
    assert len(provider.calls) == 3
    # Call 2 (the clean-turn boundary continuing plainly) must not still be
    # sending call-1's already-consumed tool_results.
    assert provider.calls[2].tool_results == ()
    assert provider.calls[2].extra_messages == ()
    assert provider.calls[2].previous_response_id is not None


def test_request_boundary_hook_injects_after_earlier_tool_round() -> None:
    """A capable provider can also receive injected content at that later boundary.

    `messages`/`extra_messages` were unsupported here before a real
    delivery mechanism existed; now that `extra_messages` reaches the
    provider's native continuation without flattening structure, this
    boundary supports the same injection the tool-round boundary does.
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
            completed_stream("no more tools"),
            completed_stream("final"),
        ]
    )
    executor = RecordingToolExecutor()
    injected = Message(role="user", content="steered")
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(stop=False), RequestBoundaryDecision(extra_messages=(injected,))]
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

    events = anyio.run(run)

    assert [event.type for event in events].count("turn.completed") == 3
    assert len(provider.calls) == 3
    assert provider.calls[2].tool_results == ()
    assert provider.calls[2].extra_messages == (injected,)
    assert provider.calls[2].messages == messages


def test_request_boundary_hook_rejects_plain_continuation_for_incapable_provider() -> None:
    """An incapable provider still cannot continue past a boundary with tool history.

    Preserves the original, narrower contract for a provider that doesn't
    declare `ContinuationMessageProvider`: no provider-native mechanism
    exists there to carry a tool round forward without either resending
    stale `tool_results` or flattening structured history.
    """

    provider = _LegacyProviderWithoutContinuationMessages(
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
            completed_stream("no more tools"),
        ]
    )
    executor = RecordingToolExecutor()
    hook = RecordingRequestBoundaryHook(
        [RequestBoundaryDecision(stop=False), RequestBoundaryDecision(stop=False)]
    )
    messages = (Message(role="user", content="hi"),)

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=executor,
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError):
        anyio.run(run)

    assert len(provider.calls) == 2


def test_request_boundary_hook_rejects_injection_for_incapable_provider() -> None:
    """An incapable provider still cannot receive injected content immediately

    after a tool round, either -- there is no delivery channel for it at
    all on that provider.
    """

    provider = _LegacyProviderWithoutContinuationMessages(
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

    async def run() -> None:
        async for _event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=executor,
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            pass

    with pytest.raises(RequestBoundaryUnsupportedError):
        anyio.run(run)

    assert len(provider.calls) == 1


def test_request_boundary_hook_can_replace_base_messages() -> None:
    """A hook can replace the loop's base history (e.g. after compaction)."""

    provider = ScriptedProvider([completed_stream("first"), completed_stream("second")])
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


def test_request_boundary_context_rebase_keeps_live_native_continuation() -> None:
    """A rebase changes only the portable base beneath an active tool cursor."""

    tool_call = ToolCall(call_id="call-1", name="noop", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="tool-response"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(tool_call,),
                    response_id="tool-response",
                    finish_reason="tool_calls",
                ),
            ],
            completed_stream("done", response_id="final-response"),
        ]
    )
    executor = RecordingToolExecutor()

    class RebaseHook:
        async def before_next_request(
            self, *, snapshot: RequestBoundarySnapshot
        ) -> RequestBoundaryDecision:
            if snapshot.had_tool_calls:
                return RequestBoundaryDecision(
                    context_rebase=RequestContextRebase(
                        base_messages=(Message(role="user", content="compacted summary"),),
                        expected_continuation_messages=snapshot.continuation_messages,
                    )
                )
            return RequestBoundaryDecision(stop=True)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    request_boundary_hook=RebaseHook(),
                ),
                messages=(Message(role="user", content="inspect"),),
            )
        ]

    anyio.run(run)

    assert len(provider.calls) == 2
    second = provider.calls[1]
    assert [(message.role, message.content) for message in second.messages] == [
        ("user", "compacted summary")
    ]
    assert second.previous_response_id == "tool-response"
    assert second.tool_results == (ToolCallResult(call_id="call-1", output="tool output"),)
    assert second.extra_messages == ()
    assert len(executor.calls) == 1


def test_clean_boundary_rebase_drops_consumed_tool_results() -> None:
    """A clean response must not resend the prior round's tool results."""

    tool_call = ToolCall(call_id="call-1", name="noop", arguments={})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="tool-response"),
                ProviderToolCallCompleted(tool_call=tool_call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(tool_call,),
                    response_id="tool-response",
                    finish_reason="tool_calls",
                ),
            ],
            completed_stream("clean", response_id="clean-response"),
            completed_stream("followed up", response_id="follow-up-response"),
        ]
    )

    class CleanRebaseHook:
        async def before_next_request(
            self, *, snapshot: RequestBoundarySnapshot
        ) -> RequestBoundaryDecision:
            if snapshot.turn == 1:
                return RequestBoundaryDecision()
            if snapshot.turn == 2:
                return RequestBoundaryDecision(
                    context_rebase=RequestContextRebase(
                        base_messages=(Message(role="user", content="compacted summary"),),
                        expected_continuation_messages=snapshot.continuation_messages,
                    ),
                    extra_messages=(Message(role="user", content="follow up"),),
                )
            return RequestBoundaryDecision(stop=True)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=RecordingToolExecutor(),
                    request_boundary_hook=CleanRebaseHook(),
                ),
                messages=(Message(role="user", content="inspect"),),
            )
        ]

    anyio.run(run)

    assert provider.calls[1].tool_results == (
        ToolCallResult(call_id="call-1", output="tool output"),
    )
    assert provider.calls[2].previous_response_id == "clean-response"
    assert provider.calls[2].tool_results == ()
    assert [message.content for message in provider.calls[2].extra_messages] == ["follow up"]


def test_request_boundary_context_rebase_rejects_stale_continuation() -> None:
    """A stale compaction plan must not mutate the loop's continuation state."""

    provider = ScriptedProvider([completed_stream("first")])

    class StaleRebaseHook:
        async def before_next_request(
            self, *, snapshot: RequestBoundarySnapshot
        ) -> RequestBoundaryDecision:
            return RequestBoundaryDecision(
                context_rebase=RequestContextRebase(
                    base_messages=(Message(role="user", content="summary"),),
                    expected_continuation_messages=(),
                )
            )

    async def run() -> None:
        with pytest.raises(
            RequestBoundaryUnsupportedError,
            match="expected continuation does not match",
        ):
            async for _event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    request_boundary_hook=StaleRebaseHook(),
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                pass

    anyio.run(run)
    assert len(provider.calls) == 1
