"""Deterministic offline provider for tests and no-credential smoke runs."""

from __future__ import annotations

import os
from collections import deque
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

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
    extra_messages: tuple[Message, ...] = ()
    effort: str | None = None
    prompt_cache_key: str | None = None


class FakeProvider:
    """Deterministic provider for tests and early CLI smoke runs."""

    name = "fake"
    default_model: str | None = "fake"

    @staticmethod
    def _stream_interval_seconds() -> float:
        """Read the optional pacing used by offline terminal benchmarks.

        Returns:
            Delay between fake response words, in seconds.

        Raises:
            ValueError: If the benchmark interval is not a nonnegative integer.
        """

        raw = os.environ.get("WISP_FAKE_STREAM_INTERVAL_MS")
        if raw is None:
            return 0.0
        try:
            interval_ms = int(raw)
        except ValueError as exc:
            raise ValueError("WISP_FAKE_STREAM_INTERVAL_MS must be a nonnegative integer") from exc
        if interval_ms < 0:
            raise ValueError("WISP_FAKE_STREAM_INTERVAL_MS must be a nonnegative integer")
        return interval_ms / 1_000

    @staticmethod
    def _extra_response_words() -> int:
        """Read the optional response length used by offline benchmarks.

        Returns:
            Number of additional deterministic response words.

        Raises:
            ValueError: If the requested word count is not a nonnegative integer.
        """

        raw = os.environ.get("WISP_FAKE_RESPONSE_WORDS")
        if raw is None:
            return 0
        try:
            count = int(raw)
        except ValueError as exc:
            raise ValueError("WISP_FAKE_RESPONSE_WORDS must be a nonnegative integer") from exc
        if count < 0:
            raise ValueError("WISP_FAKE_RESPONSE_WORDS must be a nonnegative integer")
        return count

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
        extra_words = self._extra_response_words()
        if extra_words:
            response = f"{response} {' '.join('payload' for _ in range(extra_words))}"
        response_prefix = os.environ.get("WISP_FAKE_RESPONSE_PREFIX")
        if response_prefix:
            response = f"{response_prefix} {response}"
        response_suffix = os.environ.get("WISP_FAKE_RESPONSE_SUFFIX")
        if response_suffix:
            response = f"{response} {response_suffix}"
        interval_seconds = self._stream_interval_seconds()

        yield ProviderResponseStarted(model=model or self.default_model or "fake")
        for index, word in enumerate(response.split(" ")):
            await anyio.sleep(interval_seconds)
            yield ProviderTextDelta(delta=word if index == 0 else f" {word}")
        yield ProviderResponseCompleted(content=response)


class ScriptedProvider:
    """Provider that replays predefined event streams and records each request."""

    name = "scripted"
    supports_continuation_messages: Literal[True] = True
    supports_context_rebase: Literal[True] = True

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
        extra_messages: Sequence[Message] = (),
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
                extra_messages=tuple(extra_messages),
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
