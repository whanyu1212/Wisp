"""Providers and tools shared by the coding-session tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from wisp.agent.messages import Message
from wisp.providers.base import (
    ToolCall,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.tools.base import (
    ToolArguments,
    ToolInputSchema,
)
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult


class CapturingProvider:
    name = "capturing"
    default_model: str | None = "default"
    supports_prompt_cache_key = True

    def __init__(self) -> None:
        self.seen_messages: Sequence[Message] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_effort: str | None = None
        self.seen_prompt_cache_keys: list[str | None] = []

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
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_effort = effort
        self.seen_prompt_cache_keys.append(prompt_cache_key)
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderTextDelta(delta="done")
        yield ProviderResponseCompleted(content="done")


class ToolLoopProvider:
    name = "tool-loop"
    default_model: str | None = "default"

    def __init__(self, turns: Sequence[Sequence[object]]) -> None:
        self.turns = list(turns)
        self.calls: list[tuple[Sequence[ToolCallResult], str | None]] = []

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
        self.calls.append((tool_results, previous_response_id))
        turn = self.turns.pop(0)
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        for item in turn:
            if isinstance(item, str):
                chunks.append(item)
                yield ProviderTextDelta(delta=item)
            elif isinstance(item, ToolCall):
                tool_calls.append(item)
                yield ProviderToolCallCompleted(
                    tool_call=item,
                    content_index=len(tool_calls) - 1,
                )
            else:
                raise TypeError(f"Unsupported test provider event: {item!r}")
        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=next(
                (call.response_id for call in reversed(tool_calls) if call.response_id),
                None,
            ),
            finish_reason="tool_calls" if tool_calls else "stop",
        )


class EchoTool:
    name = "echo"
    safety = "read"
    description = "Echo input text."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text=f"echo: {arguments['text']}")
