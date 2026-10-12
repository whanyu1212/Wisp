"""Stub clients and streamed chunks shared by the Google provider tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from google.genai import types as genai_types

from wisp.agent.messages import Message as WispMessage
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.google import GoogleProvider


class StubGoogleProvider(GoogleProvider):
    def __init__(self, chunks: Sequence[genai_types.GenerateContentResponse]) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.chunks = chunks
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
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results
        self.seen_effort = effort

        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            for chunk in self.chunks:
                yield chunk

        return stream()


class StubGenaiClient:
    def __init__(self, models: StubModels) -> None:
        self.aio = _StubAio(models)


class _StubAio:
    def __init__(self, models: StubModels) -> None:
        self.models = models


class StubModels:
    def __init__(
        self, responses: Sequence[Sequence[genai_types.GenerateContentResponse]] | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) if responses is not None else None

    async def generate_content_stream(
        self, **kwargs: object
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        self.calls.append(dict(kwargs))
        chunks = self._responses.pop(0) if self._responses else ()

        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            for chunk in chunks:
                yield chunk

        return stream()


def text_chunk(
    text: str,
    *,
    response_id: str | None = None,
    finish_reason: genai_types.FinishReason | None = None,
) -> genai_types.GenerateContentResponse:
    return part_chunk(
        genai_types.Part(text=text), response_id=response_id, finish_reason=finish_reason
    )


def part_chunk(
    part: genai_types.Part,
    *,
    response_id: str | None = None,
    finish_reason: genai_types.FinishReason | None = None,
) -> genai_types.GenerateContentResponse:
    return parts_chunk([part], response_id=response_id, finish_reason=finish_reason)


def parts_chunk(
    parts: Sequence[genai_types.Part],
    *,
    response_id: str | None = None,
    finish_reason: genai_types.FinishReason | None = None,
) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        response_id=response_id,
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=list(parts)),
                finish_reason=finish_reason,
            )
        ],
    )
