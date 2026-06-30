"""OpenAI Responses API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from typing import Literal, cast

from openai import AsyncOpenAI
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseErrorEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_input_param import ResponseInputParam

from wisp.agent.messages import Message, Role
from wisp.providers.base import ProviderConfigurationError, ProviderError

DEFAULT_OPENAI_MODEL = "gpt-5.5"
OpenAIRole = Literal["user", "assistant", "system", "developer"]


class OpenAIProvider:
    """Provider backed by OpenAI's Responses API."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_OPENAI_MODEL,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.default_model: str | None = default_model
        self._api_key = _normalize_optional(api_key)
        self._client = client

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text deltas from OpenAI's Responses API."""

        selected_model = model or self.default_model or DEFAULT_OPENAI_MODEL
        stream = await self._create_stream(messages, model=selected_model)

        async for event in stream:
            if isinstance(event, ResponseTextDeltaEvent):
                yield event.delta
            elif isinstance(event, ResponseErrorEvent):
                raise ProviderError(f"OpenAI API error: {event.message}")

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
    ) -> AsyncIterator[ResponseStreamEvent]:
        client = self._client_or_create()
        stream = await client.responses.create(
            model=model,
            input=_messages_to_response_input(messages),
            stream=True,
        )
        return cast(AsyncIterator[ResponseStreamEvent], stream)

    def _client_or_create(self) -> AsyncOpenAI:
        if self._client is not None:
            return self._client

        api_key = self._api_key or _normalize_optional(os.environ.get("OPENAI_API_KEY"))
        if api_key is None:
            raise ProviderConfigurationError(
                "OPENAI_API_KEY is required when using the openai provider"
            )

        self._client = AsyncOpenAI(api_key=api_key)
        return self._client


def _messages_to_response_input(messages: Sequence[Message]) -> ResponseInputParam:
    response_input: ResponseInputParam = []
    for message in messages:
        message_param: EasyInputMessageParam = {
            "role": _to_openai_role(message.role),
            "content": message.content,
        }
        response_input.append(message_param)
    return response_input


def _to_openai_role(role: Role) -> OpenAIRole:
    if role == "tool":
        return "user"
    return role


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
