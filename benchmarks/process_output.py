"""Measure bounded managed-process output retention throughput."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.support import Measurement, environment, measure
from wisp.tools.process_manager import (
    DEFAULT_MAX_RETAINED_BYTES,
    DEFAULT_MAX_RETAINED_LINES,
    _PendingText,
)

_MEBIBYTE = 1024 * 1024
DEFAULT_WORKLOADS = (
    "ascii_lines",
    "unicode",
    "short_lines",
    "long_line",
    "mixed_newlines",
    "invalid_utf8",
)


@dataclass(frozen=True)
class BenchmarkConfig:
    sample_sizes: tuple[int, ...] = (_MEBIBYTE, 2 * _MEBIBYTE)
    workloads: tuple[str, ...] = DEFAULT_WORKLOADS
    chunk_bytes: int = 8_192
    max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES
    max_retained_lines: int = DEFAULT_MAX_RETAINED_LINES
    iterations: int = 1
    track_memory: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    workload: str
    input_bytes: int
    chunk_count: int
    measurement: Measurement
    max_chunk_ms: float
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

    if not config.workloads or any(
        workload not in DEFAULT_WORKLOADS for workload in config.workloads
    ):
        raise ValueError(f"workloads must be selected from {DEFAULT_WORKLOADS}")
    if config.iterations < 1:
        raise ValueError("iterations must be positive")
    samples = tuple(
        _run_sample(size, workload, config)
        for workload in config.workloads
        for size in config.sample_sizes
    )
    return BenchmarkReport(
        config=config,
        environment=environment(),
        samples=samples,
    )


def _run_sample(input_bytes: int, workload: str, config: BenchmarkConfig) -> BenchmarkSample:
    source = _workload_bytes(workload, input_bytes)
    observed_max_chunk_ns = 0

    def consume() -> tuple[int, int, int]:
        import time

        nonlocal observed_max_chunk_ns
        pending = _PendingText(config.max_retained_bytes, config.max_retained_lines)
        chunk_count = 0
        for offset in range(0, len(source), config.chunk_bytes):
            payload = source[offset : offset + config.chunk_bytes]
            started = time.perf_counter_ns()
            pending.append_bytes(payload)
            observed_max_chunk_ns = max(
                observed_max_chunk_ns,
                time.perf_counter_ns() - started,
            )
            chunk_count += 1
        pending.append_bytes(b"", final=True)
        _text, dropped_bytes, retained_bytes, _source_byte_lengths = pending.drain()
        return chunk_count, dropped_bytes, retained_bytes

    result, measurement = measure(
        consume,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    chunk_count, dropped_bytes, retained_bytes = result
    if dropped_bytes + retained_bytes != input_bytes:
        raise RuntimeError("process-output benchmark lost source-byte accounting")
    elapsed_seconds = measurement.wall_ms_per_iteration / 1_000
    return BenchmarkSample(
        workload=workload,
        input_bytes=input_bytes,
        chunk_count=chunk_count,
        measurement=measurement,
        max_chunk_ms=observed_max_chunk_ns / 1_000_000,
        throughput_bytes_per_second=input_bytes / elapsed_seconds
        if elapsed_seconds
        else float("inf"),
        retained_bytes=retained_bytes,
        dropped_bytes=dropped_bytes,
    )


def _workload_bytes(workload: str, size: int) -> bytes:
    patterns = {
        "ascii_lines": b"x" * 127 + b"\n",
        "unicode": "lambda λ emoji 🙂\n".encode(),
        "short_lines": b"x\n",
        "long_line": b"x",
        "mixed_newlines": b"alpha\r\nbeta\rgamma\n",
        "invalid_utf8": b"\xff\xfevalid\n",
    }
    pattern = patterns[workload]
    repetitions, remainder = divmod(size, len(pattern))
    return pattern * repetitions + pattern[:remainder]


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
    parser.add_argument(
        "--workloads",
        default=",".join(BenchmarkConfig.workloads),
        help="comma-separated workload names",
    )
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            sample_sizes=parsed.sizes,
            workloads=tuple(parsed.workloads.split(",")),
            chunk_bytes=parsed.chunk_bytes,
            iterations=parsed.iterations,
            track_memory=parsed.track_memory,
        )
    )
    print(report.to_json())
    if parsed.output is not None:
        parsed.output.write_text(f"{report.to_json()}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
