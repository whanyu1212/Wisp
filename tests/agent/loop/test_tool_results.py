from __future__ import annotations

import shlex
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import anyio
import pytest

import wisp.agent.loop.runner as agent_loop_module
from tests.agent.loop.support import (
    RecordingToolExecutor,
)
from wisp.agent.loop import AgentLoopConfig, run_agent_loop
from wisp.agent.messages import Message
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
from wisp.providers.base import (
    ToolCallResult,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
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


def test_tool_result_projection_preserves_the_complete_wire_payload() -> None:
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

    original_estimate_context_budget = agent_loop_module.estimate_context_budget
    budget_spy = Mock(wraps=original_estimate_context_budget)
    monkeypatch.setattr(agent_loop_module, "estimate_context_budget", budget_spy)

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
