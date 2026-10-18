"""Stub clients and Messages stream events shared by the Anthropic provider tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from anthropic.types import (
    InputJSONDelta,
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_content_block_start_event import ContentBlock
from anthropic.types.raw_message_delta_event import Delta

from wisp.messages import Message as WispMessage
from wisp.providers.anthropic import AnthropicProvider
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)


class StubAnthropicProvider(AnthropicProvider):
    def __init__(self, events: Sequence[RawMessageStreamEvent]) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.events = events
        self.seen_model: str | None = None
        self.seen_messages: Sequence[WispMessage] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_tool_results: Sequence[ToolCallResult] | None = None
        self.seen_effort: str | None = None

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        web_search: bool = False,
        paused_turn: object = None,
    ) -> AsyncIterator[RawMessageStreamEvent]:
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results
        self.seen_effort = effort

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            for event in self.events:
                yield event

        return stream()


class StubAsyncAnthropic:
    def __init__(self, messages: StubMessagesResource) -> None:
        self.messages = messages


class StubMessagesResource:
    def __init__(self, responses: Sequence[Sequence[RawMessageStreamEvent]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) if responses is not None else None

    async def create(self, **kwargs: object) -> AsyncIterator[RawMessageStreamEvent]:
        self.calls.append(dict(kwargs))
        events = self._responses.pop(0) if self._responses else ()

        async def stream() -> AsyncIterator[RawMessageStreamEvent]:
            for event in events:
                yield event

        return stream()


def message_start_event(response_id: str) -> RawMessageStartEvent:
    return RawMessageStartEvent(
        message=Message(
            id=response_id,
            content=[],
            model="claude-test",
            role="assistant",
            type="message",
            usage=Usage(input_tokens=1, output_tokens=0),
        ),
        type="message_start",
    )


def text_delta_event(text: str, *, index: int = 0) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=TextDelta(text=text, type="text_delta"),
        index=index,
        type="content_block_delta",
    )


def thinking_delta_event(text: str, *, index: int = 0) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=ThinkingDelta(thinking=text, type="thinking_delta"),
        index=index,
        type="content_block_delta",
    )


def tool_use_start_event(call_id: str, name: str, *, index: int) -> RawContentBlockStartEvent:
    content_block = cast(
        ContentBlock,
        ToolUseBlock(id=call_id, input={}, name=name, type="tool_use"),
    )
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )


def input_json_delta_event(partial_json: str, *, index: int) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=InputJSONDelta(partial_json=partial_json, type="input_json_delta"),
        index=index,
        type="content_block_delta",
    )


def message_delta_event(stop_reason: str) -> RawMessageDeltaEvent:
    return RawMessageDeltaEvent(
        delta=Delta.model_construct(stop_reason=stop_reason),
        type="message_delta",
        usage=MessageDeltaUsage(output_tokens=1),
    )
