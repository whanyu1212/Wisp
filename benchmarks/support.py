"""Shared measurement primitives for deterministic benchmark scenarios."""

from __future__ import annotations

import platform
import time
import tracemalloc
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Measurement:
    iterations: int
    wall_ms: float
    cpu_ms: float
    wall_ms_per_iteration: float
    cpu_ms_per_iteration: float
    peak_memory_bytes: int | None


def environment() -> dict[str, str]:
    """Return stable runtime metadata included in every benchmark report."""

    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


def measure[T](
    operation: Callable[[], T],
    *,
    iterations: int = 1,
    track_memory: bool = False,
) -> tuple[T, Measurement]:
    """Measure repeated synchronous production work after fixture setup."""

    if iterations < 1:
        raise ValueError("iterations must be positive")
    if track_memory:
        tracemalloc.start()
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    try:
        result = operation()
        for _ in range(iterations - 1):
            del result
            result = operation()
        cpu_ns = time.process_time_ns() - started_cpu
        wall_ns = time.perf_counter_ns() - started_wall
        peak_memory = tracemalloc.get_traced_memory()[1] if track_memory else None
    finally:
        if track_memory:
            tracemalloc.stop()
    return result, _measurement(iterations, wall_ns, cpu_ns, peak_memory)


async def measure_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    iterations: int = 1,
    track_memory: bool = False,
) -> tuple[T, Measurement]:
    """Measure repeated asynchronous production work after fixture setup."""

    if iterations < 1:
        raise ValueError("iterations must be positive")
    if track_memory:
        tracemalloc.start()
    started_wall = time.perf_counter_ns()
    started_cpu = time.process_time_ns()
    try:
        result = await operation()
        for _ in range(iterations - 1):
            del result
            result = await operation()
        cpu_ns = time.process_time_ns() - started_cpu
        wall_ns = time.perf_counter_ns() - started_wall
        peak_memory = tracemalloc.get_traced_memory()[1] if track_memory else None
    finally:
        if track_memory:
            tracemalloc.stop()
    return result, _measurement(iterations, wall_ns, cpu_ns, peak_memory)


def _measurement(
    iterations: int,
    wall_ns: int,
    cpu_ns: int,
    peak_memory_bytes: int | None,
) -> Measurement:
    wall_ms = wall_ns / 1_000_000
    cpu_ms = cpu_ns / 1_000_000
    return Measurement(
        iterations=iterations,
        wall_ms=wall_ms,
        cpu_ms=cpu_ms,
        wall_ms_per_iteration=wall_ms / iterations,
        cpu_ms_per_iteration=cpu_ms / iterations,
        peak_memory_bytes=peak_memory_bytes,
    )


__all__ = ["Measurement", "environment", "measure", "measure_async"]
