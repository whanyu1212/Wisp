"""The web search setting in a coding session: request, prompt, and persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Literal

import anyio

from tests.coding.session_support import CapturingProvider
from wisp.agent.messages import Message
from wisp.agent.prompt import WEB_SEARCH_GUIDANCE, WEB_SEARCH_UNAVAILABLE_GUIDANCE
from wisp.coding import CodingSession
from wisp.events import WebSearchPerformed, WebSearchSource, WispEvent
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderWebSearch,
    ProviderWebSource,
)
from wisp.sessions import EventSessionEntry, JsonlSessionStore


class _SearchingProvider:
    name = "searching"
    default_model: str | None = "default"
    supports_web_search: Literal[True] = True

    def __init__(self) -> None:
        self.web_search: list[bool] = []
        self.seen_messages: Sequence[Message] = ()

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
        self.seen_messages = messages
        yield ProviderResponseStarted(model=model or "default")
        if web_search:
            yield ProviderWebSearch(
                query="linear mcp server",
                sources=(ProviderWebSource(url="https://linear.app/docs/mcp", title="Linear"),),
            )
        yield ProviderResponseCompleted(content="done")


def _system_text(messages: Sequence[Message]) -> str:
    return "\n".join(message.content for message in messages if message.role == "system")


def test_enabled_search_reaches_the_provider_and_is_saved_with_the_session(
    tmp_path: Path,
) -> None:
    provider = _SearchingProvider()
    store = JsonlSessionStore(tmp_path)
    session = store.create()
    agent = CodingSession(provider=provider, sessions=store, web_search=True)

    async def run() -> list[WispEvent]:
        return [event async for event in agent.run("find the server", session=session)]

    events = anyio.run(run)

    assert provider.web_search == [True]
    assert WEB_SEARCH_GUIDANCE in _system_text(provider.seen_messages)
    searches = [event for event in events if isinstance(event, WebSearchPerformed)]
    assert [(search.provider, search.query) for search in searches] == [
        ("searching", "linear mcp server")
    ]
    saved = [
        entry.event.payload
        for entry in session.read_entries()
        if isinstance(entry, EventSessionEntry) and entry.event.payload["type"] == "web.search"
    ]
    assert len(saved) == 1
    assert saved[0]["sources"] == [{"url": "https://linear.app/docs/mcp", "title": "Linear"}]
    page = session.read_message_page(limit=1)
    assert len(page.messages) == 1
    assert page.messages[0].web_searches == tuple(searches)
    exact = session.read_message_page(entry_ids=(page.messages[0].entry_id,))
    assert exact.messages[0].web_searches == tuple(searches)


def test_search_stays_off_by_default(tmp_path: Path) -> None:
    provider = _SearchingProvider()
    store = JsonlSessionStore(tmp_path)
    agent = CodingSession(provider=provider, sessions=store)

    async def run() -> None:
        _ = [event async for event in agent.run("hi", session=store.create())]

    anyio.run(run)

    assert provider.web_search == [False]
    system = _system_text(provider.seen_messages)
    assert WEB_SEARCH_GUIDANCE not in system
    assert WEB_SEARCH_UNAVAILABLE_GUIDANCE not in system


def test_a_provider_without_search_tells_the_model_it_cannot_search(tmp_path: Path) -> None:
    provider = CapturingProvider()
    store = JsonlSessionStore(tmp_path)
    agent = CodingSession(provider=provider, sessions=store, web_search=True)

    async def run() -> None:
        _ = [event async for event in agent.run("hi", session=store.create())]

    anyio.run(run)

    assert provider.seen_messages is not None
    system = _system_text(provider.seen_messages)
    assert WEB_SEARCH_UNAVAILABLE_GUIDANCE in system
    assert WEB_SEARCH_GUIDANCE not in system


def test_history_searches_are_bounded_and_do_not_cross_runs(tmp_path: Path) -> None:
    session = JsonlSessionStore(tmp_path).create()

    async def run() -> None:
        await session.append_message(Message(role="user", content="first"), operation_id="a")
        for _ in range(40):
            await session.append_event(
                WebSearchPerformed(
                    turn=1,
                    provider="test",
                    query="q" * 5000,
                    sources=tuple(
                        WebSearchSource(url="https://example.com/" + "x" * 2000, title="t" * 5000)
                        for _ in range(20)
                    ),
                ),
                operation_id="a",
            )
        await session.append_message(
            Message(role="assistant", content="first reply"), operation_id="a"
        )
        await session.append_event(
            WebSearchPerformed(turn=2, provider="test", query="orphan"), operation_id="a"
        )
        await session.append_message(Message(role="user", content="next"), operation_id="b")
        await session.append_message(
            Message(role="assistant", content="next reply"), operation_id="b"
        )

    anyio.run(run)
    page = session.read_message_page()
    searches = page.messages[1].web_searches
    assert len(searches) == 32
    assert len(searches[0].query.encode()) <= 1024
    assert len(searches[0].sources) == 8
    assert len(searches[0].sources[0].title.encode()) <= 1024
    assert not page.messages[-1].web_searches
