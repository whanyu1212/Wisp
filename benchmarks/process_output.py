"""Measure bounded managed-process output retention throughput."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from wisp.tools.process_manager import (
    DEFAULT_MAX_RETAINED_BYTES,
    DEFAULT_MAX_RETAINED_LINES,
    _PendingText,
)

_MEBIBYTE = 1024 * 1024


@dataclass(frozen=True)
class BenchmarkConfig:
    sample_sizes: tuple[int, ...] = (_MEBIBYTE, 2 * _MEBIBYTE)
    chunk_bytes: int = 8_192
    max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES
    max_retained_lines: int = DEFAULT_MAX_RETAINED_LINES


@dataclass(frozen=True)
class BenchmarkSample:
    input_bytes: int
    chunk_count: int
    elapsed_ms: float
    throughput_bytes_per_second: float
    retained_bytes: int
    dropped_bytes: int


@dataclass(frozen=True)
class BenchmarkReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Measure incremental retention across multiple production-sized output streams."""

    if config is None:
        config = BenchmarkConfig()
    if not config.sample_sizes or any(size <= 0 for size in config.sample_sizes):
        raise ValueError("sample_sizes must contain positive byte counts")
    if config.chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    if config.max_retained_bytes < 0 or config.max_retained_lines < 0:
        raise ValueError("retained output limits must be non-negative")

    samples = tuple(_run_sample(size, config) for size in config.sample_sizes)
    return BenchmarkReport(
        config=config,
        environment={
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        samples=samples,
    )


def _run_sample(input_bytes: int, config: BenchmarkConfig) -> BenchmarkSample:
    pending = _PendingText(config.max_retained_bytes, config.max_retained_lines)
    chunk = b"x" * (config.chunk_bytes - 1) + b"\n"
    remaining = input_bytes
    chunk_count = 0
    started = time.perf_counter_ns()
    while remaining:
        payload = chunk[:remaining]
        pending.append_bytes(payload)
        remaining -= len(payload)
        chunk_count += 1
    pending.append_bytes(b"", final=True)
    _text, dropped_bytes, retained_bytes, _source_byte_lengths = pending.drain()
    elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
    if dropped_bytes + retained_bytes != input_bytes:
        raise RuntimeError("process-output benchmark lost source-byte accounting")
    return BenchmarkSample(
        input_bytes=input_bytes,
        chunk_count=chunk_count,
        elapsed_ms=elapsed_seconds * 1_000,
        throughput_bytes_per_second=input_bytes / elapsed_seconds
        if elapsed_seconds
        else float("inf"),
        retained_bytes=retained_bytes,
        dropped_bytes=dropped_bytes,
    )


def _parse_sizes(value: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(size) for size in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sizes must be comma-separated integers") from exc
    if not sizes or any(size <= 0 for size in sizes):
        raise argparse.ArgumentTypeError("sizes must be positive integers")
    return sizes


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        type=_parse_sizes,
        default=BenchmarkConfig.sample_sizes,
        help="comma-separated input sizes in bytes",
    )
    parser.add_argument("--chunk-bytes", type=int, default=BenchmarkConfig.chunk_bytes)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(sample_sizes=parsed.sizes, chunk_bytes=parsed.chunk_bytes)
    )
    print(report.to_json())
    if parsed.output is not None:
        parsed.output.write_text(f"{report.to_json()}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
