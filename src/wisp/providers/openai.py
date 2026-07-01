"""OpenAI Responses API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from json import JSONDecodeError, loads
from typing import Literal, cast

from openai import AsyncOpenAI
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    Response,
    ResponseCreatedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
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
from wisp.providers.base import (
    JsonObject,
    ProviderConfigurationError,
    ProviderError,
    ProviderStreamEvent,
    ToolCall,
    ToolCallResult,
    ToolSpec,
)

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
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Stream text deltas and tool calls from OpenAI's Responses API."""

        selected_model = model or self.default_model or DEFAULT_OPENAI_MODEL
        stream = await self._create_stream(
            messages,
            model=selected_model,
            tools=tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
        )
        response_id: str | None = previous_response_id
        pending_tool_calls: dict[str, ResponseFunctionToolCall] = {}
        completed_tool_arguments: dict[str, str] = {}
        emitted_tool_item_ids: set[str] = set()

        async for event in stream:
            if isinstance(event, ResponseCreatedEvent):
                response_id = event.response.id
            elif isinstance(event, ResponseTextDeltaEvent | ResponseRefusalDeltaEvent):
                yield event.delta
            elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                completed_tool_arguments[event.item_id] = event.arguments
                pending = pending_tool_calls.get(event.item_id)
                if pending is not None:
                    yield _tool_call_from_openai(
                        call_id=pending.call_id,
                        name=pending.name,
                        raw_arguments=event.arguments,
                        response_id=response_id,
                    )
                    emitted_tool_item_ids.add(event.item_id)
            elif isinstance(event, ResponseOutputItemAddedEvent | ResponseOutputItemDoneEvent):
                if isinstance(event.item, ResponseFunctionToolCall):
                    item_id = event.item.id
                    if item_id is not None:
                        pending_tool_calls[item_id] = event.item
                    already_emitted = item_id is not None and item_id in emitted_tool_item_ids
                    if isinstance(event, ResponseOutputItemDoneEvent) and not already_emitted:
                        raw_arguments = (
                            completed_tool_arguments.get(item_id, event.item.arguments)
                            if item_id is not None
                            else event.item.arguments
                        )
                        yield _tool_call_from_openai(
                            call_id=event.item.call_id,
                            name=event.item.name,
                            raw_arguments=raw_arguments,
                            response_id=response_id,
                        )
                        if item_id is not None:
                            emitted_tool_item_ids.add(item_id)
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
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        client = self._client_or_create()
        openai_tools = _tool_specs_to_openai_tools(tools)
        response_input = (
            _tool_results_to_response_input(tool_results)
            if tool_results
            else _messages_to_response_input(messages)
        )
        if openai_tools and previous_response_id is not None:
            stream = await client.responses.create(
                model=model,
                input=response_input,
                stream=True,
                tools=openai_tools,
                previous_response_id=previous_response_id,
            )
        elif openai_tools:
            stream = await client.responses.create(
                model=model,
                input=response_input,
                stream=True,
                tools=openai_tools,
            )
        elif previous_response_id is not None:
            stream = await client.responses.create(
                model=model,
                input=response_input,
                stream=True,
                previous_response_id=previous_response_id,
            )
        else:
            stream = await client.responses.create(
                model=model,
                input=response_input,
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


def _tool_call_from_openai(
    *,
    call_id: str,
    name: str,
    raw_arguments: str,
    response_id: str | None,
) -> ToolCall:
    arguments, parse_error = _parse_tool_arguments(name=name, raw_arguments=raw_arguments)
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        response_id=response_id,
        parse_error=parse_error,
    )


def _parse_tool_arguments(*, name: str, raw_arguments: str) -> tuple[JsonObject, str | None]:
    try:
        parsed = loads(raw_arguments or "{}")
    except JSONDecodeError as exc:
        return {}, f"Invalid JSON arguments for tool {name}: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, f"Invalid JSON arguments for tool {name}: expected an object"
    return cast(JsonObject, parsed), None


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


def _tool_results_to_response_input(tool_results: Sequence[ToolCallResult]) -> ResponseInputParam:
    response_input: ResponseInputParam = []
    for result in tool_results:
        response_input.append(
            {
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": result.output,
            }
        )
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
