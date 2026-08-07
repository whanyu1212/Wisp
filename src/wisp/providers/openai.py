"""OpenAI Responses API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from json import JSONDecodeError, loads
from typing import Literal, Protocol, cast, runtime_checkable

import anyio
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError
from openai.types.responses import (
    EasyInputMessageParam,
    FunctionToolParam,
    Response,
    ResponseCompletedEvent,
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
from wisp.providers.auth import ProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    JsonObject,
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
OpenAIRole = Literal["user", "assistant", "system", "developer"]


@runtime_checkable
class _ClosableResponseStream(Protocol):
    async def close(self) -> None: ...


class OpenAIProvider:
    """Provider backed by OpenAI's Responses API."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_OPENAI_MODEL,
        client: AsyncOpenAI | None = None,
        auth_resolver: ProviderAuthResolver | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.default_model: str | None = default_model
        self._api_key = _normalize_optional(api_key)
        self._client = client
        self._client_is_injected = client is not None
        self._client_api_key: str | None = None
        self._auth_resolver = auth_resolver
        self._retry_policy = retry_policy or RetryPolicy()

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
        """Stream a normalized OpenAI response lifecycle.

        ``effort`` maps to ``reasoning.effort`` on the Responses API
        (``"none"``/``"minimal"``/``"low"``/``"medium"``/``"high"``/
        ``"xhigh"``, model-dependent) -- passed through unvalidated.
        """

        selected_model = model or self.default_model or DEFAULT_OPENAI_MODEL
        stream: AsyncIterator[ResponseStreamEvent] | None = None
        for retry_number in range(self._retry_policy.max_retries + 1):
            try:
                stream = await self._create_stream(
                    messages,
                    model=selected_model,
                    tools=tools,
                    tool_results=tool_results,
                    previous_response_id=previous_response_id,
                    effort=effort,
                )
                break
            except OpenAIError as exc:
                decision = _openai_retry_decision(exc)
                if decision is None or retry_number >= self._retry_policy.max_retries:
                    raise
                delay = retry_delay_seconds(
                    self._retry_policy,
                    retry_number=retry_number + 1,
                    retry_after_seconds=decision.retry_after_seconds,
                )
                if delay is None:
                    raise
                yield ProviderRetrying(
                    attempt=retry_number + 2,
                    max_attempts=self._retry_policy.max_retries + 1,
                    delay_seconds=delay,
                    reason=decision.reason,
                    status_code=decision.status_code,
                )
                await anyio.sleep(delay)
        if stream is None:
            raise AssertionError("OpenAI retry loop completed without a stream or error")
        response_id: str | None = previous_response_id
        pending_tool_calls: dict[str, ResponseFunctionToolCall] = {}
        completed_tool_arguments: dict[str, str] = {}
        emitted_tool_item_ids: set[str] = set()
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: ProviderUsage | None = None
        failure: ProviderResponseFailed | None = None
        stream_completed = False

        yield ProviderResponseStarted(model=selected_model)

        try:
            async for event in stream:
                if isinstance(event, ResponseCreatedEvent):
                    response_id = event.response.id
                elif isinstance(event, ResponseCompletedEvent):
                    response_id = event.response.id
                    usage = _usage_from_openai(event.response)
                    stream_completed = True
                    break
                elif isinstance(event, ResponseTextDeltaEvent | ResponseRefusalDeltaEvent):
                    chunks.append(event.delta)
                    yield ProviderTextDelta(
                        delta=event.delta,
                        content_index=event.content_index,
                    )
                elif isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                    completed_tool_arguments[event.item_id] = event.arguments
                    pending = pending_tool_calls.get(event.item_id)
                    if pending is not None:
                        tool_call = _tool_call_from_openai(
                            call_id=pending.call_id,
                            name=pending.name,
                            raw_arguments=event.arguments,
                            response_id=response_id,
                        )
                        tool_calls.append(tool_call)
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
                            tool_call = _tool_call_from_openai(
                                call_id=event.item.call_id,
                                name=event.item.name,
                                raw_arguments=raw_arguments,
                                response_id=response_id,
                            )
                            tool_calls.append(tool_call)
                            if item_id is not None:
                                emitted_tool_item_ids.add(item_id)
                elif isinstance(event, ResponseErrorEvent):
                    failure = ProviderResponseFailed(
                        message=f"OpenAI API error: {event.message}",
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
                elif isinstance(event, ResponseFailedEvent):
                    failure = ProviderResponseFailed(
                        message=_failed_response_message(event.response),
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
                elif isinstance(event, ResponseIncompleteEvent):
                    failure = ProviderResponseFailed(
                        message=_incomplete_response_message(event.response),
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
        except OpenAIError as exc:
            failure = failure or ProviderResponseFailed(
                message=f"OpenAI stream error: {exc}",
                partial_content="".join(chunks),
                response_id=response_id,
            )
        finally:
            if isinstance(stream, _ClosableResponseStream):
                await stream.close()

        if failure is None and not stream_completed:
            failure = ProviderResponseFailed(
                message="OpenAI stream ended before response.completed was received",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is not None:
            yield failure
            return

        for content_index, tool_call in enumerate(tool_calls):
            yield ProviderToolCallCompleted(tool_call=tool_call, content_index=content_index)

        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=usage,
        )

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        client = await self._client_or_create()
        openai_tools = _tool_specs_to_openai_tools(tools)
        response_input = (
            _tool_results_to_response_input(tool_results)
            if tool_results
            else _messages_to_response_input(messages)
        )

        # Built as a single kwargs dict rather than a create() call per
        # tools/previous_response_id/effort combination: branching per
        # optional-parameter combination doesn't scale past two independent
        # optional dimensions. mypy cannot match a **kwargs dict against
        # create()'s `@overload`s (they discriminate on `stream`, but mypy's
        # overload resolution rejects a dict-unpack call regardless) -- the
        # `create` rebinding below is the single, contained concession to
        # that limitation; every kwarg's value is still built from typed
        # sources above.
        kwargs: dict[str, object] = {
            "model": model,
            "input": response_input,
            "stream": True,
        }
        if openai_tools:
            kwargs["tools"] = openai_tools
        if previous_response_id is not None:
            kwargs["previous_response_id"] = previous_response_id
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}
        create = cast(Callable[..., Awaitable[object]], client.responses.create)
        stream = await create(**kwargs)
        return cast(AsyncIterator[ResponseStreamEvent], stream)

    async def _client_or_create(self) -> AsyncOpenAI:
        if self._client_is_injected:
            assert self._client is not None
            return self._client

        stored_api_key = (
            await self._auth_resolver.api_key(self.name)
            if self._auth_resolver is not None
            else None
        )
        api_key = (
            self._api_key or _normalize_optional(os.environ.get("OPENAI_API_KEY")) or stored_api_key
        )
        if api_key is None:
            raise ProviderConfigurationError(
                "openai credentials are required; run `/connect` in the TUI or set OPENAI_API_KEY"
            )
        if self._client is not None and self._client_api_key == api_key:
            return self._client

        # Wisp emits retry progress itself. Key changes replace only Wisp-owned clients.
        if self._client is not None:
            await self._client.close()
        self._client = AsyncOpenAI(api_key=api_key, max_retries=0)
        self._client_api_key = api_key
        return self._client


def _openai_retry_decision(exc: OpenAIError) -> RetryDecision | None:
    if isinstance(exc, APITimeoutError):
        return RetryDecision(reason="timeout")
    if isinstance(exc, APIConnectionError):
        return RetryDecision(reason="network")
    if isinstance(exc, APIStatusError):
        return http_retry_decision(
            status_code=exc.status_code,
            headers=exc.response.headers,
            error_body=exc.body,
        )
    return None


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


def _usage_from_openai(response: Response) -> ProviderUsage | None:
    usage = response.usage
    if usage is None:
        return None
    input_details = usage.input_tokens_details
    output_details = usage.output_tokens_details
    return ProviderUsage(
        input_tokens=max(0, usage.input_tokens),
        output_tokens=max(0, usage.output_tokens),
        total_tokens=max(0, usage.total_tokens),
        cache_read_input_tokens=(
            max(0, input_details.cached_tokens) if input_details is not None else None
        ),
        reasoning_output_tokens=(
            max(0, output_details.reasoning_tokens) if output_details is not None else None
        ),
    )


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
