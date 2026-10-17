"""Measure public built-in tool paths, executor overhead, batching, and external ceilings."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import anyio

from benchmarks.support import Measurement, environment, measure_async
from wisp.agent.loop.tool_execution import ToolBatch
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.events import ToolExecutionEnded, ToolResultReady
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import Tool, ToolArguments, ToolExecutionMetadata
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import ReadTool
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult
from wisp.tools.search.tools import FindTool, GrepTool, LsTool
from wisp.tools.shell.supervisor import ProcessSupervisor
from wisp.tools.shell.tool import BashTool

_KIBIBYTE = 1024
_MEBIBYTE = 1024 * _KIBIBYTE
_PAGE_LINES = 100
_MAX_RESULTS = 100
_LARGE_FILE_LINE = b"large-file benchmark line " + b"x" * 99 + b"\n"
_MIN_OUTPUT_BYTES = _PAGE_LINES * len(_LARGE_FILE_LINE)
# Built at runtime so a repository run never finds this module's own source as a match.
_ABSENT_TOKEN = "wisp-bench" + "mark-absent-token"
_ABSENT_REGEX = "wisp-bench" + r"mark-absent-\d+-token"
# The same read-only tools the runtime marks parallel safe (`wisp.extensions.builtin`).
_PARALLEL_SAFE = ToolExecutionMetadata(parallel_safe=True)
_PARALLEL_SAFE_TOOL_NAMES = frozenset({"find", "grep", "ls", "read"})
# Upper bound on files listed when sizing a repository run.
_DISCOVERY_MAX_FILES = 1_000_000
_BASH_COMMANDS = (
    ("bash_true", "true"),
    ("bash_short_lines_1mib", "seq 1 160000"),
    ("bash_long_line_1mib", f"head -c {_MEBIBYTE} /dev/zero | tr '\\000' x"),
)


@dataclass(frozen=True)
class BenchmarkConfig:
    """Workload sizes and optional measurement layers.

    Attributes:
        root (Path | None): Existing directory to search instead of synthetic fixtures.
            It must be inside the current working directory. Repository runs have no
            result oracle beyond "misses find nothing", and use the default protected paths.
        batch_size (int): When positive, also run each scenario as one model tool batch
            of this many identical calls through the production prepared scheduler.
        external_ceiling (bool): Also time `rg` doing the same full-tree scan, as an
            all-native upper bound. Skipped when `rg` is not installed.
        include_bash (bool): Also time end-to-end `bash` tool calls against a bare
            `/bin/sh -c` subprocess running the same command.
    """

    file_counts: tuple[int, ...] = (1_000, 5_000)
    file_bytes: int = 4 * _KIBIBYTE
    max_output_bytes: int = 256 * _KIBIBYTE
    max_output_lines: int = 10_000
    iterations: int = 3
    include_executor: bool = True
    track_memory: bool = False
    grep_backend: Literal["auto", "python", "native"] = "auto"
    root: Path | None = None
    batch_size: int = 0
    external_ceiling: bool = False
    include_bash: bool = False


@dataclass(frozen=True)
class BenchmarkSample:
    """One measured production path for one fixture scale.

    `layer` names the path: `tool.run`, `executor`, `batch` (batch_size concurrent calls
    per iteration), `batch.serial` (the same batch run one call at a time), `ripgrep`
    (external ceiling), or `sh` (bare subprocess baseline).
    """

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

        return json.dumps(asdict(self), indent=2, sort_keys=True, default=str)


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
    """One tool call to measure.

    `expected_count` and `expected_truncated` are the fixture oracle; None skips that
    check for repository runs. `ripgrep` holds the `rg` arguments for an equivalent
    full-tree scan, when one exists.
    """

    name: str
    tool: Tool
    arguments: ToolArguments
    input_bytes: int
    expected_count: int | None
    expected_truncated: bool | None
    ripgrep: tuple[str, ...] | None = None


@dataclass(frozen=True)
class _Workload:
    """A directory to run scenarios in, with the scale recorded on its samples."""

    cwd: Path
    file_count: int
    context: ToolContext
    scenarios: tuple[_Scenario, ...]


async def run_benchmark_async(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Build deterministic fixtures, then measure public tool and executor paths.

    Args:
        config (BenchmarkConfig | None, optional): Workload configuration. Fixture
            construction and warmup happen outside every measured interval.

    Returns:
        BenchmarkReport: One sample for each scenario, scale, and enabled layer, followed
            by the bash samples when enabled.

    Raises:
        ValueError: The configuration contains an invalid size, budget, or root.
        RuntimeError: A production tool returns a result that violates the fixture oracle.
    """

    selected = config or BenchmarkConfig()
    _validate_config(selected)
    samples: list[BenchmarkSample] = []
    with TemporaryDirectory(prefix="wisp-builtin-tools-") as temporary:
        temporary_root = Path(temporary)
        if selected.root is not None:
            workloads = [await _repository_workload(selected.root, selected)]
        else:
            workloads = [
                _synthetic_workload(temporary_root / f"scale-{count}", count, selected)
                for count in selected.file_counts
            ]
        for workload in workloads:
            samples.extend(await _measure_workload(workload, selected))
        if selected.include_bash:
            samples.extend(await _measure_bash(temporary_root, selected))

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
    if config.batch_size < 0:
        raise ValueError("batch_size must not be negative")
    if config.root is not None:
        root = config.root.resolve()
        if not root.is_dir():
            raise ValueError(f"root is not a directory: {config.root}")
        if not root.is_relative_to(Path.cwd().resolve()):
            raise ValueError("root must be inside the current working directory")


async def _measure_workload(workload: _Workload, config: BenchmarkConfig) -> list[BenchmarkSample]:
    """Measure every enabled layer for each scenario of one workload, in scenario order."""

    tools = {scenario.tool.name: scenario.tool for scenario in workload.scenarios}.values()
    registry = ToolRegistry()
    # Same tool instances without parallel-safe metadata: the prepared scheduler then runs
    # a batch one call at a time, giving a measured serial baseline through identical code.
    serial_registry = ToolRegistry()
    for tool in tools:
        registry.register(
            tool,
            execution=_PARALLEL_SAFE if tool.name in _PARALLEL_SAFE_TOOL_NAMES else None,
        )
        serial_registry.register(tool)
    executor = _approving_executor(registry, workload.context)
    serial_executor = _approving_executor(serial_registry, workload.context)
    ripgrep = shutil.which("rg") if config.external_ceiling else None
    samples: list[BenchmarkSample] = []
    try:
        for scenario in workload.scenarios:
            samples.append(await _measure_direct(scenario, workload, config))
            if config.include_executor:
                samples.append(await _measure_executor(scenario, workload, executor, config))
            if config.batch_size:
                samples.append(
                    await _measure_batch(scenario, workload, executor, config, layer="batch")
                )
                samples.append(
                    await _measure_batch(
                        scenario, workload, serial_executor, config, layer="batch.serial"
                    )
                )
            if ripgrep is not None and scenario.ripgrep is not None:
                samples.append(await _measure_ripgrep(ripgrep, scenario, workload, config))
    finally:
        for tool in registry.all():
            close = getattr(tool, "aclose", None)
            if close is not None:
                await close()
    return samples


def _approving_executor(registry: ToolRegistry, context: ToolContext) -> ConfiguredToolExecutor:
    return ConfiguredToolExecutor(
        registry=registry,
        context=context,
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
    )


def _synthetic_workload(root: Path, file_count: int, config: BenchmarkConfig) -> _Workload:
    fixture = _build_fixture(root, file_count, config)
    return _Workload(
        cwd=fixture.root,
        file_count=fixture.file_count,
        context=ToolContext(
            cwd=fixture.root,
            max_output_bytes=config.max_output_bytes,
            max_output_lines=config.max_output_lines,
            protected_paths=(),
        ),
        scenarios=_scenarios(fixture, config),
    )


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
            ripgrep=("--files", "--glob", "*.txt", "tree"),
        ),
        _Scenario(
            name="grep_literal_miss",
            tool=grep_tool,
            arguments={"path": "tree", "pattern": "absent-token", "literal": True},
            input_bytes=fixture.input_bytes,
            expected_count=0,
            expected_truncated=False,
            ripgrep=("--fixed-strings", "absent-token", "tree"),
        ),
        _Scenario(
            name="grep_regex_miss",
            tool=grep_tool,
            arguments={"path": "tree", "pattern": r"absent-\d+-token"},
            input_bytes=fixture.input_bytes,
            expected_count=0,
            expected_truncated=False,
            ripgrep=(r"absent-\d+-token", "tree"),
        ),
        _Scenario(
            name="grep_ignore_case_miss",
            tool=grep_tool,
            arguments={"path": "tree", "pattern": "absent-token", "ignore_case": True},
            input_bytes=fixture.input_bytes,
            expected_count=0,
            expected_truncated=False,
            ripgrep=("--ignore-case", "absent-token", "tree"),
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


async def _repository_workload(root: Path, config: BenchmarkConfig) -> _Workload:
    """Search an existing tree, such as `src/`, with the default protected paths.

    The recorded file count and input bytes cover only the files the search tools
    scan, and the read scenario is omitted when no scanned file is valid UTF-8.
    """

    cwd = root.resolve()
    context = ToolContext(
        cwd=cwd,
        max_output_bytes=config.max_output_bytes,
        max_output_lines=config.max_output_lines,
    )
    files = await _searchable_files(context)
    grep_tool = GrepTool(_scanner_backend=config.grep_backend)
    input_bytes = sum(path.stat().st_size for path in files)
    read_target = _largest_utf8_file(files)
    read_scenarios = (
        ()
        if read_target is None
        else (
            _Scenario(
                name="read_first_page",
                tool=ReadTool(),
                arguments={
                    "path": str(read_target.relative_to(cwd)),
                    "offset": 1,
                    "limit": _PAGE_LINES,
                },
                input_bytes=read_target.stat().st_size,
                expected_count=None,
                expected_truncated=None,
            ),
        )
    )
    scenarios = (
        *read_scenarios,
        _Scenario(
            name="find_sorted_prefix",
            tool=FindTool(),
            arguments={"path": ".", "pattern": "*.py", "max_results": _MAX_RESULTS},
            input_bytes=0,
            expected_count=None,
            expected_truncated=None,
            ripgrep=("--files", "--glob", "*.py"),
        ),
        _Scenario(
            name="grep_literal_miss",
            tool=grep_tool,
            arguments={"path": ".", "pattern": _ABSENT_TOKEN, "literal": True},
            input_bytes=input_bytes,
            expected_count=0,
            expected_truncated=False,
            ripgrep=("--fixed-strings", _ABSENT_TOKEN),
        ),
        _Scenario(
            name="grep_regex_miss",
            tool=grep_tool,
            arguments={"path": ".", "pattern": _ABSENT_REGEX},
            input_bytes=input_bytes,
            expected_count=0,
            expected_truncated=False,
            ripgrep=(_ABSENT_REGEX,),
        ),
        _Scenario(
            name="grep_ignore_case_miss",
            tool=grep_tool,
            arguments={"path": ".", "pattern": _ABSENT_TOKEN, "ignore_case": True},
            input_bytes=input_bytes,
            expected_count=0,
            expected_truncated=False,
            ripgrep=("--ignore-case", _ABSENT_TOKEN),
        ),
        _Scenario(
            name="grep_regex_capped",
            tool=grep_tool,
            arguments={"path": ".", "pattern": r"def \w+", "max_results": _MAX_RESULTS},
            input_bytes=input_bytes,
            expected_count=None,
            expected_truncated=None,
        ),
        _Scenario(
            name="grep_literal_capped",
            tool=grep_tool,
            arguments={
                "path": ".",
                "pattern": "def ",
                "literal": True,
                "max_results": _MAX_RESULTS,
            },
            input_bytes=input_bytes,
            expected_count=None,
            expected_truncated=None,
        ),
    )
    return _Workload(cwd=cwd, file_count=len(files), context=context, scenarios=scenarios)


async def _searchable_files(context: ToolContext) -> list[Path]:
    """List the files `find` and `grep` scan below the context root.

    Uses the production find tool, so ignore files, protected paths, and no-follow
    rules exclude exactly what the measured searches skip.
    """

    result = await FindTool().run(
        {"path": ".", "pattern": "*", "max_results": _DISCOVERY_MAX_FILES}, context
    )
    files = result.data.get("files")
    count = result.data.get("count")
    if not isinstance(files, list) or not isinstance(count, int):
        raise RuntimeError("find did not report its matched files")
    if count > _DISCOVERY_MAX_FILES:
        raise ValueError(f"root has more than {_DISCOVERY_MAX_FILES} searchable files")
    return [context.cwd / str(path) for path in files]


def _largest_utf8_file(files: Sequence[Path]) -> Path | None:
    """Pick the largest Python source, else the largest file, that decodes as UTF-8."""

    by_preference = sorted(
        files, key=lambda path: (path.suffix == ".py", path.stat().st_size), reverse=True
    )
    for path in by_preference:
        try:
            path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        return path
    return None


async def _measure_direct(
    scenario: _Scenario,
    workload: _Workload,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    async def invoke() -> ToolResult:
        result = await scenario.tool.run(scenario.arguments, workload.context)
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
        file_count=workload.file_count,
        input_bytes=scenario.input_bytes,
        result_count=result_count,
        output_bytes=len(result.text.encode("utf-8")),
        truncated=result.truncated,
        measurement=measurement,
    )


async def _measure_executor(
    scenario: _Scenario,
    workload: _Workload,
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
        if scenario.expected_truncated is not None and (
            result.truncated != scenario.expected_truncated
        ):
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
        file_count=workload.file_count,
        input_bytes=scenario.input_bytes,
        result_count=None,
        output_bytes=len(result.output.encode("utf-8")),
        truncated=result.truncated,
        measurement=measurement,
    )


async def _measure_batch(
    scenario: _Scenario,
    workload: _Workload,
    executor: ConfiguredToolExecutor,
    config: BenchmarkConfig,
    *,
    layer: str,
) -> BenchmarkSample:
    """Time one model tool batch of `batch_size` identical calls per iteration.

    The batch goes through the production `ToolBatch` and prepared scheduler. With the
    parallel-safe executor (`batch`) calls overlap in worker threads, where CPU-bound
    Python scanners still contend for the GIL; with the serial executor (`batch.serial`)
    the same scheduler runs them one at a time.
    """

    batch_index = 0

    async def invoke() -> tuple[ToolResultReady, ...]:
        nonlocal batch_index
        batch_index += 1
        calls = tuple(
            ToolCall(
                call_id=f"benchmark-{scenario.name}-batch-{batch_index}-{index}",
                name=scenario.tool.name,
                arguments=scenario.arguments,
            )
            for index in range(config.batch_size)
        )
        results: list[ToolResultReady] = []
        batch = ToolBatch(
            tool_executor=executor,
            tool_calls=calls,
            truncated=False,
            is_cancelled=lambda: False,
            on_result=results.append,
        )
        async for _event in batch.events():
            pass
        errors = [result.output for result in results if result.is_error]
        if len(results) != len(calls) or errors:
            raise RuntimeError(f"batch benchmark failed for {scenario.name}: {errors}")
        return tuple(results)

    await invoke()
    results, measurement = await measure_async(
        invoke,
        iterations=config.iterations,
        track_memory=config.track_memory,
    )
    return BenchmarkSample(
        scenario=scenario.name,
        layer=layer,
        file_count=workload.file_count,
        input_bytes=scenario.input_bytes * config.batch_size,
        result_count=len(results),
        output_bytes=sum(len(result.output.encode("utf-8")) for result in results),
        truncated=any(result.truncated for result in results),
        measurement=measurement,
    )


async def _measure_ripgrep(
    executable: str,
    scenario: _Scenario,
    workload: _Workload,
    config: BenchmarkConfig,
) -> BenchmarkSample:
    """Time `rg` scanning the same tree, including its process start, as a native ceiling."""

    assert scenario.ripgrep is not None
    command = (executable, "--no-messages", *scenario.ripgrep)

    async def invoke() -> bytes:
        completed = await anyio.run_process(command, cwd=workload.cwd, check=False)
        # rg exits 1 when nothing matches, which is the expected result for a miss.
        if completed.returncode not in (0, 1):
            raise RuntimeError(f"rg failed for {scenario.name}: {completed.returncode}")
        return completed.stdout

    await invoke()
    stdout, measurement = await measure_async(invoke, iterations=config.iterations)
    return BenchmarkSample(
        scenario=scenario.name,
        layer="ripgrep",
        file_count=workload.file_count,
        input_bytes=scenario.input_bytes,
        result_count=stdout.count(b"\n"),
        output_bytes=len(stdout),
        truncated=False,
        measurement=measurement,
    )


async def _measure_bash(cwd: Path, config: BenchmarkConfig) -> list[BenchmarkSample]:
    """Time end-to-end `bash` tool calls next to a bare `/bin/sh -c` subprocess.

    The tool path includes the job-recording wrapper, bounded output capture,
    and process-tree cleanup that the runtime's shared supervisor performs.
    """

    context = ToolContext(
        cwd=cwd,
        max_output_bytes=config.max_output_bytes,
        max_output_lines=config.max_output_lines,
        protected_paths=(),
    )
    supervisor = ProcessSupervisor()
    tool = BashTool(supervisor)
    samples: list[BenchmarkSample] = []
    try:
        for name, command in _BASH_COMMANDS:

            async def run_tool(command: str = command, name: str = name) -> ToolResult:
                result = await tool.run({"command": command}, context)
                if result.data.get("exit_code") != 0:
                    raise RuntimeError(f"bash benchmark failed for {name}: {result.text}")
                return result

            async def run_shell(command: str = command, name: str = name) -> bytes:
                completed = await anyio.run_process(["/bin/sh", "-c", command], cwd=cwd)
                if completed.returncode != 0:
                    raise RuntimeError(f"sh baseline failed for {name}")
                return completed.stdout

            await run_tool()
            result, tool_measurement = await measure_async(
                run_tool, iterations=config.iterations, track_memory=config.track_memory
            )
            samples.append(
                BenchmarkSample(
                    scenario=name,
                    layer="tool.run",
                    file_count=0,
                    input_bytes=0,
                    result_count=None,
                    output_bytes=len(result.text.encode("utf-8")),
                    truncated=result.truncated,
                    measurement=tool_measurement,
                )
            )
            await run_shell()
            stdout, shell_measurement = await measure_async(
                run_shell, iterations=config.iterations, track_memory=config.track_memory
            )
            samples.append(
                BenchmarkSample(
                    scenario=name,
                    layer="sh",
                    file_count=0,
                    input_bytes=0,
                    result_count=None,
                    output_bytes=len(stdout),
                    truncated=False,
                    measurement=shell_measurement,
                )
            )
    finally:
        await tool.aclose()
    return samples


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
    if scenario.expected_count is not None and count != scenario.expected_count:
        raise RuntimeError(
            f"unexpected result count for {scenario.name}: {count} != {scenario.expected_count}"
        )
    if scenario.expected_truncated is not None and (
        result.truncated != scenario.expected_truncated
    ):
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
    parser.add_argument(
        "--root",
        type=Path,
        help="search this existing directory instead of synthetic fixtures",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="also run each scenario as one concurrent tool batch of this many calls",
    )
    parser.add_argument(
        "--external-ceiling",
        action="store_true",
        help="also time rg on the same full-tree scans",
    )
    parser.add_argument(
        "--bash",
        action="store_true",
        help="also time end-to-end bash tool calls against a bare sh subprocess",
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
            root=parsed.root,
            batch_size=parsed.batch_size,
            external_ceiling=parsed.external_ceiling,
            include_bash=parsed.bash,
        )
    )
    print(report.to_json())
    if parsed.output is not None:
        parsed.output.parent.mkdir(parents=True, exist_ok=True)
        parsed.output.write_text(f"{report.to_json()}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
