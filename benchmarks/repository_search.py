"""Measure production find and grep against an existing project tree."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Literal

import anyio

from benchmarks.support import Measurement, environment, measure_async
from wisp.tools.context import ToolContext
from wisp.tools.files.secure_fs import secure_tool_path
from wisp.tools.result import ToolResult
from wisp.tools.search.tools import FindTool, GrepTool

_DISCOVERY_LIMIT = 10_000
_LITERAL_MISS = "\x00"  # The search tools skip text files containing NUL.


@dataclass(frozen=True)
class BenchmarkConfig:
    """Selected project and bounded search workload."""

    root: Path = Path(".")
    cwd: Path = field(default_factory=Path.cwd)
    iterations: int = 3
    max_results: int = 100
    common_token: str = "def "
    max_output_bytes: int = 50_000
    max_output_lines: int = 2_000
    grep_backend: Literal["auto", "python", "native"] = "auto"


@dataclass(frozen=True)
class BenchmarkSample:
    """One public tool invocation measured after warmup."""

    scenario: str
    result_count: int
    truncated: bool
    output_bytes: int
    measurement: Measurement


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable measurements and selected input scale."""

    config: BenchmarkConfig
    environment: dict[str, str]
    python_files_observed: int
    python_files_capped: bool
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        """Serialize a report without search result text or source contents."""

        return json.dumps(asdict(self), indent=2, sort_keys=True, default=str)


async def run_benchmark_async(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Measure the public direct tool paths on a selected project.

    Args:
        config: Project root, output bounds, and iteration count. Warmup and file
            discovery happen outside the measured intervals.

    Returns:
        A report for sorted Python discovery, a literal miss, and capped grep.

    Raises:
        ValueError: Configuration values are invalid.
        ToolError: The root violates normal tool path policy or cannot be opened.
        RuntimeError: The guaranteed-miss workload produces a match.
    """

    selected = config or BenchmarkConfig()
    _validate_config(selected)
    context = ToolContext(
        cwd=selected.cwd.resolve(strict=True),
        max_output_bytes=selected.max_output_bytes,
        max_output_lines=selected.max_output_lines,
    )
    root = secure_tool_path(str(selected.root), context)
    find = FindTool()
    grep = GrepTool(_scanner_backend=selected.grep_backend)
    try:
        discovered = await find.run(
            {"path": str(root.path), "pattern": "*.py", "max_results": _DISCOVERY_LIMIT},
            context,
        )
        python_files_observed = _result_count(discovered)
        scenarios = (
            (
                "find_sorted_python_prefix",
                find,
                {"path": str(root.path), "pattern": "*.py", "max_results": selected.max_results},
            ),
            (
                "grep_literal_miss",
                grep,
                {"path": str(root.path), "pattern": _LITERAL_MISS, "literal": True, "glob": "*.py"},
            ),
            (
                "grep_literal_capped",
                grep,
                {
                    "path": str(root.path),
                    "pattern": selected.common_token,
                    "literal": True,
                    "glob": "*.py",
                    "max_results": selected.max_results,
                },
            ),
        )
        samples: list[BenchmarkSample] = []
        for name, tool, arguments in scenarios:
            warmup = await tool.run(arguments, context)
            if name == "grep_literal_miss" and _result_count(warmup) != 0:
                raise RuntimeError("literal-miss workload unexpectedly found a match")
            result, measurement = await measure_async(
                partial(tool.run, arguments, context), iterations=selected.iterations
            )
            if (_result_count(result), result.truncated) != (
                _result_count(warmup),
                warmup.truncated,
            ):
                raise RuntimeError(f"search results changed during {name}")
            samples.append(
                BenchmarkSample(
                    scenario=name,
                    result_count=_result_count(result),
                    truncated=result.truncated,
                    output_bytes=len(result.text.encode("utf-8")),
                    measurement=measurement,
                )
            )
        return BenchmarkReport(
            config=selected,
            environment=environment(),
            python_files_observed=python_files_observed,
            python_files_capped=python_files_observed > _DISCOVERY_LIMIT,
            samples=tuple(samples),
        )
    finally:
        await find.aclose()
        await grep.aclose()


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Run the asynchronous benchmark from a synchronous caller."""

    return anyio.run(partial(run_benchmark_async, config))


def _validate_config(config: BenchmarkConfig) -> None:
    if config.iterations < 1:
        raise ValueError("iterations must be positive")
    if config.max_results < 1:
        raise ValueError("max_results must be positive")
    if not config.common_token:
        raise ValueError("common_token must not be empty")
    if config.max_output_bytes < 1 or config.max_output_lines < 1:
        raise ValueError("output bounds must be positive")


def _result_count(result: ToolResult) -> int:
    count = result.data.get("count")
    if not isinstance(count, int):
        raise RuntimeError("search result did not contain an integer count")
    return count


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the benchmark and optionally write its JSON report."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--common-token", default="def ")
    parser.add_argument(
        "--grep-backend",
        choices=("auto", "python", "native"),
        default="auto",
        help="grep scanner used for controlled Python/native comparisons",
    )
    parser.add_argument("--output", type=Path)
    parsed = parser.parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            root=parsed.root,
            iterations=parsed.iterations,
            max_results=parsed.max_results,
            common_token=parsed.common_token,
            grep_backend=parsed.grep_backend,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.parent.mkdir(parents=True, exist_ok=True)
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
