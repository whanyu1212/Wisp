"""Benchmark the complete Rust TUI path through a real POSIX PTY.

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
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from benchmarks.support import environment

type Renderer = Literal["rust"]

START_TOKEN = "WISP_E2E_START_7F3A"
FINAL_TOKEN = "WISP_E2E_FINAL_9C2D"
_FAKE_RESPONSE_PREFIX = "fake response to: "
_VALID_RENDERERS = frozenset(("rust",))
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"

_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_DCS = re.compile(rb"\x1bP.*?\x1b\\", re.DOTALL)
# The TUI wraps every frame in a synchronized update; output up to the last end
# marker holds only complete frames, so no escape or character is cut in half.
_FRAME_END = b"\x1b[?2026l"
_CSI_COMMAND = re.compile(rb"\x1b\[([0-?]*)[ -/]*([@-~])")
_OTHER_ESCAPE = re.compile(rb"\x1b[ -/]*[0-~]")
# A fake reply can finish before "working" is painted. The idle footer then never changes, so a
# repaint of only changed cells never writes "idle" again; one resize repaints the whole frame.
# That only confirms the run settled. The wait and the redraw distort the sample's settle time,
# total time, and output bytes, so summaries leave such samples out of those three.
_IDLE_REPAINT_AFTER_NS = 250_000_000
_READY_MARKERS = (
    "AskWispanything",
    "Typeapromptor/forcommands.",
    "/connecttoaddaprovider",
)


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for terminal-backed Rust renderer runs."""

    rust_binary: Path | None = None
    renderers: tuple[Renderer, ...] = ("rust",)
    runs: int = 3
    prompt_words: int = 64
    width: int = 100
    height: int = 24
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class BenchmarkSample:
    """Measurements and correctness observations from one CLI process.

    ``submit_to_settled_ms`` is None when ``idle_repainted`` is set: the idle footer was never
    redrawn after the reply, so a forced repaint confirmed the settle but not when it happened.
    Such a sample's ``total_ms`` and ``terminal_output_bytes`` include the wait and the redraw.
    """

    renderer: Renderer
    run: int
    order: int
    launch_to_ready_ms: float
    submit_to_prompt_echo_ms: float
    submit_to_first_response_ms: float
    submit_to_final_response_ms: float
    submit_to_settled_ms: float | None
    total_ms: float
    terminal_output_bytes: int
    idle_repainted: bool
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
    idle_repainted_samples: int
    launch_to_ready_ms: Distribution
    submit_to_prompt_echo_ms: Distribution
    submit_to_first_response_ms: Distribution
    submit_to_final_response_ms: Distribution
    submit_to_settled_ms: Distribution | None
    total_ms: Distribution | None
    terminal_output_bytes: Distribution | None
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
        # A forced idle repaint distorts these three measurements; see BenchmarkSample.
        measured = tuple(sample for sample in selected if not sample.idle_repainted)
        summaries.append(
            RendererSummary(
                renderer=renderer,
                samples=len(selected),
                idle_repainted_samples=len(selected) - len(measured),
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
                submit_to_settled_ms=_optional_distribution(
                    tuple(sample.submit_to_settled_ms for sample in measured)
                ),
                total_ms=_optional_distribution(tuple(sample.total_ms for sample in measured)),
                terminal_output_bytes=_optional_distribution(
                    tuple(float(sample.terminal_output_bytes) for sample in measured)
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
            "--session-dir",
            str(session_dir),
        )
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
                try:
                    start_token = os.read(start_reader, 1)
                finally:
                    os.close(start_reader)
                if start_token != b"\0":
                    os._exit(125)
                os.chdir(project_dir)
                os.execve(sys.executable, list(command), child_environment)
            except BaseException:
                try:
                    os.write(2, b"Wisp TUI benchmark child launch failed\n")
                except OSError:
                    pass
                os._exit(126)

        os.close(start_reader)
        output = bytearray()
        screen = _PaintedScreen()
        phase_output = bytearray()
        terminal_output_bytes = 0
        context_redrawn = False
        ready_ns: int | None = None
        submitted_ns: int | None = None
        prompt_echo_ns: int | None = None
        reply = _ReplyProgress()
        idle_repaint_requested = False
        quit_sent = False
        wait_result: _WaitResult | None = None
        initial_terminal: list[int | list[bytes]] | None = None
        start_writer_open = True
        deadline = time.monotonic() + config.timeout_seconds
        try:
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
                now_ns = time.perf_counter_ns()
                if chunk:
                    terminal_output_bytes += len(chunk)
                    output.extend(chunk)
                    if submitted_ns is not None:
                        phase_output.extend(chunk)
                    # A later frame in the same read can scroll the reply's first row
                    # away, so observe the reply after every frame.
                    for _frame in screen.replay(chunk):
                        if submitted_ns is not None:
                            reply.observe(screen.rows(), now_ns)
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
                    if (
                        reply.final_ns is not None
                        and reply.settled_ns is None
                        and not idle_repaint_requested
                        and now_ns - reply.final_ns > _IDLE_REPAINT_AFTER_NS
                    ):
                        fcntl.ioctl(
                            terminal_fd,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", config.height, config.width - 1, 0, 0),
                        )
                        idle_repaint_requested = True
                    if reply.settled_ns is not None and not quit_sent:
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
                    reply.first_ns,
                    reply.final_ns,
                    reply.settled_ns,
                )
                raise RuntimeError(
                    f"{renderer} TUI timed out during {phase}; "
                    f"terminal tail={_diagnostic_tail(output)!r}"
                )
        finally:
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
            reply.first_ns,
            reply.final_ns,
            reply.settled_ns,
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
            submit_to_settled_ms=None
            if idle_repaint_requested
            else _elapsed_ms(submitted, settled),
            total_ms=_elapsed_ms(launched_ns, completed_ns),
            terminal_output_bytes=terminal_output_bytes,
            idle_repainted=idle_repaint_requested,
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
    # A framed composer names "model provider"; a short terminal's footer names
    # "provider/model".
    provider_ready = re.search(r"fake(?:/|\s*)fake", plain) is not None
    # Incremental repaints can reach the PTY with cursor moves instead of spaces,
    # so "ctx ▰▱ ~35%" may arrive as "ctx▰▱~35%" once escapes are stripped.
    context_hydrated = re.search(r"ctx\s*[▰▱]*\s*~?[0-9]", plain) is not None
    return (
        provider_ready and context_hydrated and any(marker in compact for marker in _READY_MARKERS)
    )


def _response_started(rows: Sequence[str]) -> bool:
    """Return whether the fake reply's first row is on screen.

    Args:
        rows: Replayed screen rows.

    Returns:
        Whether a row shows the reply prefix followed by the start marker. The
        prompt echo carries the marker without the prefix.
    """

    marker = f"{_FAKE_RESPONSE_PREFIX}{START_TOKEN}"
    return any(marker in row for row in rows)


def _response_final_row(rows: Sequence[str]) -> int | None:
    """Return the index of the reply row that shows the final marker.

    Args:
        rows: Replayed screen rows.

    Returns:
        The first row with the final marker that is not part of the prompt echo,
        or ``None``. Every prompt row carries the user bar ``▎``.
    """

    return next(
        (index for index, row in enumerate(rows) if FINAL_TOKEN in row and "▎" not in row),
        None,
    )


def _settled_after_final(rows: Sequence[str]) -> bool:
    final_row = _response_final_row(rows)
    return final_row is not None and any("idle" in row for row in rows[final_row + 1 :])


@dataclass
class _ReplyProgress:
    """When the fake reply first showed, showed its final marker, and settled."""

    first_ns: int | None = None
    final_ns: int | None = None
    settled_ns: int | None = None

    def observe(self, rows: Sequence[str], now_ns: int) -> None:
        """Record each milestone the replayed screen shows for the first time.

        Args:
            rows: Replayed screen rows after one frame.
            now_ns: When the frame was read.
        """

        if self.first_ns is None and _response_started(rows):
            self.first_ns = now_ns
        if self.first_ns is None:
            return
        if self.final_ns is None and _response_final_row(rows) is not None:
            self.final_ns = now_ns
        if self.final_ns is not None and self.settled_ns is None and _settled_after_final(rows):
            self.settled_ns = now_ns


class _PaintedScreen:
    """Screen cells replayed from the complete frames the TUI has written.

    Incremental frames repaint only changed cells, so a word revealed across two
    frames reaches the PTY with other cells (spinner, borders, status) painted
    between its halves. Placing text at its cursor position joins the halves, which
    matching the printable byte stream cannot do. Only the sequences the TUI paints
    with are interpreted: cursor position, clearing the screen or the rest of a
    line, and text.
    """

    def __init__(self) -> None:
        self._cells: dict[tuple[int, int], str] = {}
        self._row = 1
        self._column = 1
        self._unframed = bytearray()
        self._rows: list[str] | None = []

    def feed(self, chunk: bytes) -> None:
        """Replay each frame that ``chunk`` completes and keep a partial frame for later.

        Args:
            chunk: PTY output in the order it was read.
        """

        for _frame in self.replay(chunk):
            pass

    def replay(self, chunk: bytes) -> Iterator[None]:
        """Replay each frame that ``chunk`` completes, pausing after each one.

        One read can hold several frames, and a later frame may scroll away what an
        earlier one showed, so a caller can inspect ``rows()`` at every pause.

        Args:
            chunk: PTY output in the order it was read.

        Yields:
            None: After each complete frame is applied.
        """

        self._unframed.extend(chunk)
        while (end := self._unframed.find(_FRAME_END)) >= 0:
            end += len(_FRAME_END)
            frame = _OSC.sub(b"", _DCS.sub(b"", bytes(self._unframed[:end])))
            del self._unframed[:end]
            self._apply_frame(frame)
            yield

    def _apply_frame(self, frames: bytes) -> None:
        self._rows = None
        position = 0
        while position < len(frames):
            command = _CSI_COMMAND.match(frames, position)
            if command is not None:
                self._apply(command.group(1), command.group(2))
                position = command.end()
                continue
            escape = _OTHER_ESCAPE.match(frames, position)
            if escape is not None:
                position = escape.end()
                continue
            text_end = frames.find(b"\x1b", position)
            text_end = len(frames) if text_end < 0 else text_end
            self._paint(frames[position:text_end].decode("utf-8", "replace"))
            position = text_end

    def shows(self, text: str) -> bool:
        """Return whether one screen row contains ``text``."""

        return any(text in row for row in self.rows())

    def rows(self) -> list[str]:
        """Return the replayed screen rows from the top."""

        if self._rows is None:
            self._rows = self._render_rows()
        return self._rows

    def _apply(self, params: bytes, final: bytes) -> None:
        if final == b"H":
            values = [int(value or 1) for value in params.split(b";")] if params else [1]
            self._row, self._column = values[0], values[1] if len(values) > 1 else 1
        elif final == b"J" and params == b"2":
            self._cells.clear()
        elif final == b"K" and params in (b"", b"0"):
            erased = [
                cell for cell in self._cells if cell[0] == self._row and cell[1] >= self._column
            ]
            for cell in erased:
                del self._cells[cell]

    def _paint(self, text: str) -> None:
        for character in text:
            if not character.isprintable():
                continue
            self._cells[(self._row, self._column)] = character
            self._column += 2 if unicodedata.east_asian_width(character) in "WF" else 1

    def _render_rows(self) -> list[str]:
        columns_by_row: dict[int, dict[int, str]] = {}
        for (row, column), character in self._cells.items():
            columns_by_row.setdefault(row, {})[column] = character
        return [
            "".join(columns.get(column, " ") for column in range(1, max(columns) + 1))
            for _row, columns in sorted(columns_by_row.items())
        ]


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
    parser.add_argument("--renderers", type=_parse_renderers, default=("rust",))
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
