"""Web search in the provider-neutral loop: opt-in requests and bounded events."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal

import anyio
import pytest

from wisp.agent.loop import AgentLoopConfig
from wisp.agent.loop.model_response import CompletedModelResponse, ModelResponseStream
from wisp.agent.loop.provider_request import open_provider_stream
from wisp.agent.messages import Message
from wisp.agent.tool_contracts import ToolExecutionEvent
from wisp.events import WebSearchPerformed, WebSearchSource
from wisp.providers.base import ProviderProtocolError, ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderWebSearch,
    ProviderWebSource,
    ToolCall,
)


class _NeverExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError(f"Unexpected tool call: {tool_call.name}")
        yield  # pragma: no cover - makes this an async generator


class _MinimalProvider:
    """Written against the minimal protocol: it accepts no web_search keyword."""

    name = "minimal"
    default_model = "minimal"

    def __init__(self, events: Sequence[ProviderEvent] = ()) -> None:
        self.events = events

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        for event in self.events:
            yield event
        yield ProviderResponseCompleted(content="ok")


class _SearchingProvider:
    name = "searching"
    default_model = "searching"
    supports_web_search: Literal[True] = True

    def __init__(self, events: Sequence[ProviderEvent] = ()) -> None:
        self.events = events
        self.web_search: list[bool] = []

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        web_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        self.web_search.append(web_search)
        for event in self.events:
            yield event
        yield ProviderResponseCompleted(content="ok")


def _open(provider: object, *, web_search: bool) -> list[ProviderEvent]:
    config = AgentLoopConfig(
        provider=provider,  # type: ignore[arg-type]
        tool_executor=_NeverExecutor(),
        web_search=web_search,
    )

    async def run() -> list[ProviderEvent]:
        return [
            event
            async for event in open_provider_stream(
                config,
                messages=(Message(role="user", content="hi"),),
                tool_results=(),
                extra_messages=(),
                previous_response_id=None,
            )
        ]

    return anyio.run(run)


def test_web_search_is_requested_only_from_a_provider_that_offers_it() -> None:
    searching = _SearchingProvider()
    _open(searching, web_search=True)
    _open(searching, web_search=False)
    assert searching.web_search == [True, False]

    # The minimal provider would raise TypeError on an unexpected keyword.
    events = _open(_MinimalProvider(), web_search=True)
    assert events == [ProviderResponseCompleted(content="ok")]


def _response_events(provider: object) -> list[object]:
    stream = ModelResponseStream(
        config=AgentLoopConfig(
            provider=provider,  # type: ignore[arg-type]
            tool_executor=_NeverExecutor(),
            web_search=True,
        ),
        messages=(Message(role="user", content="go"),),
        tool_results=(),
        extra_messages=(),
        previous_response_id=None,
    )

    async def run() -> list[object]:
        return [event async for event in stream.events(turn=3)]

    return anyio.run(run)


def test_a_provider_search_becomes_a_bounded_public_event() -> None:
    sources = tuple(
        ProviderWebSource(url=f"https://example.test/{index}" + "x" * 3_000, title="t" * 500)
        for index in range(30)
    )
    provider = _SearchingProvider(
        [
            ProviderResponseStarted(model="searching"),
            ProviderWebSearch(query="q" * 1_000, sources=sources, error="e" * 100),
        ]
    )

    events = _response_events(provider)

    searches = [event for event in events if isinstance(event, WebSearchPerformed)]
    assert len(searches) == 1
    search = searches[0]
    assert (search.turn, search.provider, search.action) == (3, "searching", "search")
    assert search.query == "q" * 500
    assert search.error == "e" * 64
    assert len(search.sources) == 20
    assert search.sources[0] == WebSearchSource(
        url=("https://example.test/0" + "x" * 3_000)[:2_048], title="t" * 200
    )


def test_a_search_before_the_response_starts_is_a_protocol_error() -> None:
    provider = _SearchingProvider([ProviderWebSearch(query="early")])

    with pytest.raises(ProviderProtocolError, match="before response_started"):
        _response_events(provider)


def test_a_search_counts_as_visible_output_for_overflow_recovery() -> None:
    stream = ModelResponseStream(
        config=AgentLoopConfig(
            provider=_SearchingProvider(  # type: ignore[arg-type]
                [ProviderResponseStarted(model="searching"), ProviderWebSearch(query="q")]
            ),
            tool_executor=_NeverExecutor(),
            web_search=True,
        ),
        messages=(Message(role="user", content="go"),),
        tool_results=(),
        extra_messages=(),
        previous_response_id=None,
    )

    async def run() -> None:
        _ = [event async for event in stream.events(turn=1)]

    anyio.run(run)

    assert isinstance(stream.outcome, CompletedModelResponse)
    assert stream.outcome.had_streamed_delta is True
