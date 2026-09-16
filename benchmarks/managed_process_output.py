"""Measure managed-process output from child startup through terminal polling."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

import anyio

from benchmarks.process_output import DEFAULT_WORKLOADS, _workload_bytes
from benchmarks.support import Measurement, environment, measure_async
from wisp.tools.process_manager import (
    DEFAULT_MAX_RETAINED_BYTES,
    DEFAULT_MAX_RETAINED_LINES,
    ProcessSupervisor,
)

_MEBIBYTE = 1024 * 1024


@dataclass(frozen=True)
class BenchmarkConfig:
    """Managed-process workloads and production retention limits."""

    sample_sizes: tuple[int, ...] = (_MEBIBYTE,)
    workloads: tuple[str, ...] = DEFAULT_WORKLOADS
    child_chunk_bytes: int = 8_192
    max_retained_bytes: int = DEFAULT_MAX_RETAINED_BYTES
    max_retained_lines: int = DEFAULT_MAX_RETAINED_LINES
    iterations: int = 1
    track_memory: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    """One end-to-end managed-process workload measurement."""

    workload: str
    input_bytes: int
    child_chunk_bytes: int
    poll_count: int
    max_poll_ms: float
    retained_source_bytes: int
    dropped_bytes: int
    throughput_bytes_per_second: float
    measurement: Measurement


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable end-to-end managed-process benchmark report."""

    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        """Serialize this report for local comparison and evidence capture."""

        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass(frozen=True)
class _RunResult:
    poll_count: int
    max_poll_ms: float
    retained_source_bytes: int
    dropped_bytes: int


async def run_benchmark_async(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Measure real managed children using deterministic temporary payload files.

    Args:
        config: Optional workload configuration. Payload generation and warmup happen
            outside each measured interval.

    Returns:
        A report containing lifecycle timing and exact source-byte accounting.

    Raises:
        ValueError: The configuration contains an invalid size, workload, or limit.
        RuntimeError: A managed child fails or loses source-byte accounting.
    """

    selected = config or BenchmarkConfig()
    _validate_config(selected)
    samples: list[BenchmarkSample] = []
    with TemporaryDirectory(prefix="wisp-managed-process-output-") as temporary:
        root = Path(temporary)
        for workload in selected.workloads:
            for sample_size in selected.sample_sizes:
                payload = root / f"{workload}-{sample_size}.bin"
                payload.write_bytes(_workload_bytes(workload, sample_size))
                samples.append(await _run_sample(payload, workload, sample_size, selected))
    return BenchmarkReport(config=selected, environment=environment(), samples=tuple(samples))


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Run the asynchronous benchmark from a synchronous caller."""

    return anyio.run(partial(run_benchmark_async, config))


def _validate_config(config: BenchmarkConfig) -> None:
    if not config.sample_sizes or any(size <= 0 for size in config.sample_sizes):
        raise ValueError("sample_sizes must contain positive byte counts")
    if not config.workloads or any(
        workload not in DEFAULT_WORKLOADS for workload in config.workloads
    ):
        raise ValueError(f"workloads must be selected from {DEFAULT_WORKLOADS}")
    if config.child_chunk_bytes <= 0:
        raise ValueError("child_chunk_bytes must be positive")
    if config.max_retained_bytes < 0 or config.max_retained_lines < 0:
        raise ValueError("retained output limits must be non-negative")
    if config.iterations < 1:
        raise ValueError("iterations must be positive")


async def _run_sample(
    payload: Path,
    workload: str,
    input_bytes: int,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    command = _child_command(payload, config.child_chunk_bytes)
    observed_max_poll_ms = 0.0

    async def invoke() -> _RunResult:
        nonlocal observed_max_poll_ms
        supervisor = ProcessSupervisor(max_processes=1)
        poll_count = 0
        retained_source_bytes = 0
        dropped_bytes = 0
        max_poll_ns = 0
        try:
            process_id = await supervisor.start(
                command,
                cwd=payload.parent,
                timeout=60,
                max_retained_bytes=config.max_retained_bytes,
                max_retained_lines=config.max_retained_lines,
            )
            while True:
                started = time.perf_counter_ns()
                update = await supervisor.poll(process_id, wait_seconds=0.1)
                max_poll_ns = max(max_poll_ns, time.perf_counter_ns() - started)
                poll_count += 1
                retained_source_bytes += update.stdout_retained_bytes or 0
                dropped_bytes += update.stdout_dropped_bytes
                if update.state == "running":
                    continue
                if update.state != "completed" or update.exit_code != 0:
                    raise RuntimeError(
                        f"managed child ended as {update.state} with {update.exit_code=}"
                    )
                break
        finally:
            await supervisor.aclose()

        if retained_source_bytes + dropped_bytes != input_bytes:
            raise RuntimeError("managed-process benchmark lost source-byte accounting")
        observed_max_poll_ms = max(observed_max_poll_ms, max_poll_ns / 1_000_000)
        return _RunResult(
            poll_count=poll_count,
            max_poll_ms=max_poll_ns / 1_000_000,
            retained_source_bytes=retained_source_bytes,
            dropped_bytes=dropped_bytes,
        )

    await invoke()
    observed_max_poll_ms = 0.0
    result, measurement = await measure_async(
        invoke,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    elapsed_seconds = measurement.wall_ms_per_iteration / 1_000
    return BenchmarkSample(
        workload=workload,
        input_bytes=input_bytes,
        child_chunk_bytes=config.child_chunk_bytes,
        poll_count=result.poll_count,
        max_poll_ms=observed_max_poll_ms,
        retained_source_bytes=result.retained_source_bytes,
        dropped_bytes=result.dropped_bytes,
        throughput_bytes_per_second=(
            input_bytes / elapsed_seconds if elapsed_seconds else float("inf")
        ),
        measurement=measurement,
    )


def _child_command(payload: Path, chunk_bytes: int) -> str:
    source = """\
import sys

source = open(sys.argv[1], "rb", buffering=0)
while chunk := source.read(int(sys.argv[2])):
    sys.stdout.buffer.write(chunk)
sys.stdout.buffer.flush()
"""
    return " ".join(
        (
            shlex.quote(sys.executable),
            "-c",
            shlex.quote(source),
            shlex.quote(str(payload)),
            str(chunk_bytes),
        )
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
        help="comma-separated payload sizes in bytes",
    )
    parser.add_argument(
        "--workloads",
        default=",".join(BenchmarkConfig.workloads),
        help="comma-separated workload names",
    )
    parser.add_argument(
        "--child-chunk-bytes",
        type=int,
        default=BenchmarkConfig.child_chunk_bytes,
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
            child_chunk_bytes=parsed.child_chunk_bytes,
            iterations=parsed.iterations,
            track_memory=parsed.track_memory,
        )
    )
    print(report.to_json())
    if parsed.output is not None:
        parsed.output.parent.mkdir(parents=True, exist_ok=True)
        parsed.output.write_text(f"{report.to_json()}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
