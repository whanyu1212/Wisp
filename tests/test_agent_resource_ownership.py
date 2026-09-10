"""Regression coverage for public payload isolation and owned stream cleanup."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from copy import deepcopy
from typing import Literal, cast

import anyio
import pytest

import wisp.agent.harness.runner as harness_module
from tests.agent_runtime import (
    assert_tool_result_pairing,
    assert_turn_invariants,
    assert_turn_terminals,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message, message_from_completion_event
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolPreparationEvent,
)
from wisp.events import (
    MessageCompleted,
    MessageDelta,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolExecutionStarted,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider


class _Executor:
    def __init__(self, *, malformed: bool = False) -> None:
        self.malformed = malformed
        self.inputs: list[dict[str, object]] = []
        self.streams: list[AsyncGenerator[ToolExecutionEvent, None]] = []
        self.closed = False

    def execute(self, call: ToolCall) -> AsyncGenerator[ToolExecutionEvent, None]:
        stream = self._execute(call)
        self.streams.append(stream)
        return stream

    async def _execute(self, call: ToolCall) -> AsyncGenerator[ToolExecutionEvent, None]:
        try:
            yield ToolApprovalRequested(
                call_id="wrong" if self.malformed else call.call_id,
                name=call.name,
                arguments=dict(call.arguments),
                safety="read",
            )
            self.inputs.append(deepcopy(dict(call.arguments)))
            yield ToolApprovalResolved(call_id=call.call_id, name=call.name, approved=True)
            yield ToolExecutionEnded(
                call_id=call.call_id, name=call.name, output="ok", is_error=False
            )
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.sleep(0)
                self.closed = True


class _PreparedExecutor(_Executor):
    def __init__(self, *, malformed: bool = False) -> None:
        super().__init__(malformed=malformed)
        self.preparations: list[AsyncGenerator[ToolPreparationEvent, None]] = []

    def prepare(self, call: ToolCall) -> AsyncGenerator[ToolPreparationEvent, None]:
        stream = self._prepare(call)
        self.preparations.append(stream)
        return stream

    async def _prepare(self, call: ToolCall) -> AsyncGenerator[ToolPreparationEvent, None]:
        try:
            yield ToolApprovalRequested(
                call_id="wrong" if self.malformed else call.call_id,
                name=call.name,
                arguments=dict(call.arguments),
                safety="read",
            )
            self.inputs.append(deepcopy(dict(call.arguments)))
            yield ToolApprovalResolved(call_id=call.call_id, name=call.name, approved=True)

            async def run() -> ToolExecutionEnded:
                self.inputs.append(deepcopy(dict(call.arguments)))
                return ToolExecutionEnded(
                    call_id=call.call_id, name=call.name, output="ok", is_error=False
                )

            yield PreparedToolExecution(
                call_id=call.call_id, name=call.name, parallel_safe=True, runner=run
            )
        finally:
            await anyio.sleep(0)
            self.closed = True


def _provider_with_call(call: ToolCall) -> ScriptedProvider:
    return ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="r1"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                    response_id="r1",
                ),
            ],
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="done")],
        ]
    )


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize(
    "mutation_event",
    [MessageCompleted, ToolCallRequested, ToolExecutionStarted, ToolApprovalRequested],
)
def test_public_arguments_do_not_alias_execution_or_history(
    prepared: bool, mutation_event: type[object]
) -> None:
    original = {"options": {"paths": ["original"]}}
    call = ToolCall(call_id="c1", name="lookup", arguments=deepcopy(original))
    executor = _PreparedExecutor() if prepared else _Executor()
    harness = AgentHarness(
        AgentHarnessConfig(provider=_provider_with_call(call), tool_executor=executor)
    )

    async def run() -> None:
        events = []
        async for event in harness.prompt("go"):
            events.append(event)
            if isinstance(event, MessageCompleted) and event.tool_calls:
                arguments = event.tool_calls[0].arguments
            elif isinstance(
                event, ToolCallRequested | ToolExecutionStarted | ToolApprovalRequested
            ):
                arguments = event.arguments
            else:
                continue
            # Earlier public mutations must not change later event snapshots either.
            assert arguments == original
            if isinstance(event, mutation_event):
                options = cast(dict[str, object], arguments["options"])
                cast(list[str], options["paths"]).append("consumer mutation")
        assert any(isinstance(event, mutation_event) for event in events)
        assert_turn_terminals(events)
        assert_tool_result_pairing(events)

    anyio.run(run)
    assert executor.inputs and all(arguments == original for arguments in executor.inputs)
    assert call.arguments == original
    assistant = next(message for message in harness.messages if message.tool_calls)
    assert assistant.tool_calls is not None
    assert assistant.tool_calls[0].arguments == original


def test_completion_message_owns_tool_snapshots() -> None:
    from wisp.events import ToolCallSnapshot

    snapshot = ToolCallSnapshot(
        call_id="c1", name="lookup", arguments={"paths": ["original"]}, provider_call_id="native"
    )
    event = MessageCompleted(turn=1, content="", tool_calls=(snapshot,), finish_reason="tool_calls")
    message = message_from_completion_event(event)
    cast(list[str], snapshot.arguments["paths"]).append("changed")
    assert message.tool_calls is not None
    assert message.tool_calls[0].arguments == {"paths": ["original"]}
    assert message.tool_calls[0].provider_call_id == "native"
    assert message.created_at == event.timestamp


@pytest.mark.parametrize("offset", ["turn_offset", "tool_iteration_offset"])
def test_invalid_startup_does_not_mutate_transcript_and_allows_retry(offset: str) -> None:
    initial = Message(role="user", content="earlier")
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="ok")]]
    )
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, tool_executor=_Executor()), messages=(initial,)
    )

    async def run() -> None:
        stream = harness.prompt("invalid", **{offset: -1})
        assert harness.messages == (initial,)
        with pytest.raises(ValueError, match=offset):
            await anext(stream)
        assert not harness.is_running
        assert not harness.cancel()
        assert harness.messages == (initial,)
        _events = [event async for event in harness.prompt("valid")]

    anyio.run(run)


def test_history_preparation_failure_releases_run(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="ok")]]
    )
    harness = AgentHarness(AgentHarnessConfig(provider=provider, tool_executor=_Executor()))
    harness.follow_up("queued")

    def fail(*args: object, **kwargs: object) -> tuple[Message, ...]:
        raise RuntimeError("history preparation failed")

    async def run() -> None:
        with monkeypatch.context() as patch:
            patch.setattr(harness_module, "prepare_provider_history", fail)
            with pytest.raises(RuntimeError, match="history preparation failed"):
                await anext(harness.prompt("accepted"))
        assert not harness.is_running
        assert not harness.cancel()
        assert [message.content for message in harness.messages] == ["accepted"]
        assert harness.pending_message_count == 1
        harness.clear_queues()
        _events = [event async for event in harness.continue_()]

    anyio.run(run)


class _ClosingProvider:
    name = "closing"
    default_model = "test"

    def __init__(
        self,
        *,
        failure: bool = False,
        close_failure: bool = False,
        close_message: str = "close failed",
    ) -> None:
        self.close_message = close_message
        self.failure = failure
        self.close_failure = close_failure
        self.closed = 0
        self.streams: list[AsyncGenerator[ProviderEvent, None]] = []

    def stream(self, messages: Sequence[Message], **kwargs: object) -> AsyncIterator[ProviderEvent]:
        stream = self._events()
        self.streams.append(stream)
        return stream

    async def _events(self) -> AsyncGenerator[ProviderEvent, None]:
        try:
            yield ProviderResponseStarted(model="test")
            yield ProviderTextDelta(delta="hello")
            if self.failure:
                yield ProviderResponseStarted(model="duplicate")
            yield ProviderResponseCompleted(content="hello")
        finally:
            await anyio.sleep(0)
            self.closed += 1
            if self.close_failure:
                raise RuntimeError(self.close_message)


@pytest.mark.parametrize("interface", ["harness", "loop"])
@pytest.mark.parametrize("ending", ["close", "cancel", "complete", "failure"])
def test_provider_closes_before_outer_stream_finishes(
    interface: str, ending: Literal["close", "cancel", "complete", "failure"]
) -> None:
    from wisp.agent.harness import SimpleCancellationToken
    from wisp.providers.base import ProviderProtocolError

    provider = _ClosingProvider(failure=ending == "failure")
    executor = _Executor()
    harness = AgentHarness(AgentHarnessConfig(provider=provider, tool_executor=executor))
    token = SimpleCancellationToken()

    async def run() -> None:
        stream = (
            harness.prompt("go")
            if interface == "harness"
            else run_agent_loop(
                AgentLoopConfig(
                    provider=provider, tool_executor=executor, cancellation_token=token
                ),
                messages=(Message(role="user", content="go"),),
            )
        )
        try:
            async for event in stream:
                if isinstance(event, MessageDelta):
                    if ending == "close":
                        await stream.aclose()
                        break
                    if ending == "cancel":
                        harness.cancel() if interface == "harness" else token.cancel()
        except ProviderProtocolError:
            assert ending == "failure"
        else:
            assert ending != "failure"
        assert provider.closed == 1
        assert not harness.is_running

    anyio.run(run)


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("ending", ["close", "cancel", "complete", "failure"])
def test_outer_exit_finishes_active_tool_iterator(prepared: bool, ending: str) -> None:
    from wisp.agent.tool_contracts import ToolExecutionProtocolError

    call = ToolCall(call_id="c1", name="lookup", arguments={})
    executor_type = _PreparedExecutor if prepared else _Executor
    executor = executor_type(malformed=ending == "failure")
    harness = AgentHarness(
        AgentHarnessConfig(provider=_provider_with_call(call), tool_executor=executor)
    )

    async def run() -> None:
        stream = harness.prompt("go")
        events = []
        try:
            async for event in stream:
                events.append(event)
                if isinstance(event, ToolApprovalRequested):
                    if ending == "close":
                        await stream.aclose()
                        break
                    if ending == "cancel":
                        harness.cancel()
        except ToolExecutionProtocolError:
            assert ending == "failure"
        else:
            assert ending != "failure"
        assert executor.closed
        if ending != "complete":
            assert executor.inputs == []
        assert not harness.is_running
        if ending != "close":
            assert_turn_terminals(events)
            assert_tool_result_pairing(events)

    anyio.run(run)


def test_child_close_failure_still_releases_harness() -> None:
    provider = _ClosingProvider(close_failure=True)
    harness = AgentHarness(AgentHarnessConfig(provider=provider, tool_executor=_Executor()))

    async def run() -> None:
        stream = harness.prompt("go")
        async for event in stream:
            if isinstance(event, MessageDelta):
                with pytest.raises(RuntimeError, match="close failed"):
                    await stream.aclose()
                break
        assert provider.closed == 1
        assert not harness.is_running
        assert not harness.cancel()
        provider.close_failure = False
        _events = [event async for event in harness.prompt("retry")]

    anyio.run(run)


def test_provider_protocol_failure_survives_close_failure() -> None:
    from wisp.providers.base import ProviderProtocolError

    provider = _ClosingProvider(failure=True, close_failure=True)
    harness = AgentHarness(AgentHarnessConfig(provider=provider, tool_executor=_Executor()))

    async def run() -> None:
        with pytest.raises(ProviderProtocolError) as caught:
            _events = [event async for event in harness.prompt("go")]
        assert str(caught.value.__cause__) == "close failed"
        assert provider.closed == 1
        assert not harness.is_running

    anyio.run(run)


@pytest.mark.parametrize("close_message", ["close failed", "maximum context length exceeded"])
def test_cancelled_turn_is_not_completed_twice_when_provider_close_fails(
    close_message: str,
) -> None:
    from wisp.agent.harness import SimpleCancellationToken

    provider = _ClosingProvider(close_failure=True, close_message=close_message)
    token = SimpleCancellationToken()

    async def run() -> None:
        events = []
        with pytest.raises(RuntimeError, match=close_message):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider, tool_executor=_Executor(), cancellation_token=token
                ),
                messages=(Message(role="user", content="go"),),
            ):
                events.append(event)
                if isinstance(event, MessageDelta):
                    token.cancel()
        assert provider.closed == 1
        assert_turn_invariants(events)

    anyio.run(run)


@pytest.mark.parametrize("truncated", [False, True])
def test_synthetic_tool_requests_own_their_arguments(truncated: bool) -> None:
    from wisp.agent.loop.tool_execution import ToolBatch

    original = {"options": {"paths": ["original"]}}
    calls = tuple(
        ToolCall(call_id=call_id, name="lookup", arguments=deepcopy(original))
        for call_id in ("c1", "c2")
    )
    executor = _PreparedExecutor()
    cancelled = False
    batch = ToolBatch(
        tool_executor=executor,
        tool_calls=calls,
        truncated=truncated,
        is_cancelled=lambda: cancelled,
        on_result=lambda _result: None,
    )

    async def run() -> None:
        nonlocal cancelled
        requested = []
        async for event in batch.events():
            if isinstance(event, ToolCallRequested):
                requested.append(event.call_id)
                assert event.arguments == original
                options = cast(dict[str, object], event.arguments["options"])
                cast(list[str], options["paths"]).append("consumer mutation")
                if not truncated:
                    cancelled = True
        assert requested == ["c1", "c2"]

    anyio.run(run)
    assert all(call.arguments == original for call in calls)
    assert executor.inputs == []
