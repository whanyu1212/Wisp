"""Anthropic Messages API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
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
    ServerToolUseBlockParam,
    TextBlockParam,
    ThinkingBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUnionParam,
    ToolUseBlockParam,
    WebSearchTool20250305Param,
    WebSearchToolResultBlock,
    WebSearchToolResultBlockParam,
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
    ProviderWebSearch,
    ProviderWebSource,
    ToolCall,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"

AnthropicCacheTtl = Literal["5m", "1h"]
"""How long Anthropic keeps a prompt-cache entry after its last use.

A 5-minute entry is written at 1.25x the input price and a 1-hour entry at 2x;
reads cost 0.1x either way. The 1-hour lifetime pays off in interactive sessions
where the next prompt often arrives more than five minutes later.
"""

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
    # pause_turn is Anthropic's server-side loop (web search) hitting its
    # iteration cap. stream() resends the paused turn so the server resumes;
    # only a turn still paused after _MAX_PAUSE_CONTINUATIONS reaches this
    # mapping. Reporting it as a clean "stop" would silently truncate the
    # response, so it maps to "length" (incomplete), matching
    # model_context_window_exceeded/compaction below.
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

# Anthropic's server-side web search. The basic tool version
# (web_search_20250305, in _create_stream) is used on purpose: later versions
# run searches from code execution by default, which adds code-execution blocks
# to every turn that the replay would have to keep.
_WEB_SEARCH_TOOL_NAME = "web_search"
# Searches allowed in one request. Past the cap the model gets a
# max_uses_exceeded result instead of another search.
_WEB_SEARCH_MAX_USES = 5
# A long server-side search loop can stop with pause_turn. The adapter resends
# the paused turn so the server can resume, at most this many times, and then
# reports the response as incomplete.
_MAX_PAUSE_CONTINUATIONS = 3


@dataclass
class _ContentBlockAccumulator:
    """Accumulates one content block's streamed deltas for the replay tail.

    Anthropic's tool-use guidance requires echoing thinking/redacted_thinking
    blocks back to the API unmodified alongside their sibling tool_use block
    on the same turn -- dropping them (e.g. replaying only text + tool_use)
    can be rejected or lose reasoning continuity. Web search adds two server
    blocks, server_tool_use and web_search_tool_result, and citations on text;
    the API rejects a later turn whose search results or citations were
    changed, so those are kept as received (``raw_block``, ``citations``).
    Every block type Wisp can receive is accumulated so the replay is a
    faithful reconstruction of the full assistant turn, not just its
    tool-relevant parts.
    """

    block_type: str
    text_chunks: list[str] = field(default_factory=list)
    citations: list[dict[str, object]] = field(default_factory=list)
    thinking_chunks: list[str] = field(default_factory=list)
    signature: str = ""
    redacted_data: str = ""
    tool_use_id: str = ""
    tool_use_name: str = ""
    tool_use_json_chunks: list[str] = field(default_factory=list)
    raw_block: dict[str, object] = field(default_factory=dict)


@dataclass
class _StreamedTurn:
    """One assistant turn assembled from one or more streamed requests.

    A turn usually comes from one request. When Anthropic pauses its server-side
    search loop, the adapter resends the turn so far and the next request's
    blocks continue it; ``index_offset`` places them after the earlier ones.
    """

    blocks: dict[int, _ContentBlockAccumulator] = field(default_factory=dict)
    chunks: list[str] = field(default_factory=list)
    response_id: str | None = None
    stop_reason: str | None = None
    finish_reason: ProviderFinishReason = "stop"
    stream_completed: bool = False
    index_offset: int = 0
    # Usage of the paused requests already finished, and of the current one.
    earlier_usage: ProviderUsage | None = None
    usage: ProviderUsage | None = None
    # Queries of searches whose results may arrive in this turn, by block id.
    # A search requested beside a client tool runs only on the next request,
    # so its server_tool_use block is in the replay, not in ``blocks``.
    search_queries: dict[str, str | None] = field(default_factory=dict)

    def apply(self, event: RawMessageStreamEvent) -> list[ProviderEvent]:
        """Record one raw stream event and return the provider events it produces.

        Args:
            event (RawMessageStreamEvent): Next event of the current request.

        Returns:
            list[ProviderEvent]: Deltas and web searches to yield, in stream order.
        """

        if isinstance(event, RawMessageStartEvent):
            self.response_id = event.message.id
            self.usage = _usage_from_anthropic_start(event)
            return []
        if isinstance(event, RawContentBlockStartEvent):
            return self._start_block(event)
        if isinstance(event, RawContentBlockDeltaEvent):
            return self._apply_delta(event)
        if isinstance(event, RawMessageDeltaEvent):
            self.usage = _usage_from_anthropic_delta(event, self.usage)
            if event.delta.stop_reason is not None:
                self.stop_reason = event.delta.stop_reason
                self.finish_reason = _STOP_REASON_TO_FINISH_REASON.get(
                    self.stop_reason, _DEFAULT_FINISH_REASON_FOR_UNKNOWN_STOP_REASON
                )
                # message_delta carrying stop_reason is Anthropic's
                # signal that the message actually finished -- the
                # only remaining event is message_stop, which carries
                # no further information. Anything else (a dropped
                # connection, a truncated proxy response) ends the
                # async iterator without this ever being set.
                self.stream_completed = True
        return []

    def continue_after_pause(self) -> None:
        """Prepare for the request that resumes a paused turn."""

        self.index_offset = max(self.blocks, default=-1) + 1
        self.earlier_usage = self.total_usage()
        self.usage = None
        self.stop_reason = None
        self.finish_reason = "stop"
        self.stream_completed = False

    def total_usage(self) -> ProviderUsage | None:
        """Return the usage of every request that built this turn."""

        if self.earlier_usage is None:
            return self.usage
        if self.usage is None:
            return self.earlier_usage
        return _combined_usage(self.earlier_usage, self.usage)

    def _start_block(self, event: RawContentBlockStartEvent) -> list[ProviderEvent]:
        block = event.content_block
        index = self.index_offset + event.index
        if block.type == "tool_use":
            accumulator = _ContentBlockAccumulator(block_type="tool_use")
            accumulator.tool_use_id = block.id
            accumulator.tool_use_name = block.name
            self.blocks[index] = accumulator
        elif block.type == "text":
            self.blocks[index] = _ContentBlockAccumulator(block_type="text")
        elif block.type == "thinking":
            self.blocks[index] = _ContentBlockAccumulator(block_type="thinking")
        elif block.type == "redacted_thinking":
            accumulator = _ContentBlockAccumulator(block_type="redacted_thinking")
            accumulator.redacted_data = block.data
            self.blocks[index] = accumulator
        elif block.type == "server_tool_use":
            # Never a ToolCall: Anthropic runs it. Its input still streams as
            # input_json_delta, like a client tool's.
            accumulator = _ContentBlockAccumulator(block_type="server_tool_use")
            accumulator.tool_use_id = block.id
            accumulator.raw_block = block.model_dump(mode="json", exclude_none=True)
            self.blocks[index] = accumulator
        elif block.type == "web_search_tool_result":
            # The whole result arrives in this start event.
            accumulator = _ContentBlockAccumulator(block_type="web_search_tool_result")
            accumulator.raw_block = block.model_dump(mode="json", exclude_none=True)
            self.blocks[index] = accumulator
            return [_web_search_from_result(block, query=self._search_query(block.tool_use_id))]
        return []

    def _apply_delta(self, event: RawContentBlockDeltaEvent) -> list[ProviderEvent]:
        delta = event.delta
        index = self.index_offset + event.index
        accumulator = self.blocks.get(index)
        if delta.type == "text_delta":
            self.chunks.append(delta.text)
            if accumulator is not None:
                accumulator.text_chunks.append(delta.text)
            return [ProviderTextDelta(delta=delta.text, content_index=index)]
        if delta.type == "thinking_delta":
            if accumulator is not None:
                accumulator.thinking_chunks.append(delta.thinking)
            return [ProviderThinkingDelta(delta=delta.thinking, content_index=index)]
        if accumulator is None:
            return []
        if delta.type == "signature_delta":
            accumulator.signature += delta.signature
        elif delta.type == "input_json_delta":
            accumulator.tool_use_json_chunks.append(delta.partial_json)
        elif delta.type == "citations_delta":
            accumulator.citations.append(delta.citation.model_dump(mode="json", exclude_none=True))
        return []

    def _search_query(self, tool_use_id: str) -> str | None:
        for accumulator in self.blocks.values():
            if accumulator.block_type == "server_tool_use" and (
                accumulator.tool_use_id == tool_use_id
            ):
                return _search_query_from_input(_server_tool_input(accumulator))
        return self.search_queries.get(tool_use_id)


class AnthropicProvider:
    """Provider backed by Anthropic's Messages API."""

    name = "anthropic"
    supports_continuation_messages: Literal[True] = True
    supports_context_rebase: Literal[True] = True
    supports_web_search: Literal[True] = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_ANTHROPIC_MODEL,
        client: AsyncAnthropic | None = None,
        auth_resolver: ProviderAuthResolver | None = None,
        retry_policy: RetryPolicy | None = None,
        cache_ttl: AnthropicCacheTtl = "5m",
    ) -> None:
        if cache_ttl not in ("5m", "1h"):
            raise ValueError(f"Unsupported Anthropic cache TTL: {cache_ttl!r}")
        self.default_model: str | None = default_model
        self._cache_ttl: AnthropicCacheTtl = cache_ttl
        self._api_key = _normalize_optional(api_key)
        self._client = client
        self._client_is_injected = client is not None
        self._client_api_key: str | None = None
        self._auth_resolver = auth_resolver
        self._retry_policy = retry_policy or RetryPolicy()
        self._replays = ContinuationStore[tuple[MessageParam, ...]]()

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        """Reject fresh tool replay that could discard signed thinking blocks.

        Anthropic requires signed thinking blocks to be preserved byte-for-byte
        beside their tool uses. ``Message`` snapshots intentionally retain only
        portable text and tool data. Models such as Fable 5.1 think even when
        effort is omitted. This capability has no request model, so it cannot
        safely opt in based on effort or the provider's default model.

        Args:
            effort (str | None): Requested effort; omission does not disable thinking.

        Returns:
            bool: Always False; tool continuation must preserve native replay.
        """

        return False

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
        web_search: bool = False,
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

        ``web_search`` adds Anthropic's server-side web search tool. Each
        search is yielded as a ``ProviderWebSearch`` when its results arrive.
        When Anthropic pauses a long search loop (``pause_turn``), the paused
        turn is resent so the server can resume; the requests together form
        one response lifecycle, with their usage summed.
        """

        selected_model = model or self.default_model or DEFAULT_ANTHROPIC_MODEL
        request: dict[str, object] = {
            "model": selected_model,
            "tools": tools,
            "tool_results": tool_results,
            "previous_response_id": previous_response_id,
            "effort": effort,
        }
        if extra_messages:
            request["extra_messages"] = extra_messages
        if web_search:
            request["web_search"] = True

        turn = _StreamedTurn(search_queries=self._pending_search_queries(previous_response_id))
        failure: ProviderResponseFailed | None = None
        started = False
        for continuation in range(_MAX_PAUSE_CONTINUATIONS + 1):
            if continuation:
                request["paused_turn"] = _replay_message_from_stream(turn.blocks)
            stream: AsyncIterator[RawMessageStreamEvent] | None = None
            # Retry progress may only precede the response start, so a resumed
            # request retries without reporting it.
            async for opened in self._open_stream(messages, request, report_retries=not started):
                if isinstance(opened, ProviderRetrying):
                    yield opened
                elif isinstance(opened, ProviderResponseFailed):
                    if not started:
                        yield opened
                        return
                    failure = replace(
                        opened,
                        partial_content="".join(turn.chunks),
                        response_id=turn.response_id,
                    )
                else:
                    stream = opened
            if failure is not None:
                break
            if stream is None:
                raise AssertionError("Anthropic retry loop completed without a stream or error")
            if not started:
                yield ProviderResponseStarted(model=selected_model)
                started = True

            try:
                async for event in stream:
                    for provider_event in turn.apply(event):
                        yield provider_event
            except AnthropicError as exc:
                failure = ProviderResponseFailed(
                    message=f"Anthropic stream error: {exc}",
                    partial_content="".join(turn.chunks),
                    response_id=turn.response_id,
                )
                break
            if turn.stop_reason != "pause_turn" or continuation == _MAX_PAUSE_CONTINUATIONS:
                break
            turn.continue_after_pause()

        response_id = turn.response_id
        content_blocks = turn.blocks
        chunks = turn.chunks
        if failure is None and turn.stop_reason == "model_context_window_exceeded":
            failure = ProviderResponseFailed(
                message="Anthropic model_context_window_exceeded",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        if failure is None and not turn.stream_completed:
            # Regression guard: the stream ended (cleanly or not) without
            # Anthropic ever reporting a stop_reason. Silently yielding a
            # ProviderResponseCompleted here would report a truncated answer
            # as a successful "stop" turn -- treat it as failed instead.
            failure = ProviderResponseFailed(
                message="Anthropic stream ended before a stop_reason was received",
                partial_content="".join(chunks),
                response_id=response_id,
            )

        tool_calls: list[ToolCall] = []
        finish_reason = turn.finish_reason
        usage = turn.total_usage()

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

    async def _open_stream(
        self,
        messages: Sequence[Message],
        request: Mapping[str, object],
        *,
        report_retries: bool,
    ) -> AsyncIterator[
        ProviderRetrying | ProviderResponseFailed | AsyncIterator[RawMessageStreamEvent]
    ]:
        """Open one Messages stream, retrying transient failures.

        Args:
            messages (Sequence[Message]): Portable base history.
            request (Mapping[str, object]): Keyword arguments for ``_create_stream``.
            report_retries (bool): Yield retry progress; False still waits and retries.

        Yields:
            ProviderRetrying | ProviderResponseFailed | AsyncIterator[RawMessageStreamEvent]:
            Retry progress, then either the opened stream or the final failure.
        """

        for retry_number in range(self._retry_policy.max_retries + 1):
            try:
                yield await self._create_stream(messages, **request)  # type: ignore[arg-type]
                return
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
                if report_retries:
                    yield ProviderRetrying(
                        attempt=retry_number + 2,
                        max_attempts=self._retry_policy.max_retries + 1,
                        delay_seconds=delay,
                        reason=decision.reason,
                        status_code=decision.status_code,
                    )
                await anyio.sleep(delay)

    def _pending_search_queries(self, previous_response_id: str | None) -> dict[str, str | None]:
        """Return the queries of searches requested in the replayed turns.

        A search requested beside a client tool runs on the next request, so its
        results arrive in a later response than its server_tool_use block.
        """

        if previous_response_id is None:
            return {}
        replay = self._replays.get(previous_response_id) or ()
        queries: dict[str, str | None] = {}
        for message in replay:
            content = message["content"]
            if isinstance(content, str):
                continue
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "server_tool_use":
                    queries[str(block.get("id"))] = _search_query_from_input(block.get("input"))
        return queries

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
        web_search: bool = False,
        paused_turn: MessageParam | None = None,
    ) -> AsyncIterator[RawMessageStreamEvent]:
        client = await self._client_or_create()
        anthropic_messages = _messages_to_anthropic(messages)
        if previous_response_id is not None:
            anthropic_messages.extend(self._require_replay(previous_response_id))
        if tool_results:
            anthropic_messages.append(_tool_results_to_message(tool_results))
        anthropic_messages.extend(_messages_to_anthropic(extra_messages))
        if paused_turn is not None:
            # Anthropic resumes a paused turn when it is sent back unchanged as
            # the last message.
            anthropic_messages.append(paused_turn)
        system = _system_from_messages(messages)
        anthropic_tools: list[ToolUnionParam] = [*_tool_specs_to_anthropic_tools(tools)]
        # A client tool already named web_search would collide with the server
        # tool; that request goes without search rather than failing.
        if web_search and all(tool.name != _WEB_SEARCH_TOOL_NAME for tool in tools):
            web_search_tool: WebSearchTool20250305Param = {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": _WEB_SEARCH_MAX_USES,
            }
            anthropic_tools.append(web_search_tool)

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
            # Anthropic caches only when asked. Top-level ("automatic") caching
            # puts the breakpoint on the last cacheable block and moves it forward
            # as the conversation grows, so tools, system, and resent history are
            # read from cache on the next request. No block-level breakpoints are
            # set, which leaves the automatic one a free slot. The 5-minute default
            # sends no ttl, keeping that request identical to the API default.
            "cache_control": (
                {"type": "ephemeral", "ttl": "1h"}
                if self._cache_ttl == "1h"
                else {"type": "ephemeral"}
            ),
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
    TextBlockParam
    | ThinkingBlockParam
    | RedactedThinkingBlockParam
    | ToolUseBlockParam
    | ServerToolUseBlockParam
    | WebSearchToolResultBlockParam
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

    Server-tool blocks and citations are sent back as received: Anthropic
    rejects a turn whose search results or citation indexes changed.
    """

    content: list[_ReplayBlockParam] = []
    for index in sorted(content_blocks):
        accumulator = content_blocks[index]
        if accumulator.block_type == "text":
            text = "".join(accumulator.text_chunks)
            if text and accumulator.citations:
                content.append(
                    cast(
                        TextBlockParam,
                        {"type": "text", "text": text, "citations": accumulator.citations},
                    )
                )
            elif text:
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
        elif accumulator.block_type == "server_tool_use":
            content.append(
                cast(
                    ServerToolUseBlockParam,
                    {**accumulator.raw_block, "input": _server_tool_input(accumulator)},
                )
            )
        elif accumulator.block_type == "web_search_tool_result":
            content.append(cast(WebSearchToolResultBlockParam, accumulator.raw_block))
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


def _server_tool_input(accumulator: _ContentBlockAccumulator) -> dict[str, object]:
    """Return a server tool's input from its streamed JSON, or the start block's."""

    raw_arguments = "".join(accumulator.tool_use_json_chunks)
    if raw_arguments:
        arguments, _parse_error = _parse_tool_arguments(
            name=_WEB_SEARCH_TOOL_NAME, raw_arguments=raw_arguments
        )
        return dict(arguments)
    started_input = accumulator.raw_block.get("input")
    return dict(started_input) if isinstance(started_input, Mapping) else {}


def _search_query_from_input(arguments: object) -> str | None:
    if not isinstance(arguments, Mapping):
        return None
    query = arguments.get("query")
    return query if isinstance(query, str) else None


def _web_search_from_result(
    block: WebSearchToolResultBlock, *, query: str | None
) -> ProviderWebSearch:
    """Report one web_search_tool_result block as a provider search."""

    if not isinstance(block.content, list):
        return ProviderWebSearch(query=query, error=block.content.error_code)
    return ProviderWebSearch(
        query=query,
        sources=tuple(
            ProviderWebSource(url=result.url, title=result.title or None)
            for result in block.content
        ),
    )


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


def _combined_usage(earlier: ProviderUsage, latest: ProviderUsage) -> ProviderUsage:
    """Sum the usage of a paused turn's requests; context size is the latest one's."""

    def total(first: int | None, second: int | None) -> int | None:
        if first is None and second is None:
            return None
        return (first or 0) + (second or 0)

    return ProviderUsage(
        input_tokens=earlier.input_tokens + latest.input_tokens,
        output_tokens=earlier.output_tokens + latest.output_tokens,
        total_tokens=earlier.total_tokens + latest.total_tokens,
        context_input_tokens=latest.context_input_tokens,
        cache_read_input_tokens=total(
            earlier.cache_read_input_tokens, latest.cache_read_input_tokens
        ),
        cache_write_input_tokens=total(
            earlier.cache_write_input_tokens, latest.cache_write_input_tokens
        ),
        reasoning_output_tokens=total(
            earlier.reasoning_output_tokens, latest.reasoning_output_tokens
        ),
    )


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = ["DEFAULT_ANTHROPIC_MODEL", "AnthropicCacheTtl", "AnthropicProvider"]
