"""Deterministic offline provider for tests and no-credential smoke runs."""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass

import anyio

from wisp.agent.messages import Message
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Immutable request snapshot recorded by :class:`ScriptedProvider`."""

    messages: tuple[Message, ...]
    model: str | None
    tools: tuple[ToolSpec, ...]
    tool_results: tuple[ToolCallResult, ...]
    previous_response_id: str | None
    effort: str | None = None
    prompt_cache_key: str | None = None


class FakeProvider:
    """Deterministic provider for tests and early CLI smoke runs."""

    name = "fake"
    default_model: str | None = "fake"

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
        prompt = _last_user_prompt(messages)
        response = f"fake response to: {prompt}"

        yield ProviderResponseStarted(model=model or self.default_model or "fake")
        for index, word in enumerate(response.split(" ")):
            await anyio.sleep(0)
            yield ProviderTextDelta(delta=word if index == 0 else f" {word}")
        yield ProviderResponseCompleted(content=response)


class ScriptedProvider:
    """Provider that replays predefined event streams and records each request."""

    name = "scripted"

    def __init__(
        self,
        streams: Iterable[Iterable[ProviderEvent | BaseException]],
        *,
        default_model: str = "scripted",
    ) -> None:
        self.default_model: str | None = default_model
        self._streams = deque(tuple(stream) for stream in streams)
        self.calls: list[ProviderRequest] = []

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
        self.calls.append(
            ProviderRequest(
                messages=tuple(messages),
                model=model,
                tools=tuple(tools),
                tool_results=tuple(tool_results),
                previous_response_id=previous_response_id,
                effort=effort,
                prompt_cache_key=prompt_cache_key,
            )
        )
        if not self._streams:
            raise RuntimeError("ScriptedProvider has no response stream remaining")
        for item in self._streams.popleft():
            await anyio.sleep(0)
            if isinstance(item, BaseException):
                raise item
            yield item


def _last_user_prompt(messages: Sequence[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


__all__ = ["FakeProvider", "ProviderRequest", "ScriptedProvider"]
