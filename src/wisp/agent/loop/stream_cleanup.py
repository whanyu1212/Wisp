"""Deterministic cleanup for request and tool iterators owned by the runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

import anyio


async def _close_stream(stream: object) -> None:
    """Close an optional adapter capability without cancelling cleanup awaits.

    Args:
        stream (object): Owned iterator, optionally exposing an async aclose method.

    Raises:
        Exception: An error raised by the iterator's close method propagates.
    """
    close = getattr(stream, "aclose", None)
    if callable(close):
        # Never span a public event yield with this scope: consumers advance
        # streams inside their own cancellation scopes.
        with anyio.CancelScope(shield=True):
            await cast(Callable[[], Awaitable[None]], close)()


@asynccontextmanager
async def closing_stream[Event](
    stream: AsyncIterator[Event],
) -> AsyncIterator[AsyncIterator[Event]]:
    """Own an iterator until exhaustion, error, cancellation, or consumer closure.

    Args:
        stream (AsyncIterator[Event]): Child iterator whose lifetime belongs to the caller.

    Yields:
        AsyncIterator[Event]: The unchanged iterator. No cancellation scope surrounds
            consumption, and iterators without aclose remain supported.

    Raises:
        BaseException: The original consumption failure or cancellation propagates.
            A concurrent cleanup failure is chained as its cause. On normal exit or
            explicit generator closure, a cleanup failure propagates directly.
    """
    try:
        yield stream
    except BaseException as error:
        try:
            await _close_stream(stream)
        except Exception as cleanup_error:
            if isinstance(error, GeneratorExit):
                raise
            raise error from cleanup_error
        raise
    else:
        await _close_stream(stream)
