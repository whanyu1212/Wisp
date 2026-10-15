"""Anthropic's server-side web search: request, events, replay, and pause_turn."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import anyio
from anthropic import AsyncAnthropic
from anthropic.types import (
    CitationsDelta,
    CitationsWebSearchResultLocation,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageStreamEvent,
    ServerToolUseBlock,
    TextBlock,
    WebSearchResultBlock,
    WebSearchToolResultBlock,
    WebSearchToolResultError,
)
from anthropic.types.raw_content_block_start_event import ContentBlock

from tests.providers.anthropic_support import (
    StubAsyncAnthropic,
    StubMessagesResource,
    input_json_delta_event,
    message_delta_event,
    message_start_event,
    text_delta_event,
    tool_use_start_event,
)
from wisp.agent.messages import Message as WispMessage
from wisp.providers.anthropic import AnthropicProvider
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderToolCallCompleted,
    ProviderUsage,
    ProviderWebSearch,
    ProviderWebSource,
)

_LOOKUP = ToolSpec(
    name="lookup",
    description="Look something up.",
    input_schema={"type": "object", "properties": {}},
)
_MESSAGES = [WispMessage(role="user", content="Add the Linear MCP server")]


def _block_start(block: object, *, index: int) -> RawContentBlockStartEvent:
    return RawContentBlockStartEvent(
        content_block=cast(ContentBlock, block),
        index=index,
        type="content_block_start",
    )


def _search_request(call_id: str, query: str, *, index: int) -> list[RawMessageStreamEvent]:
    return [
        _block_start(
            ServerToolUseBlock(id=call_id, input={}, name="web_search", type="server_tool_use"),
            index=index,
        ),
        input_json_delta_event(f'{{"query": "{query}"}}', index=index),
    ]


def _search_result(call_id: str, *, index: int) -> RawContentBlockStartEvent:
    return _block_start(
        WebSearchToolResultBlock(
            tool_use_id=call_id,
            type="web_search_tool_result",
            content=[
                WebSearchResultBlock(
                    encrypted_content="ENCRYPTED-PAGE",
                    title="Linear MCP server",
                    url="https://linear.app/docs/mcp",
                    page_age="April 30, 2026",
                    type="web_search_result",
                )
            ],
        ),
        index=index,
    )


def _cited_text(text: str, *, index: int) -> list[RawMessageStreamEvent]:
    return [
        _block_start(TextBlock(text="", type="text"), index=index),
        text_delta_event(text, index=index),
        RawContentBlockDeltaEvent(
            delta=CitationsDelta(
                citation=CitationsWebSearchResultLocation(
                    cited_text="The server is at https://mcp.linear.app/mcp",
                    encrypted_index="ENCRYPTED-INDEX",
                    title="Linear MCP server",
                    url="https://linear.app/docs/mcp",
                    type="web_search_result_location",
                ),
                type="citations_delta",
            ),
            index=index,
            type="content_block_delta",
        ),
    ]


def _provider(*responses: Sequence[RawMessageStreamEvent]) -> tuple[AnthropicProvider, Any]:
    resource = StubMessagesResource(responses=[list(response) for response in responses])
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(resource)),
    )
    return provider, resource


def _stream(provider: AnthropicProvider, **options: Any) -> list[ProviderEvent]:
    async def run() -> list[ProviderEvent]:
        return [
            event
            async for event in provider.stream(
                options.pop("messages", _MESSAGES), model="claude-test", **options
            )
        ]

    return anyio.run(run)


def test_web_search_adds_the_server_tool_with_a_search_cap() -> None:
    provider, resource = _provider([message_delta_event("end_turn")])

    _stream(provider, tools=[_LOOKUP], web_search=True)

    tools = resource.calls[0]["tools"]
    assert tools[0]["name"] == "lookup"
    assert tools[1] == {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


def test_requests_without_web_search_send_no_server_tool() -> None:
    provider, resource = _provider([message_delta_event("end_turn")])

    _stream(provider, tools=[_LOOKUP])

    assert [tool["name"] for tool in resource.calls[0]["tools"]] == ["lookup"]


def test_a_client_tool_named_web_search_keeps_the_request_valid() -> None:
    provider, resource = _provider([message_delta_event("end_turn")])
    client_tool = ToolSpec(name="web_search", description="Search.", input_schema={})

    _stream(provider, tools=[client_tool], web_search=True)

    assert resource.calls[0]["tools"] == [
        {"name": "web_search", "description": "Search.", "input_schema": {}}
    ]


def test_a_search_is_reported_with_its_query_and_sources() -> None:
    provider, _ = _provider(
        [
            message_start_event("msg-1"),
            *_search_request("srvtoolu_1", "linear mcp server url", index=0),
            _search_result("srvtoolu_1", index=1),
            *_cited_text("It is at https://mcp.linear.app/mcp.", index=2),
            message_delta_event("end_turn"),
        ]
    )

    events = _stream(provider, web_search=True)

    searches = [event for event in events if isinstance(event, ProviderWebSearch)]
    assert searches == [
        ProviderWebSearch(
            query="linear mcp server url",
            sources=(
                ProviderWebSource(url="https://linear.app/docs/mcp", title="Linear MCP server"),
            ),
        )
    ]
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.content == "It is at https://mcp.linear.app/mcp."
    assert completed.tool_calls == ()


def test_a_failed_search_reports_its_error_code() -> None:
    provider, _ = _provider(
        [
            *_search_request("srvtoolu_1", "linear", index=0),
            _block_start(
                WebSearchToolResultBlock(
                    tool_use_id="srvtoolu_1",
                    type="web_search_tool_result",
                    content=WebSearchToolResultError(
                        error_code="max_uses_exceeded", type="web_search_tool_result_error"
                    ),
                ),
                index=1,
            ),
            message_delta_event("end_turn"),
        ]
    )

    events = _stream(provider, web_search=True)

    assert ProviderWebSearch(query="linear", error="max_uses_exceeded") in events


def test_a_server_tool_block_is_never_run_as_a_client_tool() -> None:
    # Even when its name matches a registered client tool, Anthropic ran it.
    impostor = ServerToolUseBlock.model_construct(
        id="srvtoolu_1", input={}, name="lookup", type="server_tool_use"
    )
    provider, _ = _provider(
        [
            message_start_event("msg-1"),
            _block_start(impostor, index=0),
            input_json_delta_event('{"query": "rm -rf"}', index=0),
            message_delta_event("end_turn"),
        ]
    )

    events = _stream(provider, tools=[_LOOKUP], web_search=True)

    assert not any(isinstance(event, ProviderToolCallCompleted) for event in events)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.tool_calls == ()


def test_search_results_and_citations_are_replayed_exactly() -> None:
    # Anthropic rejects a later turn whose encrypted_content or encrypted_index changed.
    provider, resource = _provider(
        [
            message_start_event("msg-1"),
            *_search_request("srvtoolu_1", "linear mcp", index=0),
            _search_result("srvtoolu_1", index=1),
            *_cited_text("Found it.", index=2),
            tool_use_start_event("toolu_1", "lookup", index=3),
            input_json_delta_event("{}", index=3),
            message_delta_event("tool_use"),
        ],
        [message_delta_event("end_turn")],
    )

    first = _stream(provider, tools=[_LOOKUP], web_search=True)
    _stream(
        provider,
        tools=[_LOOKUP],
        web_search=True,
        tool_results=[ToolCallResult(call_id="toolu_1", output="ok")],
        previous_response_id="msg-1",
    )

    assert [event for event in first if isinstance(event, ProviderToolCallCompleted)][
        0
    ].tool_call.call_id == "toolu_1"
    replayed = resource.calls[1]["messages"]
    assert replayed[-2] == {
        "role": "assistant",
        "content": [
            {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
                "input": {"query": "linear mcp"},
            },
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "encrypted_content": "ENCRYPTED-PAGE",
                        "title": "Linear MCP server",
                        "url": "https://linear.app/docs/mcp",
                        "page_age": "April 30, 2026",
                    }
                ],
            },
            {
                "type": "text",
                "text": "Found it.",
                "citations": [
                    {
                        "type": "web_search_result_location",
                        "cited_text": "The server is at https://mcp.linear.app/mcp",
                        "encrypted_index": "ENCRYPTED-INDEX",
                        "title": "Linear MCP server",
                        "url": "https://linear.app/docs/mcp",
                    }
                ],
            },
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}},
        ],
    }
    assert replayed[-1]["content"][0]["tool_use_id"] == "toolu_1"


def test_a_search_beside_a_client_tool_runs_on_the_next_request() -> None:
    # Anthropic stops with tool_use before running the search; its result
    # arrives in the next response, after the client tool's result.
    provider, resource = _provider(
        [
            message_start_event("msg-1"),
            *_search_request("srvtoolu_1", "linear mcp", index=0),
            tool_use_start_event("toolu_1", "lookup", index=1),
            input_json_delta_event("{}", index=1),
            message_delta_event("tool_use"),
        ],
        [
            message_start_event("msg-2"),
            _search_result("srvtoolu_1", index=0),
            message_delta_event("end_turn"),
        ],
    )

    first = _stream(provider, tools=[_LOOKUP], web_search=True)
    second = _stream(
        provider,
        tools=[_LOOKUP],
        web_search=True,
        tool_results=[ToolCallResult(call_id="toolu_1", output="ok")],
        previous_response_id="msg-1",
    )

    assert not any(isinstance(event, ProviderWebSearch) for event in first)
    assert [
        event.tool_call.name for event in first if isinstance(event, ProviderToolCallCompleted)
    ] == ["lookup"]
    pending_turn = resource.calls[1]["messages"][-2]
    assert [block["type"] for block in pending_turn["content"]] == ["server_tool_use", "tool_use"]
    assert [event for event in second if isinstance(event, ProviderWebSearch)] == [
        ProviderWebSearch(
            query="linear mcp",
            sources=(
                ProviderWebSource(url="https://linear.app/docs/mcp", title="Linear MCP server"),
            ),
        )
    ]


def test_a_paused_search_turn_is_resumed_as_one_response() -> None:
    provider, resource = _provider(
        [
            message_start_event("msg-1"),
            *_search_request("srvtoolu_1", "linear mcp", index=0),
            _search_result("srvtoolu_1", index=1),
            message_delta_event("pause_turn"),
        ],
        [
            message_start_event("msg-2"),
            *_cited_text("Found it.", index=0),
            message_delta_event("end_turn"),
        ],
        [message_delta_event("end_turn")],
    )

    events = _stream(provider, web_search=True)

    assert sum(isinstance(event, ProviderResponseStarted) for event in events) == 1
    completed = events[-1]
    assert completed == ProviderResponseCompleted(
        content="Found it.",
        response_id="msg-2",
        finish_reason="stop",
        usage=ProviderUsage(
            input_tokens=2, output_tokens=2, total_tokens=4, context_input_tokens=1
        ),
    )
    # The resumed request ends with the paused turn, sent back unchanged.
    paused = resource.calls[1]["messages"][-1]
    assert paused["role"] == "assistant"
    assert [block["type"] for block in paused["content"]] == [
        "server_tool_use",
        "web_search_tool_result",
    ]

    # The replay keeps the whole turn as one assistant message.
    _stream(
        provider,
        web_search=True,
        messages=[*_MESSAGES],
        extra_messages=[WispMessage(role="user", content="thanks")],
        previous_response_id="msg-2",
    )
    replayed = resource.calls[2]["messages"]
    assert [block["type"] for block in replayed[-2]["content"]] == [
        "server_tool_use",
        "web_search_tool_result",
        "text",
    ]


def test_a_turn_still_paused_after_every_resume_is_incomplete() -> None:
    paused = [*_search_request("srvtoolu_1", "linear", index=0), message_delta_event("pause_turn")]
    provider, resource = _provider(*([paused] * 4))

    events = _stream(provider, web_search=True)

    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == "length"
    assert len(resource.calls) == 4
