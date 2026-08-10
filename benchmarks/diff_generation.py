"""Measure structured diff generation for representative file changes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.support import Measurement, environment, measure
from wisp.tui.tool_output import build_write_diff_presentation

type Workload = str

DEFAULT_WORKLOADS = ("localized", "replacement", "repeated", "long_line")


@dataclass(frozen=True)
class BenchmarkConfig:
    line_counts: tuple[int, ...] = (1_000, 3_500, 10_000)
    workloads: tuple[Workload, ...] = DEFAULT_WORKLOADS
    iterations: int = 3
    track_memory: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    workload: Workload
    line_count: int
    input_bytes: int
    guarded: bool
    retained_rows: int
    additions: int
    deletions: int
    generation: Measurement


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
    samples = tuple(
        _run_sample(workload, line_count, selected)
        for workload in selected.workloads
        for line_count in selected.line_counts
    )
    return BenchmarkReport(config=selected, environment=environment(), samples=samples)


def _run_sample(
    workload: Workload,
    line_count: int,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    before, after = _documents(workload, line_count)
    presentation, generation = measure(
        lambda: build_write_diff_presentation(
            before,
            {"path": "benchmark.txt", "content": after},
        ),
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    return BenchmarkSample(
        workload=workload,
        line_count=line_count,
        input_bytes=len(before.encode()) + len(after.encode()),
        guarded=presentation is None,
        retained_rows=len(presentation.rows) if presentation is not None else 0,
        additions=presentation.additions if presentation is not None else 0,
        deletions=presentation.deletions if presentation is not None else 0,
        generation=generation,
    )


def _documents(workload: Workload, line_count: int) -> tuple[str, str]:
    if workload == "long_line":
        before = "a" * line_count + "\n"
        return before, f"{before[:-2]}b\n"
    if workload == "repeated":
        lines = ["repeated value\n"] * line_count
    else:
        lines = [f"line {index}\n" for index in range(line_count)]
    before = "".join(lines)
    changed = list(lines)
    if workload == "localized" or workload == "repeated":
        changed[line_count // 2] = "changed value\n"
    elif workload == "replacement":
        changed = [f"replacement {index}\n" for index in range(line_count)]
    else:
        raise ValueError(f"unsupported diff workload: {workload}")
    return before, "".join(changed)


def _validate_config(config: BenchmarkConfig) -> None:
    if not config.line_counts or any(count < 1 for count in config.line_counts):
        raise ValueError("line_counts must contain positive counts")
    if not config.workloads or any(
        workload not in DEFAULT_WORKLOADS for workload in config.workloads
    ):
        raise ValueError(f"workloads must be selected from {DEFAULT_WORKLOADS}")
    if config.iterations < 1:
        raise ValueError("iterations must be positive")


def _parse_csv(value: str) -> tuple[str, ...]:
    values = tuple(item for item in value.split(",") if item)
    if not values:
        raise argparse.ArgumentTypeError("value must contain at least one item")
    return values


def _parse_counts(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(item) for item in _parse_csv(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("counts must be comma-separated integers") from exc


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--line-counts", type=_parse_counts, default=BenchmarkConfig.line_counts)
    parser.add_argument("--workloads", type=_parse_csv, default=BenchmarkConfig.workloads)
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            line_counts=parsed.line_counts,
            workloads=parsed.workloads,
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
