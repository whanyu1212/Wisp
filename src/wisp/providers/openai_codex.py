"""OpenAI Codex provider backed by ChatGPT subscription auth."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Literal, cast

import anyio
import httpx

from wisp.auth.openai_codex import account_id_from_access_token, refresh_openai_codex_token
from wisp.auth.storage import JsonAuthStore
from wisp.config.runtime import default_auth_path
from wisp.messages import Message, NativeOutput, Role
from wisp.providers.auth import ProviderAuthResolver, StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderConfigurationError,
    ProviderError,
    ProviderProtocolError,
    ToolCallResult,
    ToolSpec,
    is_context_overflow_message,
)
from wisp.providers.continuations import ContinuationStore, replayable_native_items
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderToolCallCompleted,
    ProviderUsage,
    ProviderWebSearch,
    ProviderWebSource,
    ToolCall,
    parse_tool_call,
)
from wisp.retry import RetryDecision, RetryPolicy, http_retry_decision, retry_delay_seconds

DEFAULT_OPENAI_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_CODEX_READ_TIMEOUT_SECONDS = 300.0
# How long a stream that ended at its terminal event is read on to its end. The
# pooled connection is kept for the next request only if the response is read
# to EOF; otherwise httpcore closes it and the next turn opens TCP and TLS again.
# The bound stays below the cost of a new connection, so a server that holds the
# stream open costs no more than reconnecting would.
_STREAM_DRAIN_SECONDS = 0.25
# Responses whose output items are kept in memory for replay on later prompts.
# Sessions also save each response's items with its row (see native_output_for),
# so this bound matters only for callers that keep transcripts without a session.
_NATIVE_OUTPUT_CAPACITY = 2048

type _NativeOutputLookup = Callable[[Message], tuple[dict[str, object], ...] | None]


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
    supports_prompt_cache_key: Literal[True] = True
    supports_continuation_messages: Literal[True] = True
    supports_context_rebase: Literal[True] = True
    supports_web_search: Literal[True] = True

    def supports_structured_tool_replacement(self, *, effort: str | None) -> bool:
        """Declare that portable call/result pairs can be reconstructed natively."""

        del effort
        return True

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_OPENAI_CODEX_MODEL,
        auth_resolver: ProviderAuthResolver | None = None,
        base_url: str = DEFAULT_CODEX_BASE_URL,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        read_timeout_seconds: float = DEFAULT_CODEX_READ_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(read_timeout_seconds) or read_timeout_seconds <= 0:
            raise ValueError("Codex read_timeout_seconds must be positive and finite")
        self._timeout = httpx.Timeout(
            connect=10.0, read=read_timeout_seconds, write=30.0, pool=10.0
        )
        self.default_model: str | None = default_model
        self._auth_resolver = auth_resolver or StoredProviderAuthResolver(
            JsonAuthStore(default_auth_path())
        )
        self._base_url = base_url
        self._client = client
        self._client_is_injected = client is not None
        self._retry_policy = retry_policy or RetryPolicy()
        self._continuations = ContinuationStore[tuple[dict[str, object], ...]]()
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
        web_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream a normalized OpenAI Codex response lifecycle.

        ``effort`` maps to ``reasoning.effort`` on the Responses API request
        body (same shape as ``OpenAIProvider``) -- passed through
        unvalidated.

        ``web_search`` adds the hosted ``web_search`` tool, as the Codex CLI's
        own search option does. Each completed ``web_search_call`` item is
        yielded as a ``ProviderWebSearch``; the item stays in the native replay.
        """

        selected_model = model or self.default_model or DEFAULT_OPENAI_CODEX_MODEL
        headers = await self._request_headers(prompt_cache_key)
        continuation_input = (
            *self._get_continuation(previous_response_id),
            *_tool_results_to_codex_input(tool_results),
            *_messages_to_codex_input(extra_messages),
        )
        body = _codex_request_body(
            messages,
            model=selected_model,
            tools=tools,
            continuation_input=continuation_input,
            effort=effort,
            prompt_cache_key=prompt_cache_key,
            web_search=web_search,
            native_output=lambda message: replayable_native_items(
                message, self._native_outputs, provider=self.name, model=selected_model
            ),
        )
        response = _CodexResponse(previous_response_id=previous_response_id)
        for retry_number in range(self._retry_policy.max_retries + 1):
            response_started = False
            try:
                async with self._create_stream(body=body, headers=headers) as stream:
                    response_started = True
                    yield ProviderResponseStarted(model=selected_model)

                    async for event in stream:
                        emitted = response.apply(event)
                        if emitted is not None:
                            yield emitted
                        if response.finished:
                            break
            except (ProviderError, httpx.HTTPError) as exc:
                error_message = _codex_exception_message(
                    exc,
                    timeout=self._client.timeout if self._client is not None else self._timeout,
                    response_started=response_started,
                    response_id=response.response_id,
                    attempt=retry_number + 1,
                )
                if response_started:
                    response.fail(error_message)
                    break
                outcome = self._opening_failure_outcome(
                    exc, message=error_message, retry_number=retry_number
                )
                yield outcome
                if isinstance(outcome, ProviderResponseFailed):
                    return
                await anyio.sleep(outcome.delay_seconds)
                continue
            break

        if not response.completed:
            response.fail("OpenAI Codex stream ended before response.completed was received")

        if response.failure is not None:
            # A context rejection does not advance or invalidate the replay
            # tail. Keep it available for a same-loop compacted retry.
            if response.failure.failure_kind != "context_overflow" and not (
                is_context_overflow_message(response.failure.message)
            ):
                self._continuations.discard(previous_response_id)
            yield response.failure
            return

        if response.tool_calls and response.response_id is None:
            raise ProviderProtocolError("OpenAI Codex tool response did not include a response id")
        self._remember_completed_response(
            response, continuation_input=continuation_input, model=selected_model
        )
        for content_index, tool_call in enumerate(response.tool_calls):
            yield ProviderToolCallCompleted(tool_call=tool_call, content_index=content_index)

        yield ProviderResponseCompleted(
            content=response.partial_content,
            tool_calls=tuple(response.tool_calls),
            response_id=response.response_id,
            finish_reason="tool_calls" if response.tool_calls else "stop",
            usage=response.usage,
        )

    def _opening_failure_outcome(
        self,
        exc: ProviderError | httpx.HTTPError,
        *,
        message: str,
        retry_number: int,
    ) -> ProviderRetrying | ProviderResponseFailed:
        """Decide whether a request that failed before streaming is retried.

        Args:
            exc (ProviderError | httpx.HTTPError): The opening failure.
            message (str): The diagnostic a terminal failure reports.
            retry_number (int): Retries already made, starting at 0.

        Returns:
            ProviderRetrying | ProviderResponseFailed: The retry to announce before
            sleeping its delay, or the terminal failure.
        """

        decision = _codex_retry_decision(exc)
        if decision is None or retry_number >= self._retry_policy.max_retries:
            return ProviderResponseFailed(
                message=message,
                failure_kind=(
                    "context_overflow" if is_context_overflow_message(str(exc)) else "error"
                ),
            )
        delay = retry_delay_seconds(
            self._retry_policy,
            retry_number=retry_number + 1,
            retry_after_seconds=decision.retry_after_seconds,
        )
        if delay is None:
            return ProviderResponseFailed(message=message)
        return ProviderRetrying(
            attempt=retry_number + 2,
            max_attempts=self._retry_policy.max_retries + 1,
            delay_seconds=delay,
            reason=decision.reason,
            status_code=decision.status_code,
        )

    async def _request_headers(self, prompt_cache_key: str | None) -> dict[str, str]:
        auth = await self._auth_resolver.bearer_token(
            self.name,
            refresh=lambda credential: refresh_openai_codex_token(credential),
        )
        if auth is None:
            raise ProviderConfigurationError(
                "openai-codex credentials are required; start Wisp and run `/connect openai-codex`"
            )
        account_id = auth.account_id or account_id_from_access_token(auth.token)
        return _codex_headers(
            token=auth.token,
            account_id=account_id,
            session_id=prompt_cache_key,
        )

    def _remember_completed_response(
        self,
        response: _CodexResponse,
        *,
        continuation_input: tuple[dict[str, object], ...],
        model: str,
    ) -> None:
        """Record a completed response for continuation and for native replay."""

        previous_response_id = response.previous_response_id
        # Codex uses a local store for store:false continuations. Preserve
        # the upstream response ID in public events; if a clean continuation
        # omits a new ID, advance the existing local replay key instead.
        continuation_id = response.response_id or previous_response_id
        replay_items = _codex_replay_items(
            response.output_items.values(), tool_calls=response.tool_calls
        )
        if continuation_id is not None:
            if previous_response_id is not None and previous_response_id != continuation_id:
                self._continuations.consume(previous_response_id)
            self._continuations.remember(
                continuation_id,
                (*continuation_input, *replay_items),
            )
        # A later prompt rebuilds this response from its transcript row. Keeping
        # the exact items lets that rebuild match what this run sent, so the
        # cached prefix extends past this run instead of stopping at its first
        # reasoning item. The row is found by its response ID, so a completion
        # that omits one keeps its portable form on later prompts; it still
        # continues natively within this run through the continuation store.
        if response.response_id is not None and replay_items:
            self._native_outputs.remember(
                response.response_id,
                NativeOutput(
                    provider=self.name,
                    model=model,
                    items=replay_items,
                ),
            )

    def native_output_for(self, response_id: str) -> NativeOutput | None:
        """Return the output items recorded for one of this provider's responses.

        Args:
            response_id (str): Upstream response ID from a completed response.

        Returns:
            NativeOutput | None: The items as its own run replayed them, or None
            when the response is unknown or was evicted.
        """

        return self._native_outputs.get(response_id)

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
        """Open one Codex response stream on the shared HTTP client.

        Each request closes its own response; the client and its connection pool
        stay open for later requests until ``aclose``.

        Args:
            body (Mapping[str, object]): JSON request body.
            headers (Mapping[str, str]): Request headers, including auth and session.

        Yields:
            AsyncIterator[dict[str, object]]: Parsed SSE events of the response.

        Raises:
            _CodexHTTPError: If the response status is not successful.
            httpx.HTTPError: If the request or its response stream fails.
        """

        async with self._http_client().stream(
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
            lines = response.aiter_lines()
            events = _sse_events(lines)
            yield events
            # Reached only when the caller left the stream normally, typically
            # at its terminal event. Errors and cancellation skip this and let
            # httpcore discard the connection as before.
            await events.aclose()
            await _drain_lines(lines)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the Wisp-owned HTTP client and its pooled connections.

        An injected client is left open for its owner. A later request creates a
        new owned client.
        """

        if self._client_is_injected or self._client is None:
            return
        client = self._client
        self._client = None
        await client.aclose()


async def _drain_lines(lines: AsyncIterator[str]) -> None:
    """Read a finished response to EOF within a short bound so its connection is pooled."""

    with anyio.move_on_after(_STREAM_DRAIN_SECONDS):
        try:
            async for _line in lines:
                pass
        except httpx.HTTPError:
            # The response already completed; a failed drain only loses the
            # connection, which httpcore then closes.
            pass


def _codex_exception_message(
    exc: ProviderError | httpx.HTTPError,
    *,
    timeout: httpx.Timeout,
    response_started: bool,
    response_id: str | None,
    attempt: int,
) -> str:
    """Describe a terminal failure without exposing transport URLs or headers.

    Args:
        exc (ProviderError | httpx.HTTPError): Failure from opening or reading a response.
        timeout (httpx.Timeout): Active client's timeout policy, including injected clients.
        response_started (bool): Whether HTTP success already opened the response stream.
        response_id (str | None): Current upstream response ID, never the prior cursor.
        attempt (int): One-based request attempt that failed.

    Returns:
        str: Nonempty diagnostic retained by the existing error event and session log.
    """

    if isinstance(exc, httpx.TimeoutException):
        phase, seconds = "request", None
        for error_type, name, limit in (
            (httpx.ReadTimeout, "read-idle", timeout.read),
            (httpx.ConnectTimeout, "connect", timeout.connect),
            (httpx.WriteTimeout, "write", timeout.write),
            (httpx.PoolTimeout, "pool", timeout.pool),
        ):
            if isinstance(exc, error_type):
                phase, seconds = name, limit
                break
        configured = f"; configured limit {seconds:g}s" if seconds is not None else ""
        detail = f"{phase} timeout{configured}"
    elif isinstance(exc, httpx.HTTPError):
        # Transport exception text can contain credential-bearing request URLs.
        detail = "network/transport failure"
    else:
        detail = str(exc).strip() or "provider failure without a description"
    diagnostic = f"OpenAI Codex {type(exc).__name__}: {detail} (attempt {attempt}"
    if response_id is not None:
        diagnostic += f"; response_id={response_id}"
    diagnostic += "). "
    if response_started:
        return diagnostic + (
            "The response stream had started; it was not automatically retried. "
            "Resume the session to continue; inspect completed tool results before rerunning work."
        )
    return diagnostic + "No response stream started; no further automatic retries will be made."


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
    prompt_cache_key: str | None = None,
    web_search: bool = False,
    native_output: _NativeOutputLookup | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "model": model,
        "store": False,
        "stream": True,
        "input": [
            *_messages_to_codex_input(messages, native_output=native_output),
            *continuation_input,
        ],
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    instructions = _instructions_from_messages(messages)
    if instructions:
        body["instructions"] = instructions
    codex_tools: list[dict[str, object]] = [_tool_spec_to_codex_tool(tool) for tool in tools]
    if web_search:
        codex_tools.append({"type": "web_search"})
    if codex_tools:
        body["tools"] = codex_tools
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    if prompt_cache_key is not None:
        body["prompt_cache_key"] = prompt_cache_key
    return body


@dataclass(slots=True)
class _CodexResponse:
    """What one Codex stream has produced so far, folded from its SSE events.

    Output items are kept by item ID, else call ID, in first-seen order, for the
    store:false replay. A function call is emitted once, from whichever arrives
    first: its ``function_call_arguments.done`` event or its finished item.
    """

    previous_response_id: str | None
    response_id: str | None = None
    chunks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: ProviderUsage | None = None
    failure: ProviderResponseFailed | None = None
    completed: bool = False
    output_items: OrderedDict[str, dict[str, object]] = field(default_factory=OrderedDict)
    pending_tool_calls: dict[str, dict[str, object]] = field(default_factory=dict)
    completed_tool_arguments: dict[str, str] = field(default_factory=dict)
    emitted_tool_item_ids: set[str] = field(default_factory=set)
    anonymous_items: int = 0

    @property
    def partial_content(self) -> str:
        return "".join(self.chunks)

    @property
    def finished(self) -> bool:
        """Whether the stream reached ``response.completed`` or a failure."""

        return self.completed or self.failure is not None

    def apply(self, event: Mapping[str, object]) -> ProviderEvent | None:
        """Fold one SSE event into the response.

        Args:
            event (Mapping[str, object]): A decoded Responses API stream event.

        Returns:
            ProviderEvent | None: An event to yield now (a text delta or a finished
            web search), or None.
        """

        event_type = _string_value(event.get("type"))
        if event_type in {"response.created", "response.in_progress"}:
            self._record_response_id(event.get("response"))
        elif event_type in {"response.output_text.delta", "response.refusal.delta"}:
            return self._text_delta(event)
        elif event_type == "response.function_call_arguments.done":
            self._arguments_done(event)
        elif event_type in {"response.output_item.added", "response.output_item.done"}:
            return self._output_item(event, done=event_type == "response.output_item.done")
        elif event_type == "response.completed":
            self._complete(event.get("response"))
        elif event_type == "error":
            self.fail(_codex_error_message(event))
        elif event_type == "response.failed":
            self.fail(_codex_failed_message(event))
        elif event_type == "response.incomplete":
            self.fail(_codex_incomplete_message(event))
        return None

    def fail(self, message: str) -> None:
        """Record the stream's failure, keeping the first one if it already failed."""

        if self.failure is None:
            self.failure = ProviderResponseFailed(
                message=message,
                partial_content=self.partial_content,
                response_id=self.response_id or self.previous_response_id,
            )

    def _record_response_id(self, response: object) -> None:
        if isinstance(response, dict):
            self.response_id = _string_value(response.get("id")) or self.response_id

    def _text_delta(self, event: Mapping[str, object]) -> ProviderTextDelta | None:
        delta = _string_value(event.get("delta"))
        if delta is None:
            return None
        self.chunks.append(delta)
        return ProviderTextDelta(delta=delta)

    def _arguments_done(self, event: Mapping[str, object]) -> None:
        item_id = _string_value(event.get("item_id"))
        if item_id is None:
            return
        arguments = _string_value(event.get("arguments")) or "{}"
        self.completed_tool_arguments[item_id] = arguments
        pending = self.pending_tool_calls.get(item_id)
        if pending is None:
            return
        pending["arguments"] = arguments
        self.output_items[item_id] = deepcopy(pending)
        self.tool_calls.append(
            _tool_call_from_codex(pending, raw_arguments=arguments, response_id=self.response_id)
        )
        self.emitted_tool_item_ids.add(item_id)

    def _output_item(self, event: Mapping[str, object], *, done: bool) -> ProviderEvent | None:
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        self._remember_output_item(item)
        if item.get("type") == "function_call":
            self._function_call_item(item, done=done)
        if item.get("type") == "web_search_call" and done:
            return _web_search_from_codex(item)
        return None

    def _function_call_item(self, item: dict[str, object], *, done: bool) -> None:
        item_id = _string_value(item.get("id"))
        if item_id is not None:
            self.pending_tool_calls[item_id] = dict(item)
        if not done or (item_id is not None and item_id in self.emitted_tool_item_ids):
            return
        item_arguments = _string_value(item.get("arguments")) or "{}"
        raw_arguments = (
            self.completed_tool_arguments.get(item_id, item_arguments)
            if item_id is not None
            else item_arguments
        )
        self.tool_calls.append(
            _tool_call_from_codex(item, raw_arguments=raw_arguments, response_id=self.response_id)
        )
        if item_id is not None:
            self.emitted_tool_item_ids.add(item_id)

    def _complete(self, response: object) -> None:
        if isinstance(response, dict):
            self.response_id = _string_value(response.get("id")) or self.response_id
            self.usage = _usage_from_codex(response.get("usage"))
            output = response.get("output")
            if isinstance(output, list):
                for item in output:
                    if isinstance(item, dict):
                        self._remember_output_item(item)
        self.completed = True

    def _remember_output_item(self, item: dict[str, object]) -> None:
        key = _string_value(item.get("id")) or _string_value(item.get("call_id"))
        if key is None:
            key = f"anonymous-{self.anonymous_items}"
            self.anonymous_items += 1
        self.output_items[key] = deepcopy(item)


def _web_search_from_codex(item: Mapping[str, object]) -> ProviderWebSearch:
    """Report one completed web_search_call item as a provider search."""

    error = "failed" if item.get("status") == "failed" else None
    action = item.get("action")
    if not isinstance(action, Mapping):
        return ProviderWebSearch(error=error)
    url = _string_value(action.get("url"))
    if action.get("type") == "open_page":
        sources = (ProviderWebSource(url=url),) if url else ()
        return ProviderWebSearch(action="open_page", sources=sources, error=error)
    if action.get("type") == "find_in_page":
        return ProviderWebSearch(
            action="find_in_page",
            query=_string_value(action.get("pattern")),
            sources=(ProviderWebSource(url=url),) if url else (),
            error=error,
        )
    queries = action.get("queries")
    query = _string_value(action.get("query")) or (
        "; ".join(query for query in queries if isinstance(query, str))
        if isinstance(queries, list)
        else None
    )
    raw_sources = action.get("sources")
    return ProviderWebSearch(
        query=query or None,
        sources=tuple(
            ProviderWebSource(url=source_url)
            for source in (raw_sources if isinstance(raw_sources, list) else ())
            if isinstance(source, Mapping)
            and (source_url := _string_value(source.get("url"))) is not None
        ),
        error=error,
    )


def _instructions_from_messages(messages: Sequence[Message]) -> str | None:
    instructions = [
        message.content for message in messages if message.role in {"system", "developer"}
    ]
    return "\n\n".join(instructions) or None


def _messages_to_codex_input(
    messages: Sequence[Message],
    *,
    native_output: _NativeOutputLookup | None = None,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            continue
        native_items = native_output(message) if native_output is not None else None
        if native_items is not None:
            result.extend(native_items)
            continue
        if message.role == "tool" and message.tool_call_id:
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            # A structured tool-call row from the still-active turn (see
            # ``normalize_provider_history``). The Responses API `store: false`
            # continuation chain does not survive a fresh ``run_agent_loop`` call, so
            # a mid-turn transcript rebuild (e.g. after auto-compaction) must replay
            # these as native function-call items — otherwise the model has no
            # record of tool calls it just made and repeats them.
            if message.content:
                result.append({"role": "assistant", "content": message.content})
            for call in message.tool_calls:
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call.call_id,
                        "name": call.name,
                        "arguments": json.dumps(dict(call.arguments), separators=(",", ":")),
                    }
                )
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
    return parse_tool_call(
        call_id=call_id, name=name, raw_arguments=raw_arguments, response_id=response_id
    )


def _codex_headers(
    *,
    token: str,
    account_id: str,
    session_id: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "originator": "wisp",
        "User-Agent": f"wisp ({os.name})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if session_id is not None:
        # The Codex backend reuses cached prefixes only for requests that carry the
        # conversation's `session_id` header, as the official Codex CLI sends; the
        # `prompt_cache_key` body field alone left most identical requests uncached.
        headers["session_id"] = _header_safe_session_id(session_id)
    return headers


def _header_safe_session_id(session_id: str) -> str:
    """Return a value that is safe to send as the ``session_id`` header.

    Wisp's own keys (``wisp:<hex>``) pass through unchanged. A key an SDK caller
    supplies may contain non-ASCII text, which httpx cannot encode in a header;
    CR/LF, which it would forward and so split the header; or leading/trailing
    whitespace, which HTTP/1.1 rejects when the request is sent. Such keys are
    replaced by a stable digest, so requests with the same key still share one
    header value.
    """

    if (
        session_id
        and session_id.isascii()
        and session_id.isprintable()
        and session_id == session_id.strip()
    ):
        return session_id
    return f"sha256:{hashlib.sha256(session_id.encode('utf-8', 'surrogatepass')).hexdigest()}"


def _resolve_codex_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/codex/responses"):
        return normalized
    if normalized.endswith("/codex"):
        return f"{normalized}/responses"
    return f"{normalized}/codex/responses"


async def _sse_events(lines: AsyncIterator[str]) -> AsyncGenerator[dict[str, object], None]:
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
        cache_write_input_tokens=_nested_int(value, "input_tokens_details", "cache_write_tokens"),
        reasoning_output_tokens=_nested_int(value, "output_tokens_details", "reasoning_tokens"),
    )


def _nested_int(value: Mapping[str, object], group: str, field: str) -> int | None:
    details = value.get(group)
    if not isinstance(details, Mapping):
        return None
    count = details.get(field)
    return max(0, count) if type(count) is int else None


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = ["DEFAULT_OPENAI_CODEX_MODEL", "OpenAICodexProvider"]
