"""Contracts for optional iterator closure and cleanup exception precedence."""

from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
import pytest

from wisp.agent.loop.stream_cleanup import closing_stream


class _LegacyIterator:
    def __aiter__(self) -> AsyncIterator[int]:
        return self

    async def __anext__(self) -> int:
        raise StopAsyncIteration


class _ClosableIterator(_LegacyIterator):
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.close_error = close_error
        self.closed = 0

    async def aclose(self) -> None:
        await anyio.sleep(0)
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def test_legacy_iterator_requires_no_close_capability() -> None:
    async def run() -> None:
        iterator = _LegacyIterator()
        async with closing_stream(iterator) as stream:
            assert stream is iterator
            assert [event async for event in stream] == []

    anyio.run(run)


@pytest.mark.parametrize("exhaust", [False, True])
def test_owned_iterator_is_closed_once(exhaust: bool) -> None:
    async def run() -> None:
        iterator = _ClosableIterator()
        async with closing_stream(iterator) as stream:
            if exhaust:
                assert [event async for event in stream] == []
        assert iterator.closed == 1

    anyio.run(run)


def test_cleanup_is_shielded_but_consumption_is_not() -> None:
    async def run() -> None:
        iterator = _ClosableIterator()
        with anyio.CancelScope() as scope:
            async with closing_stream(iterator):
                scope.cancel()
                await anyio.sleep(0)
                pytest.fail("Consumption must remain cancellable")
        assert scope.cancelled_caught
        assert iterator.closed == 1

    anyio.run(run)


def test_primary_failure_survives_cleanup_failure() -> None:
    async def run() -> None:
        primary = ValueError("consume failed")
        cleanup = RuntimeError("close failed")
        iterator = _ClosableIterator(close_error=cleanup)
        with pytest.raises(ValueError) as caught:
            async with closing_stream(iterator):
                raise primary
        assert caught.value is primary
        assert caught.value.__cause__ is cleanup
        assert iterator.closed == 1

    anyio.run(run)


def test_cancellation_survives_cleanup_failure() -> None:
    async def run() -> None:
        iterator = _ClosableIterator(close_error=RuntimeError("close failed"))
        with anyio.CancelScope() as scope:
            try:
                async with closing_stream(iterator):
                    scope.cancel()
                    await anyio.sleep(0)
            except anyio.get_cancelled_exc_class() as error:
                assert isinstance(error.__cause__, RuntimeError)
                assert str(error.__cause__) == "close failed"
                raise
        assert scope.cancelled_caught
        assert iterator.closed == 1

    anyio.run(run)


@pytest.mark.parametrize("explicit_close", [False, True])
def test_close_error_propagates_without_a_primary_failure(explicit_close: bool) -> None:
    async def run() -> None:
        cleanup = RuntimeError("close failed")
        iterator = _ClosableIterator(close_error=cleanup)
        with pytest.raises(RuntimeError) as caught:
            async with closing_stream(iterator):
                if explicit_close:
                    raise GeneratorExit
        assert caught.value is cleanup
        assert iterator.closed == 1

    anyio.run(run)
