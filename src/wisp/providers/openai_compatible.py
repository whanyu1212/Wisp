"""OpenAI-compatible Chat Completions provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from json import JSONDecodeError, loads
from typing import Protocol, cast, runtime_checkable
from uuid import uuid4

import anyio
import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionChunk

from wisp.agent.messages import Message
from wisp.openai_compatible import (
    openai_compatible_api_key_environment,
    validate_openai_compatible_provider_name,
)
from wisp.providers.auth import ProviderAuthResolver
from wisp.providers.base import ProviderConfigurationError, ToolCallResult, ToolSpec
from wisp.providers.continuations import ContinuationStore
from wisp.providers.events import (
    JsonObject,
    ProviderEvent,
    ProviderFinishReason,
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

OPENAI_COMPATIBLE_PROVIDER_NAME = "openai-compatible"
OPENAI_COMPATIBLE_API_KEY_ENV = "OPENAI_COMPATIBLE_API_KEY"

# Chat Completions message/tool dictionaries are kept provider-local. The OpenAI SDK's
# generated TypedDict union changes frequently, while compatible servers intentionally
# implement a smaller, structurally equivalent wire contract.
type ChatPayload = dict[str, object]


@runtime_checkable
class _ClosableChatStream(Protocol):
    async def close(self) -> None: ...


@dataclass(slots=True)
class _ToolCallAccumulator:
    call_id: str = ""
    name_chunks: list[str] = field(default_factory=list)
    argument_chunks: list[str] = field(default_factory=list)


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible streaming Chat Completions endpoints."""

    name = OPENAI_COMPATIBLE_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        default_model: str,
        provider_name: str = OPENAI_COMPATIBLE_PROVIDER_NAME,
        requires_api_key: bool = True,
        ca_bundle: str | os.PathLike[str] | None = None,
        api_key: str | None = None,
        client: AsyncOpenAI | None = None,
        auth_resolver: ProviderAuthResolver | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.name = validate_openai_compatible_provider_name(provider_name)
        self.default_model: str | None = default_model
        self._base_url = base_url
        self._requires_api_key = requires_api_key
        self._ca_bundle = os.fspath(ca_bundle) if ca_bundle is not None else None
        self._api_key = _normalize_optional(api_key)
        self._client = client
        self._client_is_injected = client is not None
        self._client_api_key: str | None = None
        self._auth_resolver = auth_resolver
        self._retry_policy = retry_policy or RetryPolicy()
        self._continuations = ContinuationStore[tuple[ChatPayload, ...]]()

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
        """Stream one normalized Chat Completions response lifecycle."""

        selected_model = model or self.default_model
        if selected_model is None:
            raise ProviderConfigurationError(f"{self.name} requires a model")

        native_stream: AsyncIterator[ChatCompletionChunk] | None = None
        for retry_number in range(self._retry_policy.max_retries + 1):
            try:
                native_stream = await self._create_stream(
                    messages,
                    model=selected_model,
                    tools=tools,
                    tool_results=tool_results,
                    previous_response_id=previous_response_id,
                    effort=effort,
                )
                break
            except OpenAIError as exc:
                decision = _retry_decision(exc)
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
        if native_stream is None:
            raise AssertionError(f"{self.name} retry loop completed without a stream or error")

        yield ProviderResponseStarted(model=selected_model)
        response_id: str | None = None
        chunks: list[str] = []
        accumulators: dict[int, _ToolCallAccumulator] = {}
        finish_reason: str | None = None
        usage: ProviderUsage | None = None
        failure: ProviderResponseFailed | None = None

        try:
            async for chunk in native_stream:
                response_id = chunk.id or response_id
                if chunk.usage is not None:
                    usage = _usage_from_chat(chunk)
                for choice in chunk.choices:
                    if choice.index != 0:
                        continue
                    delta = choice.delta
                    text = delta.content or delta.refusal
                    if text:
                        chunks.append(text)
                        yield ProviderTextDelta(delta=text)
                    for fragment in delta.tool_calls or ():
                        accumulator = accumulators.setdefault(
                            fragment.index, _ToolCallAccumulator()
                        )
                        if fragment.id:
                            accumulator.call_id = fragment.id
                        if fragment.function is not None:
                            if fragment.function.name:
                                accumulator.name_chunks.append(fragment.function.name)
                            if fragment.function.arguments:
                                accumulator.argument_chunks.append(fragment.function.arguments)
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
        except OpenAIError as exc:
            failure = ProviderResponseFailed(
                message=f"{self.name} stream error: {exc}",
                partial_content="".join(chunks),
                response_id=response_id,
            )
        finally:
            if isinstance(native_stream, _ClosableChatStream):
                await native_stream.close()

        if failure is None and finish_reason is None:
            failure = ProviderResponseFailed(
                message=f"{self.name} stream ended before a finish reason was received",
                partial_content="".join(chunks),
                response_id=response_id,
            )
        if failure is None and finish_reason not in {"stop", "tool_calls", "length"}:
            failure = ProviderResponseFailed(
                message=f"{self.name} stream ended with unsupported reason: {finish_reason}",
                partial_content="".join(chunks),
                response_id=response_id,
            )
        if failure is not None:
            self._continuations.discard(previous_response_id)
            yield failure
            return

        if finish_reason == "tool_calls" and not accumulators:
            self._continuations.discard(previous_response_id)
            yield ProviderResponseFailed(
                message=f"{self.name} response reported tool_calls without any tool calls",
                partial_content="".join(chunks),
                response_id=response_id,
            )
            return

        tool_calls: list[ToolCall] = []
        if finish_reason == "tool_calls":
            for index in sorted(accumulators):
                accumulator = accumulators[index]
                raw_arguments = "".join(accumulator.argument_chunks)
                call_id = accumulator.call_id or f"call_{uuid4().hex}"
                name = "".join(accumulator.name_chunks)
                tool_call = _tool_call(
                    call_id=call_id,
                    name=name,
                    raw_arguments=raw_arguments,
                    response_id=response_id,
                )
                tool_calls.append(tool_call)
                yield ProviderToolCallCompleted(
                    tool_call=tool_call,
                    content_index=len(tool_calls) - 1,
                )

        if tool_calls:
            continuation_id = response_id or f"chatcmpl_wisp_{uuid4().hex}"
            previous = self._get_continuation(previous_response_id)
            assistant = _assistant_tool_message("".join(chunks), tool_calls)
            replay = (
                *previous,
                *(_tool_results_to_messages(tool_results) if tool_results else ()),
                assistant,
            )
            if previous_response_id != continuation_id:
                self._continuations.consume(previous_response_id)
            self._continuations.remember(continuation_id, replay)
            response_id = continuation_id
        else:
            self._continuations.consume(previous_response_id)

        normalized_reason: ProviderFinishReason = cast(
            ProviderFinishReason, "tool_calls" if tool_calls else finish_reason
        )
        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason=normalized_reason,
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
    ) -> AsyncIterator[ChatCompletionChunk]:
        client = await self._client_or_create()
        chat_messages = _messages_to_chat(messages)
        if tool_results:
            chat_messages.extend(self._get_continuation(previous_response_id))
            chat_messages.extend(_tool_results_to_messages(tool_results))
        kwargs: dict[str, object] = {
            "model": model,
            "messages": chat_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            kwargs["tools"] = [_tool_spec_to_chat(tool) for tool in tools]
        if effort is not None:
            kwargs["reasoning_effort"] = effort
        create = cast(Callable[..., Awaitable[object]], client.chat.completions.create)
        stream = await create(**kwargs)
        return cast(AsyncIterator[ChatCompletionChunk], stream)

    async def _client_or_create(self) -> AsyncOpenAI:
        if self._client_is_injected:
            assert self._client is not None
            return self._client

        provider_api_key_env = openai_compatible_api_key_environment(self.name)
        api_key = self._api_key or _normalize_optional(os.environ.get(provider_api_key_env))
        if api_key is None:
            api_key = _normalize_optional(os.environ.get(OPENAI_COMPATIBLE_API_KEY_ENV))
        if api_key is None and self._auth_resolver is not None:
            api_key = await self._auth_resolver.api_key(self.name)
        if api_key is None:
            if self._requires_api_key:
                raise ProviderConfigurationError(
                    f"{self.name} credentials are required; run `/connect {self.name}` in the TUI "
                    f"or set {provider_api_key_env} (fallback: {OPENAI_COMPATIBLE_API_KEY_ENV})"
                )
            api_key = "not-required"
        if self._client is not None and self._client_api_key == api_key:
            return self._client
        if self._client is not None:
            await self._client.close()
        http_client = (
            httpx.AsyncClient(verify=self._ca_bundle) if self._ca_bundle is not None else None
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=self._base_url,
            max_retries=0,
            http_client=http_client,
        )
        self._client_api_key = api_key
        return self._client

    async def aclose(self) -> None:
        """Close the Wisp-owned OpenAI client and its HTTP transport."""

        if self._client_is_injected or self._client is None:
            return
        client = self._client
        self._client = None
        self._client_api_key = None
        await client.close()

    def _get_continuation(self, response_id: str | None) -> tuple[ChatPayload, ...]:
        return self._continuations.get(response_id) or ()


def _messages_to_chat(messages: Sequence[Message]) -> list[ChatPayload]:
    result: list[ChatPayload] = []
    for message in messages:
        if message.role == "tool" and message.tool_call_id:
            result.append(
                {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id}
            )
        else:
            role = "user" if message.role == "tool" else message.role
            result.append({"role": role, "content": message.content})
    return result


def _tool_results_to_messages(results: Sequence[ToolCallResult]) -> tuple[ChatPayload, ...]:
    return tuple(
        {"role": "tool", "tool_call_id": result.call_id, "content": result.output}
        for result in results
    )


def _assistant_tool_message(content: str, tool_calls: Sequence[ToolCall]) -> ChatPayload:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": tool_call.call_id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.raw_arguments,
                },
            }
            for tool_call in tool_calls
        ],
    }


def _tool_spec_to_chat(tool: ToolSpec) -> ChatPayload:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": deepcopy(dict(tool.input_schema)),
        },
    }


def _tool_call(*, call_id: str, name: str, raw_arguments: str, response_id: str | None) -> ToolCall:
    parse_error: str | None
    try:
        parsed = loads(raw_arguments or "{}")
    except JSONDecodeError as exc:
        arguments: JsonObject = {}
        parse_error = f"Invalid JSON arguments for tool {name}: {exc.msg}"
    else:
        if isinstance(parsed, dict):
            arguments = cast(JsonObject, parsed)
            parse_error = None
        else:
            arguments = {}
            parse_error = f"Invalid JSON arguments for tool {name}: expected an object"
    if not name:
        parse_error = "Invalid tool call: missing function name"
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        response_id=response_id,
        parse_error=parse_error,
    )


def _usage_from_chat(chunk: ChatCompletionChunk) -> ProviderUsage | None:
    value = chunk.usage
    if value is None:
        return None
    prompt_details = value.prompt_tokens_details
    completion_details = value.completion_tokens_details
    return ProviderUsage(
        input_tokens=max(0, value.prompt_tokens),
        output_tokens=max(0, value.completion_tokens),
        total_tokens=max(0, value.total_tokens),
        cache_read_input_tokens=(
            max(0, prompt_details.cached_tokens)
            if prompt_details is not None and prompt_details.cached_tokens is not None
            else None
        ),
        reasoning_output_tokens=(
            max(0, completion_details.reasoning_tokens)
            if completion_details is not None and completion_details.reasoning_tokens is not None
            else None
        ),
    )


def _retry_decision(exc: OpenAIError) -> RetryDecision | None:
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


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "OPENAI_COMPATIBLE_API_KEY_ENV",
    "OPENAI_COMPATIBLE_PROVIDER_NAME",
    "OpenAICompatibleProvider",
]
