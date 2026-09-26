from __future__ import annotations

import anyio
import pytest

import wisp.agent.harness.boundaries as agent_boundary_module
from tests.agent.harness.support import (
    append_nested_path,
    build_harness,
    message_with_nested_arguments,
)
from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
    RequestContextRebase,
)
from wisp.events import (
    TurnCompleted,
    TurnStarted,
)
from wisp.providers.base import (
    ContextOverflowError,
    ToolSpec,
    prepare_provider_history,
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


def test_boundary_coordinator_rejects_unarmed_and_mismatched_boundaries() -> None:
    harness = build_harness(ScriptedProvider([]))
    coordinator = agent_boundary_module._HarnessBoundaryCoordinator(
        get_messages=lambda: harness.messages,
        provider=harness.config.provider,
        effort=harness.config.effort,
        active_from=0,
        boundary_preparer=None,
        context_overflow_hook=None,
    )
    snapshot = RequestBoundarySnapshot(
        turn=1,
        tool_iterations=0,
        had_tool_calls=False,
        can_append_user_messages=False,
        continuation_messages=(),
    )

    async def run() -> None:
        with pytest.raises(RuntimeError, match="unarmed request boundary"):
            await coordinator.before_next_request(snapshot=snapshot)
        coordinator.arm(
            turn=2,
            had_tool_calls=False,
            injected_messages=(),
            stop_by_default=True,
        )
        with pytest.raises(RuntimeError, match="did not match its completed turn"):
            await coordinator.before_next_request(snapshot=snapshot)

    anyio.run(run)


def test_boundary_coordinator_returns_replacement_without_mutating_transcript() -> None:
    original = Message(role="user", content="old")
    replacement = Message(role="user", content="compressed")
    extra = Message(role="user", content="steered")
    harness = build_harness(ScriptedProvider([]), messages=(original,))

    class Preparer:
        async def prepare_boundary(
            self, *, context: agent_boundary_module.HarnessBoundaryContext
        ) -> RequestBoundaryDecision:
            assert context.active_from == 1
            return RequestBoundaryDecision(
                messages=(replacement,),
                extra_messages=(extra,),
            )

    coordinator = agent_boundary_module._HarnessBoundaryCoordinator(
        get_messages=lambda: harness.messages,
        provider=harness.config.provider,
        effort=harness.config.effort,
        active_from=1,
        boundary_preparer=Preparer(),
        context_overflow_hook=None,
    )
    coordinator.arm(
        turn=1,
        had_tool_calls=False,
        injected_messages=(extra,),
        stop_by_default=False,
    )
    snapshot = RequestBoundarySnapshot(
        turn=1,
        tool_iterations=0,
        had_tool_calls=False,
        can_append_user_messages=False,
        continuation_messages=(Message(role="assistant", content="answer"),),
    )

    async def run() -> None:
        decision = await coordinator.before_next_request(snapshot=snapshot)
        assert decision.messages == (replacement,)

    anyio.run(run)
    assert harness.messages == (original,)

    assert coordinator.take_transcript_replacement() == (replacement, extra)
    assert coordinator.take_transcript_replacement() is None
    assert harness.messages == (original,)
    assert coordinator.active_from == 1
    assert coordinator.pending_transcript_replacement is None


def test_boundary_coordinator_fallback_replacement_needs_no_pending_transition() -> None:
    user = Message(role="user", content="initial")
    injected = Message(role="user", content="steered")
    harness = build_harness(ScriptedProvider([]), messages=(user, injected))
    coordinator = agent_boundary_module._HarnessBoundaryCoordinator(
        get_messages=lambda: harness.messages,
        provider=harness.config.provider,
        effort=harness.config.effort,
        active_from=1,
        boundary_preparer=None,
        context_overflow_hook=None,
    )
    coordinator.arm(
        turn=1,
        had_tool_calls=True,
        injected_messages=(injected,),
        stop_by_default=False,
    )
    snapshot = RequestBoundarySnapshot(
        turn=1,
        tool_iterations=1,
        had_tool_calls=True,
        can_append_user_messages=False,
        continuation_messages=(),
    )

    async def run() -> RequestBoundaryDecision:
        return await coordinator.before_next_request(snapshot=snapshot)

    decision = anyio.run(run)

    assert decision.messages is not None
    assert coordinator.pending_transcript_replacement is None
    assert coordinator.take_transcript_replacement() is None
    assert harness.messages == (user, injected)


@pytest.mark.parametrize("raised", [False, True])
@pytest.mark.parametrize("empty_replacement", [False, True])
def test_harness_applies_overflow_replacement_only_when_retry_starts(
    raised: bool, empty_replacement: bool
) -> None:
    original = Message(role="user", content="long prompt")
    replacement = () if empty_replacement else (Message(role="user", content="summary"),)
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="rejected"),
                ProviderTextDelta(delta="partial answer"),
                ContextOverflowError("context window exceeded")
                if raised
                else ProviderResponseFailed(
                    message="context window exceeded", failure_kind="context_overflow"
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="recovered"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )
    harness = build_harness(provider, messages=(original,))

    class RecoverOverflow:
        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision:
            assert snapshot.had_streamed_delta
            assert harness.messages[0] == original
            assert harness.messages[-1].content == "partial answer"
            return RequestBoundaryDecision(messages=replacement)

    async def run() -> None:
        started_turns = []
        async for event in harness.continue_(context_overflow_hook=RecoverOverflow()):
            if isinstance(event, TurnCompleted) and event.turn == 1:
                assert harness.messages[0] == original
                assert harness.messages[-1].content == "partial answer"
            if isinstance(event, TurnStarted):
                started_turns.append(event.turn)
                if event.turn == 2:
                    assert harness.messages == replacement
        assert started_turns == [1, 2]

    anyio.run(run)

    assert provider.calls[1].messages == replacement
    assert harness.messages[:-1] == replacement
    assert harness.messages[-1].content == "done"


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
            self, *, context: agent_boundary_module.HarnessBoundaryContext
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
    harness = build_harness(
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


@pytest.mark.parametrize("transition", ["replacement", "rebase", "overflow"])
def test_harness_boundary_adoption_detaches_callback_and_provider_messages(transition: str) -> None:
    class NativeProvider(ScriptedProvider):
        def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
            return True

    provider = NativeProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id=f"response-{index}"),
                ProviderResponseFailed(message="too large", failure_kind="context_overflow")
                if transition == "overflow" and index == 0
                else ProviderResponseCompleted(content=str(index), response_id=f"response-{index}"),
            ]
            for index in range(2)
        ]
    )
    original = message_with_nested_arguments(role="assistant")
    base = [
        original,
        Message(role="tool", content="read output", tool_call_id="nested", tool_name="read"),
        Message(role="user", content="continue"),
    ]
    expected_base = tuple(message.model_copy(deep=True) for message in base)
    expected_transcript: tuple[Message, ...] = ()
    harness = build_harness(provider)

    class Preparer:
        async def prepare_boundary(
            self, *, context: agent_boundary_module.HarnessBoundaryContext
        ) -> RequestBoundaryDecision | None:
            nonlocal expected_transcript
            if context.snapshot.turn != 1:
                return None
            if transition == "replacement":
                expected_transcript = expected_base
                return RequestBoundaryDecision(messages=base)
            expected_transcript = (*expected_base, *context.snapshot.continuation_messages)
            return RequestBoundaryDecision(
                context_rebase=RequestContextRebase(
                    base_messages=base,
                    expected_continuation_messages=context.snapshot.continuation_messages,
                )
            )

    class RecoverOverflow:
        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision:
            nonlocal expected_transcript
            expected_transcript = expected_base
            return RequestBoundaryDecision(messages=base)

    async def run() -> None:
        async for event in harness.prompt(
            "initial", boundary_preparer=Preparer(), context_overflow_hook=RecoverOverflow()
        ):
            if isinstance(event, TurnStarted) and event.turn == 2:
                assert harness.messages == expected_transcript
                append_nested_path(original)
                base.clear()
                assert harness.messages == expected_transcript
        assert len(provider.calls) == 2
        assert provider.calls[1].messages[: len(expected_base)] == expected_base
        append_nested_path(provider.calls[1].messages[0])
        assert harness.messages[: len(expected_base)] == expected_base

    anyio.run(run)


def test_boundary_callback_cannot_mutate_transcript_injections_or_fallback() -> None:
    original = message_with_nested_arguments(role="user")
    expected = original.model_copy(deep=True)
    harness = build_harness(ScriptedProvider([]), messages=(original,))
    continuation = message_with_nested_arguments(role="assistant")
    expected_continuation = continuation.model_copy(deep=True)

    class Preparer:
        async def prepare_boundary(
            self, *, context: agent_boundary_module.HarnessBoundaryContext
        ) -> None:
            append_nested_path(context.messages[0])
            append_nested_path(context.injected_messages[0])
            append_nested_path(context.snapshot.continuation_messages[0])

    coordinator = agent_boundary_module._HarnessBoundaryCoordinator(
        get_messages=lambda: harness.messages,
        provider=harness.config.provider,
        effort=None,
        active_from=0,
        boundary_preparer=Preparer(),
        context_overflow_hook=None,
    )
    coordinator.arm(
        turn=1, had_tool_calls=True, injected_messages=(original,), stop_by_default=False
    )
    snapshot = RequestBoundarySnapshot(
        turn=1,
        tool_iterations=1,
        had_tool_calls=True,
        can_append_user_messages=True,
        continuation_messages=(continuation,),
    )

    async def run() -> None:
        decision = await coordinator.before_next_request(snapshot=snapshot)
        assert harness.messages == (expected,)
        assert original == expected
        assert snapshot.continuation_messages == (expected_continuation,)
        assert decision.extra_messages == (expected,)
        append_nested_path(decision.extra_messages[0])
        assert original == expected

    anyio.run(run)


def test_harness_rejects_a_stale_rebase_without_mutating_its_transcript() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderResponseCompleted(content="answer", response_id="response-1"),
            ]
        ]
    )
    harness = build_harness(provider)

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
