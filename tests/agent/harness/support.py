"""Executors, harness builders, and message fixtures shared by the harness tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal

from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.messages import Message
from wisp.agent.tool_contracts import (
    ToolExecutionEvent,
    ToolExecutor,
)
from wisp.events import (
    ToolCallSnapshot,
    ToolExecutionEnded,
)
from wisp.providers.base import (
    Provider,
    ToolSpec,
)
from wisp.providers.events import (
    ToolCall,
)


class RecordingToolExecutor:
    def __init__(self, output: str = "tool output", *, is_error: bool = False) -> None:
        self.output = output
        self.is_error = is_error
        self.calls: list[ToolCall] = []

    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        self.calls.append(tool_call)
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=self.output,
            is_error=self.is_error,
        )


def build_harness(
    provider: Provider,
    *,
    executor: ToolExecutor | None = None,
    messages: Sequence[Message] = (),
    tools: tuple[ToolSpec, ...] = (),
) -> AgentHarness:
    return AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=executor or RecordingToolExecutor(),
            tools=tools,
        ),
        messages=messages,
    )


def message_with_nested_arguments(*, role: Literal["user", "assistant"]) -> Message:
    return Message(
        role=role,
        content="original",
        tool_calls=(
            ToolCallSnapshot(
                call_id="nested",
                name="read",
                arguments={"paths": ["original.txt"]},
                provider_call_id="native-call",
            ),
        ),
    )


def append_nested_path(message: Message) -> None:
    assert message.tool_calls is not None
    paths = message.tool_calls[0].arguments["paths"]
    assert isinstance(paths, list)
    paths.append("changed.txt")
