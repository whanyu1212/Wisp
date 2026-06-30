"""OpenAI Responses API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from typing import Literal, cast

from openai import AsyncOpenAI
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    Response,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseFunctionToolCall,
    ResponseIncompleteEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseRefusalDeltaEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_input_param import ResponseInputParam

from wisp.agent.messages import Message, Role
from wisp.providers.base import ProviderConfigurationError, ProviderError, ToolSpec

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
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[str]:
        """Stream text deltas from OpenAI's Responses API."""

        selected_model = model or self.default_model or DEFAULT_OPENAI_MODEL
        stream = await self._create_stream(messages, model=selected_model, tools=tools)

        async for event in stream:
            if isinstance(event, ResponseTextDeltaEvent | ResponseRefusalDeltaEvent):
                yield event.delta
            elif _is_function_tool_call_event(event):
                raise ProviderError(
                    "OpenAI returned a function tool call, but Wisp does not execute tools yet"
                )
            elif isinstance(event, ResponseErrorEvent):
                raise ProviderError(f"OpenAI API error: {event.message}")
            elif isinstance(event, ResponseFailedEvent):
                raise ProviderError(_failed_response_message(event.response))
            elif isinstance(event, ResponseIncompleteEvent):
                raise ProviderError(_incomplete_response_message(event.response))

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
    ) -> AsyncIterator[ResponseStreamEvent]:
        client = self._client_or_create()
        openai_tools = _tool_specs_to_openai_tools(tools)
        if openai_tools:
            stream = await client.responses.create(
                model=model,
                input=_messages_to_response_input(messages),
                stream=True,
                tools=openai_tools,
            )
        else:
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


def _is_function_tool_call_event(event: ResponseStreamEvent) -> bool:
    if isinstance(
        event,
        ResponseFunctionCallArgumentsDeltaEvent | ResponseFunctionCallArgumentsDoneEvent,
    ):
        return True
    if isinstance(event, ResponseOutputItemAddedEvent | ResponseOutputItemDoneEvent):
        return isinstance(event.item, ResponseFunctionToolCall)
    return False


def _failed_response_message(response: Response) -> str:
    if response.error is not None:
        return f"OpenAI response failed: {response.error.message}"
    if response.status:
        return f"OpenAI response failed with status: {response.status}"
    return "OpenAI response failed"


def _incomplete_response_message(response: Response) -> str:
    if response.incomplete_details is not None and response.incomplete_details.reason:
        return f"OpenAI response incomplete: {response.incomplete_details.reason}"
    if response.status:
        return f"OpenAI response incomplete with status: {response.status}"
    return "OpenAI response incomplete"


def _messages_to_response_input(messages: Sequence[Message]) -> ResponseInputParam:
    response_input: ResponseInputParam = []
    for message in messages:
        message_param: EasyInputMessageParam = {
            "role": _to_openai_role(message.role),
            "content": message.content,
        }
        response_input.append(message_param)
    return response_input


def _tool_specs_to_openai_tools(tools: Sequence[ToolSpec]) -> list[FunctionToolParam]:
    return [_tool_spec_to_openai_tool(tool) for tool in tools]


def _tool_spec_to_openai_tool(tool: ToolSpec) -> FunctionToolParam:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": deepcopy(dict(tool.input_schema)),
        "strict": False,
    }


def _to_openai_role(role: Role) -> OpenAIRole:
    if role == "tool":
        return "user"
    return role


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
