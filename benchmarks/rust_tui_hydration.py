"""Measure Rust TUI startup and attributed long-history hydration stages.

Each run starts the source CLI in a fresh PTY with disposable saved history.
The ready timestamp ends at PTY output, which is a paint proxy. Process-tree
CPU and RSS are sampled and may miss short-lived processes or peaks.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pty
import select
import struct
import sys
import tempfile
import termios
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.rust_tui_e2e import (
    BenchmarkConfig as E2EConfig,
)
from benchmarks.rust_tui_e2e import (
    Distribution,
    _child_environment,
    _diagnostic_tail,
    _distribution,
    _drain_available,
    _elapsed_ms,
    _kill_process_group,
    _plain_text,
    _read_available,
    _ready,
    _wait_for_child,
)
from benchmarks.rust_tui_e2e import (
    validate_config as validate_e2e,
)
from benchmarks.rust_tui_interaction import _history_ready, _TreeObserver
from benchmarks.support import environment
from scripts.smoke_installed_rust_tui import _seed_history

_HISTORY_READY = "RC2 saved history ready"
_OUTPUT_TAIL_LIMIT = 65_536
_REQUIRED_STAGES = frozenset(
    (
        "python.page_read",
        "python.page_publish",
        "rust.page_projection",
        "rust.page_clone",
        "rust.history_install",
    )
)


@dataclass(frozen=True)
class BenchmarkConfig:
    """Rust binary, saved-history conditions, and PTY settings."""

    rust_binary: Path | None = None
    runs: int = 3
    history_messages: tuple[int, ...] = (0, 10_000)
    width: int = 100
    height: int = 24
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class ProfileRecord:
    """One stage measurement emitted by a profiled Wisp process."""

    source: str
    stage: str
    duration_ms: float
    pid: int
    count: int | None = None
    bytes: int | None = None


@dataclass(frozen=True)
class StageTotal:
    """All events for one stage within one startup."""

    stage: str
    duration_ms: float
    records: int
    count: int
    bytes: int


@dataclass(frozen=True)
class SessionSample:
    """Raw startup observation, including every emitted stage record."""

    history_messages: int
    history_bytes: int
    run: int
    launch_to_ready_ms: float
    total_ms: float
    terminal_output_bytes: int
    observed_process_tree_cpu_ms: float
    observed_simultaneous_rss_peak_bytes: int
    resource_observations: int
    profile_records: tuple[ProfileRecord, ...]
    stage_totals: tuple[StageTotal, ...]
    clean_exit: bool
    terminal_restored: bool


@dataclass(frozen=True)
class StageSummary:
    """Distribution of summed stage work per startup in one condition."""

    stage: str
    duration_ms: Distribution
    records: Distribution
    count: Distribution
    bytes: Distribution


@dataclass(frozen=True)
class ConditionSummary:
    """Startup and per-stage distributions for one history size."""

    history_messages: int
    sessions: int
    launch_to_ready_ms: Distribution
    observed_process_tree_cpu_ms: Distribution
    observed_simultaneous_rss_peak_bytes: Distribution
    stages: tuple[StageSummary, ...]


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable raw measurements and condition summaries."""

    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[SessionSample, ...]
    summaries: tuple[ConditionSummary, ...]

    def to_json(self) -> str:
        """Serialize measurements without a machine-specific binary path.

        Returns:
            Human-readable JSON with raw stage records and distributions.
        """

        payload = asdict(self)
        payload["config"]["rust_binary"] = self.config.rust_binary is not None
        return json.dumps(payload, indent=2, sort_keys=True)


def validate_config(config: BenchmarkConfig) -> None:
    """Validate executable, history sizes, and terminal settings.

    Args:
        config: Requested benchmark conditions.

    Raises:
        ValueError: If the settings cannot produce comparable runs.
        RuntimeError: If POSIX PTYs are unavailable.
    """

    validate_e2e(
        E2EConfig(
            rust_binary=config.rust_binary,
            renderers=("rust",),
            runs=config.runs,
            width=config.width,
            height=config.height,
            timeout_seconds=config.timeout_seconds,
        )
    )
    if not config.history_messages or len(set(config.history_messages)) != len(
        config.history_messages
    ):
        raise ValueError("history messages must be a nonempty set of conditions")
    if any(count < 0 or count % 4 for count in config.history_messages):
        raise ValueError("history messages must be nonnegative multiples of four")


def read_profile_records(directory: Path) -> tuple[ProfileRecord, ...]:
    """Read process-emitted JSONL records from one startup.

    Args:
        directory: Per-run profile directory passed to the CLI.

    Returns:
        Validated records in filename and line order.

    Raises:
        RuntimeError: If records are missing or malformed.
    """

    records = []
    for path in sorted(directory.glob("*.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record must be a JSON object")
                stage = value["stage"]
                duration = value["duration_ms"]
                pid = value["pid"]
                count = value.get("count")
                size = value.get("bytes")
                if not isinstance(stage, str) or not stage:
                    raise ValueError("stage must be a nonempty string")
                if (
                    type(duration) not in (int, float)
                    or not math.isfinite(duration)
                    or duration < 0
                ):
                    raise ValueError("duration_ms must be a nonnegative finite number")
                if type(pid) is not int or pid <= 0:
                    raise ValueError("pid must be a positive integer")
                if count is not None and (type(count) is not int or count < 0):
                    raise ValueError("count must be a nonnegative integer")
                if size is not None and (type(size) is not int or size < 0):
                    raise ValueError("bytes must be a nonnegative integer")
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"invalid profile record at {path}:{line_number}: {exc}"
                ) from exc
            records.append(ProfileRecord(path.name, stage, float(duration), pid, count, size))
    if not records:
        raise RuntimeError(f"no hydration profile records found in {directory}")
    return tuple(records)


def total_stages(records: Sequence[ProfileRecord]) -> tuple[StageTotal, ...]:
    """Sum repeated stage events within one startup.

    Args:
        records: Validated records from one run.

    Returns:
        Stage totals in first-emitted order.
    """

    stages = tuple(dict.fromkeys(record.stage for record in records))
    return tuple(
        StageTotal(
            stage,
            sum(record.duration_ms for record in records if record.stage == stage),
            sum(record.stage == stage for record in records),
            sum((record.count or 0) for record in records if record.stage == stage),
            sum((record.bytes or 0) for record in records if record.stage == stage),
        )
        for stage in stages
    )


def validate_profile_coverage(records: Sequence[ProfileRecord], history_messages: int) -> None:
    """Require both processes to report the complete seeded history.

    Args:
        records: Stage measurements from one startup.
        history_messages: Number of messages seeded before the sentinel.

    Raises:
        RuntimeError: If either profiler is absent or Rust saw an incomplete transcript.
    """
    totals = {total.stage: total for total in total_stages(records)}
    missing = _REQUIRED_STAGES - totals.keys()
    if missing:
        raise RuntimeError(f"hydration profile is missing stages: {', '.join(sorted(missing))}")
    expected = history_messages + int(history_messages > 0)
    for stage in ("rust.page_projection", "rust.history_install"):
        if totals[stage].count != expected:
            raise RuntimeError(
                f"{stage} counted {totals[stage].count} messages; expected {expected}"
            )
    if expected > 200 and "rust.final_projection" not in totals:
        raise RuntimeError("hydration profile is missing final transcript projection")


def summarize_samples(samples: Sequence[SessionSample]) -> tuple[ConditionSummary, ...]:
    """Summarize startup and stage totals across repeated conditions.

    Args:
        samples: Completed startup observations.

    Returns:
        Summaries in first-observed history-size order.

    Raises:
        ValueError: If no samples are available or stages differ within a condition.
    """

    if not samples:
        raise ValueError("at least one startup sample is required")
    result = []
    for history_messages in dict.fromkeys(sample.history_messages for sample in samples):
        selected = tuple(s for s in samples if s.history_messages == history_messages)
        stage_names = tuple(total.stage for total in selected[0].stage_totals)
        if any({total.stage for total in s.stage_totals} != set(stage_names) for s in selected):
            raise ValueError(f"profile stages differ between history={history_messages} runs")
        stages = []
        for stage in stage_names:
            totals = tuple(next(t for t in s.stage_totals if t.stage == stage) for s in selected)
            stages.append(
                StageSummary(
                    stage=stage,
                    duration_ms=_distribution([t.duration_ms for t in totals]),
                    records=_distribution([float(t.records) for t in totals]),
                    count=_distribution([float(t.count) for t in totals]),
                    bytes=_distribution([float(t.bytes) for t in totals]),
                )
            )
        result.append(
            ConditionSummary(
                history_messages=history_messages,
                sessions=len(selected),
                launch_to_ready_ms=_distribution([s.launch_to_ready_ms for s in selected]),
                observed_process_tree_cpu_ms=_distribution(
                    [s.observed_process_tree_cpu_ms for s in selected]
                ),
                observed_simultaneous_rss_peak_bytes=_distribution(
                    [float(s.observed_simultaneous_rss_peak_bytes) for s in selected]
                ),
                stages=tuple(stages),
            )
        )
    return tuple(result)


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Run each history condition through fresh source-CLI PTYs.

    Args:
        config: Workload configuration, or defaults when omitted.

    Returns:
        Raw samples and per-condition distributions.
    """

    selected = config or BenchmarkConfig()
    validate_config(selected)
    samples = []
    for run_index in range(selected.runs):
        conditions = (
            selected.history_messages
            if run_index % 2 == 0
            else tuple(reversed(selected.history_messages))
        )
        for history_messages in conditions:
            samples.append(_run_sample(selected, history_messages, run_index + 1))
    completed = tuple(samples)
    return BenchmarkReport(selected, environment(), completed, summarize_samples(completed))


def _run_sample(config: BenchmarkConfig, history_messages: int, run: int) -> SessionSample:
    with tempfile.TemporaryDirectory(prefix="wisp-tui-hydration-") as temporary:
        root = Path(temporary)
        home, project_dir, session_dir, profile_dir = (
            root / "home",
            root / "project",
            root / "sessions",
            root / "profiles",
        )
        for directory in (
            home,
            home / ".config",
            home / ".cache",
            home / ".local" / "share",
            project_dir,
            session_dir,
            profile_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        history_bytes = _seed_history(session_dir, history_messages) if history_messages else 0
        child_env = _child_environment(
            E2EConfig(rust_binary=config.rust_binary), renderer="rust", home=home
        )
        child_env["WISP_HYDRATION_PROFILE_DIR"] = str(profile_dir)
        child_env["WISP_AUTO_COMPACTION"] = "0"
        command = [
            sys.executable,
            "-m",
            "wisp",
            "tui",
            "--renderer",
            "rust",
            "--session-dir",
            str(session_dir),
            *(["--continue"] if history_messages else []),
        ]
        launched_ns = time.perf_counter_ns()
        start_reader, start_writer = os.pipe()
        try:
            child_pid, terminal_fd = pty.fork()
        except BaseException:
            os.close(start_reader)
            os.close(start_writer)
            raise
        if child_pid == 0:
            try:
                os.close(start_writer)
                token = os.read(start_reader, 1)
                os.close(start_reader)
                if token != b"\0":
                    os._exit(125)
                os.chdir(project_dir)
                os.execve(sys.executable, command, child_env)
            except BaseException:
                os._exit(126)

        os.close(start_reader)
        output = bytearray()
        terminal_output_bytes = 0
        ready_ns: int | None = None
        context_redrawn = False
        history_marker_seen = False
        quit_sent = False
        wait_result = None
        initial_terminal = None
        start_writer_open = True
        observer: _TreeObserver | None = None
        deadline = time.monotonic() + config.timeout_seconds
        try:
            observer = _TreeObserver(child_pid)
            observer.start()
            fcntl.ioctl(
                terminal_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", config.height, config.width - 1, 0, 0),
            )
            initial_terminal = termios.tcgetattr(terminal_fd)
            os.write(start_writer, b"\0")
            os.close(start_writer)
            start_writer_open = False
            while time.monotonic() < deadline:
                chunk = _read_available(terminal_fd)
                if chunk:
                    terminal_output_bytes += len(chunk)
                    output.extend(chunk)
                    if len(output) > _OUTPUT_TAIL_LIMIT:
                        del output[:-_OUTPUT_TAIL_LIMIT]
                now_ns = time.perf_counter_ns()
                startup_plain = _plain_text(output) if ready_ns is None else ""
                if history_messages and "".join(_HISTORY_READY.split()) in "".join(
                    startup_plain.split()
                ):
                    history_marker_seen = True
                context_visible = "~" in startup_plain and any(
                    marker in startup_plain for marker in ("ctx", "context", "offline")
                )
                if not context_redrawn and context_visible:
                    fcntl.ioctl(
                        terminal_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", config.height, config.width, 0, 0),
                    )
                    context_redrawn = True
                    output.clear()
                    continue
                ready_visible = (
                    _history_ready(startup_plain, "rust", history_marker_seen)
                    if history_messages
                    else _ready(startup_plain, "rust")
                )
                if context_redrawn and ready_ns is None and ready_visible:
                    ready_ns = now_ns
                    os.write(terminal_fd, b"/quit\r")
                    quit_sent = True
                wait_result = _wait_for_child(child_pid, block=False)
                if wait_result is not None:
                    terminal_output_bytes += len(_drain_available(terminal_fd))
                    break
                if not chunk:
                    select.select([terminal_fd], [], [], 0.005)
            if wait_result is None:
                raise RuntimeError(
                    f"rust history={history_messages} timed out; "
                    f"ready={ready_ns is not None} quit={quit_sent}; "
                    f"terminal tail={_diagnostic_tail(output)!r}"
                )
        finally:
            if observer is not None:
                observer.stop()
            if start_writer_open:
                os.close(start_writer)
            if wait_result is None:
                _kill_process_group(child_pid, terminal_fd)
                wait_result = _wait_for_child(child_pid, block=True)
            try:
                restored = (
                    initial_terminal is not None
                    and termios.tcgetattr(terminal_fd) == initial_terminal
                )
            except (OSError, termios.error):
                restored = False
            os.close(terminal_fd)
        completed_ns = time.perf_counter_ns()
        if wait_result is None:
            raise RuntimeError("could not collect TUI exit status")
        exit_code = os.waitstatus_to_exitcode(wait_result.status)
        if exit_code != 0 or ready_ns is None:
            raise RuntimeError(
                f"rust history={history_messages} exited {exit_code} before ready/clean exit; "
                f"terminal tail={_diagnostic_tail(output)!r}"
            )
        if observer is not None and observer.error is not None:
            raise RuntimeError("rust process-tree sampling failed") from observer.error
        if observer is None or observer.observations == 0 or observer.rss_peak_bytes == 0:
            raise RuntimeError("rust process-tree sampling produced no resource evidence")
        if not restored:
            raise RuntimeError("rust did not restore the terminal")
        records = read_profile_records(profile_dir)
        validate_profile_coverage(records, history_messages)
        return SessionSample(
            history_messages=history_messages,
            history_bytes=history_bytes,
            run=run,
            launch_to_ready_ms=_elapsed_ms(launched_ns, ready_ns),
            total_ms=_elapsed_ms(launched_ns, completed_ns),
            terminal_output_bytes=terminal_output_bytes,
            observed_process_tree_cpu_ms=observer.cpu_ms,
            observed_simultaneous_rss_peak_bytes=observer.rss_peak_bytes,
            resource_observations=observer.observations,
            profile_records=records,
            stage_totals=total_stages(records),
            clean_exit=True,
            terminal_restored=True,
        )


def _parse_history(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "history messages must be comma-separated integers"
        ) from exc


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the startup benchmark and write its JSON report.

    Args:
        arguments: Optional command-line arguments for embedded callers.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-binary", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=BenchmarkConfig.runs)
    parser.add_argument("--history-messages", type=_parse_history, default=(0, 10_000))
    parser.add_argument("--width", type=int, default=BenchmarkConfig.width)
    parser.add_argument("--height", type=int, default=BenchmarkConfig.height)
    parser.add_argument("--timeout-seconds", type=float, default=BenchmarkConfig.timeout_seconds)
    parser.add_argument("--output", type=Path)
    parsed = parser.parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            rust_binary=parsed.rust_binary,
            runs=parsed.runs,
            history_messages=parsed.history_messages,
            width=parsed.width,
            height=parsed.height,
            timeout_seconds=parsed.timeout_seconds,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
