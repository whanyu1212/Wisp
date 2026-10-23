"""Open provider requests using explicitly supported optional capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import TYPE_CHECKING, cast

import wisp.providers.events as provider_events
from wisp.messages import Message
from wisp.providers.base import (
    ContextOverflowError,
    ToolCallResult,
    is_context_overflow_message,
    provider_supports_continuation_messages,
    provider_supports_prompt_cache_key,
    provider_supports_web_search,
)
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseFailed

from .stream_cleanup import closing_stream

if TYPE_CHECKING:
    from .config import AgentLoopConfig


def open_provider_stream(
    config: AgentLoopConfig,
    *,
    messages: Sequence[Message],
    tool_results: Sequence[ToolCallResult],
    extra_messages: Sequence[Message],
    previous_response_id: str | None,
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Open a provider stream using only the optional keywords it supports.

    Args:
        config (AgentLoopConfig): Provider, model, tool schemas, effort, cache, and
            web search settings.
        messages (Sequence[Message]): Portable base history.
        tool_results (Sequence[ToolCallResult]): Pending outputs for the next request.
        extra_messages (Sequence[Message]): User messages for a continued request; sent
            only when the adapter advertises continuation-message support.
        previous_response_id (str | None): Native continuation cursor, if available.

    Returns:
        AsyncIterator[provider_events.ProviderEvent]: Provider stream, not yet consumed.
            The caller owns its lifetime and must close it when supported. Effort is omitted
            when unset; cache keys and web search are sent only to supporting adapters.

    Raises:
        Exception: Synchronous errors from the provider's stream factory propagate.
            Errors while consuming the returned iterator occur later.
    """

    provider = config.provider
    # Each optional keyword goes only to an adapter that declares it, so a
    # third-party provider written against the minimal protocol keeps working.
    options: dict[str, object] = {}
    if extra_messages and provider_supports_continuation_messages(provider):
        options["extra_messages"] = extra_messages
    if config.effort is not None:
        options["effort"] = config.effort
    if config.prompt_cache_key is not None and provider_supports_prompt_cache_key(provider):
        options["prompt_cache_key"] = config.prompt_cache_key
    if config.web_search and provider_supports_web_search(provider):
        options["web_search"] = True
    stream = cast(Callable[..., AsyncIterator[provider_events.ProviderEvent]], provider.stream)
    return stream(
        messages,
        model=config.model,
        tools=config.tools,
        tool_results=tool_results,
        previous_response_id=previous_response_id,
        **options,
    )


async def iter_provider_events(
    stream: AsyncIterator[provider_events.ProviderEvent],
) -> AsyncIterator[provider_events.ProviderEvent]:
    """Forward provider events, normalize overflow errors, and close the owned iterator.

    Args:
        stream (AsyncIterator[provider_events.ProviderEvent]): Provider event source.

    Yields:
        provider_events.ProviderEvent: Each provider event unchanged and in source order.

    Raises:
        ContextOverflowError: Advancing the iterator raises an error whose message
            matches a known overflow pattern before a terminal event; the original
            error is chained as its cause.
        Exception: Non-overflow iterator errors and post-terminal cleanup errors
            propagate unchanged.
    """

    terminal_received = False
    async with closing_stream(aiter(stream)) as iterator:
        while True:
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                return
            except Exception as exc:
                # Async-generator finally/aclose errors surface on the anext after
                # the last event. They are not a rejected request.
                if terminal_received or not is_context_overflow_message(str(exc)):
                    raise
                raise ContextOverflowError(str(exc)) from exc
            if isinstance(event, ProviderResponseCompleted | ProviderResponseFailed):
                terminal_received = True
            yield event
