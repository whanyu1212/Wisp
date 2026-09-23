"""Open provider requests using explicitly supported optional capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, cast

import wisp.providers.events as provider_events
from wisp.agent.messages import Message
from wisp.providers.base import (
    ContextOverflowError,
    ContinuationMessageProvider,
    PromptCacheContinuationMessageProvider,
    PromptCacheKeyProvider,
    Provider,
    ToolCallResult,
    is_context_overflow_message,
)
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseFailed

from .stream_cleanup import closing_stream

if TYPE_CHECKING:
    from .config import AgentLoopConfig


def provider_supports_prompt_cache_key(provider: Provider) -> bool:
    """Check whether an adapter explicitly accepts a prompt cache key.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_prompt_cache_key is literally True.
    """
    return getattr(provider, "supports_prompt_cache_key", False) is True


def provider_supports_continuation_messages(provider: Provider) -> bool:
    """Check whether an adapter explicitly accepts continuation messages.

    Args:
        provider (Provider): Adapter to inspect without requiring a new protocol member.

    Returns:
        bool: True only when supports_continuation_messages is literally True.
    """
    return getattr(provider, "supports_continuation_messages", False) is True


def provider_supports_context_rebase(provider: Provider) -> bool:
    """Check whether an adapter explicitly supports context rebasing.

    Args:
        provider (Provider): Adapter whose optional capability is inspected.

    Returns:
        bool: True only when supports_context_rebase is literally True.
    """
    return getattr(provider, "supports_context_rebase", False) is True


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
        config (AgentLoopConfig): Provider, model, tool schemas, effort, and cache settings.
        messages (Sequence[Message]): Portable base history.
        tool_results (Sequence[ToolCallResult]): Pending outputs for the next request.
        extra_messages (Sequence[Message]): User messages for a continued request; sent
            only when the adapter advertises continuation-message support.
        previous_response_id (str | None): Native continuation cursor, if available.

    Returns:
        AsyncIterator[provider_events.ProviderEvent]: Provider stream, not yet consumed.
            The caller owns its lifetime and must close it when supported. Effort is omitted
            when unset; cache keys are sent only to supporting adapters.

    Raises:
        Exception: Synchronous errors from the provider's stream factory propagate.
            Errors while consuming the returned iterator occur later.
    """

    provider = config.provider
    supports_continuation_messages = provider_supports_continuation_messages(provider)
    supports_prompt_cache_key = provider_supports_prompt_cache_key(provider)
    use_prompt_cache_key = config.prompt_cache_key is not None and supports_prompt_cache_key

    if extra_messages and supports_continuation_messages and use_prompt_cache_key:
        combined_provider = cast(PromptCacheContinuationMessageProvider, provider)
        if config.effort is not None:
            return combined_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id=previous_response_id,
                effort=config.effort,
                prompt_cache_key=config.prompt_cache_key,
            )
        return combined_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            extra_messages=extra_messages,
            previous_response_id=previous_response_id,
            prompt_cache_key=config.prompt_cache_key,
        )

    if extra_messages and supports_continuation_messages:
        continuation_provider = cast(ContinuationMessageProvider, provider)
        if config.effort is not None:
            return continuation_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                extra_messages=extra_messages,
                previous_response_id=previous_response_id,
                effort=config.effort,
            )
        return continuation_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            extra_messages=extra_messages,
            previous_response_id=previous_response_id,
        )

    if use_prompt_cache_key:
        cache_provider = cast(PromptCacheKeyProvider, provider)
        if config.effort is not None:
            return cache_provider.stream(
                messages,
                model=config.model,
                tools=config.tools,
                tool_results=tool_results,
                previous_response_id=previous_response_id,
                effort=config.effort,
                prompt_cache_key=config.prompt_cache_key,
            )
        return cache_provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
            prompt_cache_key=config.prompt_cache_key,
        )

    if config.effort is not None:
        return provider.stream(
            messages,
            model=config.model,
            tools=config.tools,
            tool_results=tool_results,
            previous_response_id=previous_response_id,
            effort=config.effort,
        )
    return provider.stream(
        messages,
        model=config.model,
        tools=config.tools,
        tool_results=tool_results,
        previous_response_id=previous_response_id,
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
