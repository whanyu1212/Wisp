from __future__ import annotations

import shlex
import sys
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import cast

import anyio
import pytest

import wisp.agent.loop as agent_loop_module
from wisp.agent.execution import (
    ContextOverflowSnapshot,
    PreparedToolExecution,
    RequestBoundaryDecision,
    RequestBoundarySnapshot,
    RequestBoundaryUnsupportedError,
    RequestContextRebase,
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
    ToolPreparationEvent,
)
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    BillableTokenUsage,
    ContextEstimated,
    ContextOverflow,
    ErrorEvent,
    MessageCompleted,
    MessageStarted,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
    TurnCompleted,
    UsageCost,
    UsageCostRates,
    wisp_event_from_json,
)
from wisp.providers.base import (
    ContextOverflowError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderFinishReason,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.tool_types import ToolSafety
from wisp.tools import shell as shell_module
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolExecutionMetadata
from wisp.tools.builtin import BashTool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult


class NeverToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class CacheAwareScriptedProvider(ScriptedProvider):
    """Scripted provider opting into the prompt-cache-key capability."""

    supports_prompt_cache_key = True


class OpaqueReplayScriptedProvider(ScriptedProvider):
    """Scripted provider that cannot fresh-replay opaque tool-turn state."""

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        return effort is None


class RecordingToolExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        arguments = dict(tool_call.arguments)
        yield ToolApprovalRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=arguments,
            safety="command",
        )
        yield ToolApprovalResolved(
            call_id=tool_call.call_id,
            name=tool_call.name,
            approved=True,
        )
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="tool output",
            is_error=False,
            exit_code=0,
            process_id="proc-1",
            process_state="completed",
            stdout="tool stdout\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_dropped_bytes=0,
            stderr_dropped_bytes=0,
        )


class MissingResultExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolApprovalRequested(
            call_id=tool_call.call_id,
            name=tool_call.name,
            arguments=dict(tool_call.arguments),
            safety="command",
        )


class MismatchedResultExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id="different-call",
            name=tool_call.name,
            output="wrong",
            is_error=False,
        )


class ExtraEventExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="done",
            is_error=False,
        )
        yield ToolApprovalResolved(
            call_id=tool_call.call_id,
            name=tool_call.name,
            approved=True,
        )


class ScriptedToolExecutor:
    def __init__(self, events: tuple[object, ...]) -> None:
        self.events = events

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        del tool_call
        for event in self.events:
            yield cast(ToolExecutionEvent, event)


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
    monkeypatch.setattr(agent_loop_module, "_MAX_PARALLEL_TOOL_EXECUTIONS", 2)
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


def test_agent_loop_config_preserves_legacy_positional_field_order() -> None:
    config = AgentLoopConfig(
        ScriptedProvider([]),
        NeverToolExecutor(),
        None,
        (),
        None,
        None,
        None,
        None,
        16_384,
        0.8,
        0,
        0,
        None,
        True,
    )

    assert config.defer_context_overflow_errors is True
    assert config.prompt_cache_key is None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_tool_iterations", -1, "max_tool_iterations"),
        ("max_tool_iterations", True, "max_tool_iterations"),
        ("context_window", 0, "context_window"),
        ("context_window", True, "context_window"),
        ("context_reserve_tokens", -1, "context_reserve_tokens"),
        ("context_reserve_tokens", False, "context_reserve_tokens"),
        ("context_pressure_threshold", 0, "context_pressure_threshold"),
        ("context_pressure_threshold", 1.1, "context_pressure_threshold"),
        ("context_pressure_threshold", 10**1000, "context_pressure_threshold"),
        ("context_pressure_threshold", float("nan"), "context_pressure_threshold"),
        ("context_pressure_threshold", float("inf"), "context_pressure_threshold"),
        ("turn_offset", -1, "turn_offset"),
        ("turn_offset", True, "turn_offset"),
        ("tool_iteration_offset", -1, "tool_iteration_offset"),
        ("tool_iteration_offset", False, "tool_iteration_offset"),
    ],
)
def test_agent_loop_config_rejects_invalid_runtime_limits(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentLoopConfig(
            provider=ScriptedProvider([]),
            tool_executor=NeverToolExecutor(),
            **cast(dict[str, object], {field: value}),
        )


def test_agent_loop_config_accepts_runtime_limit_boundaries() -> None:
    config = AgentLoopConfig(
        provider=ScriptedProvider([]),
        tool_executor=NeverToolExecutor(),
        max_tool_iterations=0,
        context_window=1,
        context_reserve_tokens=1,
        context_pressure_threshold=1,
        turn_offset=0,
        tool_iteration_offset=0,
    )

    assert config.max_tool_iterations == 0
    assert config.context_pressure_threshold == 1


def test_pure_loop_streams_without_application_dependencies() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(
                    content="hello",
                    usage=ProviderUsage(
                        input_tokens=12,
                        output_tokens=7,
                        total_tokens=19,
                    ),
                ),
            ]
        ]
    )
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
    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.usage is not None
    assert completed.usage.total_tokens == 19
    assert provider.calls[0].messages == messages


class RecordingRequestBoundaryHook:
    """Records each boundary snapshot and replays scripted decisions in order."""

    def __init__(self, decisions: Iterable[RequestBoundaryDecision]) -> None:
        self._decisions = deque(decisions)
        self.snapshots: list[RequestBoundarySnapshot] = []

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        self.snapshots.append(snapshot)
        if not self._decisions:
            return RequestBoundaryDecision(stop=True)
        return self._decisions.popleft()


def _completed_stream(
    content: str, *, response_id: str | None = "scripted-response"
) -> list[ProviderEvent]:
    return [
        ProviderResponseStarted(model="test", response_id=response_id),
        ProviderTextDelta(delta=content),
        ProviderResponseCompleted(content=content, response_id=response_id),
    ]


def test_request_boundary_hook_not_configured_matches_default_behavior() -> None:
    """No hook configured must produce the exact same events as before hooks existed."""

    provider = ScriptedProvider([_completed_stream("hi")])
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
            _completed_stream("no more tools"),
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
            _completed_stream("adjusted answer"),
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

    provider = ScriptedProvider([_completed_stream("first")])
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

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
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

    provider = ScriptedProvider([_completed_stream("first")])
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

    provider = ScriptedProvider([_completed_stream("first")])
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

    provider = OpaqueReplayScriptedProvider([_completed_stream("first")])
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

    provider = ScriptedProvider([_completed_stream("first")])
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

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
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
        [_completed_stream("first"), _completed_stream("second")]
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

    provider = _PromptCacheOnlyProvider([_completed_stream("first"), _completed_stream("second")])
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

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
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

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
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
        [_completed_stream("first", response_id=None), _completed_stream("second")]
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


class RaisingRequestBoundaryHook:
    """A hook whose `before_next_request` always raises."""

    async def before_next_request(
        self, *, snapshot: RequestBoundarySnapshot
    ) -> RequestBoundaryDecision:
        raise RuntimeError("boundary hook failed")


def test_request_boundary_hook_failure_does_not_double_complete_the_turn() -> None:
    """A hook failure after a clean turn must not emit a second TurnCompleted.

    Regression for #363 review: the turn's one terminal TurnCompleted(outcome=
    "completed") is yielded before the hook is invoked. If the hook then
    raises, the outer exception handler previously still saw `turn_started`
    as true and emitted a second, contradictory TurnCompleted(outcome=
    "failed") for the same turn.
    """

    provider = ScriptedProvider([_completed_stream("first")])
    hook = RaisingRequestBoundaryHook()
    messages = (Message(role="user", content="hi"),)
    collected: list[object] = []

    async def run() -> None:
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                request_boundary_hook=hook,
            ),
            messages=messages,
        ):
            collected.append(event)

    with pytest.raises(RuntimeError, match="boundary hook failed"):
        anyio.run(run)

    assert [event.type for event in collected].count("turn.completed") == 1
    assert [event.type for event in collected].count("error") == 1


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
            _completed_stream("no more tools"),
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
            _completed_stream("no more tools"),
            _completed_stream("final"),
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
            _completed_stream("no more tools"),
            _completed_stream("final"),
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
            _completed_stream("no more tools"),
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

    provider = ScriptedProvider([_completed_stream("first"), _completed_stream("second")])
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


class RaisingCancellationToken:
    """Cancellation token whose is_cancelled() raises instead of returning a bool."""

    def is_cancelled(self) -> bool:
        raise RuntimeError("boom")


def test_pure_loop_does_not_mask_cancellation_token_errors() -> None:
    """A cancellation-token failure before the first turn must not raise UnboundLocalError.

    Regression test for #359: `turn` was previously only bound inside the loop body
    after the first cancellation check, so an exception raised by that very check
    (before `turn = state.begin_turn()` executed) hit `if turn > 0:` in the outer
    `except` and raised UnboundLocalError, masking the original failure.
    """

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="boom"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=ScriptedProvider([]),
                    tool_executor=NeverToolExecutor(),
                    cancellation_token=RaisingCancellationToken(),
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == ["error"]


def test_pure_loop_does_not_complete_unstarted_turn_with_nonzero_offset() -> None:
    """A nonzero turn_offset must not make an unstarted turn look completed.

    Regression test for a follow-up to #359: using `turn > 0` to decide whether to
    emit a failure `TurnCompleted` is wrong once `turn` is pre-seeded to
    `config.turn_offset` before the loop starts. With a nonzero offset and a
    cancellation-token failure on the very first check, no `TurnStarted` is ever
    emitted for this invocation, so no matching `TurnCompleted` must be emitted
    either -- only the explicit `turn_started` flag can tell the two cases apart.
    """

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="boom"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=ScriptedProvider([]),
                    tool_executor=NeverToolExecutor(),
                    cancellation_token=RaisingCancellationToken(),
                    turn_offset=5,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events] == ["error"]


@pytest.mark.parametrize(
    ("started_id", "terminal_id", "tool_call_id"),
    [
        ("response-1", None, None),
        (None, "response-1", None),
        (None, None, "response-1"),
        ("response-1", "response-1", "response-1"),
    ],
)
def test_pure_loop_resolves_response_id_for_messages_and_tool_continuation(
    started_id: str | None,
    terminal_id: str | None,
    tool_call_id: str | None,
) -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd"},
        response_id=tool_call_id,
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id=started_id),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    response_id=terminal_id,
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)

    completed = [event for event in events if isinstance(event, MessageCompleted)]
    assert completed[0].response_id == "response-1"
    assert provider.calls[1].previous_response_id == "response-1"


@pytest.mark.parametrize(
    "provider_events",
    [
        [
            ProviderResponseStarted(model="test", response_id="started-id"),
            ProviderResponseCompleted(content="done", response_id="terminal-id"),
        ],
        [
            ProviderResponseStarted(model="test", response_id="started-id"),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-1",
                    name="bash",
                    arguments={"command": "pwd"},
                    response_id="tool-id",
                )
            ),
            ProviderResponseCompleted(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        name="bash",
                        arguments={"command": "pwd"},
                        response_id="tool-id",
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ],
        [
            ProviderResponseStarted(model="test"),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-1", name="bash", arguments={}, response_id="tool-id"
                )
            ),
            ProviderResponseCompleted(
                content="",
                tool_calls=(
                    ToolCall(call_id="call-1", name="bash", arguments={}, response_id="tool-id"),
                ),
                response_id="terminal-id",
                finish_reason="tool_calls",
            ),
        ],
        [
            ProviderResponseStarted(model="test"),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-1", name="bash", arguments={}, response_id="response-1"
                )
            ),
            ProviderToolCallCompleted(
                tool_call=ToolCall(
                    call_id="call-2", name="bash", arguments={}, response_id="response-2"
                )
            ),
            ProviderResponseCompleted(
                content="",
                tool_calls=(
                    ToolCall(call_id="call-1", name="bash", arguments={}, response_id="response-1"),
                    ToolCall(call_id="call-2", name="bash", arguments={}, response_id="response-2"),
                ),
                finish_reason="tool_calls",
            ),
        ],
    ],
)
def test_pure_loop_rejects_conflicting_provider_response_ids(
    provider_events: list[ProviderEvent],
) -> None:
    provider = ScriptedProvider([provider_events])

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ProviderProtocolError, match="conflicting response ids"):
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert not any(isinstance(event, MessageCompleted) for event in events)
    assert not any(event.type == "tool.call" for event in events)


def test_pure_loop_validates_failed_terminal_response_id() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderResponseFailed(message="upstream failed", response_id="response-1"),
            ],
            [
                ProviderResponseStarted(model="test", response_id="started-id"),
                ProviderResponseFailed(message="upstream failed", response_id="terminal-id"),
            ],
        ]
    )

    async def run_failed() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run_failed)
    assert [event.type for event in events[-3:]] == [
        "message.completed",
        "error",
        "turn.completed",
    ]

    async def run_invalid() -> None:
        with pytest.raises(ProviderProtocolError, match="conflicting response ids"):
            async for _ in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            ):
                pass

    anyio.run(run_invalid)


@pytest.mark.parametrize(
    ("finish_reason", "calls", "error"),
    [
        ("tool_calls", (), "requires at least one tool call"),
        (
            "stop",
            (ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"}),),
            "cannot include tool calls",
        ),
    ],
)
def test_pure_loop_rejects_inconsistent_finish_reason_tool_call_combinations(
    finish_reason: ProviderFinishReason,
    calls: tuple[ToolCall, ...],
    error: str,
) -> None:
    provider_events: list[ProviderEvent] = [ProviderResponseStarted(model="test")]
    provider_events.extend(ProviderToolCallCompleted(tool_call=call) for call in calls)
    provider_events.append(
        ProviderResponseCompleted(
            content="",
            tool_calls=calls,
            finish_reason=finish_reason,
        )
    )
    provider = ScriptedProvider([provider_events])
    executor = RecordingToolExecutor()
    hook = RecordingRequestBoundaryHook([])

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ProviderProtocolError, match=error):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    request_boundary_hook=hook,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert executor.calls == []
    assert hook.snapshots == []
    assert not any(isinstance(event, MessageCompleted) for event in events)
    assert not any(event.type.startswith("tool.") for event in events)


def test_pure_loop_fails_truncated_tool_batch_in_band_without_execution() -> None:
    calls = (
        ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"}),
        ToolCall(
            call_id="call-2",
            name="bash",
            arguments={"command": "echo incomplete"},
            parse_error="truncated arguments",
        ),
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="truncated-response"),
                *(ProviderToolCallCompleted(tool_call=call) for call in calls),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=calls,
                    response_id="truncated-response",
                    finish_reason="length",
                ),
            ],
            _completed_stream("recovered", response_id="recovered-response"),
        ]
    )
    hook = RecordingRequestBoundaryHook([RequestBoundaryDecision(stop=False)])

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

    completed = [event for event in events if isinstance(event, MessageCompleted)]
    assert completed[0].finish_reason == "length"
    assert [call.call_id for call in completed[0].tool_calls] == ["call-1", "call-2"]
    tool_events = [event for event in events if event.type.startswith("tool.")]
    assert [(event.type, event.call_id) for event in tool_events] == [
        ("tool.call", "call-1"),
        ("tool.execution.ended", "call-1"),
        ("tool.result", "call-1"),
        ("tool.call", "call-2"),
        ("tool.execution.ended", "call-2"),
        ("tool.result", "call-2"),
    ]
    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert all(result.is_error for result in results)
    assert all(result.failure_code == "invalid_arguments" for result in results)
    assert all(result.retryable for result in results)
    assert all(
        result.recovery_hint == "Re-issue the tool call with complete arguments."
        for result in results
    )
    assert provider.calls[1].previous_response_id == "truncated-response"
    assert provider.calls[1].tool_results == (
        ToolCallResult(
            call_id="call-1",
            output=results[0].output,
            is_error=True,
        ),
        ToolCallResult(
            call_id="call-2",
            output=results[1].output,
            is_error=True,
        ),
    )
    assert hook.snapshots[0].had_tool_calls is True
    assert [message.role for message in hook.snapshots[0].continuation_messages] == [
        "assistant",
        "tool",
        "tool",
    ]


def test_truncated_tool_batch_honors_cancellation_after_synthetic_result() -> None:
    call = ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="length",
                ),
            ]
        ]
    )

    class MutableCancellationToken:
        cancelled = False

        def is_cancelled(self) -> bool:
            return self.cancelled

    token = MutableCancellationToken()

    async def run() -> list[object]:
        events: list[object] = []
        async for event in run_agent_loop(
            AgentLoopConfig(
                provider=provider,
                tool_executor=NeverToolExecutor(),
                cancellation_token=token,
            ),
            messages=(Message(role="user", content="hi"),),
        ):
            events.append(event)
            if isinstance(event, ToolResultReady):
                token.cancelled = True
        return events

    events = anyio.run(run)

    assert [event.type for event in events[-3:]] == [
        "tool.result",
        "error",
        "turn.completed",
    ]
    terminal = cast(TurnCompleted, events[-1])
    assert terminal.outcome == "cancelled"
    assert terminal.finish_reason == "cancelled"
    assert len(provider.calls) == 1


def test_truncated_tool_batches_count_toward_the_tool_iteration_limit() -> None:
    first = ToolCall(call_id="call-1", name="read", arguments={"path": "one.txt"})
    second = ToolCall(call_id="call-2", name="read", arguments={"path": "two.txt"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=first),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(first,),
                    finish_reason="length",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=second),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(second,),
                    finish_reason="length",
                ),
            ],
        ]
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(RuntimeError, match="Maximum tool iterations exceeded: 1"):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    max_tool_iterations=1,
                ),
                messages=(Message(role="user", content="hi"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    results = [event for event in events if isinstance(event, ToolResultReady)]
    assert [event.call_id for event in results] == ["call-1"]
    assert len(provider.calls) == 2


def test_failed_response_does_not_execute_streamed_tool_calls() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseFailed(message="truncated"),
            ]
        ]
    )
    executor = RecordingToolExecutor()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)

    assert executor.calls == []
    assert not any(event.type in {"tool.call", "tool.execution.started"} for event in events)


def test_failed_response_id_does_not_leak_into_a_later_run() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="failed-response"),
                ProviderResponseFailed(message="upstream failed", response_id="failed-response"),
            ],
            [
                ProviderResponseStarted(model="test", response_id="response-2"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def fail() -> None:
        async for _ in run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
            messages=(Message(role="user", content="first"),),
        ):
            pass

    async def succeed() -> None:
        async for _ in run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
            messages=(Message(role="user", content="second"),),
        ):
            pass

    anyio.run(fail)
    anyio.run(succeed)

    assert provider.calls[1].previous_response_id is None


def test_pure_loop_recovers_empty_completion_from_streamed_text() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="complete "),
                ProviderTextDelta(delta="streamed response"),
                ProviderResponseCompleted(content=""),
            ]
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hi"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.content == "complete streamed response"


def test_pure_loop_passes_the_provider_response_model_to_cost_estimator() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="resolved-model"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, total_tokens=15),
                ),
            ]
        ]
    )
    calls: list[tuple[str, str | None, str | None]] = []

    def estimate(
        provider_name: str,
        requested_model: str | None,
        response_model: str | None,
        usage: object,
    ) -> UsageCost:
        del usage
        calls.append((provider_name, requested_model, response_model))
        return UsageCost(
            provider=provider_name,
            requested_model=requested_model,
            model=response_model,
            billable=BillableTokenUsage(
                input_tokens=10,
                cache_read_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=5,
            ),
            rates=UsageCostRates(
                input_usd_per_million=Decimal("1"),
                output_usd_per_million=Decimal("2"),
            ),
            estimated_usd=Decimal("0.00002"),
        )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    model="requested-model",
                    cost_estimator=estimate,
                ),
                messages=(Message(role="user", content="hello"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert calls == [("scripted", "requested-model", "resolved-model")]
    assert completed.cost is not None
    assert completed.cost.estimated_usd == Decimal("0.00002")


def test_pure_loop_marks_missing_usage_unpriced_without_losing_the_response() -> None:
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="model"), ProviderResponseCompleted(content="done")]]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=NeverToolExecutor()),
                messages=(Message(role="user", content="hello"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.usage is None
    assert completed.cost is not None
    assert completed.cost.unavailable_reason == "usage_incomplete"
    assert events[-1].type == "turn.completed"


def test_pure_loop_contains_cost_estimator_failures() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="model"),
                ProviderResponseCompleted(
                    content="done",
                    usage=ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                ),
            ]
        ]
    )

    def fail_estimate(*args: object) -> UsageCost:
        del args
        raise RuntimeError("pricing lookup failed")

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    cost_estimator=fail_estimate,
                ),
                messages=(Message(role="user", content="hello"),),
            )
        ]

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    assert completed.content == "done"
    assert completed.cost is not None
    assert completed.cost.unavailable_reason == "estimation_failed"
    assert not any(event.type == "error" for event in events)


class _LegacyProviderWithoutEffortParameter:
    """A `Provider` implemented against the pre-`effort` `stream()` signature.

    `Provider` is a structural `typing.Protocol` with no runtime enforcement,
    so a third-party provider written before `effort` was added is still a
    perfectly valid implementation. `run_agent_loop` must not unconditionally
    pass `effort=` to every provider on every turn, or a provider like this
    one breaks on its very first call.
    """

    name = "legacy"
    default_model: str | None = "legacy"
    supports_prompt_cache_key = False

    async def stream(
        self,
        messages: object,
        *,
        model: str | None = None,
        tools: object = (),
        tool_results: object = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[object]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderTextDelta(delta="hello")
        yield ProviderResponseCompleted(content="hello")


def test_pure_loop_does_not_break_a_provider_without_an_effort_parameter() -> None:
    # Regression test for a real Codex finding: config.effort defaults to
    # None, but the loop previously passed effort=None unconditionally on
    # every call, which raised TypeError against any Provider implemented
    # before this keyword existed.
    provider = _LegacyProviderWithoutEffortParameter()
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(  # type: ignore[arg-type]
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    prompt_cache_key="wisp:session-1",
                ),
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


def test_pure_loop_forwards_effort_to_a_provider_that_supports_it() -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    messages = (Message(role="user", content="hi"),)

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    effort="high",
                ),
                messages=messages,
            )
        ]

    anyio.run(run)

    assert provider.calls[0].effort == "high"


def test_pure_loop_forwards_executor_events_and_provider_results() -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd"},
        response_id="response-1",
    )
    provider = CacheAwareScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test", response_id="response-1"),
                ProviderThinkingDelta(delta="reasoning" * 100),
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
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )
    executor = RecordingToolExecutor()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=executor,
                    prompt_cache_key="wisp:session-1",
                ),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)

    assert executor.calls == [call]
    assert [event.type for event in events] == [
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "tool.call",
        "tool.execution.started",
        "tool.approval.requested",
        "tool.approval.resolved",
        "tool.execution.ended",
        "tool.result",
        "turn.completed",
        "turn.started",
        "context.estimated",
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == "tool output"
    assert provider.calls[1].tool_results[0].output == "tool output"
    assert provider.calls[1].previous_response_id == "response-1"
    assert [call.prompt_cache_key for call in provider.calls] == [
        "wisp:session-1",
        "wisp:session-1",
    ]
    estimates = [event for event in events if isinstance(event, ContextEstimated)]
    assert len(estimates) == 2
    assert estimates[1].budget.estimate.total_tokens > estimates[0].budget.estimate.total_tokens
    assert estimates[1].budget.estimate.message_tokens < len("reasoning" * 100) // 4
    assert all("reasoning" not in message.content for message in provider.calls[1].messages)
    # The promoted exit_code reaches the event AND crosses the wire: the TUI
    # renderer only sees events after they are serialized (agent subprocess →
    # JSON → client), so the presentation signal must survive round-tripping.
    assert result.exit_code == 0
    assert result.process_id == "proc-1"
    assert result.process_state == "completed"
    assert result.stdout == "tool stdout\n"
    result_round_tripped = wisp_event_from_json(result.model_dump_json())
    assert isinstance(result_round_tripped, ToolResultReady)
    assert result_round_tripped.exit_code == 0
    assert result_round_tripped.process_id == "proc-1"
    assert result_round_tripped.process_state == "completed"
    assert result_round_tripped.stdout == "tool stdout\n"
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.exit_code == 0
    ended_round_tripped = wisp_event_from_json(ended.model_dump_json())
    assert isinstance(ended_round_tripped, ToolExecutionEnded)
    assert ended_round_tripped.exit_code == 0
    assert ended_round_tripped.process_id == "proc-1"
    assert ended_round_tripped.process_state == "completed"
    assert ended_round_tripped.stdout == "tool stdout\n"


def test_tool_result_projection_preserves_the_complete_wire_payload() -> None:
    ended = ToolExecutionEnded(
        call_id="call-1",
        name="bash",
        output="Command exited with code 2: output",
        is_error=True,
        exit_code=2,
        output_has_exit_status=True,
        before_text="before\n",
        created=True,
        summary="summary",
        truncated=True,
        process_id="proc-1",
        process_state="failed",
        process_error="process failed",
        stdout="stdout\n",
        stderr="stderr\n",
        stdout_truncated=True,
        stderr_truncated=True,
        stdout_dropped_bytes=11,
        stderr_dropped_bytes=12,
    )

    result = ToolResultReady.from_execution_ended(ended)
    expected_payload = {
        "call_id": "call-1",
        "name": "bash",
        "output": "Command exited with code 2: output",
        "is_error": True,
        "failure_code": None,
        "retryable": False,
        "recovery_hint": None,
        "exit_code": 2,
        "output_has_exit_status": True,
        "before_text": "before\n",
        "created": True,
        "summary": "summary",
        "truncated": True,
        "process_id": "proc-1",
        "process_state": "failed",
        "process_error": "process failed",
        "stdout": "stdout\n",
        "stderr": "stderr\n",
        "stdout_truncated": True,
        "stderr_truncated": True,
        "stdout_dropped_bytes": 11,
        "stderr_dropped_bytes": 12,
    }

    envelope_fields = {"type", "schema_version", "timestamp"}
    assert ended.model_dump(exclude=envelope_fields) == expected_payload
    assert result.model_dump(exclude=envelope_fields) == expected_payload
    assert result.type == "tool.result"
    assert result.timestamp >= ended.timestamp
    assert wisp_event_from_json(ended.model_dump_json()) == ended
    assert wisp_event_from_json(result.model_dump_json()) == result


def test_completion_and_continuation_snapshot_projection_is_single_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd", "options": {"cwd": "before"}},
        parse_error="example parse error",
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="checking",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )

    estimated_messages: list[tuple[Message, ...]] = []
    original_estimate_context = agent_loop_module.estimate_context

    def capture_estimate(messages: tuple[Message, ...], tools: tuple[ToolSpec, ...]) -> object:
        estimated_messages.append(messages)
        return original_estimate_context(messages, tools)

    monkeypatch.setattr(agent_loop_module, "estimate_context", capture_estimate)

    async def run() -> list[object]:
        events: list[object] = []
        loop = run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
            messages=(Message(role="user", content="run pwd"),),
        )
        async for event in loop:
            events.append(event)
            if isinstance(event, MessageCompleted) and event.tool_calls:
                event.tool_calls[0].arguments["command"] = "malicious"
                options = cast(dict[str, object], event.tool_calls[0].arguments["options"])
                options["cwd"] = "after"
        return events

    events = anyio.run(run)

    completed = next(event for event in events if isinstance(event, MessageCompleted))
    expected_snapshot = ToolCallSnapshot(
        call_id="call-1",
        name="bash",
        arguments={"command": "malicious", "options": {"cwd": "after"}},
        parse_error="example parse error",
    )
    assert completed.tool_calls == (expected_snapshot,)
    continuation = next(message for message in estimated_messages[1] if message.role == "assistant")
    assert continuation.tool_calls == (
        ToolCallSnapshot(
            call_id="call-1",
            name="bash",
            arguments={"command": "pwd", "options": {"cwd": "before"}},
            parse_error="example parse error",
        ),
    )


def test_execution_end_immediately_precedes_projected_tool_result() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=RecordingToolExecutor()),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    events = anyio.run(run)
    ended_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolExecutionEnded)
    )
    ended = events[ended_index]
    result = events[ended_index + 1]

    assert isinstance(ended, ToolExecutionEnded)
    assert isinstance(result, ToolResultReady)
    envelope_fields = {"type", "schema_version", "timestamp"}
    assert result.model_dump(exclude=envelope_fields) == ended.model_dump(exclude=envelope_fields)


def _run_bash_loop(
    tmp_path: Path,
    *,
    command: str,
    timeout: int | None = None,
    bash_tool: BashTool | None = None,
) -> tuple[ScriptedProvider, list[object]]:
    arguments: dict[str, object] = {"command": command}
    if timeout is not None:
        arguments["timeout"] = timeout
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments=arguments,
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )
    registry = ToolRegistry()
    registry.register(bash_tool or BashTool())
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run verification"),),
            )
        ]

    return provider, anyio.run(run)


@pytest.mark.parametrize("exit_code", [0, 3])
def test_pure_loop_exposes_bash_exit_code_to_provider(
    tmp_path: Path,
    exit_code: int,
) -> None:
    python = shlex.quote(sys.executable)
    command = f"{python} -c \"import sys; print('evidence'); sys.exit({exit_code})\""
    provider, events = _run_bash_loop(tmp_path, command=command)

    expected = f"Command exited with code {exit_code}: evidence"
    assert provider.calls[1].tool_results[0].output == expected
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == expected
    assert result.exit_code == exit_code
    assert result.output_has_exit_status is True


def test_pure_loop_exposes_bash_timeout_as_inconclusive_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def time_out(*_args: object, **_kwargs: object) -> object:
        raise ToolError(
            "Command timed out after 30 seconds",
            failure_code="timeout",
            retryable=True,
            recovery_hint="The result is inconclusive; retry with a suitable timeout.",
        )

    monkeypatch.setattr(shell_module, "_run_shell", time_out)

    provider, events = _run_bash_loop(
        tmp_path,
        command="slow check",
        timeout=30,
        bash_tool=BashTool(None),
    )

    tool_result = provider.calls[1].tool_results[0]
    assert tool_result.output.startswith("Command timed out after 30 seconds")
    assert "Recovery:" in tool_result.output
    assert tool_result.is_error is True
    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.output == tool_result.output
    assert result.is_error is True
    assert result.failure_code == "timeout"
    assert result.retryable is True
    assert result.exit_code is None


def test_pure_loop_rejects_executor_with_unresolved_approval() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(ToolExecutionProtocolError, match="unresolved approval"):
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=MissingResultExecutor()),
                messages=(Message(role="user", content="run pwd"),),
            ):
                events.append(event)
        return events

    events = anyio.run(run)

    assert [event.type for event in events[-2:]] == ["error", "turn.completed"]
    assert events[-1].outcome == "failed"


@pytest.mark.parametrize(
    ("executor", "error"),
    [
        (MismatchedResultExecutor(), "does not match the requested call"),
        (ExtraEventExecutor(), "emitted an event after the result"),
    ],
)
def test_pure_loop_rejects_malformed_terminal_results(
    executor: ToolExecutor,
    error: str,
) -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    async def run() -> None:
        with pytest.raises(ToolExecutionProtocolError, match=error):
            async for _ in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            ):
                pass

    anyio.run(run)


def _approval_request(*, arguments: dict[str, object] | None = None) -> ToolApprovalRequested:
    return ToolApprovalRequested(
        call_id="call-1",
        name="bash",
        arguments=arguments or {"command": "pwd"},
        safety="command",
    )


def _approval_resolution(*, approved: bool = True) -> ToolApprovalResolved:
    return ToolApprovalResolved(
        call_id="call-1",
        name="bash",
        approved=approved,
    )


def _terminal_result(*, is_error: bool = False) -> ToolExecutionEnded:
    return ToolExecutionEnded(
        call_id="call-1",
        name="bash",
        output="denied" if is_error else "done",
        is_error=is_error,
    )


@pytest.mark.parametrize(
    ("events", "error"),
    [
        (
            (),
            "ended without a result",
        ),
        (
            (_approval_resolution(), _terminal_result()),
            "resolved approval before requesting it",
        ),
        (
            (_approval_request(), _approval_request(), _terminal_result()),
            "requested approval more than once",
        ),
        (
            (
                _approval_request(),
                _approval_resolution(),
                _approval_resolution(),
                _terminal_result(),
            ),
            "resolved approval more than once",
        ),
        (
            (_approval_request(),),
            "ended with an unresolved approval",
        ),
        (
            (_approval_request(), _terminal_result()),
            "ended with an unresolved approval",
        ),
        (
            (_approval_request(arguments={"command": "whoami"}), _terminal_result()),
            "approval arguments do not match",
        ),
        (
            (_approval_request(), _approval_resolution(approved=False), _terminal_result()),
            "reported success after approval was denied",
        ),
        (
            (object(),),
            "unsupported event type",
        ),
    ],
)
def test_pure_loop_rejects_malformed_approval_lifecycle(
    events: tuple[object, ...],
    error: str,
) -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )

    async def run() -> list[object]:
        emitted: list[object] = []
        with pytest.raises(ToolExecutionProtocolError, match=error):
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=ScriptedToolExecutor(events),
                ),
                messages=(Message(role="user", content="run pwd"),),
            ):
                emitted.append(event)
        return emitted

    emitted = anyio.run(run)

    assert not any(isinstance(event, ToolResultReady) for event in emitted)
    assert [event.type for event in emitted[-2:]] == ["error", "turn.completed"]
    assert emitted[-1].outcome == "failed"
    assert len(provider.calls) == 1


def test_pure_loop_rejects_type_changing_nested_approval_arguments() -> None:
    call = ToolCall(
        call_id="call-1",
        name="bash",
        arguments={"command": "pwd", "options": [1]},
    )
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ]
        ]
    )
    executor = ScriptedToolExecutor(
        (
            _approval_request(arguments={"command": "pwd", "options": [True]}),
            _terminal_result(),
        )
    )

    async def run() -> None:
        with pytest.raises(ToolExecutionProtocolError, match="approval arguments do not match"):
            async for _ in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            ):
                pass

    anyio.run(run)


def test_pure_loop_accepts_denied_approval_with_error_result() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderToolCallCompleted(tool_call=call),
                ProviderResponseCompleted(
                    content="",
                    tool_calls=(call,),
                    finish_reason="tool_calls",
                ),
            ],
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="noted"),
            ],
        ]
    )
    executor = ScriptedToolExecutor(
        (
            _approval_request(),
            _approval_resolution(approved=False),
            _terminal_result(is_error=True),
        )
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=executor),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    emitted = anyio.run(run)

    result = next(event for event in emitted if isinstance(event, ToolResultReady))
    assert result.is_error is True
    assert provider.calls[1].tool_results == (
        ToolCallResult(call_id="call-1", output="denied", is_error=True),
    )


class WriteSnapshotExecutor:
    """Emits a terminal result carrying a pre-write snapshot, like the write tool."""

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="Wrote 4 bytes to f.txt",
            is_error=False,
            before_text="old\n",
            created=False,
        )


def test_pure_loop_forwards_before_text_across_the_wire() -> None:
    # The write tool's pre-write snapshot AND its create flag must reach
    # ToolResultReady AND survive serialization: the TUI renderer only sees events
    # after the agent subprocess serializes them to JSON, so a field that doesn't
    # round-trip renders no diff — the exact failure that retired the opaque `data`
    # field. created rides alongside before_text to disambiguate a None snapshot.
    call = ToolCall(
        call_id="call-1",
        name="write",
        arguments={"path": "f.txt", "content": "new\n"},
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
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=WriteSnapshotExecutor()),
                messages=(Message(role="user", content="write f.txt"),),
            )
        ]

    events = anyio.run(run)

    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.before_text == "old\n"
    assert result.created is False
    round_tripped = wisp_event_from_json(result.model_dump_json())
    assert round_tripped.before_text == "old\n"
    assert round_tripped.created is False
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.before_text == "old\n"
    assert ended.created is False
    assert wisp_event_from_json(ended.model_dump_json()).before_text == "old\n"


class SummaryExecutor:
    """Emits a terminal result carrying a one-line summary, like a read-type tool."""

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output="line 1\nline 2\nline 3\n",
            is_error=False,
            summary="read 3 lines from f.txt",
            truncated=True,
        )


def test_pure_loop_forwards_summary_across_the_wire() -> None:
    # A read-type tool's one-line summary AND its truncation flag must reach
    # ToolResultReady AND survive serialization — the renderer shows the summary in
    # place of the raw output, and the card shows a "truncated" marker on expand, so a
    # field that doesn't round-trip would silently drop either signal.
    call = ToolCall(
        call_id="call-1",
        name="read",
        arguments={"path": "f.txt"},
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
                ProviderTextDelta(delta="done"),
                ProviderResponseCompleted(content="done", response_id="response-2"),
            ],
        ]
    )

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(provider=provider, tool_executor=SummaryExecutor()),
                messages=(Message(role="user", content="read f.txt"),),
            )
        ]

    events = anyio.run(run)

    result = next(event for event in events if isinstance(event, ToolResultReady))
    assert result.summary == "read 3 lines from f.txt"
    assert result.truncated is True
    round_tripped = wisp_event_from_json(result.model_dump_json())
    assert round_tripped.summary == "read 3 lines from f.txt"
    assert round_tripped.truncated is True
    ended = next(event for event in events if isinstance(event, ToolExecutionEnded))
    assert ended.summary == "read 3 lines from f.txt"
    assert ended.truncated is True
    round_tripped_ended = wisp_event_from_json(ended.model_dump_json())
    assert round_tripped_ended.summary == "read 3 lines from f.txt"
    assert round_tripped_ended.truncated is True


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
            _completed_stream("done", response_id="final-response"),
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
            _completed_stream("clean", response_id="clean-response"),
            _completed_stream("followed up", response_id="follow-up-response"),
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

    provider = ScriptedProvider([_completed_stream("first")])

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


def test_raised_context_overflow_closes_started_message_before_retry() -> None:
    """A raised overflow must settle the public response before a retry turn."""

    class StartedOverflowProvider:
        name = "started-overflow"
        default_model = "test"

        def __init__(self) -> None:
            self.calls = 0

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
            self.calls += 1
            yield ProviderResponseStarted(
                model="test",
                response_id="failed-response" if self.calls == 1 else "recovered-response",
            )
            if self.calls == 1:
                raise ContextOverflowError("maximum context length exceeded")
            yield ProviderResponseCompleted(
                content="recovered",
                response_id="recovered-response",
            )

    class RecoverOverflow:
        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision:
            del snapshot
            return RequestBoundaryDecision(
                messages=(Message(role="user", content="compacted summary"),)
            )

    provider = StartedOverflowProvider()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    context_overflow_hook=RecoverOverflow(),
                ),
                messages=(Message(role="user", content="long prompt"),),
            )
        ]

    events = anyio.run(run)

    lifecycle_events = [
        event for event in events if isinstance(event, MessageStarted | MessageCompleted)
    ]
    assert [type(event) for event in lifecycle_events] == [
        MessageStarted,
        MessageCompleted,
        MessageStarted,
        MessageCompleted,
    ]
    failed_completion = lifecycle_events[1]
    assert isinstance(failed_completion, MessageCompleted)
    assert failed_completion.finish_reason == "error"
    assert failed_completion.response_id == "failed-response"
    assert [event.turn for event in events if isinstance(event, TurnCompleted)] == [1, 2]
    assert provider.calls == 2


def test_context_overflow_hook_retries_in_the_same_loop() -> None:
    """A hook may replace the failed request without constructing another loop."""

    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseFailed(
                    message="context window exceeded",
                    failure_kind="context_overflow",
                ),
            ],
            _completed_stream("recovered", response_id="recovered-response"),
        ]
    )

    class RecoverOverflow:
        def __init__(self) -> None:
            self.snapshots: list[ContextOverflowSnapshot] = []

        async def recover_context_overflow(
            self, *, snapshot: ContextOverflowSnapshot
        ) -> RequestBoundaryDecision | None:
            self.snapshots.append(snapshot)
            return RequestBoundaryDecision(
                messages=(Message(role="user", content="compacted summary"),)
            )

    hook = RecoverOverflow()

    async def run() -> list[object]:
        return [
            event
            async for event in run_agent_loop(
                AgentLoopConfig(
                    provider=provider,
                    tool_executor=NeverToolExecutor(),
                    context_window=100,
                    context_overflow_hook=hook,
                ),
                messages=(Message(role="user", content="long prompt"),),
            )
        ]

    events = anyio.run(run)

    assert [event.turn for event in events if isinstance(event, TurnCompleted)] == [1, 2]
    assert any(isinstance(event, ContextOverflow) for event in events)
    assert hook.snapshots[0].had_streamed_delta is False
    assert len(provider.calls) == 2
    assert [(message.role, message.content) for message in provider.calls[1].messages] == [
        ("user", "compacted summary")
    ]
    assert provider.calls[1].previous_response_id is None
    assert provider.calls[1].tool_results == ()
    assert provider.calls[1].extra_messages == ()
