"""Google Gemini (Generative Language API) provider."""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
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
    ProviderUsage,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_GOOGLE_MODEL = "gemini-3.5-flash"

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


@dataclass(frozen=True, slots=True)
class _CallInfo:
    """What a functionResponse needs to know about the call it answers."""

    name: str
    # None when Gemini never issued an id for this call (confirmed live:
    # non-Gemini-3 models can omit it) -- sending back a Wisp-synthetic id
    # Gemini never issued is semantically wrong and, per Codex's review,
    # risks ambiguous or rejected matching on models that validate it.
    provider_id: str | None


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
        # Remember each call's name (and whether Gemini actually issued an
        # id, vs. Wisp's synthetic fallback -- see _tool_call_from_google) at
        # the moment its tool call is emitted, so the next round's
        # functionResponse can look both up. Keyed the same way as _replays
        # and evicted alongside it.
        self._call_info: OrderedDict[str, _CallInfo] = OrderedDict()

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
        """Stream a normalized Gemini response lifecycle.

        Gemini's API is stateless, so a follow-up call carrying
        ``tool_results`` must resend every prior round's model-turn (the
        turn that produced the tool call) and its functionResponse
        immediately before the newest ones. ``AgentHarness``'s ``messages``
        never grows across tool rounds, so this provider reconstructs the
        accumulated tail from its own ``_replays`` cache keyed by
        ``previous_response_id`` -- see ``_MAX_PENDING_REPLAYS``.

        ``effort`` maps to ``ThinkingConfig.thinking_level`` (Gemini's
        ``"MINIMAL"``/``"LOW"``/``"MEDIUM"``/``"HIGH"`` tiers where the
        selected model supports them) and is passed through unvalidated.
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
                    effort=effort,
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
        usage: ProviderUsage | None = None
        failure: ProviderResponseFailed | None = None

        yield ProviderResponseStarted(model=selected_model)

        try:
            async for chunk in stream:
                if chunk.usage_metadata is not None:
                    chunk_usage = _usage_from_google(chunk.usage_metadata)
                    if chunk_usage is not None:
                        usage = chunk_usage
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
        tool_call_provider_ids: list[str | None] = []
        if raw_finish_reason == genai_types.FinishReason.STOP:
            for index, part in enumerate(parts):
                if part.function_call is None:
                    continue
                tool_call = _tool_call_from_google(
                    part.function_call, index=index, response_id=response_id
                )
                tool_calls.append(tool_call)
                tool_call_provider_ids.append(part.function_call.id)
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
            for tool_call, provider_id in zip(tool_calls, tool_call_provider_ids, strict=True):
                self._remember_call_info(
                    tool_call.call_id,
                    _CallInfo(name=tool_call.name, provider_id=_normalize_optional(provider_id)),
                )
        elif previous_response_id is not None:
            self._replays.pop(previous_response_id, None)

        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason="tool_calls" if tool_calls else finish_reason,
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
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        client = self._client_or_create()
        system_instruction = _system_from_messages(messages)
        contents = _messages_to_google(messages)
        if tool_results:
            contents.extend(self._get_replay(previous_response_id))
            contents.append(self._tool_results_to_content(tool_results))

        # thinking_level only -- Gemini 2.5 models need thinking_budget (a
        # numeric token count) instead. This provider does not translate
        # named tiers into a budget number, so effort is a no-op on models
        # that require thinking_budget -- Gemini raises its own 400 for an
        # unsupported combination, which surfaces as a normal retry-
        # classified error, not a silent no-op on Wisp's side.
        # thinking_level is typed as ThinkingLevel | None, but the SDK's own
        # CaseInSensitiveEnum coerces a plain string at runtime (confirmed
        # live: "MEDIUM" -> ThinkingLevel.MEDIUM) -- effort stays an
        # unvalidated provider-native string per the Provider protocol, so
        # an unrecognized value reaches the API as-is rather than being
        # rejected client-side by a premature enum cast.
        thinking_config = genai_types.ThinkingConfig(
            include_thoughts=True,
            thinking_level=cast("genai_types.ThinkingLevel | None", effort),
        )
        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[_tools_to_google(tools)] if tools else None,
            thinking_config=thinking_config,
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

        # Mirrors google.genai's own env-var resolution order (get_env_api_key
        # in _api_client.py): GOOGLE_API_KEY takes precedence, GEMINI_API_KEY
        # is the accepted fallback -- a user who already has Gemini configured
        # with only GEMINI_API_KEY (the SDK's own recognized variable) should
        # not be rejected here before the SDK ever gets a chance to use it.
        api_key = (
            self._api_key
            or _normalize_optional(os.environ.get("GOOGLE_API_KEY"))
            or _normalize_optional(os.environ.get("GEMINI_API_KEY"))
        )
        if api_key is None:
            raise ProviderConfigurationError(
                "GOOGLE_API_KEY or GEMINI_API_KEY is required when using the google provider"
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

    def _remember_call_info(self, call_id: str, info: _CallInfo) -> None:
        self._call_info.pop(call_id, None)
        self._call_info[call_id] = info
        while len(self._call_info) > _MAX_PENDING_REPLAYS:
            self._call_info.popitem(last=False)

    def _tool_results_to_content(
        self, tool_results: Sequence[ToolCallResult]
    ) -> genai_types.Content:
        parts = []
        for result in tool_results:
            info = self._call_info.get(result.call_id)
            parts.append(
                genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        # Only echo an id Gemini actually issued for this
                        # call -- when it never did (info is None or its
                        # provider_id is None), omit id entirely rather than
                        # sending Wisp's own synthetic call_id back as if it
                        # were a real Gemini-issued one.
                        id=info.provider_id if info is not None else None,
                        name=info.name if info is not None else result.call_id,
                        response={"error" if result.is_error else "output": result.output},
                    )
                )
            )
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
    function_call: genai_types.FunctionCall, *, index: int, response_id: str | None
) -> ToolCall:
    name = function_call.name or ""
    arguments = function_call.args or {}
    # Gemini does not guarantee an id on non-Gemini-3 models -- a parallel
    # response with two calls to the same tool would otherwise collapse to
    # the same fallback id (confirmed live: gemini-2.5-flash returns
    # id=None on every function_call part), producing duplicate
    # ToolCall.call_ids and ambiguous functionResponse matching on replay.
    # The part's stream position is already a stable per-call value.
    call_id = function_call.id or f"call-{name}-{index}"
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


def _usage_from_google(
    value: genai_types.GenerateContentResponseUsageMetadata,
) -> ProviderUsage | None:
    counts = (
        value.prompt_token_count,
        value.candidates_token_count,
        value.total_token_count,
        value.cached_content_token_count,
        value.thoughts_token_count,
        value.tool_use_prompt_token_count,
    )
    if all(count is None for count in counts):
        return None

    prompt_tokens = max(0, value.prompt_token_count or 0)
    tool_use_tokens = max(0, value.tool_use_prompt_token_count or 0)
    input_tokens = prompt_tokens + tool_use_tokens
    output_tokens = max(0, value.candidates_token_count or 0)
    reasoning_tokens = (
        max(0, value.thoughts_token_count) if value.thoughts_token_count is not None else None
    )
    total_tokens = value.total_token_count
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens + (reasoning_tokens or 0)
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=max(0, total_tokens),
        cache_read_input_tokens=(
            max(0, value.cached_content_token_count)
            if value.cached_content_token_count is not None
            else None
        ),
        reasoning_output_tokens=reasoning_tokens,
    )


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["DEFAULT_GOOGLE_MODEL", "GoogleProvider"]
