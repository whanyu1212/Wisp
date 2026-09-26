from __future__ import annotations

import anyio
import pytest

from benchmarks.support import measure, measure_async

pytestmark = pytest.mark.benchmark

_ALLOCATION_BYTES = 1_000_000


def test_repeated_memory_measurements_release_the_previous_result() -> None:
    result, synchronous = measure(
        lambda: bytearray(_ALLOCATION_BYTES),
        iterations=3,
        track_memory=True,
    )

    async def scenario() -> tuple[bytearray, int | None]:
        async def allocate() -> bytearray:
            return bytearray(_ALLOCATION_BYTES)

        value, measurement = await measure_async(allocate, iterations=3, track_memory=True)
        return value, measurement.peak_memory_bytes

    async_result, async_peak = anyio.run(scenario)

    assert len(result) == _ALLOCATION_BYTES
    assert len(async_result) == _ALLOCATION_BYTES
    assert synchronous.peak_memory_bytes is not None
    assert synchronous.peak_memory_bytes < _ALLOCATION_BYTES * 3 // 2
    assert async_peak is not None
    assert async_peak < _ALLOCATION_BYTES * 3 // 2
