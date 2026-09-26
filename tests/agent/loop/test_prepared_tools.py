from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import anyio
import pytest

import wisp.agent.loop.prepared_tools as prepared_tools_module
from tests.support.agent_runtime import (
    assert_continuation_invariants,
    assert_settled_tool_calls,
    assert_turn_terminals,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolPreparationEvent,
)
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    ErrorEvent,
    ToolCallRequested,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.tool_types import ToolSafety
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolExecutionMetadata
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult


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
        *,
        parallel_safe: Mapping[str, bool] | None = None,
    ) -> None:
        self._runner = runner
        self._parallel_safe = parallel_safe or {}

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            return await self._runner(tool_call)

        yield PreparedToolExecution(
            call_id=tool_call.call_id,
            name=tool_call.name,
            parallel_safe=self._parallel_safe.get(tool_call.call_id, True),
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


def test_prepared_tool_batch_overlaps_execution_and_publishes_source_order() -> None:
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


def test_configured_parallel_batch_isolates_tool_owned_failure() -> None:
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
def test_prepared_cancellation_distinguishes_tool_results_from_settlement(
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


def test_prepared_failure_matches_later_repeated_call_occurrence() -> None:
    calls = (
        ToolCall(call_id="repeat", name="lookup", arguments={"n": 1}),
        ToolCall(call_id="middle", name="lookup", arguments={"n": 2}),
        ToolCall(call_id="repeat", name="lookup", arguments={"n": 3}),
    )

    async def runner(call: ToolCall) -> ToolExecutionEnded:
        if call.arguments["n"] == 1:
            raise RuntimeError("first occurrence failed")
        return ToolExecutionEnded(
            call_id=call.call_id, name=call.name, output=str(call.arguments["n"]), is_error=False
        )

    async def run() -> list[object]:
        events: list[object] = []
        with anyio.fail_after(3), pytest.raises(RuntimeError, match="first occurrence failed"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=_scripted_tool_batch_provider(calls),
                    tool_executor=PreparedScriptExecutor(runner),
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)
    assert [
        (event.call_id, event.output) for event in events if isinstance(event, ToolExecutionEnded)
    ] == [("middle", "2"), ("repeat", "3")]
    assert_continuation_invariants(events)


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
def test_prepared_batch_propagates_an_unrequested_scope_cancellation(
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
def test_prepared_batch_propagates_native_asyncio_task_cancellation(
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
def test_prepared_batch_settles_a_cancellation_requested_by_the_harness(
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


def test_prepared_cancellation_skips_already_settled_duplicate_snapshot() -> None:
    class Token:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    token = Token()
    calls = (
        ToolCall(call_id="repeat", name="lookup", arguments={"n": 1}),
        ToolCall(call_id="repeat", name="lookup", arguments={"n": 2}),
        ToolCall(call_id="middle", name="lookup", arguments={"n": 3}),
    )

    async def runner(call: ToolCall) -> ToolExecutionEnded:
        raise AssertionError(f"Unexpected execution: {call.call_id}")

    async def run() -> list[object]:
        events: list[object] = []
        with anyio.fail_after(3):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=_scripted_tool_batch_provider(calls),
                    tool_executor=PreparedScriptExecutor(runner),
                    cancellation_token=token,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
                if isinstance(event, ToolCallRequested):
                    token.cancelled = True
        return events

    events = anyio.run(run)
    assert [event.call_id for event in events if isinstance(event, ToolCallRequested)] == [
        "repeat",
        "middle",
    ]
    assert_continuation_invariants(events)


def test_prepared_tool_batch_does_not_start_after_cooperative_cancellation() -> None:
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


def test_prepared_tool_batch_enforces_bounded_live_tasks(
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


def test_prepared_tool_batch_publishes_sibling_results_before_fatal_error() -> None:
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


def test_prepared_tool_batch_publishes_sibling_after_malformed_terminal() -> None:
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


def test_prepared_tool_batch_with_sequential_call_runs_entire_batch_serially() -> None:
    calls = tuple(
        ToolCall(call_id=f"call-{index}", name="tool", arguments={}) for index in range(1, 4)
    )
    provider = _scripted_tool_batch_provider(calls)

    async def run() -> tuple[list[str], int]:
        active = 0
        max_active = 0
        started: list[str] = []

        async def runner(tool_call: ToolCall) -> ToolExecutionEnded:
            nonlocal active, max_active
            started.append(tool_call.call_id)
            active += 1
            max_active = max(max_active, active)
            await anyio.sleep(0)
            active -= 1
            return ToolExecutionEnded(
                call_id=tool_call.call_id,
                name=tool_call.name,
                output="done",
                is_error=False,
            )

        executor = PreparedScriptExecutor(
            runner,
            parallel_safe={"call-2": False},
        )
        _ = [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="hi"),),
            )
        ]
        return started, max_active

    started, max_active = anyio.run(run)

    assert started == ["call-1", "call-2", "call-3"]
    assert max_active == 1
