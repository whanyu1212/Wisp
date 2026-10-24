"""Executors, harness builders, and message fixtures shared by the harness tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal

import anyio

from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.agent.tool_contracts import (
    PreparedToolExecution,
    ToolExecutionEvent,
    ToolExecutor,
    ToolPreparationEvent,
)
from wisp.events import (
    ToolCallSnapshot,
    ToolExecutionEnded,
)
from wisp.messages import Message
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


class BlockingPreparedExecutor:
    def __init__(self, all_started: anyio.Event) -> None:
        self._all_started = all_started
        self.started_call_ids: set[str] = set()

    async def prepare(self, tool_call: ToolCall) -> AsyncIterator[ToolPreparationEvent]:
        async def run() -> ToolExecutionEnded:
            self.started_call_ids.add(tool_call.call_id)
            if len(self.started_call_ids) == 2:
                self._all_started.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

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
