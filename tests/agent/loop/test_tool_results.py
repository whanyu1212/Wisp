"""Tool results through run_agent_loop: validation, projection, and provider payloads."""

from __future__ import annotations

import shlex
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import anyio
import pytest

import wisp.agent.loop.runner as agent_loop_module
from tests.agent.loop.support import (
    ApprovingToolExecutor,
    FakeClock,
    install_fake_clock,
    tool_call_stream,
)
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.loop.tool_lifecycle import ToolBatchSettlement
from wisp.agent.tool_contracts import (
    ToolExecutionEvent,
    ToolExecutionProtocolError,
    ToolExecutor,
)
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import (
    MessageCompleted,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolCallSnapshot,
    ToolExecutionEnded,
    ToolResultReady,
    wisp_event_from_json,
)
from wisp.messages import Message
from wisp.providers.base import (
    ToolCallResult,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.builtin import BashTool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError
from wisp.tools.shell import tool as shell_module


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


def test_projection_keeps_complete_wire_payload() -> None:
    ended = ToolExecutionEnded(
        message_entry_id="persisted-result",
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
        duration_ms=1250,
    )

    result = ToolResultReady.from_execution_ended(ended)
    expected_payload = {
        "message_entry_id": "persisted-result",
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
        "duration_ms": 1250,
        "process_id": "proc-1",
        "process_state": "failed",
        "process_error": "process failed",
        "stdout": "stdout\n",
        "stderr": "stderr\n",
        "stdout_truncated": True,
        "stderr_truncated": True,
        "stdout_dropped_bytes": 11,
        "stderr_dropped_bytes": 12,
        "started_at": None,
        "process_started_at": None,
        "process_ended_at": None,
    }

    envelope_fields = {"type", "schema_version", "timestamp"}
    assert ended.model_dump(exclude=envelope_fields) == expected_payload
    assert result.model_dump(exclude=envelope_fields) == expected_payload
    assert result.type == "tool.result"
    assert result.timestamp >= ended.timestamp
    assert wisp_event_from_json(ended.model_dump_json()) == ended
    assert wisp_event_from_json(result.model_dump_json()) == result


def test_snapshots_isolated_from_consumer_mutation(
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
            tool_call_stream(call, content="checking"),
            [
                ProviderResponseStarted(model="test"),
                ProviderResponseCompleted(content="done"),
            ],
        ]
    )

    original_estimate_context_budget = agent_loop_module.estimate_context_budget
    budget_spy = Mock(wraps=original_estimate_context_budget)
    monkeypatch.setattr(agent_loop_module, "estimate_context_budget", budget_spy)

    async def run() -> list[object]:
        events: list[object] = []
        loop = run_agent_loop(
            AgentLoopConfig(provider=provider, tool_executor=ApprovingToolExecutor()),
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
    estimated_messages = [tuple(call.args[0]) for call in budget_spy.call_args_list]
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
            tool_call_stream(call),
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
def test_bash_exit_code_reaches_provider(
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


def test_bash_timeout_reports_inconclusive_error(
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


@pytest.mark.parametrize(
    ("executor", "error"),
    [
        (MismatchedResultExecutor(), "does not match the requested call"),
        (ExtraEventExecutor(), "emitted an event after the result"),
    ],
)
def test_rejects_malformed_tool_results(
    executor: ToolExecutor,
    error: str,
) -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider([tool_call_stream(call)])

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
            (_approval_request(),),
            "unresolved approval",
        ),
        (
            (_approval_resolution(), _terminal_result()),
            "resolved approval before requesting it",
        ),
    ],
    ids=["no-events", "unresolved-approval", "resolution-before-request"],
)
def test_rejects_malformed_approval_lifecycle(
    events: tuple[object, ...],
    error: str,
) -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider([tool_call_stream(call)])

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


def test_accepts_error_after_denied_approval() -> None:
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            tool_call_stream(call),
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


class SlowApprovalExecutor:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        del tool_call
        yield _approval_request()
        self.clock.advance(0.3)  # The user takes a while to decide.
        yield _approval_resolution()
        self.clock.advance(0.05)
        yield _terminal_result()


def test_duration_skips_approval_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = install_fake_clock(monkeypatch)
    call = ToolCall(call_id="call-1", name="bash", arguments={"command": "pwd"})
    provider = ScriptedProvider(
        [
            tool_call_stream(call),
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
                AgentLoopConfig(provider=provider, tool_executor=SlowApprovalExecutor(clock)),
                messages=(Message(role="user", content="run pwd"),),
            )
        ]

    emitted = anyio.run(run)

    ended = next(event for event in emitted if isinstance(event, ToolExecutionEnded))
    result = next(event for event in emitted if isinstance(event, ToolResultReady))
    # Timing starts at the approval, not when the executor was first called.
    assert ended.duration_ms == result.duration_ms == 50
    assert ended.started_at is not None
    assert result.started_at == ended.started_at
    assert result.timestamp - ended.started_at >= timedelta(milliseconds=50)


def test_interrupted_results_have_no_duration() -> None:
    calls = (
        ToolCall(call_id="a", name="read", arguments={}),
        ToolCall(call_id="b", name="read", arguments={}),
    )
    settlement = ToolBatchSettlement(calls)
    settlement.request(calls[0])

    settled = list(settlement.settle_unfinished())

    results = [event for event in settled if isinstance(event, ToolResultReady)]
    assert [result.call_id for result in results] == ["a", "b"]
    assert all(result.duration_ms is None for result in results)
