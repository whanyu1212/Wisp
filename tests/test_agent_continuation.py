from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from wisp.agent.loop import AgentLoopConfig
from wisp.agent.loop.continuation import (
    ContinuationState,
    apply_request_boundary_decision,
    at_context_overflow,
    at_request_boundary,
    has_valid_replacement_tool_order,
    is_tool_shaped,
)
from wisp.agent.messages import Message
from wisp.agent.request_boundary import (
    ContextOverflowSnapshot,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
    RequestContextRebase,
)
from wisp.agent.tool_contracts import ToolExecutionEvent
from wisp.events import (
    ContextBudget,
    ContextEstimate,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
)
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import ProviderEvent, ToolCall


class _NeverExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class _CursorlessProvider:
    name = "cursorless"
    default_model = "test"

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
        raise AssertionError("Provider stream is not used by continuation unit tests")
        yield  # pragma: no cover - makes this an async generator


class _ContinuationProvider(_CursorlessProvider):
    supports_continuation_messages = True
    supports_context_rebase = True


class _OpaqueReplayProvider(_ContinuationProvider):
    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        del effort
        return False


def _config(*, provider: object | None = None) -> AgentLoopConfig:
    return AgentLoopConfig(
        provider=provider or _ContinuationProvider(),
        tool_executor=_NeverExecutor(),
    )


def _user(content: str = "hi") -> Message:
    return Message(role="user", content=content)


def _assistant(content: str = "answer", *, response_id: str | None = "resp-1") -> Message:
    return Message(role="assistant", content=content, response_id=response_id)


def _tool_call(*, call_id: str = "call-1", name: str = "noop") -> ToolCallSnapshot:
    return ToolCallSnapshot(call_id=call_id, name=name, arguments={"path": "a.txt"})


def _assistant_with_tool() -> Message:
    return Message(role="assistant", content="", tool_calls=(_tool_call(),), response_id="resp-1")


def _tool_result(*, call_id: str = "call-1", name: str = "noop") -> Message:
    return Message(role="tool", content="ok", tool_call_id=call_id, tool_name=name)


def _budget() -> ContextBudget:
    return ContextBudget(
        estimate=ContextEstimate(
            system_tokens=0,
            message_tokens=0,
            tool_schema_tokens=0,
            total_tokens=0,
        ),
        reserve_tokens=0,
    )


def _state_with_cursor(*, tool_history: bool = False) -> ContinuationState:
    state = ContinuationState(previous_response_id="resp-1")
    if tool_history:
        state.continuation_messages.extend((_assistant_with_tool(), _tool_result()))
        state.pending_tool_results = (
            ToolCallResult(call_id="call-1", output="ok", is_error=False),
        )
    else:
        state.continuation_messages.append(_assistant())
    return state


def test_is_tool_shaped_detects_assistant_calls_and_tool_rows() -> None:
    assert is_tool_shaped(_assistant_with_tool())
    assert is_tool_shaped(_tool_result())
    assert not is_tool_shaped(_assistant())
    assert not is_tool_shaped(_user())


def test_has_valid_replacement_tool_order_rejects_orphans_and_mismatches() -> None:
    assert has_valid_replacement_tool_order((_user(), _assistant()))
    assert has_valid_replacement_tool_order((_user(), _assistant_with_tool(), _tool_result()))
    assert not has_valid_replacement_tool_order((_tool_result(),))
    assert not has_valid_replacement_tool_order((_assistant_with_tool(),))
    assert not has_valid_replacement_tool_order(
        (_assistant_with_tool(), _tool_result(call_id="other"))
    )


def test_stop_does_not_mutate_continuation() -> None:
    state = _state_with_cursor()
    messages = (_user(),)
    result, stop = apply_request_boundary_decision(
        _config(),
        state,
        messages=messages,
        had_tool_calls=False,
        decision=RequestBoundaryDecision(stop=True, messages=(_user("compacted"),)),
        allow_extra_messages=True,
    )

    assert stop is True
    assert result is messages
    assert state.previous_response_id == "resp-1"
    assert [message.content for message in state.continuation_messages] == ["answer"]


def test_messages_and_rebase_are_mutually_exclusive() -> None:
    state = _state_with_cursor()
    with pytest.raises(RequestBoundaryUnsupportedError, match="mutually exclusive"):
        apply_request_boundary_decision(
            _config(),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(
                messages=(_user("compacted"),),
                context_rebase=RequestContextRebase(
                    base_messages=(_user("compacted"),),
                    expected_continuation_messages=tuple(state.continuation_messages),
                ),
            ),
            allow_extra_messages=True,
        )
    assert state.previous_response_id == "resp-1"


def test_extra_messages_must_be_plain_user_rows() -> None:
    state = ContinuationState()
    with pytest.raises(RequestBoundaryUnsupportedError, match="plain user messages"):
        apply_request_boundary_decision(
            _config(),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(extra_messages=(_assistant(),)),
            allow_extra_messages=True,
        )


def test_overflow_recovery_cannot_append_extras() -> None:
    state = _state_with_cursor()
    with pytest.raises(RequestBoundaryUnsupportedError, match="cannot append extra messages"):
        apply_request_boundary_decision(
            _config(),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(
                messages=(_user("compacted"),),
                extra_messages=(_user("follow up"),),
            ),
            allow_extra_messages=False,
        )
    assert state.previous_response_id == "resp-1"


def test_replacement_clears_cursor_and_appends_extras() -> None:
    state = _state_with_cursor(tool_history=True)
    extra = _user("steered")
    messages, stop = apply_request_boundary_decision(
        _config(),
        state,
        messages=(_user("original"),),
        had_tool_calls=True,
        decision=RequestBoundaryDecision(
            messages=(_user("compacted"),),
            extra_messages=(extra,),
        ),
        allow_extra_messages=True,
    )

    assert stop is False
    assert [(message.role, message.content) for message in messages] == [
        ("user", "compacted"),
        ("user", "steered"),
    ]
    assert state.previous_response_id is None
    assert state.pending_tool_results == ()
    assert state.pending_extra_messages == ()
    assert state.continuation_messages == []


def test_unpaired_replacement_is_rejected() -> None:
    state = ContinuationState()
    with pytest.raises(RequestBoundaryUnsupportedError, match="unpaired structured tool exchange"):
        apply_request_boundary_decision(
            _config(),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(messages=(_assistant_with_tool(),)),
            allow_extra_messages=True,
        )


def test_explicit_false_structured_replacement_is_rejected() -> None:
    state = ContinuationState()
    with pytest.raises(RequestBoundaryUnsupportedError, match="cannot fresh-replay"):
        apply_request_boundary_decision(
            _config(provider=_OpaqueReplayProvider()),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(
                messages=(_user(), _assistant_with_tool(), _tool_result())
            ),
            allow_extra_messages=True,
        )


def test_rebase_keeps_cursor_and_pending_tool_results() -> None:
    state = _state_with_cursor(tool_history=True)
    replacement, stop = apply_request_boundary_decision(
        _config(),
        state,
        messages=(_user("original"),),
        had_tool_calls=True,
        decision=RequestBoundaryDecision(
            context_rebase=RequestContextRebase(
                base_messages=(_user("compacted"),),
                expected_continuation_messages=tuple(state.continuation_messages),
            )
        ),
        allow_extra_messages=True,
    )

    assert stop is False
    assert [(message.role, message.content) for message in replacement] == [("user", "compacted")]
    assert state.previous_response_id == "resp-1"
    assert state.pending_tool_results == (
        ToolCallResult(call_id="call-1", output="ok", is_error=False),
    )
    assert [message.role for message in state.continuation_messages] == ["assistant", "tool"]


def test_clean_rebase_consumes_pending_tool_results_and_queues_extras() -> None:
    state = _state_with_cursor(tool_history=True)
    extra = _user("follow up")
    apply_request_boundary_decision(
        _config(),
        state,
        messages=(_user("original"),),
        had_tool_calls=False,
        decision=RequestBoundaryDecision(
            context_rebase=RequestContextRebase(
                base_messages=(_user("compacted"),),
                expected_continuation_messages=tuple(state.continuation_messages),
            ),
            extra_messages=(extra,),
        ),
        allow_extra_messages=True,
    )

    assert state.previous_response_id == "resp-1"
    assert state.pending_tool_results == ()
    assert state.pending_extra_messages == (extra,)
    assert state.continuation_messages[-1] is extra


def test_stale_rebase_leaves_state_untouched() -> None:
    state = _state_with_cursor()
    with pytest.raises(RequestBoundaryUnsupportedError, match="does not match live state"):
        apply_request_boundary_decision(
            _config(),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(
                context_rebase=RequestContextRebase(
                    base_messages=(_user("compacted"),),
                    expected_continuation_messages=(),
                )
            ),
            allow_extra_messages=True,
        )
    assert state.previous_response_id == "resp-1"
    assert [message.content for message in state.continuation_messages] == ["answer"]


def test_rebase_without_cursor_is_rejected() -> None:
    state = ContinuationState(continuation_messages=[_assistant()])
    with pytest.raises(RequestBoundaryUnsupportedError, match="usable provider continuation"):
        apply_request_boundary_decision(
            _config(),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(
                context_rebase=RequestContextRebase(
                    base_messages=(_user("compacted"),),
                    expected_continuation_messages=tuple(state.continuation_messages),
                )
            ),
            allow_extra_messages=True,
        )


def test_native_append_queues_extras_on_live_cursor() -> None:
    state = _state_with_cursor()
    extra = _user("steered")
    messages = (_user(),)
    result, stop = apply_request_boundary_decision(
        _config(),
        state,
        messages=messages,
        had_tool_calls=False,
        decision=RequestBoundaryDecision(extra_messages=(extra,)),
        allow_extra_messages=True,
    )

    assert stop is False
    assert result is messages
    assert state.pending_extra_messages == (extra,)
    assert state.continuation_messages[-1] is extra
    assert state.previous_response_id == "resp-1"


def test_cursorless_tool_history_cannot_append_or_continue() -> None:
    state = ContinuationState(continuation_messages=[_assistant_with_tool(), _tool_result()])
    with pytest.raises(RequestBoundaryUnsupportedError, match="after a tool round"):
        apply_request_boundary_decision(
            _config(provider=_CursorlessProvider()),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(extra_messages=(_user("steered"),)),
            allow_extra_messages=True,
        )
    with pytest.raises(RequestBoundaryUnsupportedError, match="without a usable provider"):
        apply_request_boundary_decision(
            _config(provider=_CursorlessProvider()),
            state,
            messages=(_user(),),
            had_tool_calls=False,
            decision=RequestBoundaryDecision(),
            allow_extra_messages=True,
        )


def test_post_tool_round_continues_without_cursor() -> None:
    state = ContinuationState(
        continuation_messages=[_assistant_with_tool(), _tool_result()],
        pending_tool_results=(ToolCallResult(call_id="call-1", output="ok"),),
    )
    messages = (_user(),)
    result, stop = apply_request_boundary_decision(
        _config(provider=_CursorlessProvider()),
        state,
        messages=messages,
        had_tool_calls=True,
        decision=RequestBoundaryDecision(),
        allow_extra_messages=True,
    )

    assert stop is False
    assert result is messages
    assert state.pending_tool_results == (ToolCallResult(call_id="call-1", output="ok"),)


def test_cursorless_clean_response_folds_portable_history() -> None:
    state = ContinuationState(continuation_messages=[_assistant()])
    folded, stop = apply_request_boundary_decision(
        _config(provider=_CursorlessProvider()),
        state,
        messages=(_user(),),
        had_tool_calls=False,
        decision=RequestBoundaryDecision(extra_messages=(_user("next"),)),
        allow_extra_messages=True,
    )

    assert stop is False
    assert [(message.role, message.content) for message in folded] == [
        ("user", "hi"),
        ("assistant", "answer"),
        ("user", "next"),
    ]
    assert state.previous_response_id is None
    assert state.continuation_messages == []


def test_record_response_keeps_existing_cursor_when_id_is_missing() -> None:
    state = ContinuationState(previous_response_id="kept")
    state.record_response(_assistant(response_id=None), response_id=None)
    assert state.previous_response_id == "kept"
    state.record_response(_assistant(response_id="new"), response_id="new")
    assert state.previous_response_id == "new"


def test_snapshot_does_not_leak_live_tool_arguments() -> None:
    live = _assistant_with_tool()
    state = ContinuationState(continuation_messages=[live])
    snapshot = state.snapshot()
    assert snapshot[0].tool_calls is not None
    snapshot[0].tool_calls[0].arguments["path"] = "mutated"
    assert live.tool_calls is not None
    assert live.tool_calls[0].arguments == {"path": "a.txt"}


def test_record_tool_result_appends_matching_tool_row() -> None:
    state = ContinuationState()
    ended = ToolExecutionEnded(call_id="call-1", name="noop", output="ok", is_error=False)
    state.record_tool_result(ToolResultReady.from_execution_ended(ended))
    recorded = state.continuation_messages[0]
    assert recorded.role == "tool"
    assert recorded.content == "ok"
    assert recorded.tool_call_id == "call-1"
    assert recorded.tool_name == "noop"
    assert recorded.is_error is False


def test_request_boundary_hook_stop_by_default_without_hook() -> None:
    original = (_user(),)

    async def run() -> None:
        messages, stop = await at_request_boundary(
            _config(),
            ContinuationState(),
            turn=1,
            tool_iterations=0,
            messages=original,
            had_tool_calls=False,
            stop_by_default=True,
        )
        assert messages is original
        assert stop is True

    anyio.run(run)


def test_request_boundary_hook_snapshot_is_isolated() -> None:
    live = _assistant_with_tool()
    state = ContinuationState(previous_response_id="resp-1", continuation_messages=[live])

    class Hook:
        def __init__(self) -> None:
            self.snapshot: RequestBoundarySnapshot | None = None

        async def before_next_request(
            self, *, snapshot: RequestBoundarySnapshot
        ) -> RequestBoundaryDecision:
            self.snapshot = snapshot
            assert snapshot.continuation_messages[0].tool_calls is not None
            snapshot.continuation_messages[0].tool_calls[0].arguments["path"] = "mutated"
            return RequestBoundaryDecision(stop=True)

    hook = Hook()
    config = AgentLoopConfig(
        provider=_ContinuationProvider(),
        tool_executor=_NeverExecutor(),
        request_boundary_hook=hook,
    )

    async def run() -> None:
        await at_request_boundary(
            config,
            state,
            turn=2,
            tool_iterations=1,
            messages=(_user(),),
            had_tool_calls=True,
            stop_by_default=False,
        )

    anyio.run(run)
    assert hook.snapshot is not None
    assert hook.snapshot.turn == 2
    assert hook.snapshot.tool_iterations == 1
    assert live.tool_calls is not None
    assert live.tool_calls[0].arguments == {"path": "a.txt"}


def test_overflow_recovery_requires_replacement_or_rebase() -> None:
    class Hook:
        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision:
            del snapshot
            return RequestBoundaryDecision()

    config = AgentLoopConfig(
        provider=_ContinuationProvider(),
        tool_executor=_NeverExecutor(),
        context_overflow_hook=Hook(),
    )

    async def run() -> None:
        with pytest.raises(
            RequestBoundaryUnsupportedError, match="fresh replacement or context rebase"
        ):
            await at_context_overflow(
                config,
                ContinuationState(previous_response_id="resp-1"),
                turn=1,
                tool_iterations=0,
                messages=(_user(),),
                context_budget=_budget(),
                had_streamed_delta=False,
                message="overflow",
            )

    anyio.run(run)
