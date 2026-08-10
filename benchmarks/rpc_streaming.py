"""Measure streamed-event construction, serialization, and validation."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.support import Measurement, environment, measure
from wisp.events import MessageDelta, wisp_event_from_json


@dataclass(frozen=True)
class BenchmarkConfig:
    response_bytes: int = 256 * 1024
    chunk_sizes: tuple[int, ...] = (1, 4, 32, 256, 1_024)
    iterations: int = 3
    track_memory: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    chunk_bytes: int
    event_count: int
    encoded_bytes: int
    construction: Measurement
    serialization: Measurement
    parsing: Measurement
    complete_round_trip: Measurement
    round_trips_per_second: float


@dataclass(frozen=True)
class BenchmarkReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    selected = config or BenchmarkConfig()
    _validate_config(selected)
    samples = tuple(_run_sample(size, selected) for size in selected.chunk_sizes)
    return BenchmarkReport(config=selected, environment=environment(), samples=samples)


def _run_sample(chunk_bytes: int, config: BenchmarkConfig) -> BenchmarkSample:
    deltas = _deltas(config.response_bytes, chunk_bytes)
    events, construction = measure(
        lambda: tuple(MessageDelta(turn=1, delta=delta) for delta in deltas),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    lines, serialization = measure(
        lambda: tuple(event.model_dump_json() for event in events),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    _parsed, parsing = measure(
        lambda: tuple(wisp_event_from_json(line) for line in lines),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )

    def round_trip() -> None:
        for delta in deltas:
            line = MessageDelta(turn=1, delta=delta).model_dump_json()
            wisp_event_from_json(line)

    _result, complete = measure(
        round_trip,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    total_round_trips = len(deltas) * config.iterations
    seconds = complete.wall_ms / 1_000
    return BenchmarkSample(
        chunk_bytes=chunk_bytes,
        event_count=len(events),
        encoded_bytes=sum(len(line.encode("utf-8")) for line in lines),
        construction=construction,
        serialization=serialization,
        parsing=parsing,
        complete_round_trip=complete,
        round_trips_per_second=total_round_trips / seconds if seconds else float("inf"),
    )


def _deltas(response_bytes: int, chunk_bytes: int) -> tuple[str, ...]:
    content = "x" * response_bytes
    return tuple(
        content[index : index + chunk_bytes] for index in range(0, response_bytes, chunk_bytes)
    )


def _validate_config(config: BenchmarkConfig) -> None:
    if config.response_bytes < 1 or config.iterations < 1:
        raise ValueError("response_bytes and iterations must be positive")
    if not config.chunk_sizes or any(size < 1 for size in config.chunk_sizes):
        raise ValueError("chunk_sizes must contain positive sizes")


def _parse_sizes(value: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sizes must be comma-separated integers") from exc
    if not sizes or any(size < 1 for size in sizes):
        raise argparse.ArgumentTypeError("sizes must be positive integers")
    return sizes


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response-bytes", type=int, default=BenchmarkConfig.response_bytes)
    parser.add_argument("--chunk-sizes", type=_parse_sizes, default=BenchmarkConfig.chunk_sizes)
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            response_bytes=parsed.response_bytes,
            chunk_sizes=parsed.chunk_sizes,
            iterations=parsed.iterations,
            track_memory=parsed.track_memory,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
