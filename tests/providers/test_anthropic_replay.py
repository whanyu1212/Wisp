from __future__ import annotations

from typing import cast

import anyio
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import (
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RedactedThinkingBlock,
    SignatureDelta,
    TextBlock,
    ThinkingBlock,
)
from anthropic.types.raw_content_block_start_event import ContentBlock

from tests.providers.anthropic_support import (
    StubAsyncAnthropic,
    StubMessagesResource,
    input_json_delta_event,
    message_delta_event,
    message_start_event,
    text_delta_event,
    thinking_delta_event,
    tool_use_start_event,
)
from wisp.agent.messages import Message as WispMessage
from wisp.events import ToolCallSnapshot
from wisp.providers.anthropic import AnthropicProvider
from wisp.providers.base import (
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import ProviderResponseCompleted


def test_anthropic_provider_replays_tool_use_turn_before_tool_results() -> None:
    # Regression test: Anthropic's Messages API requires the assistant's
    # tool_use turn to immediately precede the matching tool_result -- but
    # AgentHarness's provider-neutral loop only ever passes the *new*
    # tool_results on a follow-up call, assuming a Responses-API-style
    # backend that remembers the prior turn server-side (previous_response_id
    # is otherwise unused, per stream()'s docstring). Without a replay, the
    # second request would 400 against the real API.
    messages_resource = StubMessagesResource(
        responses=[
            [
                message_start_event("response-id"),
                tool_use_start_event("call-id", "lookup", index=0),
                input_json_delta_event('{"query": "wisp"}', index=0),
                message_delta_event("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> tuple[list[object], str | None]:
        first_events = [
            event async for event in provider.stream(messages, model="claude-test", tools=[tool])
        ]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        second_events = [
            event
            async for event in provider.stream(
                messages,
                model="claude-test",
                tools=[tool],
                tool_results=[ToolCallResult(call_id="call-id", output="found it")],
                previous_response_id=completed.response_id,
            )
        ]
        return second_events, completed.response_id

    anyio.run(run)

    assert len(messages_resource.calls) == 2
    second_call_messages = messages_resource.calls[1]["messages"]
    assert second_call_messages == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-id", "name": "lookup", "input": {"query": "wisp"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-id",
                    "content": "found it",
                    "is_error": False,
                }
            ],
        },
    ]


def test_anthropic_provider_serializes_active_tool_exchange_in_fresh_context() -> None:
    messages_resource = StubMessagesResource(responses=[[]])
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [
        WispMessage(role="user", content="search"),
        WispMessage(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"query": "wisp"}),
                ToolCallSnapshot(call_id="call-2", name="read", arguments={"path": "README.md"}),
            ),
        ),
        WispMessage(role="tool", content="found it", tool_call_id="call-1", tool_name="lookup"),
        WispMessage(
            role="tool",
            content="missing",
            tool_call_id="call-2",
            tool_name="read",
            is_error=True,
        ),
    ]

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            messages,
            model="claude-test",
            extra_messages=[WispMessage(role="user", content="steered")],
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls[0]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "search"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "lookup",
                    "input": {"query": "wisp"},
                },
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "found it",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "missing",
                    "is_error": True,
                },
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "steered"}]},
    ]


def test_anthropic_provider_replays_clean_response_before_appended_user_message() -> None:
    messages_resource = StubMessagesResource(
        responses=[
            [
                message_start_event("response-id"),
                _text_block_start(index=0),
                text_delta_event("first", index=0),
                message_delta_event("end_turn"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="claude-test",
            previous_response_id=completed.response_id,
            extra_messages=[WispMessage(role="user", content="steered")],
        ):
            pass

    anyio.run(run)

    assert messages_resource.calls[1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
        {"role": "user", "content": [{"type": "text", "text": "steered"}]},
    ]


def test_anthropic_provider_replay_includes_text_alongside_tool_use() -> None:
    messages_resource = StubMessagesResource(
        responses=[
            [
                message_start_event("response-id"),
                _text_block_start(index=0),
                text_delta_event("Let me check.", index=0),
                tool_use_start_event("call-id", "lookup", index=1),
                input_json_delta_event("{}", index=1),
                message_delta_event("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id=completed.response_id,
        ):
            pass

    anyio.run(run)

    replay_message = messages_resource.calls[1]["messages"][1]
    assert replay_message == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "call-id", "name": "lookup", "input": {}},
        ],
    }


def test_anthropic_provider_replay_preserves_thinking_blocks_alongside_tool_use() -> None:
    # Regression test: Anthropic's tool-use guidance requires thinking and
    # redacted_thinking blocks to be echoed back to the API unmodified
    # alongside their sibling tool_use block on the same turn -- dropping
    # them (e.g. replaying only text + tool_use) can be rejected or lose
    # reasoning continuity.
    messages_resource = StubMessagesResource(
        responses=[
            [
                message_start_event("response-id"),
                _thinking_block_start(index=0),
                thinking_delta_event("checking the lookup table", index=0),
                _signature_delta("sig-part-1", index=0),
                _signature_delta("sig-part-2", index=0),
                _redacted_thinking_block_start(index=1),
                tool_use_start_event("call-id", "lookup", index=2),
                input_json_delta_event("{}", index=2),
                message_delta_event("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id=completed.response_id,
        ):
            pass

    anyio.run(run)

    replay_message = messages_resource.calls[1]["messages"][1]
    assert replay_message == {
        "role": "assistant",
        "content": [
            {
                "type": "thinking",
                "thinking": "checking the lookup table",
                "signature": "sig-part-1sig-part-2",
            },
            {"type": "redacted_thinking", "data": "redacted"},
            {"type": "tool_use", "id": "call-id", "name": "lookup", "input": {}},
        ],
    }


def test_anthropic_provider_requests_automatic_prompt_caching_on_every_request() -> None:
    """Every request asks for top-level caching and sets no block-level breakpoints.

    Anthropic caches only when asked. A block-level breakpoint would also take
    one of the four breakpoint slots automatic caching needs, and a breakpoint on
    a replayed signed thinking block would change bytes Anthropic requires to be
    echoed back unmodified.
    """

    messages_resource = StubMessagesResource(
        responses=[
            [
                message_start_event("response-id"),
                _thinking_block_start(index=0),
                thinking_delta_event("checking the lookup table", index=0),
                _signature_delta("sig", index=0),
                tool_use_start_event("call-id", "lookup", index=1),
                input_json_delta_event("{}", index=1),
                message_delta_event("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [
        WispMessage(role="system", content="instructions"),
        WispMessage(role="user", content="hello"),
    ]
    tools = [
        ToolSpec(
            name="lookup",
            description="Look something up.",
            input_schema={"type": "object", "properties": {}},
        )
    ]

    async def run() -> None:
        first_events = [
            event
            async for event in provider.stream(
                messages, model="claude-test", tools=tools, effort="high"
            )
        ]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tools=tools,
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id=completed.response_id,
            effort="high",
        ):
            pass

    anyio.run(run)

    def nested_cache_controls(value: object) -> int:
        if isinstance(value, dict):
            return int("cache_control" in value) + sum(
                nested_cache_controls(item) for item in value.values()
            )
        if isinstance(value, list):
            return sum(nested_cache_controls(item) for item in value)
        return 0

    assert len(messages_resource.calls) == 2
    for request in messages_resource.calls:
        assert request["cache_control"] == {"type": "ephemeral"}
        body = {key: value for key, value in request.items() if key != "cache_control"}
        assert nested_cache_controls(body) == 0
    # The follow-up resends the signed thinking block unchanged.
    assert messages_resource.calls[1]["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "checking the lookup table",
        "signature": "sig",
    }


def test_anthropic_provider_accumulates_replay_across_multiple_tool_rounds() -> None:
    # Regression test: run_agent_loop never grows `messages` across tool
    # rounds and only ever passes each round's own new `tool_results` --
    # so round 2's replay must carry BOTH round 1's tool_use/tool_result
    # pair AND round 2's own tool_use turn, not just the latest round.
    # Losing round 1 here would silently drop an earlier tool observation
    # from a multi-step tool workflow.
    messages_resource = StubMessagesResource(
        responses=[
            [
                message_start_event("response-1"),
                tool_use_start_event("call-1", "lookup", index=0),
                input_json_delta_event('{"query": "a"}', index=0),
                message_delta_event("tool_use"),
            ],
            [
                message_start_event("response-2"),
                tool_use_start_event("call-2", "lookup", index=0),
                input_json_delta_event('{"query": "b"}', index=0),
                message_delta_event("tool_use"),
            ],
            [],
        ]
    )
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        # Round 1: no prior tool state.
        first_events = [event async for event in provider.stream(messages, model="claude-test")]
        first_completed = first_events[-1]
        assert isinstance(first_completed, ProviderResponseCompleted)

        # Round 2: mirrors run_agent_loop exactly -- same fixed `messages`,
        # only this round's own tool_results, previous_response_id carried
        # forward from round 1's response.
        second_events = [
            event
            async for event in provider.stream(
                messages,
                model="claude-test",
                tool_results=[ToolCallResult(call_id="call-1", output="result-a")],
                previous_response_id=first_completed.response_id,
            )
        ]
        second_completed = second_events[-1]
        assert isinstance(second_completed, ProviderResponseCompleted)

        # Round 3: same shape again, now carrying round 2's tool_results.
        async for _event in provider.stream(
            messages,
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-2", output="result-b")],
            previous_response_id=second_completed.response_id,
        ):
            pass

    anyio.run(run)

    assert len(messages_resource.calls) == 3
    third_call_messages = messages_resource.calls[2]["messages"]
    assert third_call_messages == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"query": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "result-a",
                    "is_error": False,
                }
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-2", "name": "lookup", "input": {"query": "b"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "result-b",
                    "is_error": False,
                }
            ],
        },
    ]


def test_anthropic_provider_rejects_tool_results_without_replay() -> None:
    """Never send an orphaned tool result when its structured turn is unavailable."""

    messages_resource = StubMessagesResource(responses=[[]])
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        async for _event in provider.stream(
            [WispMessage(role="user", content="hello")],
            model="claude-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id="unknown-response-id",
        ):
            pass

    with pytest.raises(
        ProviderProtocolError,
        match="Anthropic continuation state is unavailable for unknown-response-id",
    ):
        anyio.run(run)
    assert messages_resource.calls == []


def _signature_delta(signature: str, *, index: int) -> RawContentBlockDeltaEvent:
    return RawContentBlockDeltaEvent(
        delta=SignatureDelta(signature=signature, type="signature_delta"),
        index=index,
        type="content_block_delta",
    )


def _text_block_start(*, index: int = 0) -> RawContentBlockStartEvent:
    content_block = cast(ContentBlock, TextBlock(text="", type="text"))
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )


def _thinking_block_start(*, index: int = 0) -> RawContentBlockStartEvent:
    content_block = cast(ContentBlock, ThinkingBlock(thinking="", signature="", type="thinking"))
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )


def _redacted_thinking_block_start(*, index: int = 0) -> RawContentBlockStartEvent:
    content_block = cast(
        ContentBlock, RedactedThinkingBlock(data="redacted", type="redacted_thinking")
    )
    return RawContentBlockStartEvent(
        content_block=content_block,
        index=index,
        type="content_block_start",
    )
