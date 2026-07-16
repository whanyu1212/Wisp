"""Google Gemini (Generative Language API) provider."""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from json import dumps
from typing import cast

import anyio
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from wisp.agent.messages import Message
from wisp.providers.base import (
    ProviderConfigurationError,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    JsonObject,
    ProviderEvent,
    ProviderFinishReason,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_GOOGLE_MODEL = "gemini-flash-latest"

# Gemini's API is stateless per call, like Anthropic's -- but unlike Anthropic,
# a streamed tool call arrives as one fully-formed function_call part (args is
# already a parsed dict, never partial JSON), so there is no per-part
# input-accumulation step. What Gemini *does* require preserving across
# rounds is the same thing Anthropic does: the model-role turn that produced
# the tool call (including any thought_signature parts) must be resent ahead
# of the tool's functionResponse, since AgentHarness's `messages` never grows
# across tool rounds. Mirrors AnthropicProvider._replays exactly.
_MAX_PENDING_REPLAYS = 128

_TERMINAL_FINISH_REASON_TO_PROVIDER: dict[genai_types.FinishReason, ProviderFinishReason] = {
    genai_types.FinishReason.STOP: "stop",
    genai_types.FinishReason.MAX_TOKENS: "length",
}
# Any other terminal reason (SAFETY, RECITATION, PROHIBITED_CONTENT, a future
# enum value, ...) means the response was cut short or blocked, not a clean
# completion -- default to "length" (incomplete), not "stop", so a caller
# never mistakes a blocked/truncated turn for success.
_DEFAULT_FINISH_REASON_FOR_UNKNOWN_FINISH_REASON: ProviderFinishReason = "length"


class GoogleProvider:
    """Provider backed by Google's Gemini Generative Language API."""

    name = "google"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_GOOGLE_MODEL,
        client: genai.Client | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.default_model: str | None = default_model
        self._api_key = _normalize_optional(api_key)
        self._client = client
        self._retry_policy = retry_policy or RetryPolicy()
        self._replays: OrderedDict[str, tuple[genai_types.Content, ...]] = OrderedDict()
        # Gemini's functionResponse.name is required and must match the
        # original FunctionCall.name -- but ToolCallResult (provider-neutral,
        # shared with every other provider) carries only call_id, not name.
        # Remember each call's name at the moment its tool call is emitted so
        # the next round's functionResponse can look it up. Keyed the same
        # way as _replays and evicted alongside it.
        self._call_names: OrderedDict[str, str] = OrderedDict()

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream a normalized Gemini response lifecycle.

        Gemini's API is stateless, so a follow-up call carrying
        ``tool_results`` must resend every prior round's model-turn (the
        turn that produced the tool call) and its functionResponse
        immediately before the newest ones. ``AgentHarness``'s ``messages``
        never grows across tool rounds, so this provider reconstructs the
        accumulated tail from its own ``_replays`` cache keyed by
        ``previous_response_id`` -- see ``_MAX_PENDING_REPLAYS``.
        """

        selected_model = model or self.default_model or DEFAULT_GOOGLE_MODEL
        stream: AsyncIterator[genai_types.GenerateContentResponse] | None = None
        for retry_number in range(self._retry_policy.max_retries + 1):
            try:
                stream = await self._create_stream(
                    messages,
                    model=selected_model,
                    tools=tools,
                    tool_results=tool_results,
                    previous_response_id=previous_response_id,
                )
                break
            except (genai_errors.APIError, httpx.TimeoutException, httpx.ConnectError) as exc:
                decision = _google_retry_decision(exc)
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
            raise AssertionError("Google retry loop completed without a stream or error")

        response_id: str | None = None
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        parts: list[genai_types.Part] = []
        finish_reason: ProviderFinishReason = "stop"
        raw_finish_reason: genai_types.FinishReason | None = None
        stream_completed = False
        failure: ProviderResponseFailed | None = None

        yield ProviderResponseStarted(model=selected_model)

        try:
            async for chunk in stream:
                if chunk.response_id is not None:
                    response_id = chunk.response_id
                for candidate in chunk.candidates or ():
                    if candidate.finish_reason is not None:
                        raw_finish_reason = candidate.finish_reason
                        finish_reason = _TERMINAL_FINISH_REASON_TO_PROVIDER.get(
                            raw_finish_reason, _DEFAULT_FINISH_REASON_FOR_UNKNOWN_FINISH_REASON
                        )
                        # A candidate carrying finish_reason is Gemini's signal
                        # that the response actually finished. Anything else (a
                        # dropped connection, a truncated proxy response) ends
                        # the async iterator without this ever being set.
                        stream_completed = True
                    content = candidate.content
                    if content is None:
                        continue
                    for part in content.parts or ():
                        parts.append(part)
                        if part.function_call is not None:
                            continue
                        text = part.text
                        if not text:
                            continue
                        if part.thought:
                            yield ProviderThinkingDelta(delta=text, content_index=len(parts) - 1)
                        else:
                            chunks.append(text)
                            yield ProviderTextDelta(delta=text, content_index=len(parts) - 1)
        except (genai_errors.APIError, httpx.TimeoutException, httpx.ConnectError) as exc:
            failure = ProviderResponseFailed(
                message=f"Google stream error: {exc}",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is None and not stream_completed:
            # Regression guard: the stream ended (cleanly or not) without
            # Gemini ever reporting a finish_reason. Silently yielding a
            # ProviderResponseCompleted here would report a truncated answer
            # as a successful "stop" turn -- treat it as failed instead.
            failure = ProviderResponseFailed(
                message="Google stream ended before a finish_reason was received",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is not None:
            yield failure
            return

        # Only finish_reason == STOP alongside a function_call part means
        # Gemini finished the turn with a complete, executable tool call.
        # Any other terminal reason (MAX_TOKENS, a safety block, ...) means
        # the turn was cut short -- acting on a function_call part that
        # arrived before the cutoff would hand the agent loop a tool request
        # Gemini never actually finished deciding on.
        if raw_finish_reason == genai_types.FinishReason.STOP:
            for index, part in enumerate(parts):
                if part.function_call is None:
                    continue
                tool_call = _tool_call_from_google(part.function_call, response_id=response_id)
                tool_calls.append(tool_call)
                yield ProviderToolCallCompleted(tool_call=tool_call, content_index=index)

        if tool_calls:
            if response_id is None:
                failure = ProviderResponseFailed(
                    message="Google tool-call response did not include a response id",
                    partial_content="".join(chunks),
                    response_id=None,
                )
                yield failure
                return
            previous_tail = self._get_replay(previous_response_id)
            new_turn = genai_types.Content(role="model", parts=parts)
            replay_tail = (
                *previous_tail,
                *((self._tool_results_to_content(tool_results),) if tool_results else ()),
                new_turn,
            )
            if previous_response_id is not None and previous_response_id != response_id:
                self._replays.pop(previous_response_id, None)
            self._store_replay(response_id, replay_tail)
            for tool_call in tool_calls:
                self._remember_call_name(tool_call.call_id, tool_call.name)
        elif previous_response_id is not None:
            self._replays.pop(previous_response_id, None)

        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason="tool_calls" if tool_calls else finish_reason,
        )

    async def _create_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        client = self._client_or_create()
        system_instruction = _system_from_messages(messages)
        contents = _messages_to_google(messages)
        if tool_results:
            contents.extend(self._get_replay(previous_response_id))
            contents.append(self._tool_results_to_content(tool_results))

        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[_tools_to_google(tools)] if tools else None,
            thinking_config=genai_types.ThinkingConfig(include_thoughts=True),
        )
        stream = await client.aio.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        )
        return stream

    def _client_or_create(self) -> genai.Client:
        if self._client is not None:
            return self._client

        api_key = self._api_key or _normalize_optional(os.environ.get("GOOGLE_API_KEY"))
        if api_key is None:
            raise ProviderConfigurationError(
                "GOOGLE_API_KEY is required when using the google provider"
            )

        # Wisp emits retry progress itself; leave the SDK's own retry_options
        # unset (its default is a single attempt, no retries).
        self._client = genai.Client(api_key=api_key)
        return self._client

    def _get_replay(self, previous_response_id: str | None) -> tuple[genai_types.Content, ...]:
        if previous_response_id is None:
            return ()
        # A peek, not a pop: _create_stream can be re-invoked by the retry loop
        # in `stream()` before a response ever completes, so the replay must
        # survive a failed attempt to be available to the next one.
        return self._replays.get(previous_response_id, ())

    def _store_replay(self, response_id: str, replay_tail: tuple[genai_types.Content, ...]) -> None:
        self._replays.pop(response_id, None)
        self._replays[response_id] = replay_tail
        while len(self._replays) > _MAX_PENDING_REPLAYS:
            self._replays.popitem(last=False)

    def _remember_call_name(self, call_id: str, name: str) -> None:
        self._call_names.pop(call_id, None)
        self._call_names[call_id] = name
        while len(self._call_names) > _MAX_PENDING_REPLAYS:
            self._call_names.popitem(last=False)

    def _tool_results_to_content(
        self, tool_results: Sequence[ToolCallResult]
    ) -> genai_types.Content:
        parts = [
            genai_types.Part(
                function_response=genai_types.FunctionResponse(
                    id=result.call_id,
                    name=self._call_names.get(result.call_id, result.call_id),
                    response={"error" if result.is_error else "output": result.output},
                )
            )
            for result in tool_results
        ]
        return genai_types.Content(role="user", parts=parts)


def _google_retry_decision(
    exc: genai_errors.APIError | httpx.TimeoutException | httpx.ConnectError,
) -> RetryDecision | None:
    if isinstance(exc, httpx.TimeoutException):
        return RetryDecision(reason="timeout")
    if isinstance(exc, httpx.ConnectError):
        return RetryDecision(reason="network")
    headers = exc.response.headers if isinstance(exc.response, httpx.Response) else None
    return http_retry_decision(
        status_code=exc.code,
        headers=headers,
        error_body=exc.details,
    )


def _tool_call_from_google(
    function_call: genai_types.FunctionCall, *, response_id: str | None
) -> ToolCall:
    name = function_call.name or ""
    arguments = function_call.args or {}
    call_id = function_call.id or f"call-{name}"
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments=cast(JsonObject, arguments),
        raw_arguments=dumps(arguments),
        response_id=response_id,
    )


def _system_from_messages(messages: Sequence[Message]) -> str | None:
    system_parts = [message.content for message in messages if message.role == "system"]
    return "\n\n".join(system_parts) or None


def _messages_to_google(messages: Sequence[Message]) -> list[genai_types.Content]:
    contents: list[genai_types.Content] = []
    for message in messages:
        if message.role == "system":
            continue
        role = "model" if message.role == "assistant" else "user"
        contents.append(
            genai_types.Content(role=role, parts=[genai_types.Part(text=message.content)])
        )
    return contents


def _tools_to_google(tools: Sequence[ToolSpec]) -> genai_types.Tool:
    return genai_types.Tool(function_declarations=[_tool_spec_to_google(tool) for tool in tools])


def _tool_spec_to_google(tool: ToolSpec) -> genai_types.FunctionDeclaration:
    return genai_types.FunctionDeclaration(
        name=tool.name,
        description=tool.description,
        parameters_json_schema=dict(tool.input_schema),
    )


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["DEFAULT_GOOGLE_MODEL", "GoogleProvider"]
