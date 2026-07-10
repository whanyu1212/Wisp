"""OpenAI Codex provider backed by ChatGPT subscription auth."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from json import JSONDecodeError
from typing import cast

import httpx

from wisp.agent.messages import Message, Role
from wisp.auth.openai_codex import account_id_from_access_token, refresh_openai_codex_token
from wisp.auth.storage import JsonAuthStore
from wisp.config import default_auth_path
from wisp.providers.auth import ProviderAuthResolver, StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    JsonObject,
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ToolCall,
)

DEFAULT_OPENAI_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"


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
    ) -> None:
        self.default_model: str | None = default_model
        self._auth_resolver = auth_resolver or StoredProviderAuthResolver(
            JsonAuthStore(default_auth_path())
        )
        self._base_url = base_url
        self._client = client

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
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
        body = _codex_request_body(
            messages,
            model=selected_model,
            tools=tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
        )
        response_id: str | None = previous_response_id
        pending_tool_calls: dict[str, dict[str, object]] = {}
        completed_tool_arguments: dict[str, str] = {}
        emitted_tool_item_ids: set[str] = set()
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []

        yield ProviderResponseStarted(model=selected_model)

        async for event in self._create_stream(body=body, headers=headers):
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
                        tool_call = _tool_call_from_codex(
                            pending,
                            raw_arguments=arguments,
                            response_id=response_id,
                        )
                        tool_calls.append(tool_call)
                        yield ProviderToolCallCompleted(
                            tool_call=tool_call,
                            content_index=len(tool_calls) - 1,
                        )
                        emitted_tool_item_ids.add(item_id)
            elif event_type in {"response.output_item.added", "response.output_item.done"}:
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    item_id = _string_value(item.get("id"))
                    if item_id is not None:
                        pending_tool_calls[item_id] = dict(item)
                    already_emitted = item_id is not None and item_id in emitted_tool_item_ids
                    if event_type == "response.output_item.done" and not already_emitted:
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
                        yield ProviderToolCallCompleted(
                            tool_call=tool_call,
                            content_index=len(tool_calls) - 1,
                        )
                        if item_id is not None:
                            emitted_tool_item_ids.add(item_id)
            elif event_type == "error":
                yield ProviderResponseFailed(
                    message=_codex_error_message(event),
                    partial_content="".join(chunks),
                    response_id=response_id,
                )
                return
            elif event_type == "response.failed":
                yield ProviderResponseFailed(
                    message=_codex_failed_message(event),
                    partial_content="".join(chunks),
                    response_id=response_id,
                )
                return
            elif event_type == "response.incomplete":
                yield ProviderResponseFailed(
                    message=_codex_incomplete_message(event),
                    partial_content="".join(chunks),
                    response_id=response_id,
                )
                return

        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    async def _create_stream(
        self,
        *,
        body: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> AsyncIterator[dict[str, object]]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=None)
        try:
            async with client.stream(
                "POST",
                _resolve_codex_url(self._base_url),
                headers=headers,
                json=body,
            ) as response:
                if not response.is_success:
                    text = await response.aread()
                    raise ProviderError(
                        f"OpenAI Codex API error ({response.status_code}): "
                        f"{text.decode('utf-8', errors='replace') or response.reason_phrase}"
                    )
                async for event in _sse_events(response.aiter_lines()):
                    yield event
        finally:
            if owns_client:
                await client.aclose()


def _codex_request_body(
    messages: Sequence[Message],
    *,
    model: str,
    tools: Sequence[ToolSpec],
    tool_results: Sequence[ToolCallResult],
    previous_response_id: str | None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "store": False,
        "stream": True,
        "input": _tool_results_to_codex_input(tool_results)
        if tool_results
        else _messages_to_codex_input(messages),
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
    if previous_response_id is not None:
        body["previous_response_id"] = previous_response_id
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


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["DEFAULT_OPENAI_CODEX_MODEL", "OpenAICodexProvider"]
