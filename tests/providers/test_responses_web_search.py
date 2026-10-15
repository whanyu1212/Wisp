"""Hosted web search on Responses backends: OpenAI, xAI, and the Codex login."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import anyio
from openai import AsyncOpenAI
from openai.types.responses import (
    ResponseFunctionWebSearch,
    ResponseOutputItemDoneEvent,
    ResponseStreamEvent,
)
from openai.types.responses.response_function_web_search import (
    ActionFind,
    ActionOpenPage,
    ActionSearch,
    ActionSearchSource,
)

from tests.providers.openai_codex_support import (
    StubOpenAICodexProvider,
    store_with_oauth,
)
from tests.providers.openai_codex_support import (
    completed_event as codex_completed_event,
)
from tests.providers.openai_support import (
    StubAsyncOpenAI,
    StubResponsesResource,
    completed_event,
)
from wisp.agent.messages import Message
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderWebSearch,
    ProviderWebSource,
)
from wisp.providers.openai import OpenAIProvider
from wisp.providers.xai import XAIProvider

_MESSAGES = [Message(role="user", content="Find the Linear MCP server")]


class _ScriptedResponses(StubResponsesResource):
    def __init__(self, events: Sequence[ResponseStreamEvent]) -> None:
        super().__init__()
        self.events = events

    async def create(self, **kwargs: object) -> AsyncIterator[ResponseStreamEvent]:
        self.calls.append(dict(kwargs))

        async def stream() -> AsyncIterator[ResponseStreamEvent]:
            for event in self.events:
                yield event

        return stream()


def _web_search_done(action: object, *, status: str = "completed") -> ResponseOutputItemDoneEvent:
    return ResponseOutputItemDoneEvent(
        item=ResponseFunctionWebSearch.model_construct(
            id="ws_1", action=action, status=status, type="web_search_call"
        ),
        output_index=0,
        sequence_number=0,
        type="response.output_item.done",
    )


def _stream(
    provider: OpenAIProvider | StubOpenAICodexProvider, **options: Any
) -> list[ProviderEvent]:
    async def run() -> list[ProviderEvent]:
        return [event async for event in provider.stream(_MESSAGES, **options)]

    return anyio.run(run)


def _openai(
    events: Sequence[ResponseStreamEvent] = (),
    *,
    provider_type: type[OpenAIProvider] = OpenAIProvider,
) -> tuple[OpenAIProvider, _ScriptedResponses]:
    responses = _ScriptedResponses([*events, completed_event()])
    provider = provider_type(
        api_key="test-key", client=cast(AsyncOpenAI, StubAsyncOpenAI(responses))
    )
    return provider, responses


def test_openai_web_search_adds_the_hosted_tool_and_asks_for_sources() -> None:
    provider, responses = _openai()

    _stream(provider, web_search=True)

    assert responses.calls[0]["tools"] == [{"type": "web_search"}]
    assert responses.calls[0]["include"] == ["web_search_call.action.sources"]


def test_openai_requests_without_web_search_send_no_search_tool() -> None:
    provider, responses = _openai()

    _stream(provider)

    assert "tools" not in responses.calls[0]
    assert "include" not in responses.calls[0]


def test_xai_web_search_adds_the_tool_without_openais_source_request() -> None:
    provider, responses = _openai(provider_type=XAIProvider)

    _stream(provider, web_search=True)

    assert responses.calls[0]["tools"] == [{"type": "web_search"}]
    assert responses.calls[0]["include"] == ["reasoning.encrypted_content"]


def test_openai_reports_each_completed_web_search_call() -> None:
    provider, _ = _openai(
        [
            _web_search_done(
                ActionSearch(
                    type="search",
                    query="linear mcp server",
                    sources=[ActionSearchSource(type="url", url="https://linear.app/docs/mcp")],
                )
            ),
            _web_search_done(ActionOpenPage(type="open_page", url="https://linear.app/docs/mcp")),
            _web_search_done(
                ActionFind(type="find_in_page", pattern="mcp.linear.app", url="https://linear.app")
            ),
            _web_search_done(ActionSearch(type="search", queries=["a", "b"]), status="failed"),
        ]
    )

    events = _stream(provider, web_search=True)

    assert [event for event in events if isinstance(event, ProviderWebSearch)] == [
        ProviderWebSearch(
            query="linear mcp server",
            sources=(ProviderWebSource(url="https://linear.app/docs/mcp"),),
        ),
        ProviderWebSearch(
            action="open_page", sources=(ProviderWebSource(url="https://linear.app/docs/mcp"),)
        ),
        ProviderWebSearch(
            action="find_in_page",
            query="mcp.linear.app",
            sources=(ProviderWebSource(url="https://linear.app"),),
        ),
        ProviderWebSearch(query="a; b", error="failed"),
    ]
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.tool_calls == ()


def _codex(tmp_path: Path, events: Sequence[dict[str, object]] = ()) -> StubOpenAICodexProvider:
    return StubOpenAICodexProvider(
        [*events, codex_completed_event("response-1")],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )


def test_codex_web_search_adds_the_hosted_tool(tmp_path: Path) -> None:
    provider = _codex(tmp_path)

    _stream(provider, web_search=True)

    assert provider.seen_body is not None
    assert provider.seen_body["tools"] == [{"type": "web_search"}]


def test_codex_requests_without_web_search_send_no_search_tool(tmp_path: Path) -> None:
    provider = _codex(tmp_path)

    _stream(provider)

    assert provider.seen_body is not None
    assert "tools" not in provider.seen_body


def test_codex_reports_completed_web_search_calls(tmp_path: Path) -> None:
    item = {
        "type": "web_search_call",
        "id": "ws_1",
        "status": "completed",
        "action": {
            "type": "search",
            "query": "linear mcp server",
            "sources": [{"type": "url", "url": "https://linear.app/docs/mcp"}, {"type": "url"}],
        },
    }
    provider = _codex(
        tmp_path,
        [
            {"type": "response.output_item.added", "item": {**item, "status": "in_progress"}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.output_item.done",
                "item": {"type": "web_search_call", "id": "ws_2", "status": "completed"},
            },
        ],
    )

    events = _stream(provider, web_search=True)

    assert [event for event in events if isinstance(event, ProviderWebSearch)] == [
        ProviderWebSearch(
            query="linear mcp server",
            sources=(ProviderWebSource(url="https://linear.app/docs/mcp"),),
        ),
        ProviderWebSearch(),
    ]
