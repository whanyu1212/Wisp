"""Anthropic Messages API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from json import JSONDecodeError, loads
from typing import Literal, cast

import anyio
from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
)
from anthropic.types import (
    MessageParam,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStreamEvent,
    RedactedThinkingBlockParam,
    TextBlockParam,
    ThinkingBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from wisp.agent.messages import Message
from wisp.providers.auth import ProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
    is_context_overflow_message,
)
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
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"

# Anthropic's Messages API requires an explicit max_tokens (unlike OpenAI's
# Responses API, which Wisp calls with no output cap). Wisp always streams, so
# this follows the streaming default recommended for current-generation models
# rather than the lower non-streaming default -- low enough to avoid runaway
# cost on a single turn, high enough not to truncate a long agentic response
# or a turn with many tool calls.
_MAX_OUTPUT_TOKENS = 64_000

_STOP_REASON_TO_FINISH_REASON: dict[str, ProviderFinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "refusal": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    # pause_turn is Anthropic's server-side sampling loop (server-side tools
    # like web search/code execution) hitting its internal iteration cap --
    # unreachable today since this provider only ever sends client-defined
    # tools (see _tool_spec_to_anthropic_tool), never a server-tool type. If
    # server-tool support is added later, Anthropic's docs say the client
    # must resend the conversation with the paused assistant turn appended to
    # let the server resume -- reporting it as a clean "stop" would silently
    # truncate the response instead, so it maps to "length" (incomplete)
    # defensively, matching model_context_window_exceeded/compaction below.
    "pause_turn": "length",
    # Beta-only stop reasons not in the GA API this provider calls (server-side
    # compaction's "compaction", and context-window truncation) -- included
    # defensively in case Anthropic starts returning them outside the beta
    # surface. Both mean the response is incomplete, matching "max_tokens".
    "model_context_window_exceeded": "length",
    "compaction": "length",
}
# Anthropic may add stop reasons this provider doesn't yet recognize. Default
# to "length" (incomplete), not "stop" (success) -- an unrecognized reason
# must never be silently reported as a clean completion.
_DEFAULT_FINISH_REASON_FOR_UNKNOWN_STOP_REASON: ProviderFinishReason = "length"

# Anthropic's Messages API is stateless. The adapter keeps the exact native
# replay tail keyed by the provider cursor so it can append each request's new
# tool results or user steering without flattening structured tool history.


@dataclass
class _ContentBlockAccumulator:
    """Accumulates one content block's streamed deltas for the replay tail.

    Anthropic's tool-use guidance requires echoing thinking/redacted_thinking
    blocks back to the API unmodified alongside their sibling tool_use block
    on the same turn -- dropping them (e.g. replaying only text + tool_use)
    can be rejected or lose reasoning continuity. This accumulates every
    content-block type Wisp can actually receive (text, thinking,
    redacted_thinking, tool_use -- Wisp declares no server-side tools, so the
    other SDK block types cannot appear in its streams) so the replay is a
    faithful reconstruction of the full assistant turn, not just its
    tool-relevant parts.
    """

    block_type: str
    text_chunks: list[str] = field(default_factory=list)
    thinking_chunks: list[str] = field(default_factory=list)
    signature: str = ""
    redacted_data: str = ""
    tool_use_id: str = ""
    tool_use_name: str = ""
    tool_use_json_chunks: list[str] = field(default_factory=list)


class AnthropicProvider:
    """Provider backed by Anthropic's Messages API."""

    name = "anthropic"
    supports_continuation_messages: Literal[True] = True
    supports_context_rebase: Literal[True] = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_ANTHROPIC_MODEL,
        client: AsyncAnthropic | None = None,
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
        self._replays = ContinuationStore[tuple[MessageParam, ...]]()

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        """Reject fresh tool replay while adaptive thinking is enabled.

        Anthropic requires signed thinking blocks to be preserved byte-for-byte
        beside their tool uses. ``Message`` snapshots intentionally retain only
        portable text and tool data, so a replacement cannot safely rebuild a
        tool exchange when ``effort`` enabled adaptive thinking.
        """

        return effort is None

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
    ) -> AsyncIterator[ProviderEvent]:
        """Stream a normalized Anthropic response lifecycle.

        Anthropic's Messages API is stateless, so a follow-up call carrying
        ``previous_response_id`` must resend every prior round's assistant
        tool_use turn and tool_result immediately before the newest ones,
        plus any ``tool_results``/``extra_messages`` this call adds.
        ``AgentHarness``'s ``messages`` never grows across turns (it assumes
        a Responses-API-style backend that remembers everything
        server-side), so this provider reconstructs the accumulated tail
        from its own replay state keyed by ``previous_response_id``.

        ``effort`` maps directly to ``output_config.effort`` on the Messages
        API (``"low"``/``"medium"``/``"high"``/``"xhigh"``/``"max"``,
        model-dependent) -- passed through unvalidated; Anthropic rejects an
        unsupported tier for the selected model with a 400, which surfaces
        as a normal retry-classified error, not a silent no-op. Setting
        ``effort`` also enables ``thinking: {"type": "adaptive"}`` --
        Anthropic's migration guide pairs the two in every documented
        example and describes effort as controlling thinking depth, which
        has nothing to modulate without adaptive thinking enabled.
        """

        selected_model = model or self.default_model or DEFAULT_ANTHROPIC_MODEL
        stream: AsyncIterator[RawMessageStreamEvent] | None = None
        for retry_number in range(self._retry_policy.max_retries + 1):
            try:
                if extra_messages:
                    stream = await self._create_stream(
                        messages,
                        model=selected_model,
                        tools=tools,
                        tool_results=tool_results,
                        extra_messages=extra_messages,
                        previous_response_id=previous_response_id,
                        effort=effort,
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
            except AnthropicError as exc:
                decision = _anthropic_retry_decision(exc)
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
            raise AssertionError("Anthropic retry loop completed without a stream or error")

        response_id: str | None = None
        chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        content_blocks: dict[int, _ContentBlockAccumulator] = {}
        finish_reason: ProviderFinishReason = "stop"
        stop_reason: str | None = None
        stream_completed = False
        usage: ProviderUsage | None = None
        failure: ProviderResponseFailed | None = None

        yield ProviderResponseStarted(model=selected_model)

        try:
            async for event in stream:
                if isinstance(event, RawMessageStartEvent):
                    response_id = event.message.id
                    usage = _usage_from_anthropic_start(event)
                elif isinstance(event, RawContentBlockStartEvent):
                    block = event.content_block
                    if block.type == "tool_use":
                        accumulator = _ContentBlockAccumulator(block_type="tool_use")
                        accumulator.tool_use_id = block.id
                        accumulator.tool_use_name = block.name
                        content_blocks[event.index] = accumulator
                    elif block.type == "text":
                        content_blocks[event.index] = _ContentBlockAccumulator(block_type="text")
                    elif block.type == "thinking":
                        content_blocks[event.index] = _ContentBlockAccumulator(
                            block_type="thinking"
                        )
                    elif block.type == "redacted_thinking":
                        accumulator = _ContentBlockAccumulator(block_type="redacted_thinking")
                        accumulator.redacted_data = block.data
                        content_blocks[event.index] = accumulator
                elif isinstance(event, RawContentBlockDeltaEvent):
                    delta = event.delta
                    block_accumulator = content_blocks.get(event.index)
                    if delta.type == "text_delta":
                        chunks.append(delta.text)
                        if block_accumulator is not None:
                            block_accumulator.text_chunks.append(delta.text)
                        yield ProviderTextDelta(delta=delta.text, content_index=event.index)
                    elif delta.type == "thinking_delta":
                        if block_accumulator is not None:
                            block_accumulator.thinking_chunks.append(delta.thinking)
                        yield ProviderThinkingDelta(delta=delta.thinking, content_index=event.index)
                    elif delta.type == "signature_delta":
                        if block_accumulator is not None:
                            block_accumulator.signature += delta.signature
                    elif delta.type == "input_json_delta":
                        if block_accumulator is not None:
                            block_accumulator.tool_use_json_chunks.append(delta.partial_json)
                elif isinstance(event, RawMessageDeltaEvent):
                    usage = _usage_from_anthropic_delta(event, usage)
                    event_stop_reason = event.delta.stop_reason
                    if event_stop_reason is not None:
                        stop_reason = event_stop_reason
                        finish_reason = _STOP_REASON_TO_FINISH_REASON.get(
                            stop_reason, _DEFAULT_FINISH_REASON_FOR_UNKNOWN_STOP_REASON
                        )
                        # message_delta carrying stop_reason is Anthropic's
                        # signal that the message actually finished -- the
                        # only remaining event is message_stop, which carries
                        # no further information. Anything else (a dropped
                        # connection, a truncated proxy response) ends the
                        # async iterator without this ever being set.
                        stream_completed = True
        except AnthropicError as exc:
            failure = ProviderResponseFailed(
                message=f"Anthropic stream error: {exc}",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is None and stop_reason == "model_context_window_exceeded":
            failure = ProviderResponseFailed(
                message="Anthropic model_context_window_exceeded",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is None and not stream_completed:
            # Regression guard: the stream ended (cleanly or not) without
            # Anthropic ever reporting a stop_reason. Silently yielding a
            # ProviderResponseCompleted here would report a truncated answer
            # as a successful "stop" turn -- treat it as failed instead.
            failure = ProviderResponseFailed(
                message="Anthropic stream ended before a stop_reason was received",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is not None:
            # A context rejection does not advance or invalidate the replay
            # tail. Keep it available for a same-loop compacted retry.
            if failure.failure_kind != "context_overflow" and not is_context_overflow_message(
                failure.message
            ):
                self._replays.discard(previous_response_id)
            yield failure
            return

        # Preserve every observed tool_use block through normalization. A
        # max_tokens response may contain incomplete JSON, but the
        # provider-neutral loop must see both the call and the terminal reason
        # so it can reject the batch without executing it.
        for index in sorted(content_blocks):
            accumulator = content_blocks[index]
            if accumulator.block_type != "tool_use":
                continue
            raw_arguments = "".join(accumulator.tool_use_json_chunks)
            tool_call = _tool_call_from_anthropic(
                call_id=accumulator.tool_use_id,
                name=accumulator.tool_use_name,
                raw_arguments=raw_arguments,
                response_id=response_id,
            )
            tool_calls.append(tool_call)
            yield ProviderToolCallCompleted(tool_call=tool_call, content_index=len(tool_calls) - 1)

        if tool_calls and response_id is None:
            failure = ProviderResponseFailed(
                message="Anthropic tool-call response did not include a message id",
                partial_content="".join(chunks),
                response_id=None,
            )
            self._replays.discard(previous_response_id)
            yield failure
            return
        # Save every successful turn: a later follow-up may arrive after a
        # clean response, not only after tool use. A missing clean response ID
        # does not change the public completion; an already-active local replay
        # safely advances under its existing key.
        continuation_id = response_id or previous_response_id
        if continuation_id is not None:
            previous = self._require_replay(previous_response_id)
            replay_tail = (
                *previous,
                *((_tool_results_to_message(tool_results),) if tool_results else ()),
                *_messages_to_anthropic(extra_messages),
                _replay_message_from_stream(content_blocks),
            )
            if previous_response_id is not None and previous_response_id != continuation_id:
                self._replays.consume(previous_response_id)
            self._replays.remember(continuation_id, replay_tail)

        yield ProviderResponseCompleted(
            content="".join(chunks),
            tool_calls=tuple(tool_calls),
            response_id=response_id,
            finish_reason=finish_reason,
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
    ) -> AsyncIterator[RawMessageStreamEvent]:
        client = await self._client_or_create()
        anthropic_messages = _messages_to_anthropic(messages)
        if previous_response_id is not None:
            anthropic_messages.extend(self._require_replay(previous_response_id))
        if tool_results:
            anthropic_messages.append(_tool_results_to_message(tool_results))
        anthropic_messages.extend(_messages_to_anthropic(extra_messages))
        system = _system_from_messages(messages)
        anthropic_tools = _tool_specs_to_anthropic_tools(tools)

        # Built as a single kwargs dict rather than a create() call per
        # system/tools/effort combination: branching per optional-parameter
        # combination doesn't scale past two independent optional dimensions
        # (system x tools was already 4 branches; adding effort would make
        # 8). mypy cannot match a **kwargs dict against create()'s
        # `@overload`s (they only discriminate on `stream`, but mypy's
        # overload resolution rejects a dict-unpack call regardless) -- the
        # `create` rebinding below is the single, contained concession to
        # that limitation; every kwarg's value is still built from typed
        # sources above.
        kwargs: dict[str, object] = {
            "model": model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "messages": anthropic_messages,
            "stream": True,
        }
        if system is not None:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if effort is not None:
            kwargs["output_config"] = {"effort": effort}
            # Anthropic's migration guide pairs output_config.effort with
            # thinking: {"type": "adaptive"} in every documented example and
            # describes effort as controlling "thinking depth" -- without
            # adaptive thinking enabled, effort has nothing to modulate.
            # thinking is otherwise never sent by this provider at all.
            kwargs["thinking"] = {"type": "adaptive"}
        create = cast(Callable[..., Awaitable[object]], client.messages.create)
        stream = await create(**kwargs)
        return cast(AsyncIterator[RawMessageStreamEvent], stream)

    async def _client_or_create(self) -> AsyncAnthropic:
        if self._client_is_injected:
            assert self._client is not None
            return self._client

        api_key = self._api_key or _normalize_optional(os.environ.get("ANTHROPIC_API_KEY"))
        if api_key is None and self._auth_resolver is not None:
            api_key = await self._auth_resolver.api_key(self.name)
        if api_key is None:
            raise ProviderConfigurationError(
                "anthropic credentials are required; run `/connect` in the TUI "
                "or set ANTHROPIC_API_KEY"
            )
        if self._client is not None and self._client_api_key == api_key:
            return self._client

        # Wisp emits retry progress itself. Key changes replace only Wisp-owned clients.
        if self._client is not None:
            await self._client.close()
        self._client = AsyncAnthropic(api_key=api_key, max_retries=0)
        self._client_api_key = api_key
        return self._client

    def _require_replay(self, previous_response_id: str | None) -> tuple[MessageParam, ...]:
        if previous_response_id is None:
            return ()
        # A peek, not a pop: _create_stream can be re-invoked by the retry loop
        # in `stream()` before a response ever completes, so the replay must
        # survive a failed attempt to be available to the next one.
        continuation = self._replays.get(previous_response_id)
        if continuation is None:
            # Silently returning an empty continuation here would resend
            # only the original base `messages`, dropping every tool call/
            # result and injected message since -- exactly the corruption
            # this whole continuation mechanism exists to prevent. A missing
            # entry (evicted, or a caller passing a stale/foreign id) must
            # fail loudly instead, matching OpenAICodexProvider's existing
            # _get_continuation contract.
            raise ProviderProtocolError(
                f"Anthropic continuation state is unavailable for {previous_response_id}"
            )
        return continuation


_ReplayBlockParam = (
    TextBlockParam | ThinkingBlockParam | RedactedThinkingBlockParam | ToolUseBlockParam
)


def _replay_message_from_stream(
    content_blocks: dict[int, _ContentBlockAccumulator],
) -> MessageParam:
    """Build the assistant-turn replay for a completed tool-use response.

    Reconstructs every content block Anthropic actually sent, in the order it
    sent them, not just the tool-relevant parts. Thinking and
    redacted_thinking blocks must be echoed back unmodified alongside their
    sibling tool_use block on the same turn -- Anthropic's tool-use guidance
    says dropping them (e.g. replaying only text + tool_use) can be rejected
    or lose reasoning continuity.

    Anthropic's ``tool_use.input`` must be a valid JSON object; a tool call
    whose accumulated ``input_json_delta`` text failed to parse (surfaced to
    the caller via ``ToolCall.parse_error``) still needs *some* valid input
    to replay -- falling back to ``{}`` here only affects what is echoed back
    to Anthropic's own conversation history, not the ``ToolCall.raw_arguments``
    Wisp's tool-execution layer sees.
    """

    content: list[_ReplayBlockParam] = []
    for index in sorted(content_blocks):
        accumulator = content_blocks[index]
        if accumulator.block_type == "text":
            text = "".join(accumulator.text_chunks)
            if text:
                content.append({"type": "text", "text": text})
        elif accumulator.block_type == "thinking":
            content.append(
                {
                    "type": "thinking",
                    "thinking": "".join(accumulator.thinking_chunks),
                    "signature": accumulator.signature,
                }
            )
        elif accumulator.block_type == "redacted_thinking":
            content.append({"type": "redacted_thinking", "data": accumulator.redacted_data})
        elif accumulator.block_type == "tool_use":
            raw_arguments = "".join(accumulator.tool_use_json_chunks)
            arguments, _parse_error = _parse_tool_arguments(
                name=accumulator.tool_use_name, raw_arguments=raw_arguments
            )
            content.append(
                {
                    "type": "tool_use",
                    "id": accumulator.tool_use_id,
                    "name": accumulator.tool_use_name,
                    "input": dict(arguments),
                }
            )
    return cast(MessageParam, {"role": "assistant", "content": content})


def _anthropic_retry_decision(exc: AnthropicError) -> RetryDecision | None:
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


def _tool_call_from_anthropic(
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


def _system_from_messages(messages: Sequence[Message]) -> str | None:
    system_parts = [message.content for message in messages if message.role == "system"]
    return "\n\n".join(system_parts) or None


def _messages_to_anthropic(messages: Sequence[Message]) -> list[MessageParam]:
    """Encode fresh base context without flattening an active tool exchange."""

    anthropic_messages: list[MessageParam] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "system":
            index += 1
            continue
        if message.role == "tool" and message.tool_call_id:
            results: list[ToolCallResult] = []
            while index < len(messages):
                result = messages[index]
                if result.role != "tool" or not result.tool_call_id:
                    break
                results.append(
                    ToolCallResult(
                        call_id=result.tool_call_id,
                        output=result.content,
                        is_error=result.is_error is True,
                    )
                )
                index += 1
            anthropic_messages.append(_tool_results_to_message(results))
            continue
        if message.role == "assistant" and message.tool_calls:
            blocks: list[_ReplayBlockParam] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": tool_call.call_id,
                    "name": tool_call.name,
                    "input": dict(tool_call.arguments),
                }
                for tool_call in message.tool_calls
            )
            anthropic_messages.append(cast(MessageParam, {"role": "assistant", "content": blocks}))
            index += 1
            continue
        role = "user" if message.role == "tool" else message.role
        content: TextBlockParam = {"type": "text", "text": message.content}
        anthropic_messages.append(cast(MessageParam, {"role": role, "content": [content]}))
        index += 1
    return anthropic_messages


def _tool_results_to_message(tool_results: Sequence[ToolCallResult]) -> MessageParam:
    blocks: list[ToolResultBlockParam] = [
        {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.output,
            "is_error": result.is_error,
        }
        for result in tool_results
    ]
    return cast(MessageParam, {"role": "user", "content": blocks})


def _tool_specs_to_anthropic_tools(tools: Sequence[ToolSpec]) -> list[ToolParam]:
    return [_tool_spec_to_anthropic_tool(tool) for tool in tools]


def _tool_spec_to_anthropic_tool(tool: ToolSpec) -> ToolParam:
    return cast(
        ToolParam,
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": deepcopy(dict(tool.input_schema)),
        },
    )


def _usage_from_anthropic_start(event: RawMessageStartEvent) -> ProviderUsage:
    value = event.message.usage
    input_tokens = max(0, value.input_tokens)
    output_tokens = max(0, value.output_tokens)
    cache_read_tokens = (
        max(0, value.cache_read_input_tokens) if value.cache_read_input_tokens is not None else None
    )
    cache_write_tokens = (
        max(0, value.cache_creation_input_tokens)
        if value.cache_creation_input_tokens is not None
        else None
    )
    output_details = value.output_tokens_details
    reasoning_tokens = (
        max(0, output_details.thinking_tokens) if output_details is not None else None
    )
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            input_tokens + output_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0)
        ),
        context_input_tokens=input_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0),
        cache_read_input_tokens=cache_read_tokens,
        cache_write_input_tokens=cache_write_tokens,
        reasoning_output_tokens=reasoning_tokens,
    )


def _usage_from_anthropic_delta(
    event: RawMessageDeltaEvent, current: ProviderUsage | None
) -> ProviderUsage | None:
    if current is None:
        return None
    output_tokens = event.usage.output_tokens
    input_tokens = event.usage.input_tokens
    if input_tokens is None:
        input_tokens = current.input_tokens
    cache_read_tokens = event.usage.cache_read_input_tokens
    if cache_read_tokens is None:
        cache_read_tokens = current.cache_read_input_tokens
    cache_write_tokens = event.usage.cache_creation_input_tokens
    if cache_write_tokens is None:
        cache_write_tokens = current.cache_write_input_tokens
    output_details = event.usage.output_tokens_details
    reasoning_tokens = current.reasoning_output_tokens
    if output_details is not None:
        reasoning_tokens = max(0, output_details.thinking_tokens)
    input_tokens = max(0, input_tokens)
    output_tokens = max(0, output_tokens)
    cache_read_tokens = max(0, cache_read_tokens) if cache_read_tokens is not None else None
    cache_write_tokens = max(0, cache_write_tokens) if cache_write_tokens is not None else None
    return ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=(
            input_tokens + output_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0)
        ),
        context_input_tokens=input_tokens + (cache_read_tokens or 0) + (cache_write_tokens or 0),
        cache_read_input_tokens=cache_read_tokens,
        cache_write_input_tokens=cache_write_tokens,
        reasoning_output_tokens=reasoning_tokens,
    )


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["DEFAULT_ANTHROPIC_MODEL", "AnthropicProvider"]
