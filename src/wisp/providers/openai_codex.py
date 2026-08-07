"""OpenAI Codex provider backed by ChatGPT subscription auth."""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from json import JSONDecodeError
from typing import cast

import anyio
import httpx

from wisp.agent.messages import Message, Role
from wisp.auth.openai_codex import account_id_from_access_token, refresh_openai_codex_token
from wisp.auth.storage import JsonAuthStore
from wisp.config import default_auth_path
from wisp.providers.auth import ProviderAuthResolver, StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
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
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_OPENAI_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
_CODEX_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)


class _CodexHTTPError(ProviderError):
    """HTTP failure retaining the metadata needed for retry classification."""

    def __init__(
        self, *, status_code: int, headers: Mapping[str, str], body: bytes, reason: str
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers)
        self.body = body
        super().__init__(f"OpenAI Codex API error ({status_code}): {reason}")


class OpenAICodexProvider:
    """Provider for ChatGPT Plus/Pro Codex subscription access."""

    name = "openai-codex"

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_OPENAI_CODEX_MODEL,
        auth_resolver: ProviderAuthResolver | None = None,
        base_url: str = DEFAULT_CODEX_BASE_URL,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.default_model: str | None = default_model
        self._auth_resolver = auth_resolver or StoredProviderAuthResolver(
            JsonAuthStore(default_auth_path())
        )
        self._base_url = base_url
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy()
        self._continuations = ContinuationStore[tuple[dict[str, object], ...]]()

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
        """Stream a normalized OpenAI Codex response lifecycle.

        ``effort`` maps to ``reasoning.effort`` on the Responses API request
        body (same shape as ``OpenAIProvider``) -- passed through
        unvalidated.
        """

        selected_model = model or self.default_model or DEFAULT_OPENAI_CODEX_MODEL
        auth = await self._auth_resolver.bearer_token(
            self.name,
            refresh=lambda credential: refresh_openai_codex_token(credential),
        )
        if auth is None:
            raise ProviderConfigurationError(
                "openai-codex credentials are required; run `wisp auth login openai-codex`"
            )
        account_id = auth.account_id or account_id_from_access_token(auth.token)
        headers = _codex_headers(token=auth.token, account_id=account_id)
        continuation_items = self._get_continuation(previous_response_id)
        continuation_input = (
            *continuation_items,
            *_tool_results_to_codex_input(tool_results),
        )
        body = _codex_request_body(
            messages,
            model=selected_model,
            tools=tools,
            continuation_input=continuation_input,
            effort=effort,
        )
        response_id: str | None = previous_response_id
        pending_tool_calls: dict[str, dict[str, object]] = {}
        completed_tool_arguments: dict[str, str] = {}
        emitted_tool_item_ids: set[str] = set()
        output_items: OrderedDict[str, dict[str, object]] = OrderedDict()
        anonymous_output_item = 0
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: ProviderUsage | None = None
        failure: ProviderResponseFailed | None = None
        stream_completed = False
        for retry_number in range(self._retry_policy.max_retries + 1):
            response_started = False
            try:
                async with self._create_stream(body=body, headers=headers) as stream:
                    response_started = True
                    yield ProviderResponseStarted(model=selected_model)

                    async for event in stream:
                        event_type = _string_value(event.get("type"))
                        if event_type in {"response.created", "response.in_progress"}:
                            event_response = event.get("response")
                            if isinstance(event_response, dict):
                                response_id = _string_value(event_response.get("id")) or response_id
                        elif event_type in {"response.output_text.delta", "response.refusal.delta"}:
                            delta = _string_value(event.get("delta"))
                            if delta is not None:
                                chunks.append(delta)
                                yield ProviderTextDelta(delta=delta)
                        elif event_type == "response.function_call_arguments.done":
                            item_id = _string_value(event.get("item_id"))
                            arguments = _string_value(event.get("arguments")) or "{}"
                            if item_id is not None:
                                completed_tool_arguments[item_id] = arguments
                                pending = pending_tool_calls.get(item_id)
                                if pending is not None:
                                    pending["arguments"] = arguments
                                    output_items[item_id] = deepcopy(pending)
                                    tool_call = _tool_call_from_codex(
                                        pending,
                                        raw_arguments=arguments,
                                        response_id=response_id,
                                    )
                                    tool_calls.append(tool_call)
                                    emitted_tool_item_ids.add(item_id)
                        elif event_type in {
                            "response.output_item.added",
                            "response.output_item.done",
                        }:
                            item = event.get("item")
                            if not isinstance(item, dict):
                                continue
                            item_id = _string_value(item.get("id"))
                            item_key = item_id or _string_value(item.get("call_id"))
                            if item_key is None:
                                item_key = f"anonymous-{anonymous_output_item}"
                                anonymous_output_item += 1
                            output_items[item_key] = deepcopy(item)
                            if item.get("type") == "function_call":
                                item_id = _string_value(item.get("id"))
                                if item_id is not None:
                                    pending_tool_calls[item_id] = dict(item)
                                already_emitted = (
                                    item_id is not None and item_id in emitted_tool_item_ids
                                )
                                if (
                                    event_type == "response.output_item.done"
                                    and not already_emitted
                                ):
                                    raw_arguments = (
                                        completed_tool_arguments.get(
                                            item_id, _string_value(item.get("arguments")) or "{}"
                                        )
                                        if item_id is not None
                                        else _string_value(item.get("arguments")) or "{}"
                                    )
                                    tool_call = _tool_call_from_codex(
                                        item,
                                        raw_arguments=raw_arguments,
                                        response_id=response_id,
                                    )
                                    tool_calls.append(tool_call)
                                    if item_id is not None:
                                        emitted_tool_item_ids.add(item_id)
                        elif event_type == "response.completed":
                            event_response = event.get("response")
                            if isinstance(event_response, dict):
                                response_id = _string_value(event_response.get("id")) or response_id
                                usage = _usage_from_codex(event_response.get("usage"))
                                response_output = event_response.get("output")
                                if isinstance(response_output, list):
                                    for item in response_output:
                                        if not isinstance(item, dict):
                                            continue
                                        item_id = _string_value(item.get("id"))
                                        item_key = item_id or _string_value(item.get("call_id"))
                                        if item_key is None:
                                            item_key = f"anonymous-{anonymous_output_item}"
                                            anonymous_output_item += 1
                                        output_items[item_key] = deepcopy(item)
                            stream_completed = True
                            break
                        elif event_type == "error":
                            failure = ProviderResponseFailed(
                                message=_codex_error_message(event),
                                partial_content="".join(chunks),
                                response_id=response_id,
                            )
                            break
                        elif event_type == "response.failed":
                            failure = ProviderResponseFailed(
                                message=_codex_failed_message(event),
                                partial_content="".join(chunks),
                                response_id=response_id,
                            )
                            break
                        elif event_type == "response.incomplete":
                            failure = ProviderResponseFailed(
                                message=_codex_incomplete_message(event),
                                partial_content="".join(chunks),
                                response_id=response_id,
                            )
                            break
            except (ProviderError, httpx.HTTPError) as exc:
                if response_started:
                    failure = failure or ProviderResponseFailed(
                        message=str(exc),
                        partial_content="".join(chunks),
                        response_id=response_id,
                    )
                    break
                decision = _codex_retry_decision(exc)
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
                continue
            break

        if failure is None and not stream_completed:
            failure = ProviderResponseFailed(
                message="OpenAI Codex stream ended before response.completed was received",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is not None:
            self._continuations.discard(previous_response_id)
            yield failure
            return

        if tool_calls:
            if response_id is None:
                raise ProviderProtocolError(
                    "OpenAI Codex tool response did not include a response id"
                )
            replay_items = _codex_replay_items(output_items.values(), tool_calls=tool_calls)
            if previous_response_id is not None and previous_response_id != response_id:
                self._continuations.consume(previous_response_id)
            self._continuations.remember(
                response_id,
                (*continuation_input, *replay_items),
            )
        elif previous_response_id is not None:
            self._continuations.consume(previous_response_id)

        for content_index, tool_call in enumerate(tool_calls):
            yield ProviderToolCallCompleted(tool_call=tool_call, content_index=content_index)

        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=usage,
        )

    def _get_continuation(
        self,
        previous_response_id: str | None,
    ) -> tuple[dict[str, object], ...]:
        if previous_response_id is None:
            return ()
        continuation = self._continuations.get(previous_response_id, refresh=True)
        if continuation is None:
            raise ProviderProtocolError(
                f"OpenAI Codex continuation state is unavailable for {previous_response_id}"
            )
        return continuation

    @asynccontextmanager
    async def _create_stream(
        self,
        *,
        body: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> AsyncIterator[AsyncIterator[dict[str, object]]]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_CODEX_TIMEOUT)
        try:
            async with client.stream(
                "POST",
                _resolve_codex_url(self._base_url),
                headers=headers,
                json=body,
            ) as response:
                if not response.is_success:
                    text = await response.aread()
                    raise _CodexHTTPError(
                        status_code=response.status_code,
                        headers=response.headers,
                        body=text,
                        reason=text.decode("utf-8", errors="replace") or response.reason_phrase,
                    )
                yield _sse_events(response.aiter_lines())
        finally:
            if owns_client:
                await client.aclose()


def _codex_retry_decision(exc: ProviderError | httpx.HTTPError) -> RetryDecision | None:
    if isinstance(exc, _CodexHTTPError):
        return http_retry_decision(
            status_code=exc.status_code,
            headers=exc.headers,
            error_body=exc.body,
        )
    if isinstance(exc, httpx.TimeoutException):
        return RetryDecision(reason="timeout")
    if isinstance(exc, httpx.HTTPError):
        return RetryDecision(reason="network")
    return None


def _codex_request_body(
    messages: Sequence[Message],
    *,
    model: str,
    tools: Sequence[ToolSpec],
    continuation_input: Sequence[Mapping[str, object]],
    effort: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "store": False,
        "stream": True,
        "input": [*_messages_to_codex_input(messages), *continuation_input],
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    instructions = _instructions_from_messages(messages)
    if instructions:
        body["instructions"] = instructions
    if tools:
        body["tools"] = [_tool_spec_to_codex_tool(tool) for tool in tools]
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    return body


def _instructions_from_messages(messages: Sequence[Message]) -> str | None:
    instructions = [
        message.content for message in messages if message.role in {"system", "developer"}
    ]
    return "\n\n".join(instructions) or None


def _messages_to_codex_input(messages: Sequence[Message]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            continue
        result.append({"role": _to_codex_role(message.role), "content": message.content})
    return result


def _tool_results_to_codex_input(tool_results: Sequence[ToolCallResult]) -> list[dict[str, object]]:
    return [
        {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": result.output,
        }
        for result in tool_results
    ]


def _codex_replay_items(
    items: Iterable[Mapping[str, object]],
    *,
    tool_calls: Sequence[ToolCall],
) -> tuple[dict[str, object], ...]:
    """Return store:false replay items with every emitted function call represented."""

    replay_items: list[dict[str, object]] = []
    replayed_call_ids: set[str] = set()
    for item in items:
        replay_item = deepcopy(dict(item))
        replay_item.pop("id", None)
        replay_items.append(replay_item)
        if replay_item.get("type") == "function_call":
            call_id = _string_value(replay_item.get("call_id"))
            if call_id is not None:
                replayed_call_ids.add(call_id)

    for tool_call in tool_calls:
        if tool_call.call_id in replayed_call_ids:
            continue
        replay_items.append(
            {
                "type": "function_call",
                "call_id": tool_call.call_id,
                "name": tool_call.name,
                "arguments": tool_call.raw_arguments
                or json.dumps(dict(tool_call.arguments), separators=(",", ":")),
            }
        )
    return tuple(replay_items)


def _tool_spec_to_codex_tool(tool: ToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.input_schema),
        "strict": False,
    }


def _tool_call_from_codex(
    item: Mapping[str, object],
    *,
    raw_arguments: str,
    response_id: str | None,
) -> ToolCall:
    call_id = _string_value(item.get("call_id")) or _string_value(item.get("id")) or ""
    name = _string_value(item.get("name")) or ""
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
        parsed = json.loads(raw_arguments or "{}")
    except JSONDecodeError as exc:
        return {}, f"Invalid JSON arguments for tool {name}: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, f"Invalid JSON arguments for tool {name}: expected an object"
    return cast(JsonObject, parsed), None


def _codex_headers(*, token: str, account_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "originator": "wisp",
        "User-Agent": f"wisp ({os.name})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


def _resolve_codex_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, object]]:
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                event = _parse_sse_data("\n".join(data_lines))
                if event is not None:
                    yield event
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        event = _parse_sse_data("\n".join(data_lines))
        if event is not None:
            yield event


def _parse_sse_data(data: str) -> dict[str, object] | None:
    if data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except JSONDecodeError as exc:
        raise ProviderError(f"Invalid OpenAI Codex SSE event: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("Invalid OpenAI Codex SSE event: expected object")
    return cast(dict[str, object], parsed)


def _codex_error_message(event: Mapping[str, object]) -> str:
    message = _string_value(event.get("message"))
    nested = event.get("error")
    if message is None and isinstance(nested, dict):
        message = _string_value(nested.get("message"))
    return f"OpenAI Codex error: {message or json.dumps(dict(event), sort_keys=True)}"


def _codex_failed_message(event: Mapping[str, object]) -> str:
    response = event.get("response")
    if isinstance(response, dict):
        error = response.get("error")
        if isinstance(error, dict):
            message = _string_value(error.get("message"))
            if message:
                return f"OpenAI Codex response failed: {message}"
    return "OpenAI Codex response failed"


def _codex_incomplete_message(event: Mapping[str, object]) -> str:
    response = event.get("response")
    if isinstance(response, dict):
        details = response.get("incomplete_details")
        if isinstance(details, dict):
            reason = _string_value(details.get("reason"))
            if reason:
                return f"OpenAI Codex response incomplete: {reason}"
    return "OpenAI Codex response incomplete"


def _to_codex_role(role: Role) -> str:
    if role == "assistant":
        return "assistant"
    return "user"


def _usage_from_codex(value: object) -> ProviderUsage | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    total_tokens = value.get("total_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if not isinstance(total_tokens, int):
        return None
    return ProviderUsage(
        input_tokens=max(0, input_tokens),
        output_tokens=max(0, output_tokens),
        total_tokens=max(0, total_tokens),
        cache_read_input_tokens=_nested_int(value, "input_tokens_details", "cached_tokens"),
        reasoning_output_tokens=_nested_int(value, "output_tokens_details", "reasoning_tokens"),
    )


def _nested_int(value: Mapping[str, object], group: str, field: str) -> int | None:
    details = value.get(group)
    if not isinstance(details, Mapping):
        return None
    count = details.get(field)
    return max(0, count) if isinstance(count, int) else None


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["DEFAULT_OPENAI_CODEX_MODEL", "OpenAICodexProvider"]
