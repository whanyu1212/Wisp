"""Measure public built-in tool paths and executor normalization overhead."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import anyio

from benchmarks.support import Measurement, environment, measure_async
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import ToolExecutionEnded
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool, ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import ReadTool
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult
from wisp.tools.search.tools import FindTool, GrepTool, LsTool

_KIBIBYTE = 1024
_PAGE_LINES = 100
_MAX_RESULTS = 100
_LARGE_FILE_LINE = b"large-file benchmark line " + b"x" * 99 + b"\n"
_MIN_OUTPUT_BYTES = _PAGE_LINES * len(_LARGE_FILE_LINE)


@dataclass(frozen=True)
class BenchmarkConfig:
    """Workload sizes for deterministic temporary project fixtures."""

    file_counts: tuple[int, ...] = (1_000, 5_000)
    file_bytes: int = 4 * _KIBIBYTE
    max_output_bytes: int = 256 * _KIBIBYTE
    max_output_lines: int = 10_000
    iterations: int = 3
    include_executor: bool = True
    track_memory: bool = False
    grep_backend: Literal["auto", "python", "native"] = "auto"


@dataclass(frozen=True)
class BenchmarkSample:
    """One measured production path for one fixture scale."""

    scenario: str
    layer: str
    file_count: int
    input_bytes: int
    result_count: int | None
    output_bytes: int
    truncated: bool
    measurement: Measurement


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable built-in tool benchmark report."""

    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]

    def to_json(self) -> str:
        """Serialize this report for local comparison and evidence capture."""

        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass(frozen=True)
class _Fixture:
    root: Path
    tree: Path
    listing: Path
    large_file: Path
    file_count: int
    input_bytes: int
    large_file_lines: int
    listing_output_bytes: int


@dataclass(frozen=True)
class _Scenario:
    name: str
    tool: Tool
    arguments: ToolArguments
    input_bytes: int
    expected_count: int
    expected_truncated: bool


async def run_benchmark_async(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Build deterministic fixtures, then measure public tool and executor paths.

    Args:
        config: Optional workload configuration. Fixture construction and warmup happen
            outside every measured interval.

    Returns:
        A serializable report with one sample for each scenario, scale, and layer.

    Raises:
        ValueError: The benchmark configuration contains an invalid size or output budget.
        RuntimeError: A production tool returns a result that violates the fixture oracle.
    """

    selected = config or BenchmarkConfig()
    _validate_config(selected)
    samples: list[BenchmarkSample] = []
    with TemporaryDirectory(prefix="wisp-builtin-tools-") as temporary:
        temporary_root = Path(temporary)
        for file_count in selected.file_counts:
            fixture = _build_fixture(temporary_root / f"scale-{file_count}", file_count, selected)
            context = ToolContext(
                cwd=fixture.root,
                max_output_bytes=selected.max_output_bytes,
                max_output_lines=selected.max_output_lines,
                protected_paths=(),
            )
            scenarios = _scenarios(fixture, selected)
            registry = ToolRegistry()
            for tool in {scenario.tool.name: scenario.tool for scenario in scenarios}.values():
                registry.register(tool)
            executor = ConfiguredToolExecutor(
                registry=registry,
                context=context,
                policy=ToolPolicy.allow_all_tools(),
                approval_policy=ToolApprovalPolicy.approve_all(),
            )

            try:
                for scenario in scenarios:
                    samples.append(await _measure_direct(scenario, fixture, context, selected))
                    if selected.include_executor:
                        samples.append(
                            await _measure_executor(scenario, fixture, executor, selected)
                        )
            finally:
                for tool in registry.all():
                    close = getattr(tool, "aclose", None)
                    if close is not None:
                        await close()

    return BenchmarkReport(
        config=selected,
        environment=environment(),
        samples=tuple(samples),
    )


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Run the asynchronous benchmark from a synchronous caller."""

    return anyio.run(partial(run_benchmark_async, config))


def _validate_config(config: BenchmarkConfig) -> None:
    if not config.file_counts or any(count <= 0 for count in config.file_counts):
        raise ValueError("file_counts must contain positive counts")
    if config.file_bytes < 64:
        raise ValueError("file_bytes must be at least 64")
    if config.max_output_bytes < _MIN_OUTPUT_BYTES:
        raise ValueError(f"max_output_bytes must be at least {_MIN_OUTPUT_BYTES}")
    if config.max_output_lines < _PAGE_LINES:
        raise ValueError(f"max_output_lines must be at least {_PAGE_LINES}")
    if config.iterations < 1:
        raise ValueError("iterations must be positive")


def _build_fixture(root: Path, file_count: int, config: BenchmarkConfig) -> _Fixture:
    tree = root / "tree"
    listing = root / "listing"
    tree.mkdir(parents=True)
    listing.mkdir()
    group_count = min(32, file_count)
    groups = tuple(tree / f"group-{index:02d}" for index in range(group_count))
    for group in groups:
        group.mkdir()

    listing_output_bytes = 0
    for index in range(file_count):
        listing_name = f"entry-{index:06d}.txt"
        (listing / listing_name).touch()
        listing_output_bytes += len(listing_name.encode("utf-8"))
        content = _file_content(index, config.file_bytes)
        (groups[index % group_count] / f"file-{index:06d}.txt").write_bytes(content)
    listing_output_bytes += file_count - 1

    input_bytes = file_count * config.file_bytes
    large_file_lines = max(_PAGE_LINES + 1, input_bytes // len(_LARGE_FILE_LINE))
    large_file = root / "large.txt"
    large_file.write_bytes(_LARGE_FILE_LINE * large_file_lines)
    return _Fixture(
        root=root,
        tree=tree,
        listing=listing,
        large_file=large_file,
        file_count=file_count,
        input_bytes=input_bytes,
        large_file_lines=large_file_lines,
        listing_output_bytes=listing_output_bytes,
    )


def _file_content(index: int, size: int) -> bytes:
    marker = f"needle-{index:06d}\n".encode()
    filler = b"ordinary benchmark text without the target token\n"
    remaining = size - len(marker)
    repetitions, remainder = divmod(remaining, len(filler))
    return marker + filler * repetitions + filler[:remainder]


def _scenarios(fixture: _Fixture, config: BenchmarkConfig) -> tuple[_Scenario, ...]:
    read_tool = ReadTool()
    grep_tool = GrepTool(_scanner_backend=config.grep_backend)
    find_tool = FindTool()
    ls_tool = LsTool()
    return (
        _Scenario(
            name="read_first_page",
            tool=read_tool,
            arguments={"path": "large.txt", "offset": 1, "limit": _PAGE_LINES},
            input_bytes=fixture.large_file.stat().st_size,
            expected_count=_PAGE_LINES,
            expected_truncated=False,
        ),
        _Scenario(
            name="read_tail_page",
            tool=read_tool,
            arguments={
                "path": "large.txt",
                "offset": fixture.large_file_lines - _PAGE_LINES + 1,
                "limit": _PAGE_LINES,
            },
            input_bytes=fixture.large_file.stat().st_size,
            expected_count=_PAGE_LINES,
            expected_truncated=False,
        ),
        _Scenario(
            name="ls_sorted_prefix",
            tool=ls_tool,
            arguments={"path": "listing"},
            input_bytes=0,
            expected_count=fixture.file_count,
            expected_truncated=(
                fixture.file_count > min(config.max_output_lines, config.max_output_bytes)
                or fixture.listing_output_bytes > config.max_output_bytes
            ),
        ),
        _Scenario(
            name="find_sorted_prefix",
            tool=find_tool,
            arguments={"path": "tree", "pattern": "*.txt", "max_results": _MAX_RESULTS},
            input_bytes=0,
            expected_count=min(fixture.file_count, _MAX_RESULTS + 1),
            expected_truncated=fixture.file_count > _MAX_RESULTS,
        ),
        _Scenario(
            name="grep_literal_miss",
            tool=grep_tool,
            arguments={"path": "tree", "pattern": "absent-token", "literal": True},
            input_bytes=fixture.input_bytes,
            expected_count=0,
            expected_truncated=False,
        ),
        _Scenario(
            name="grep_regex_capped",
            tool=grep_tool,
            arguments={
                "path": "tree",
                "pattern": r"needle-\d+",
                "max_results": _MAX_RESULTS,
            },
            input_bytes=fixture.input_bytes,
            expected_count=min(fixture.file_count, _MAX_RESULTS),
            expected_truncated=fixture.file_count > _MAX_RESULTS,
        ),
        _Scenario(
            name="grep_literal_capped",
            tool=grep_tool,
            arguments={
                "path": "tree",
                "pattern": "needle-",
                "literal": True,
                "max_results": _MAX_RESULTS,
            },
            input_bytes=fixture.input_bytes,
            expected_count=min(fixture.file_count, _MAX_RESULTS),
            expected_truncated=fixture.file_count > _MAX_RESULTS,
        ),
    )


async def _measure_direct(
    scenario: _Scenario,
    fixture: _Fixture,
    context: ToolContext,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    async def invoke() -> ToolResult:
        result = await scenario.tool.run(scenario.arguments, context)
        _validate_direct_result(scenario, result)
        return result

    await invoke()
    result, measurement = await measure_async(
        invoke,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    result_count = _result_count(scenario.name, result.data)
    return BenchmarkSample(
        scenario=scenario.name,
        layer="tool.run",
        file_count=fixture.file_count,
        input_bytes=scenario.input_bytes,
        result_count=result_count,
        output_bytes=len(result.text.encode("utf-8")),
        truncated=result.truncated,
        measurement=measurement,
    )


async def _measure_executor(
    scenario: _Scenario,
    fixture: _Fixture,
    executor: ConfiguredToolExecutor,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    call_index = 0

    async def invoke() -> ToolExecutionEnded:
        nonlocal call_index
        call_index += 1
        call = ToolCall(
            call_id=f"benchmark-{scenario.name}-{call_index}",
            name=scenario.tool.name,
            arguments=scenario.arguments,
        )
        events = [event async for event in executor.execute(call)]
        result = next(event for event in events if isinstance(event, ToolExecutionEnded))
        if result.is_error:
            raise RuntimeError(f"executor benchmark failed for {scenario.name}: {result.output}")
        if result.truncated != scenario.expected_truncated:
            raise RuntimeError(f"unexpected executor truncation for {scenario.name}")
        return result

    await invoke()
    result, measurement = await measure_async(
        invoke,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    return BenchmarkSample(
        scenario=scenario.name,
        layer="executor",
        file_count=fixture.file_count,
        input_bytes=scenario.input_bytes,
        result_count=None,
        output_bytes=len(result.output.encode("utf-8")),
        truncated=result.truncated,
        measurement=measurement,
    )


def _result_count(scenario: str, data: Mapping[str, object]) -> int:
    if scenario.startswith("read_"):
        key = "selected_count"
    elif scenario.startswith("ls_"):
        key = "entry_count"
    else:
        key = "count"
    value = data.get(key)
    if not isinstance(value, int):
        raise RuntimeError(f"benchmark scenario {scenario} did not return integer {key}")
    return value


def _validate_direct_result(scenario: _Scenario, result: ToolResult) -> None:
    count = _result_count(scenario.name, result.data)
    if count != scenario.expected_count:
        raise RuntimeError(
            f"unexpected result count for {scenario.name}: {count} != {scenario.expected_count}"
        )
    if result.truncated != scenario.expected_truncated:
        raise RuntimeError(f"unexpected truncation for {scenario.name}")


def _parse_counts(value: str) -> tuple[int, ...]:
    try:
        counts = tuple(int(count) for count in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("file counts must be comma-separated integers") from exc
    if not counts or any(count <= 0 for count in counts):
        raise argparse.ArgumentTypeError("file counts must be positive integers")
    return counts


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file-counts",
        type=_parse_counts,
        default=BenchmarkConfig.file_counts,
        help="comma-separated project file counts",
    )
    parser.add_argument("--file-bytes", type=int, default=BenchmarkConfig.file_bytes)
    parser.add_argument("--max-output-bytes", type=int, default=BenchmarkConfig.max_output_bytes)
    parser.add_argument("--max-output-lines", type=int, default=BenchmarkConfig.max_output_lines)
    parser.add_argument("--iterations", type=int, default=BenchmarkConfig.iterations)
    parser.add_argument("--tool-only", action="store_true")
    parser.add_argument("--track-memory", action="store_true")
    parser.add_argument(
        "--grep-backend",
        choices=("auto", "python", "native"),
        default="auto",
        help="grep scanner used for controlled Python/native comparisons",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            file_counts=parsed.file_counts,
            file_bytes=parsed.file_bytes,
            max_output_bytes=parsed.max_output_bytes,
            max_output_lines=parsed.max_output_lines,
            iterations=parsed.iterations,
            include_executor=not parsed.tool_only,
            track_memory=parsed.track_memory,
            grep_backend=parsed.grep_backend,
        )
    )
    print(report.to_json())
    if parsed.output is not None:
        parsed.output.parent.mkdir(parents=True, exist_ok=True)
        parsed.output.write_text(f"{report.to_json()}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
