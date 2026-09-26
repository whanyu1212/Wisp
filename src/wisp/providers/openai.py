"""OpenAI Responses API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from json import JSONDecodeError, dumps, loads
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
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseReasoningTextDeltaEvent,
    ResponseRefusalDeltaEvent,
    ResponseStreamEvent,
    ResponseTextDeltaEvent,
)
from openai.types.responses.response_input_param import ResponseInputItemParam, ResponseInputParam

from wisp.agent.messages import Message, NativeOutput, Role
from wisp.providers.auth import ProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ToolCallResult,
    ToolSpec,
    is_context_overflow_message,
)
from wisp.providers.continuations import ContinuationStore
from wisp.providers.events import (
    JsonObject,
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
OpenAIRole = Literal["user", "assistant", "system", "developer"]
# Responses whose output items are kept in memory for replay on later prompts.
# Sessions also save each response's items with its row (see native_output_for).
_NATIVE_OUTPUT_CAPACITY = 2048

type _NativeOutputLookup = Callable[[Message], tuple[dict[str, object], ...] | None]


@runtime_checkable
class _ClosableResponseStream(Protocol):
    async def close(self) -> None: ...


class OpenAIProvider:
    """Provider backed by OpenAI's Responses API."""

    name = "openai"
    _display_name = "OpenAI"
    _api_key_environment = "OPENAI_API_KEY"
    _connect_command = "/connect"
    _base_url: str | None = None
    supports_prompt_cache_key = True
    supports_continuation_messages: Literal[True] = True
    # Whether a fresh request replays each earlier response's own output items
    # (including encrypted reasoning) instead of a portable rebuild, so the
    # prompt cache reaches the previous run's tail. Enabled per provider once
    # its backend has been checked to accept the replayed items.
    _replays_native_output: bool = False

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        """Declare that portable call/result pairs can be reconstructed natively."""

        del effort
        return True

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
        self._native_outputs = ContinuationStore[NativeOutput](capacity=_NATIVE_OUTPUT_CAPACITY)

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
        """Stream a normalized OpenAI response lifecycle.

        ``effort`` maps to ``reasoning.effort`` on the Responses API
        (``"none"``/``"minimal"``/``"low"``/``"medium"``/``"high"``/
        ``"xhigh"``, model-dependent) -- passed through unvalidated.
        """

        selected_model = model or self.default_model or DEFAULT_OPENAI_MODEL
        stream: AsyncIterator[ResponseStreamEvent] | None = None
        for retry_number in range(self._retry_policy.max_retries + 1):
            try:
                if extra_messages:
                    if prompt_cache_key is not None:
                        stream = await self._create_stream(
                            messages,
                            model=selected_model,
                            tools=tools,
                            tool_results=tool_results,
                            extra_messages=extra_messages,
                            previous_response_id=previous_response_id,
                            effort=effort,
                            prompt_cache_key=prompt_cache_key,
                        )
                    else:
                        stream = await self._create_stream(
                            messages,
                            model=selected_model,
                            tools=tools,
                            tool_results=tool_results,
                            extra_messages=extra_messages,
                            previous_response_id=previous_response_id,
                            effort=effort,
                        )
                elif prompt_cache_key is not None:
                    stream = await self._create_stream(
                        messages,
                        model=selected_model,
                        tools=tools,
                        tool_results=tool_results,
                        previous_response_id=previous_response_id,
                        effort=effort,
                        prompt_cache_key=prompt_cache_key,
                    )
                else:
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
                    yield ProviderResponseFailed(
                        message=str(exc),
                        failure_kind=(
                            "context_overflow" if is_context_overflow_message(str(exc)) else "error"
                        ),
                    )
                    return
                delay = retry_delay_seconds(
                    self._retry_policy,
                    retry_number=retry_number + 1,
                    retry_after_seconds=decision.retry_after_seconds,
                )
                if delay is None:
                    yield ProviderResponseFailed(message=str(exc))
                    return
                yield ProviderRetrying(
                    attempt=retry_number + 2,
                    max_attempts=self._retry_policy.max_retries + 1,
                    delay_seconds=delay,
                    reason=decision.reason,
                    status_code=decision.status_code,
                )
                await anyio.sleep(delay)
        if stream is None:
            raise AssertionError(
                f"{self._display_name} retry loop completed without a stream or error"
            )
        # A completion ID identifies this upstream response only. Do not
        # expose the prior continuation cursor when an unusual stream omits a
        # current response ID.
        response_id: str | None = None
        pending_tool_calls: dict[str, ResponseFunctionToolCall] = {}
        completed_tool_arguments: dict[str, str] = {}
        emitted_tool_item_ids: set[str] = set()
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: ProviderUsage | None = None
        completed_response: Response | None = None
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
                    completed_response = event.response
                    stream_completed = True
                    break
                elif isinstance(event, ResponseTextDeltaEvent | ResponseRefusalDeltaEvent):
                    chunks.append(event.delta)
                    yield ProviderTextDelta(
                        delta=event.delta,
                        content_index=event.content_index,
                    )
                elif isinstance(
                    event,
                    ResponseReasoningTextDeltaEvent | ResponseReasoningSummaryTextDeltaEvent,
                ):
                    yield ProviderThinkingDelta(
                        delta=event.delta,
                        content_index=event.output_index,
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
                        message=f"{self._display_name} API error: {event.message}",
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
                elif isinstance(event, ResponseFailedEvent):
                    failure = ProviderResponseFailed(
                        message=_failed_response_message(
                            event.response, display_name=self._display_name
                        ),
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
                elif isinstance(event, ResponseIncompleteEvent):
                    failure = ProviderResponseFailed(
                        message=_incomplete_response_message(
                            event.response, display_name=self._display_name
                        ),
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
        except OpenAIError as exc:
            failure = failure or ProviderResponseFailed(
                message=f"{self._display_name} stream error: {exc}",
                partial_content="".join(chunks),
                response_id=response_id,
            )
        finally:
            if isinstance(stream, _ClosableResponseStream):
                await stream.close()

        if failure is None and not stream_completed:
            failure = ProviderResponseFailed(
                message=(
                    f"{self._display_name} stream ended before response.completed was received"
                ),
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is not None:
            yield failure
            return
        if previous_response_id is not None and response_id is None:
            # Unlike the stateless adapters, the Responses API keeps this
            # chain server-side. Reusing an old cursor would omit the clean
            # response that just completed, so fail rather than corrupt a
            # later continuation.
            yield ProviderResponseFailed(
                message=(
                    f"{self._display_name} continuation response did not include a response id"
                ),
                partial_content="".join(chunks),
            )
            return

        if (
            self._replays_native_output
            and response_id is not None
            and completed_response is not None
        ):
            # A later prompt rebuilds this response from its transcript row;
            # replaying these exact items keeps that rebuild cacheable.
            replay_items = _replay_items(completed_response)
            if replay_items:
                self._native_outputs.remember(
                    response_id,
                    NativeOutput(provider=self.name, model=selected_model, items=replay_items),
                )

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
        extra_messages: Sequence[Message] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
        prompt_cache_key: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        client = await self._client_or_create()
        openai_tools = _tool_specs_to_openai_tools(tools)
        explicit_prompt_cache = _uses_explicit_prompt_cache(
            messages,
            model=model,
            prompt_cache_key=prompt_cache_key,
        )
        # The Responses API retains the prior chain server-side. A fresh
        # request receives the complete supplied base; a continued request
        # receives only this round's tool outputs and appended user messages.
        response_input = (
            _messages_to_response_input(
                (*messages, *extra_messages),
                explicit_prompt_cache=explicit_prompt_cache,
                native_output=(
                    (lambda message: self._native_output(message, model=model))
                    if self._replays_native_output
                    else None
                ),
            )
            if previous_response_id is None
            else [
                *_tool_results_to_response_input(tool_results),
                *_messages_to_response_input(extra_messages),
            ]
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
            **self._request_options(),
        }
        if openai_tools:
            kwargs["tools"] = openai_tools
        if previous_response_id is not None:
            kwargs["previous_response_id"] = previous_response_id
        if effort is not None:
            kwargs["reasoning"] = {"effort": effort}
        if prompt_cache_key is not None:
            kwargs["prompt_cache_key"] = prompt_cache_key
        if self._replays_native_output:
            kwargs["include"] = ["reasoning.encrypted_content"]
        if explicit_prompt_cache:
            # openai-python 2.44 does not yet expose GPT-5.6's request-wide
            # prompt_cache_options field, so use its documented forward-compatible
            # escape hatch while keeping every typed field above unchanged.
            kwargs["extra_body"] = {"prompt_cache_options": {"mode": "explicit"}}
        create = cast(Callable[..., Awaitable[object]], client.responses.create)
        stream = await create(**kwargs)
        return cast(AsyncIterator[ResponseStreamEvent], stream)

    def _request_options(self) -> dict[str, object]:
        """Return provider-specific Responses request fields."""

        return {}

    def native_output_for(self, response_id: str) -> NativeOutput | None:
        """Return the output items recorded for one of this provider's responses.

        Args:
            response_id (str): Upstream response ID from a completed response.

        Returns:
            NativeOutput | None: The response's items, or None when replay is
            disabled for this provider or the response is unknown.
        """

        return self._native_outputs.get(response_id)

    def _native_output(
        self,
        message: Message,
        *,
        model: str,
    ) -> tuple[dict[str, object], ...] | None:
        """Return the output items that produced an assistant row, if replayable.

        Items saved on the row win over the in-memory record. Encrypted reasoning
        belongs to the model that produced it, so items from another provider or
        model are not replayed, and a record without a message item cannot stand
        for a row that has text.

        Args:
            message (Message): Transcript row being rebuilt for a fresh request.
            model (str): Model the request is sent to.

        Returns:
            tuple[dict[str, object], ...] | None: The response's items in their
            original order, or None to render the row in portable form.
        """

        if message.role != "assistant" or message.response_id is None:
            return None
        native = message.native_output or self._native_outputs.get(
            message.response_id, refresh=True
        )
        if native is None or native.provider != self.name or native.model != model:
            return None
        if message.content and not any(item.get("type") == "message" for item in native.items):
            return None
        return native.items

    async def _client_or_create(self) -> AsyncOpenAI:
        if self._client_is_injected:
            assert self._client is not None
            return self._client

        api_key = self._api_key or _normalize_optional(os.environ.get(self._api_key_environment))
        if api_key is None and self._auth_resolver is not None:
            api_key = await self._auth_resolver.api_key(self.name)
        if api_key is None:
            raise ProviderConfigurationError(
                f"{self.name} credentials are required; run `{self._connect_command}` in the TUI "
                f"or set {self._api_key_environment}"
            )
        if self._client is not None and self._client_api_key == api_key:
            return self._client

        # Wisp emits retry progress itself. Key changes replace only Wisp-owned clients.
        if self._client is not None:
            await self._client.close()
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            max_retries=0,
        )
        self._client_api_key = api_key
        return self._client

    async def aclose(self) -> None:
        """Close the Wisp-owned OpenAI-compatible client and transport."""

        if self._client_is_injected or self._client is None:
            return
        client = self._client
        self._client = None
        self._client_api_key = None
        await client.close()


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


def _failed_response_message(response: Response, *, display_name: str = "OpenAI") -> str:
    if response.error is not None:
        return f"{display_name} response failed: {response.error.message}"
    if response.status:
        return f"{display_name} response failed with status: {response.status}"
    return f"{display_name} response failed"


def _incomplete_response_message(response: Response, *, display_name: str = "OpenAI") -> str:
    if response.incomplete_details is not None and response.incomplete_details.reason:
        return f"{display_name} response incomplete: {response.incomplete_details.reason}"
    if response.status:
        return f"{display_name} response incomplete with status: {response.status}"
    return f"{display_name} response incomplete"


def _replay_items(response: Response) -> tuple[dict[str, object], ...]:
    """Return a completed response's output items as a fresh request can resend them."""

    items: list[dict[str, object]] = []
    for item in response.output:
        payload = item.model_dump(mode="json", exclude_none=True)
        payload.pop("id", None)
        items.append(payload)
    return tuple(items)


def _messages_to_response_input(
    messages: Sequence[Message],
    *,
    explicit_prompt_cache: bool = False,
    native_output: _NativeOutputLookup | None = None,
) -> ResponseInputParam:
    response_input: ResponseInputParam = []
    boundary_written = False
    for message in messages:
        native_items = native_output(message) if native_output is not None else None
        if native_items is not None:
            response_input.extend(cast(list[ResponseInputItemParam], list(native_items)))
            continue
        if message.role == "tool" and message.tool_call_id:
            response_input.append(
                cast(
                    ResponseInputItemParam,
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content,
                    },
                )
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            if message.content:
                response_input.append({"role": "assistant", "content": message.content})
            for tool_call in message.tool_calls:
                response_input.append(
                    cast(
                        ResponseInputItemParam,
                        {
                            "type": "function_call",
                            "call_id": tool_call.call_id,
                            "name": tool_call.name,
                            "arguments": dumps(dict(tool_call.arguments), separators=(",", ":")),
                        },
                    )
                )
            continue
        if explicit_prompt_cache and message.prompt_cache_boundary and not boundary_written:
            message_param = cast(
                EasyInputMessageParam,
                {
                    "role": _to_openai_role(message.role),
                    "content": [
                        {
                            "type": "input_text",
                            "text": message.content,
                            "prompt_cache_breakpoint": {"mode": "explicit"},
                        }
                    ],
                },
            )
            boundary_written = True
        else:
            message_param = {
                "role": _to_openai_role(message.role),
                "content": message.content,
            }
        response_input.append(message_param)
    return response_input


def _uses_explicit_prompt_cache(
    messages: Sequence[Message],
    *,
    model: str,
    prompt_cache_key: str | None,
) -> bool:
    return (
        prompt_cache_key is not None
        and (model == "gpt-5.6" or model.startswith("gpt-5.6-"))
        and any(message.prompt_cache_boundary and message.content for message in messages)
    )


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
            _nonnegative_int(input_details.cached_tokens) if input_details is not None else None
        ),
        cache_write_input_tokens=(
            _nonnegative_int(getattr(input_details, "cache_write_tokens", None))
            if input_details is not None
            else None
        ),
        reasoning_output_tokens=(
            _nonnegative_int(output_details.reasoning_tokens)
            if output_details is not None
            else None
        ),
    )


def _nonnegative_int(value: object) -> int | None:
    return max(0, value) if type(value) is int else None


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None
