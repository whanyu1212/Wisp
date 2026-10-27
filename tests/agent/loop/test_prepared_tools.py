"""Prepared tool batches through the loop: concurrency, ordering, timing, and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import anyio
import pytest

import wisp.agent.loop.prepared_tools as prepared_tools_module
from tests.agent.loop.support import ApprovingToolExecutor, install_fake_clock
from tests.support.agent_runtime import (
    assert_continuation_invariants,
    assert_settled_tool_calls,
    assert_turn_terminals,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolPreparationEvent,
)
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
)
from wisp.extensions import builtin
from wisp.messages import Message
from wisp.providers.base import ProviderProtocolError
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime import EventBus, ExtensionAPI, ProviderRegistry
from wisp.runtime.registry import ToolRegistry
from wisp.tool_types import ToolSafety
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolExecutionMetadata
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.search import tools as search_tools_module


class CallbackTool:
    safety: ToolSafety = "read"
    description = "Run a test callback."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        callback: Callable[[str], Awaitable[ToolResult]],
    ) -> None:
        self.name = name
        self._callback = callback

    async def run(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del arguments, context
        return await self._callback(self.name)


class PreparedScriptExecutor:
    def __init__(
        self,
        runner: Callable[[ToolCall], Awaitable[ToolExecutionEnded]],
    ) -> None:
        self._runner = runner

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            return await self._runner(tool_call)

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


def _scripted_tool_batch_provider(calls: tuple[ToolCall, ...]) -> ScriptedProvider:
    return ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=calls,
                    finish_reason="tool_calls",
                    response_id="response-1",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(
                    content="done",
                    finish_reason="stop",
                    response_id="response-2",
                ),
            ],
        ]
    )


def test_publishes_overlapped_calls_in_source_order() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = _scripted_tool_batch_provider(calls)

    async def run() -> tuple[list[object], list[str]]:
        first_started = anyio.Event()
        release_first = anyio.Event()
        completion_order: list[str] = []

        async def runner(tool_call: ToolCall) -> ToolExecutionEnded:
            if tool_call.call_id == "call-1":
                first_started.set()
                await release_first.wait()
            else:
                await first_started.wait()
                completion_order.append(tool_call.call_id)
                release_first.set()
            if tool_call.call_id == "call-1":
                completion_order.append(tool_call.call_id)
            return ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output=f"output-{tool_call.call_id}",
                is_error=False,
            )

        with anyio.fail_after(2):
            events = [
                event
                async for event in run_agent_loop(
                    AgentLoopConfig(
                        provider=provider,
                        tool_executor=PreparedScriptExecutor(runner),
                    ),
                    messages=(Message(role="user", content="hi"),),
                )
            ]
        return events, completion_order

    events, completion_order = anyio.run(run)

    assert completion_order == ["call-2", "call-1"]
    terminal_ids = [event.call_id for event in events if isinstance(event, ToolExecutionEnded)]
    result_ids = [event.call_id for event in events if isinstance(event, ToolResultReady)]
    assert terminal_ids == ["call-1", "call-2"]
    assert result_ids == ["call-1", "call-2"]
    assert [result.call_id for result in provider.calls[1].tool_results] == [
        "call-1",
        "call-2",
    ]
    assert_turn_terminals(events)
    assert_settled_tool_calls(events, ("call-1", "call-2"))


def test_parallel_calls_time_themselves(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = install_fake_clock(monkeypatch)
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = _scripted_tool_batch_provider(calls)
    quick_call_returned = anyio.Event()

    async def runner(tool_call: ToolCall) -> ToolExecutionEnded:
        if tool_call.call_id == "call-1":
            # The quick call's duration is stamped before its task yields again.
            await quick_call_returned.wait()
            clock.advance(0.3)
        else:
            quick_call_returned.set()
        return ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="ok",
            is_error=False,
        )

    async def run() -> list[object]:
        with anyio.fail_after(2):
            return [
                event
                async for event in run_agent_loop(
                    AgentLoopConfig(
                        provider=provider,
                        tool_executor=PreparedScriptExecutor(runner),
                    ),
                    messages=(Message(role="user", content="hi"),),
                )
            ]

    events = anyio.run(run)

    durations = {
        event.call_id: event.duration_ms for event in events if isinstance(event, ToolResultReady)
    }
    # The quick sibling reports its own time, not the wait for the slow one.
    assert durations == {"call-1": 300, "call-2": 0}


def test_isolates_one_parallel_failure() -> None:
    calls = (
        ToolCall(call_id="call-1", name="first", arguments={}),
        ToolCall(call_id="call-2", name="second", arguments={}),
    )
    provider = _scripted_tool_batch_provider(calls)

    async def run() -> list[object]:
        second_started = anyio.Event()

        async def callback(name: str) -> ToolResult:
            if name == "first":
                await second_started.wait()
                return ToolResult(text="first succeeded")
            second_started.set()
            raise ToolError("second failed")

        registry = ToolRegistry()
        execution = ToolExecutionMetadata(parallel_safe=True)
        registry.register(CallbackTool("first", callback), execution=execution)
        registry.register(CallbackTool("second", callback), execution=execution)
        executor = ConfiguredToolExecutor(
            registry=registry,
            context=ToolContext(cwd=Path.cwd(), protected_paths=()),
            policy=ToolPolicy.allow_all_tools(),
            approval_policy=ToolApprovalPolicy.approve_all(),
        )
        with anyio.fail_after(2):
            return [
                event
                async for event in run_agent_loop(
                    AgentLoopConfig(provider=provider, tool_executor=executor),
                    messages=(Message(role="user", content="hi"),),
                )
            ]

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in results] == ["call-1", "call-2"]
    assert [event.is_error for event in results] == [False, True]
    assert results[0].output == "first succeeded"
    assert results[1].output == "second failed"
    assert [result.call_id for result in provider.calls[1].tool_results] == [
        "call-1",
        "call-2",
    ]


@pytest.mark.parametrize("first_raises", [True, False])
def test_cancel_separates_results_from_settlement(
    first_raises: bool,
) -> None:
    class Token:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    token = Token()
    calls = (
        ToolCall(call_id="call-1", name="lookup", arguments={}),
        ToolCall(call_id="call-2", name="lookup", arguments={}),
    )

    async def runner(call: ToolCall) -> ToolExecutionEnded:
        if call.call_id == "call-1":
            if first_raises:
                raise RuntimeError("first call failed")
            return ToolExecutionEnded(
                call_id=call.call_id,
                name=call.name,
                output="Tool stopped",
                is_error=True,
                process_state="cancelled",
            )
        token.cancelled = True
        return ToolExecutionEnded(
            call_id=call.call_id, name=call.name, output="done", is_error=False
        )

    async def run() -> list[object]:
        with anyio.fail_after(3):
            return [
                event
                async for event in run_agent_loop(
                    AgentLoopConfig(
                        provider=_scripted_tool_batch_provider(calls),
                        tool_executor=PreparedScriptExecutor(runner),
                        cancellation_token=token,
                    ),
                    messages=(Message(role="user", content="hi"),),
                )
            ]

    events = anyio.run(run)
    assert [
        (event.call_id, event.process_state)
        for event in events
        if isinstance(event, ToolExecutionEnded)
    ] == (
        [("call-2", None), ("call-1", "cancelled")]
        if first_raises
        else [("call-1", "cancelled"), ("call-2", None)]
    )
    assert_continuation_invariants(events)


@pytest.mark.parametrize("path", ["sequential", "prepared"])
def test_duplicate_call_id_runs_no_tool(
    path: Literal["sequential", "prepared"],
) -> None:
    """A response that repeats a mutating call's ID fails before any tool runs."""

    repeated = ToolCall(call_id="call-1", name="bash", arguments={"command": "rm -rf build"})
    calls = (repeated, ToolCall(call_id="call-1", name="bash", arguments={"command": "make"}))
    ran: list[ToolCall] = []

    async def runner(call: ToolCall) -> ToolExecutionEnded:
        ran.append(call)
        return ToolExecutionEnded(call_id=call.call_id, name=call.name, output="", is_error=False)

    sequential = ApprovingToolExecutor()
    executor = sequential if path == "sequential" else PreparedScriptExecutor(runner)

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ProviderProtocolError, match="'call-1' more than once"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=_scripted_tool_batch_provider(calls),
                    tool_executor=executor,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)
    assert ran == [] and sequential.calls == []
    assert not any(event.type.startswith("tool.") for event in events)
    completion = next(event for event in events if isinstance(event, MessageCompleted))
    assert (completion.finish_reason, completion.tool_calls) == ("error", ())
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted) and terminal.outcome == "failed"


class _BlockingPreparedExecutor:
    """Block while preparing or while running a prepared call until cancelled."""

    def __init__(self, phase: Literal["preparation", "execution"]) -> None:
        self.phase = phase
        self.blocked = anyio.Event()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            self.blocked.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        if self.phase == "preparation":
            # Stands in for an approval prompt that is still waiting on the user.
            self.blocked.set()
            await anyio.sleep_forever()
        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=True,
            runner=run,
        )

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError("prepared executors are scheduled through prepare()")
        yield  # pragma: no cover - makes this an async generator


def _blocking_batch_loop(
    executor: _BlockingPreparedExecutor, events: list[object]
) -> Callable[[], Awaitable[None]]:
    call = ToolCall(call_id="call-1", name="read", arguments={})

    async def consume() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=_scripted_tool_batch_provider((call,)), tool_executor=executor
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            events.append(event)

    return consume


def _assert_batch_left_unsettled(events: Sequence[object]) -> None:
    # The caller's cancellation is not the run's: no settlement or cancelled turn.
    assert [getattr(event, "type", None) for event in events][-2:] == [
        "tool.call",
        "tool.execution.started",
    ]
    assert not any(
        isinstance(event, ToolResultReady | ErrorEvent | TurnCompleted) for event in events
    )


@pytest.mark.parametrize("phase", ["preparation", "execution"])
def test_propagates_unrequested_scope_cancellation(
    phase: Literal["preparation", "execution"],
) -> None:
    async def run() -> tuple[bool, list[object]]:
        executor = _BlockingPreparedExecutor(phase)
        events: list[object] = []
        consume = _blocking_batch_loop(executor, events)
        scope = anyio.CancelScope()

        async def consume_in_scope() -> None:
            with scope:
                await consume()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(consume_in_scope)
                await executor.blocked.wait()
                scope.cancel()
        return scope.cancelled_caught, events

    cancelled_caught, events = anyio.run(run)

    assert cancelled_caught
    _assert_batch_left_unsettled(events)


@pytest.mark.parametrize("phase", ["preparation", "execution"])
def test_propagates_asyncio_task_cancellation(
    phase: Literal["preparation", "execution"],
) -> None:
    async def main() -> list[object]:
        executor = _BlockingPreparedExecutor(phase)
        events: list[object] = []
        task = asyncio.create_task(_blocking_batch_loop(executor, events)())
        await asyncio.wait_for(executor.blocked.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        return events

    _assert_batch_left_unsettled(asyncio.run(main()))


@pytest.mark.parametrize("phase", ["preparation", "execution"])
def test_settles_cancellation_requested_by_harness(
    phase: Literal["preparation", "execution"],
) -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})

    async def run() -> list[object]:
        executor = _BlockingPreparedExecutor(phase)
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=_scripted_tool_batch_provider((call,)),
                tool_executor=executor,
            )
        )
        events: list[object] = []

        async def collect() -> None:
            events.extend([event async for event in harness.prompt("hi")])

        with anyio.fail_after(5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await executor.blocked.wait()
                assert harness.cancel()
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [(event.call_id, event.process_state) for event in results] == [("call-1", "cancelled")]
    assert [
        (event.outcome, event.finish_reason) for event in events if isinstance(event, TurnCompleted)
    ] == [("cancelled", "cancelled")]
    assert_settled_tool_calls(events, ("call-1",))


def test_skips_start_after_cooperative_cancellation() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={})
    provider = _scripted_tool_batch_provider((call,))

    class Token:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    token = Token()
    runner_calls = 0

    class CancelDuringPreparationExecutor:
        async def prepare(
            self,
            tool_call: ToolCall,
        ) -> AsyncIterator[ToolPreparationEvent]:
            async def run() -> ToolExecutionEnded:
                nonlocal runner_calls
                runner_calls += 1
                return ToolExecutionEnded(
                    call_id=tool_call.call_id,
                    name=tool_call.name,
                    output="must not run",
                    is_error=False,
                )

            token.cancelled = True
            yield PreparedToolExecution(
                call_id=tool_call.call_id,
                name=tool_call.name,
                parallel_safe=True,
                runner=run,
            )

        async def execute(
            self,
            tool_call: ToolCall,
        ) -> AsyncIterator[ToolExecutionEvent]:
            async for event in self.prepare(tool_call):
                if isinstance(event, PreparedToolExecution):
                    yield await event.run()
                else:
                    yield event

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=CancelDuringPreparationExecutor(),
                    cancellation_token=token,
                ),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)

    assert runner_calls == 0
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in results] == ["call-1"]
    assert results[0].process_state == "cancelled"
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].outcome == "cancelled"
    assert len(provider.calls) == 1


def test_bounds_live_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prepared_tools_module, "_MAX_PARALLEL_TOOL_EXECUTIONS", 2)
    calls = tuple(
        ToolCall(call_id=f"call-{index}", name="read", arguments={}) for index in range(1, 6)
    )
    provider = _scripted_tool_batch_provider(calls)

    async def run() -> tuple[int, list[object]]:
        active = 0
        max_active = 0
        limit_reached = anyio.Event()
        release = anyio.Event()
        events: list[object] = []

        async def runner(tool_call: ToolCall) -> ToolExecutionEnded:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                limit_reached.set()
            await release.wait()
            active -= 1
            return ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output="done",
                is_error=False,
            )

        async def collect() -> None:
            events.extend(
                [
                    event
                    async for event in run_agent_loop(
                        AgentLoopConfig(
                            provider=provider,
                            tool_executor=PreparedScriptExecutor(runner),
                        ),
                        messages=(Message(role="user", content="hi"),),
                    )
                ]
            )

        with anyio.fail_after(2):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(collect)
                await limit_reached.wait()
                await anyio.sleep(0)
                observed_max = max_active
                release.set()
        return observed_max, events

    max_active, events = anyio.run(run)

    assert max_active == 2
    assert len([event for event in events if isinstance(event, ToolResultReady)]) == 5


def test_publishes_siblings_before_fatal_error() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = _scripted_tool_batch_provider(calls)

    async def runner(tool_call: ToolCall) -> ToolExecutionEnded:
        if tool_call.call_id == "call-1":
            raise RuntimeError("executor failed")
        return ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="completed sibling",
            is_error=False,
        )

    async def run() -> tuple[list[object], RuntimeError]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="executor failed") as raised:
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=PreparedScriptExecutor(runner),
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events, raised.value

    events, error = anyio.run(run)

    assert str(error) == "executor failed"
    assert [event.call_id for event in events if isinstance(event, ToolResultReady)] == ["call-2"]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [event.message for event in errors] == ["executor failed"]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert completed[-1].outcome == "failed"
    assert len(provider.calls) == 1


def test_publishes_sibling_after_malformed_result() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={}),
        ToolCall(call_id="call-2", name="read", arguments={}),
    )
    provider = _scripted_tool_batch_provider(calls)

    async def runner(tool_call: ToolCall) -> ToolExecutionEnded:
        return ToolExecutionEnded(
            call_id=("wrong-call" if tool_call.call_id == "call-1" else tool_call.call_id),
            name=tool_call.name,
            output=f"output-{tool_call.call_id}",
            is_error=False,
        )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ToolExecutionProtocolError, match="does not match"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=PreparedScriptExecutor(runner),
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.call_id for event in events if isinstance(event, ToolResultReady)] == ["call-2"]
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert completed[-1].outcome == "failed"


def test_rejects_result_emitted_during_preparation() -> None:
    """A result during preparation is a protocol error and never reaches the consumer.

    Preparation may only request approval. Publishing an early result would emit a
    terminal without its provider-facing result, before any side effect was scheduled.
    """

    call = ToolCall(call_id="call-1", name="read", arguments={})
    provider = _scripted_tool_batch_provider((call,))
    ran: list[str] = []

    class EarlyResultExecutor:
        async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
            yield ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output="early",
                is_error=False,
            )

            async def run() -> ToolExecutionEnded:
                ran.append(tool_call.call_id)
                raise AssertionError("prepared runner must not start")

            yield PreparedToolExecution(call_id=tool_call.call_id, name=tool_call.name, runner=run)

        async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
            raise AssertionError("a prepared executor is not driven through execute")
            yield  # pragma: no cover

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ToolExecutionProtocolError, match="terminal result during preparation"):
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=EarlyResultExecutor()),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert not any(isinstance(event, ToolExecutionEnded | ToolResultReady) for event in events)
    assert ran == []
    assert_turn_terminals(events)
    completed = [event for event in events if isinstance(event, TurnCompleted)]
    assert completed[-1].outcome == "failed"


class _LabelTool:
    """Hand each call's ``label`` argument to a shared test callback."""

    description = "Run a labelled test callback."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        safety: ToolSafety,
        callback: Callable[[str], Awaitable[None]],
    ) -> None:
        self.name = name
        self.safety = safety
        self._callback = callback

    async def run(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        del context
        label = str(arguments["label"])
        await self._callback(label)
        return ToolResult(text=label)


class _ArgumentRejectingTool:
    name = "checked"
    safety: ToolSafety = "read"
    description = "Reject every call during preparation."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def check_arguments(self, arguments: Mapping[str, object]) -> str | None:
        del arguments
        return "label is not accepted"

    async def run(
        self,
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolResult:
        raise AssertionError("a call rejected during preparation must not run")


def _rejecting_executor(callback: Callable[[str], Awaitable[None]]) -> ConfiguredToolExecutor:
    """Build an executor whose tools cover each way preparation rejects a call.

    ``slow`` is parallel safe and ``side_effect`` is a valid sequential tool, like
    bash or write. ``blocked`` is denied by policy, ``guarded`` is denied approval,
    and ``checked`` rejects its arguments.
    """

    registry = ToolRegistry()
    parallel = ToolExecutionMetadata(parallel_safe=True)
    registry.register(_LabelTool("slow", "read", callback), execution=parallel)
    registry.register(_LabelTool("side_effect", "mutating", callback))
    registry.register(_LabelTool("guarded", "mutating", callback))
    registry.register(_LabelTool("blocked", "read", callback), execution=parallel)
    registry.register(_ArgumentRejectingTool(), execution=parallel)
    return ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=Path.cwd(), protected_paths=()),
        policy=ToolPolicy.allow_tool_names({"slow", "side_effect", "guarded", "checked"}),
        approval_policy=ToolApprovalPolicy.approve_tool_names({"side_effect"}),
    )


async def _run_tool_batch(
    calls: tuple[ToolCall, ...], executor: ConfiguredToolExecutor
) -> list[object]:
    with anyio.fail_after(2):
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=_scripted_tool_batch_provider(calls),
                    tool_executor=executor,
                ),
                messages=(Message(role="user", content="hi"),),
            )
        ]


def _labelled_call(call_id: str, name: str) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments={"label": call_id})


@pytest.mark.parametrize(
    ("rejected_call", "failure_code"),
    [
        pytest.param(_labelled_call("rejected", "missing"), "unknown_tool", id="unknown-tool"),
        pytest.param(
            ToolCall(call_id="rejected", name="slow", arguments={}, parse_error="bad JSON"),
            "invalid_arguments",
            id="parse-error",
        ),
        pytest.param(
            _labelled_call("rejected", "checked"), "invalid_arguments", id="invalid-arguments"
        ),
        pytest.param(_labelled_call("rejected", "blocked"), "policy_denied", id="policy-denied"),
        pytest.param(
            _labelled_call("rejected", "guarded"), "approval_denied", id="approval-denied"
        ),
    ],
)
def test_rejected_call_keeps_siblings_concurrent(
    rejected_call: ToolCall,
    failure_code: str,
) -> None:
    calls = (
        _labelled_call("slow-1", "slow"),
        rejected_call,
        _labelled_call("slow-2", "slow"),
        _labelled_call("slow-3", "slow"),
    )

    async def run() -> tuple[list[object], list[str]]:
        started: list[str] = []
        completed: list[str] = []
        all_started = anyio.Event()
        others_completed = anyio.Event()

        async def callback(label: str) -> None:
            # Every slow call must be running at once to pass the barrier, so a
            # serial batch times out. slow-1 then finishes last.
            started.append(label)
            if len(started) == 3:
                all_started.set()
            await all_started.wait()
            if label == "slow-1":
                await others_completed.wait()
            completed.append(label)
            if len(completed) == 2:
                others_completed.set()

        events = await _run_tool_batch(calls, _rejecting_executor(callback))
        return events, completed

    events, completed = anyio.run(run)

    assert completed[-1] == "slow-1"
    published = [
        (type(event).__name__, event.call_id)
        for event in events
        if isinstance(event, ToolExecutionEnded | ToolResultReady)
    ]
    assert published == [
        (kind, call.call_id) for call in calls for kind in ("ToolExecutionEnded", "ToolResultReady")
    ]
    terminals = {event.call_id: event for event in events if isinstance(event, ToolExecutionEnded)}
    assert terminals["rejected"].is_error
    assert terminals["rejected"].failure_code == failure_code
    assert [terminals[f"slow-{index}"].output for index in range(1, 4)] == [
        "slow-1",
        "slow-2",
        "slow-3",
    ]
    assert_settled_tool_calls(events, tuple(call.call_id for call in calls))


@pytest.mark.parametrize(
    "first_call",
    [
        pytest.param(_labelled_call("first", "slow"), id="parallel-safe-sibling"),
        pytest.param(_labelled_call("first", "guarded"), id="approval-denied-sibling"),
    ],
)
def test_valid_side_effecting_call_keeps_batch_serial(first_call: ToolCall) -> None:
    calls = (
        first_call,
        _labelled_call("side-1", "side_effect"),
        _labelled_call("slow-2", "slow"),
        _labelled_call("side-2", "side_effect"),
    )

    async def run() -> tuple[list[object], list[str], int]:
        started: list[str] = []
        active = 0
        max_active = 0

        async def callback(label: str) -> None:
            nonlocal active, max_active
            started.append(label)
            active += 1
            max_active = max(max_active, active)
            await anyio.sleep(0)
            await anyio.sleep(0)
            active -= 1

        events = await _run_tool_batch(calls, _rejecting_executor(callback))
        return events, started, max_active

    events, started, max_active = anyio.run(run)

    ran = [call.call_id for call in calls if call.name != "guarded"]
    assert started == ran
    assert max_active == 1
    assert [event.call_id for event in events if isinstance(event, ToolExecutionEnded)] == [
        call.call_id for call in calls
    ]


def _builtin_metadata_executor(
    callback: Callable[[str], Awaitable[None]],
) -> ConfiguredToolExecutor:
    """Build an executor of read-only label tools that carry the builtin execution metadata.

    Each label tool takes the name of a builtin read-only tool and the parallel-safe
    metadata the builtin extension registers for it, so batches schedule as the real
    tools would without scanning the filesystem.
    """

    builtin_registry = ToolRegistry()
    builtin.activate(
        ExtensionAPI(providers=ProviderRegistry(), tools=builtin_registry, events=EventBus())
    )
    names = ("find", "grep", "ls", "read")
    registry = ToolRegistry()
    for name in names:
        registry.register(
            _LabelTool(name, "read", callback),
            execution=builtin_registry.execution_metadata_for(name),
        )
    return ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=Path.cwd(), protected_paths=()),
        policy=ToolPolicy.allow_tool_names(set(names)),
        approval_policy=ToolApprovalPolicy.approve_tool_names(set(names)),
    )


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param(
            tuple(_labelled_call(f"grep-{index}", "grep") for index in range(1, 4)),
            id="grep-only",
        ),
        pytest.param(
            tuple(_labelled_call(f"find-{index}", "find") for index in range(1, 4)),
            id="find-only",
        ),
        pytest.param(
            (
                _labelled_call("read-1", "read"),
                _labelled_call("grep-1", "grep"),
                _labelled_call("ls-1", "ls"),
                _labelled_call("find-1", "find"),
            ),
            id="mixed-with-read-and-ls",
        ),
    ],
)
def test_builtin_search_calls_run_serially(
    calls: tuple[ToolCall, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the native walk, grep and find contend for the GIL, so a batch with them is serial.

    A grep or find call also serializes read and ls siblings, because a batch only runs
    concurrently when every call is parallel safe.
    """

    monkeypatch.setattr(search_tools_module, "_NATIVE_WALK", None)

    async def run() -> tuple[list[object], list[str], int]:
        started: list[str] = []
        active = 0
        max_active = 0

        async def callback(label: str) -> None:
            nonlocal active, max_active
            started.append(label)
            active += 1
            max_active = max(max_active, active)
            await anyio.sleep(0)
            await anyio.sleep(0)
            active -= 1

        events = await _run_tool_batch(calls, _builtin_metadata_executor(callback))
        return events, started, max_active

    events, started, max_active = anyio.run(run)

    call_ids = [call.call_id for call in calls]
    assert started == call_ids
    assert max_active == 1
    published = [
        (type(event).__name__, event.call_id)
        for event in events
        if isinstance(event, ToolExecutionEnded | ToolResultReady)
    ]
    assert published == [
        (kind, call_id)
        for call_id in call_ids
        for kind in ("ToolExecutionEnded", "ToolResultReady")
    ]
    assert_settled_tool_calls(events, tuple(call_ids))


def test_builtin_read_and_ls_overlap() -> None:
    calls = (
        _labelled_call("read-1", "read"),
        _labelled_call("ls-1", "ls"),
        _labelled_call("read-2", "read"),
    )

    async def run() -> tuple[list[object], list[str]]:
        started: list[str] = []
        completed: list[str] = []
        all_started = anyio.Event()

        async def callback(label: str) -> None:
            # Every call must be running at once to pass the barrier, so a serial
            # batch times out. read-1 then finishes last.
            started.append(label)
            if len(started) == len(calls):
                all_started.set()
            await all_started.wait()
            if label == "read-1":
                await anyio.sleep(0.01)
            completed.append(label)

        events = await _run_tool_batch(calls, _builtin_metadata_executor(callback))
        return events, completed

    events, completed = anyio.run(run)

    assert completed[-1] == "read-1"
    published = [
        (type(event).__name__, event.call_id)
        for event in events
        if isinstance(event, ToolExecutionEnded | ToolResultReady)
    ]
    assert published == [
        (kind, call.call_id) for call in calls for kind in ("ToolExecutionEnded", "ToolResultReady")
    ]
    assert_settled_tool_calls(events, tuple(call.call_id for call in calls))
