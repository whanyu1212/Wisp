"""Benchmark complete Rust and Textual TUI paths through a real POSIX PTY.

PTY marker visibility is a practical proxy for terminal paint, not a measurement of
photon-level display latency.  ``wait4`` resource observations describe the directly
launched CLI process; in particular, maximum RSS is not whole-process-tree RSS.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import math
import os
import pty
import re
import select
import signal
import statistics
import struct
import sys
import tempfile
import termios
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from benchmarks.support import environment

type Renderer = Literal["rust", "textual"]

START_TOKEN = "WISP_E2E_START_7F3A"
FINAL_TOKEN = "WISP_E2E_FINAL_9C2D"
_FAKE_RESPONSE_PREFIX = "fake response to: "
_VALID_RENDERERS = frozenset(("rust", "textual"))
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"

_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_DCS = re.compile(rb"\x1bP.*?\x1b\\", re.DOTALL)
_READY_MARKERS = (
    "AskWispanything",
    "Typeapromptor/forcommands.",
    "/connecttoaddaprovider",
)


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for paired terminal-backed renderer runs."""

    rust_binary: Path | None = None
    renderers: tuple[Renderer, ...] = ("rust", "textual")
    runs: int = 3
    prompt_words: int = 64
    width: int = 100
    height: int = 24
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class BenchmarkSample:
    """Measurements and correctness observations from one CLI process."""

    renderer: Renderer
    run: int
    order: int
    launch_to_ready_ms: float
    submit_to_prompt_echo_ms: float
    submit_to_first_response_ms: float
    submit_to_final_response_ms: float
    submit_to_settled_ms: float
    total_ms: float
    terminal_output_bytes: int
    child_user_cpu_ms: float | None
    child_system_cpu_ms: float | None
    child_max_rss_bytes: int | None
    exact_markers_visible: bool
    clean_exit: bool
    terminal_restored: bool


@dataclass(frozen=True)
class Distribution:
    """Median, 95th percentile, and maximum for one numeric measurement."""

    median: float
    p95: float
    max: float


@dataclass(frozen=True)
class RendererSummary:
    """Aggregate distributions for one renderer."""

    renderer: Renderer
    samples: int
    launch_to_ready_ms: Distribution
    submit_to_prompt_echo_ms: Distribution
    submit_to_first_response_ms: Distribution
    submit_to_final_response_ms: Distribution
    submit_to_settled_ms: Distribution
    total_ms: Distribution
    terminal_output_bytes: Distribution
    child_user_cpu_ms: Distribution | None
    child_system_cpu_ms: Distribution | None
    child_max_rss_bytes: Distribution | None


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable raw samples and per-renderer summaries."""

    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[BenchmarkSample, ...]
    summaries: tuple[RendererSummary, ...]

    def to_json(self) -> str:
        """Serialize the report as stable, human-readable JSON.

        Returns:
            The complete benchmark report.
        """

        payload = asdict(self)
        payload["config"]["rust_binary"] = (
            str(self.config.rust_binary) if self.config.rust_binary is not None else None
        )
        return json.dumps(payload, indent=2, sort_keys=True)


@dataclass(frozen=True)
class _WaitResult:
    status: int
    user_cpu_ms: float | None
    system_cpu_ms: float | None
    max_rss_bytes: int | None


def validate_config(config: BenchmarkConfig) -> None:
    """Validate benchmark inputs and the requested Rust executable.

    Args:
        config: Candidate benchmark configuration.

    Raises:
        ValueError: If values are invalid or the Rust executable is unavailable.
        RuntimeError: If the current platform cannot provide a POSIX PTY.
    """

    if os.name != "posix":
        raise RuntimeError("the end-to-end TUI benchmark requires a POSIX PTY")
    if not config.renderers:
        raise ValueError("at least one renderer is required")
    if len(set(config.renderers)) != len(config.renderers):
        raise ValueError("renderers must not contain duplicates")
    invalid = set(config.renderers) - _VALID_RENDERERS
    if invalid:
        raise ValueError(f"unknown renderers: {', '.join(sorted(invalid))}")
    if config.runs < 1 or config.prompt_words < 1:
        raise ValueError("runs and prompt words must be positive")
    if config.width < 60 or config.height < 12:
        raise ValueError("the terminal must be at least 60 columns by 12 rows")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("timeout seconds must be a positive finite number")
    if "rust" not in config.renderers:
        return
    binary = config.rust_binary
    if binary is None:
        raise ValueError("--rust-binary is required when benchmarking the Rust renderer")
    if not binary.is_absolute():
        raise ValueError("--rust-binary must be an absolute path")
    if not binary.is_file():
        raise ValueError(f"Rust TUI binary does not exist: {binary}")
    if not os.access(binary, os.X_OK):
        raise ValueError(f"Rust TUI binary is not executable: {binary}")


def summarize_samples(samples: Sequence[BenchmarkSample]) -> tuple[RendererSummary, ...]:
    """Aggregate samples into stable per-renderer distributions.

    Args:
        samples: Completed samples from one benchmark configuration.

    Returns:
        Summaries in first-observed renderer order.

    Raises:
        ValueError: If no samples are supplied.
    """

    if not samples:
        raise ValueError("at least one sample is required")
    renderer_order = tuple(dict.fromkeys(sample.renderer for sample in samples))
    summaries = []
    for renderer in renderer_order:
        selected = tuple(sample for sample in samples if sample.renderer == renderer)
        summaries.append(
            RendererSummary(
                renderer=renderer,
                samples=len(selected),
                launch_to_ready_ms=_distribution(
                    tuple(sample.launch_to_ready_ms for sample in selected)
                ),
                submit_to_prompt_echo_ms=_distribution(
                    tuple(sample.submit_to_prompt_echo_ms for sample in selected)
                ),
                submit_to_first_response_ms=_distribution(
                    tuple(sample.submit_to_first_response_ms for sample in selected)
                ),
                submit_to_final_response_ms=_distribution(
                    tuple(sample.submit_to_final_response_ms for sample in selected)
                ),
                submit_to_settled_ms=_distribution(
                    tuple(sample.submit_to_settled_ms for sample in selected)
                ),
                total_ms=_distribution(tuple(sample.total_ms for sample in selected)),
                terminal_output_bytes=_distribution(
                    tuple(float(sample.terminal_output_bytes) for sample in selected)
                ),
                child_user_cpu_ms=_optional_distribution(
                    tuple(sample.child_user_cpu_ms for sample in selected)
                ),
                child_system_cpu_ms=_optional_distribution(
                    tuple(sample.child_system_cpu_ms for sample in selected)
                ),
                child_max_rss_bytes=_optional_distribution(
                    tuple(
                        float(sample.child_max_rss_bytes)
                        if sample.child_max_rss_bytes is not None
                        else None
                        for sample in selected
                    )
                ),
            )
        )
    return tuple(summaries)


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Run paired renderer workloads through fresh PTYs and session directories.

    Args:
        config: Benchmark configuration. Defaults to :class:`BenchmarkConfig`.

    Returns:
        Raw samples and aggregate distributions.

    Raises:
        RuntimeError: If a renderer times out, exits unsuccessfully, fails to
            display an exact marker, or fails to restore its terminal.
        ValueError: If the configuration is invalid.
    """

    selected = config or BenchmarkConfig()
    validate_config(selected)
    samples: list[BenchmarkSample] = []
    for run_index in range(selected.runs):
        renderers = (
            selected.renderers if run_index % 2 == 0 else tuple(reversed(selected.renderers))
        )
        for order, renderer in enumerate(renderers, start=1):
            samples.append(
                _run_sample(
                    selected,
                    renderer=renderer,
                    run=run_index + 1,
                    order=order,
                )
            )
    completed = tuple(samples)
    return BenchmarkReport(
        config=selected,
        environment=environment(),
        samples=completed,
        summaries=summarize_samples(completed),
    )


def _run_sample(
    config: BenchmarkConfig,
    *,
    renderer: Renderer,
    run: int,
    order: int,
) -> BenchmarkSample:
    prompt = " ".join((START_TOKEN, *("payload" for _ in range(config.prompt_words)), FINAL_TOKEN))
    response_start_marker = f"{_FAKE_RESPONSE_PREFIX}{START_TOKEN}"

    with tempfile.TemporaryDirectory(prefix=f"wisp-tui-e2e-{renderer}-") as temporary:
        root = Path(temporary)
        home = root / "home"
        project_dir = root / "project"
        session_dir = root / "sessions"
        for directory in (
            home,
            home / ".config",
            home / ".cache",
            home / ".local" / "share",
            project_dir,
            session_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        child_environment = _child_environment(config, renderer=renderer, home=home)
        command = (
            sys.executable,
            "-m",
            "wisp",
            "tui",
            "--renderer",
            renderer,
            "--session-dir",
            str(session_dir),
        )
        launched_ns = time.perf_counter_ns()
        child_pid, terminal_fd = pty.fork()
        if child_pid == 0:
            os.chdir(project_dir)
            os.execve(sys.executable, list(command), child_environment)

        fcntl.ioctl(
            terminal_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", config.height, config.width - 1, 0, 0),
        )
        initial_terminal = termios.tcgetattr(terminal_fd)
        output = bytearray()
        phase_output = bytearray()
        terminal_output_bytes = 0
        context_redrawn = False
        ready_ns: int | None = None
        submitted_ns: int | None = None
        prompt_echo_ns: int | None = None
        first_response_ns: int | None = None
        final_response_ns: int | None = None
        settled_ns: int | None = None
        quit_sent = False
        wait_result: _WaitResult | None = None
        deadline = time.monotonic() + config.timeout_seconds
        try:
            while time.monotonic() < deadline:
                chunk = _read_available(terminal_fd)
                if chunk:
                    terminal_output_bytes += len(chunk)
                    output.extend(chunk)
                    if submitted_ns is not None:
                        phase_output.extend(chunk)
                now_ns = time.perf_counter_ns()
                startup_plain = _plain_text(output) if ready_ns is None else ""
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
                if context_redrawn and ready_ns is None and _ready(startup_plain, renderer):
                    ready_ns = now_ns
                    phase_output.clear()
                    submitted_ns = time.perf_counter_ns()
                    os.write(terminal_fd, f"{prompt}\r".encode())
                elif submitted_ns is not None:
                    plain = _plain_text(phase_output)
                    if prompt_echo_ns is None and START_TOKEN in plain:
                        prompt_echo_ns = now_ns
                    response_index = plain.find(response_start_marker)
                    if first_response_ns is None and response_index >= 0:
                        first_response_ns = now_ns
                    if first_response_ns is not None and final_response_ns is None:
                        response_tail = (
                            plain[response_index + len(response_start_marker) :]
                            if response_index >= 0
                            else ""
                        )
                        if FINAL_TOKEN in response_tail:
                            final_response_ns = now_ns
                    if final_response_ns is not None and settled_ns is None:
                        final_index = plain.rfind(FINAL_TOKEN)
                        if final_index >= 0 and _settled_after_final(
                            plain[final_index + len(FINAL_TOKEN) :], renderer
                        ):
                            settled_ns = now_ns
                    if settled_ns is not None and not quit_sent:
                        os.write(terminal_fd, b"/quit\r")
                        quit_sent = True

                wait_result = _wait_for_child(child_pid, block=False)
                if wait_result is not None:
                    try:
                        trailing = _drain_available(terminal_fd)
                    except RuntimeError:
                        _kill_process_group(child_pid, terminal_fd)
                        raise
                    terminal_output_bytes += len(trailing)
                    output.extend(trailing)
                    if submitted_ns is not None:
                        phase_output.extend(trailing)
                    break
                if not chunk:
                    select.select([terminal_fd], [], [], 0.01)

            if wait_result is None:
                phase = _phase_name(
                    ready_ns,
                    prompt_echo_ns,
                    first_response_ns,
                    final_response_ns,
                    settled_ns,
                )
                raise RuntimeError(
                    f"{renderer} TUI timed out during {phase}; "
                    f"terminal tail={_diagnostic_tail(output)!r}"
                )
        finally:
            if wait_result is None:
                _kill_process_group(child_pid, terminal_fd)
                wait_result = _wait_for_child(child_pid, block=True)
            restored = termios.tcgetattr(terminal_fd) == initial_terminal
            os.close(terminal_fd)

        completed_ns = time.perf_counter_ns()
        if wait_result is None:
            raise RuntimeError(f"failed to collect {renderer} TUI process status")
        exit_code = os.waitstatus_to_exitcode(wait_result.status)
        if exit_code != 0:
            raise RuntimeError(
                f"{renderer} TUI exited with status {exit_code}; "
                f"terminal tail={_diagnostic_tail(output)!r}"
            )
        milestones = (
            ready_ns,
            submitted_ns,
            prompt_echo_ns,
            first_response_ns,
            final_response_ns,
            settled_ns,
        )
        if any(value is None for value in milestones):
            raise RuntimeError(f"{renderer} TUI exited before every exact marker became visible")
        if not restored:
            raise RuntimeError(f"{renderer} TUI did not restore the terminal")
        ready, submitted, prompt_echo, first_response, final_response, settled = cast(
            tuple[int, int, int, int, int, int], milestones
        )
        return BenchmarkSample(
            renderer=renderer,
            run=run,
            order=order,
            launch_to_ready_ms=_elapsed_ms(launched_ns, ready),
            submit_to_prompt_echo_ms=_elapsed_ms(submitted, prompt_echo),
            submit_to_first_response_ms=_elapsed_ms(submitted, first_response),
            submit_to_final_response_ms=_elapsed_ms(submitted, final_response),
            submit_to_settled_ms=_elapsed_ms(submitted, settled),
            total_ms=_elapsed_ms(launched_ns, completed_ns),
            terminal_output_bytes=terminal_output_bytes,
            child_user_cpu_ms=wait_result.user_cpu_ms,
            child_system_cpu_ms=wait_result.system_cpu_ms,
            child_max_rss_bytes=wait_result.max_rss_bytes,
            exact_markers_visible=True,
            clean_exit=True,
            terminal_restored=True,
        )


def _child_environment(
    config: BenchmarkConfig,
    *,
    renderer: Renderer,
    home: Path,
) -> dict[str, str]:
    child_environment = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONPATH": os.pathsep.join(
            value for value in (str(_SOURCE_ROOT), os.environ.get("PYTHONPATH", "")) if value
        ),
        "TERM": "xterm-256color",
        "WISP_PROVIDER": "fake",
        "WISP_MODEL": "",
        "WISP_TRUST": "1",
        "WISP_TUI_MOUSE": "0",
        "WISP_UPDATE_CHECK": "0",
    }
    if renderer == "rust":
        if config.rust_binary is None:
            raise ValueError("Rust binary disappeared after configuration validation")
        child_environment["WISP_RUST_TUI_BINARY"] = str(config.rust_binary)
    else:
        child_environment.pop("WISP_RUST_TUI_BINARY", None)
    return child_environment


def _read_available(terminal_fd: int) -> bytes:
    readable, _, _ = select.select([terminal_fd], [], [], 0)
    if not readable:
        return b""
    try:
        return os.read(terminal_fd, 1 << 20)
    except OSError as exc:
        if exc.errno == errno.EIO:
            return b""
        raise


def _drain_available(terminal_fd: int, *, timeout_seconds: float = 0.1) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    chunks = []
    while chunk := _read_available(terminal_fd):
        chunks.append(chunk)
        if time.monotonic() >= deadline:
            raise RuntimeError("PTY output did not quiesce after the CLI process exited")
    return b"".join(chunks)


def _wait_for_child(child_pid: int, *, block: bool) -> _WaitResult | None:
    flags = 0 if block else os.WNOHANG
    if hasattr(os, "wait4"):
        waited_pid, status, usage = os.wait4(child_pid, flags)
        if waited_pid == 0:
            return None
        return _WaitResult(
            status=status,
            user_cpu_ms=usage.ru_utime * 1_000,
            system_cpu_ms=usage.ru_stime * 1_000,
            max_rss_bytes=_rss_bytes(usage.ru_maxrss),
        )
    waited_pid, status = os.waitpid(child_pid, flags)
    if waited_pid == 0:
        return None
    return _WaitResult(status=status, user_cpu_ms=None, system_cpu_ms=None, max_rss_bytes=None)


def _rss_bytes(max_rss: int | float) -> int:
    value = int(max_rss)
    return value if sys.platform == "darwin" else value * 1024


def _kill_process_group(child_pid: int, terminal_fd: int) -> None:
    try:
        foreground = os.tcgetpgrp(terminal_fd)
        if foreground > 0 and foreground != os.getpgrp():
            os.killpg(foreground, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        os.killpg(child_pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _plain_text(output: bytes | bytearray) -> str:
    data = _DCS.sub(b"", bytes(output))
    data = _OSC.sub(b"", data)
    data = _CSI.sub(b"", data)
    return "".join(
        character for character in data.decode("utf-8", "ignore") if character.isprintable()
    )


def _ready(plain: str, renderer: Renderer) -> bool:
    compact = "".join(plain.split())
    if renderer == "rust":
        provider_ready = "fake/fake" in plain
        context_hydrated = re.search(r"ctx\s+~?[0-9]", plain) is not None
    else:
        provider_ready = "offline" in plain or "fake" in plain
        context_hydrated = re.search(r"~?[0-9]+(?:\.[0-9]+)?%", plain) is not None
    return (
        provider_ready and context_hydrated and any(marker in compact for marker in _READY_MARKERS)
    )


def _settled_after_final(tail: str, renderer: Renderer) -> bool:
    if renderer == "rust":
        return "idle" in tail
    return "Ask Wisp anything" in tail


def _phase_name(
    ready_ns: int | None,
    prompt_echo_ns: int | None,
    first_response_ns: int | None,
    final_response_ns: int | None,
    settled_ns: int | None,
) -> str:
    if ready_ns is None:
        return "startup"
    if prompt_echo_ns is None:
        return "prompt echo"
    if first_response_ns is None:
        return "first response"
    if final_response_ns is None:
        return "final response"
    if settled_ns is None:
        return "settle"
    return "clean exit"


def _diagnostic_tail(output: bytearray) -> str:
    return _plain_text(output[-4096:])[-1000:]


def _elapsed_ms(start_ns: int, end_ns: int) -> float:
    return (end_ns - start_ns) / 1_000_000


def _distribution(values: Sequence[float]) -> Distribution:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("a distribution requires at least one value")
    position = (len(ordered) - 1) * 0.95
    lower = math.floor(position)
    upper = math.ceil(position)
    p95 = ordered[lower]
    if upper != lower:
        p95 += (ordered[upper] - ordered[lower]) * (position - lower)
    return Distribution(median=float(statistics.median(ordered)), p95=p95, max=ordered[-1])


def _optional_distribution(values: Sequence[float | None]) -> Distribution | None:
    present = tuple(value for value in values if value is not None)
    return _distribution(present) if present else None


def _parse_renderers(value: str) -> tuple[Renderer, ...]:
    renderers = tuple(part.strip() for part in value.split(",") if part.strip())
    invalid = set(renderers) - _VALID_RENDERERS
    if not renderers or invalid:
        allowed = ",".join(sorted(_VALID_RENDERERS))
        raise argparse.ArgumentTypeError(f"renderers must be a comma-separated subset of {allowed}")
    return cast(tuple[Renderer, ...], renderers)


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-binary", type=Path)
    parser.add_argument("--renderers", type=_parse_renderers, default=("rust", "textual"))
    parser.add_argument("--runs", type=int, default=BenchmarkConfig.runs)
    parser.add_argument("--prompt-words", type=int, default=BenchmarkConfig.prompt_words)
    parser.add_argument("--width", type=int, default=BenchmarkConfig.width)
    parser.add_argument("--height", type=int, default=BenchmarkConfig.height)
    parser.add_argument("--timeout-seconds", type=float, default=BenchmarkConfig.timeout_seconds)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the command-line benchmark and emit its JSON report.

    Args:
        arguments: Optional argument sequence for tests and embedded callers.
    """

    parsed = _parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            rust_binary=parsed.rust_binary,
            renderers=parsed.renderers,
            runs=parsed.runs,
            prompt_words=parsed.prompt_words,
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
