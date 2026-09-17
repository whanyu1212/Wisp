"""Measure live typing in matched Rust and Textual source-CLI PTY sessions.

The first appearance of an input marker in PTY output is a terminal-paint proxy,
not a display-photon measurement. Process-tree CPU and RSS are sampled observations:
short-lived descendants between samples can be missed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pty
import secrets
import select
import struct
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import psutil

from benchmarks.rust_tui_e2e import (
    START_TOKEN,
    Distribution,
    Renderer,
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
from benchmarks.support import environment
from scripts.smoke_installed_rust_tui import _seed_history

type ProbePhase = Literal["idle", "streaming"]
type NavigationDirection = Literal["page_up", "page_down"]
_RESPONSE_SUFFIX = "WISP_INTERACTION_RESPONSE_DONE_6E2C"
_RESPONSE_PREFIX = "WISP_INTERACTION_RESPONSE_BEGIN_A7C8D9E01234"
_HISTORY_READY = "RC2 saved history ready"
_SAMPLE_INTERVAL_SECONDS = 0.02
_OUTPUT_TAIL_LIMIT = 65_536


@dataclass(frozen=True)
class BenchmarkConfig:
    """Paired renderer conditions and controlled fake-provider stream settings."""

    rust_binary: Path | None = None
    renderers: tuple[Renderer, ...] = ("rust", "textual")
    runs: int = 3
    history_messages: tuple[int, ...] = (0, 10_000)
    response_words: int = 400
    stream_interval_ms: int = 20
    input_probes: int = 5
    width: int = 100
    height: int = 24
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class ProbeSample:
    """One complete single-write input-to-visible-marker observation."""

    phase: ProbePhase
    index: int
    visible_ms: float


@dataclass(frozen=True)
class NavigationSample:
    """Time from a navigation key to subsequent PTY output during streaming."""

    direction: NavigationDirection
    output_activity_ms: float


@dataclass(frozen=True)
class SessionSample:
    """One renderer process and its paired typing probes."""

    renderer: Renderer
    history_messages: int
    history_bytes: int
    run: int
    order: int
    launch_to_ready_ms: float
    submit_to_first_response_ms: float
    submit_to_final_response_ms: float
    total_ms: float
    terminal_output_bytes: int
    observed_process_tree_cpu_ms: float
    observed_simultaneous_rss_peak_bytes: int
    resource_observations: int
    probes: tuple[ProbeSample, ...]
    navigation: tuple[NavigationSample, ...]
    clean_exit: bool
    terminal_restored: bool


@dataclass(frozen=True)
class ConditionSummary:
    """Distributions for a renderer and history-size condition."""

    renderer: Renderer
    history_messages: int
    sessions: int
    idle_input_visible_ms: Distribution
    streaming_input_visible_ms: Distribution
    page_up_output_activity_ms: Distribution
    page_down_output_activity_ms: Distribution
    launch_to_ready_ms: Distribution
    observed_process_tree_cpu_ms: Distribution
    observed_simultaneous_rss_peak_bytes: Distribution


@dataclass(frozen=True)
class BenchmarkReport:
    """Serializable raw sessions and per-condition summaries."""

    config: BenchmarkConfig
    environment: dict[str, str]
    samples: tuple[SessionSample, ...]
    summaries: tuple[ConditionSummary, ...]

    def to_json(self) -> str:
        """Serialize without filesystem paths or transcript contents.

        Returns:
            Stable, human-readable JSON for the benchmark run.
        """

        payload = asdict(self)
        payload["config"]["rust_binary"] = self.config.rust_binary is not None
        return json.dumps(payload, indent=2, sort_keys=True)


class _TreeObserver:
    """Sample all currently reachable CLI descendants with psutil."""

    def __init__(self, child_pid: int) -> None:
        self._root = psutil.Process(child_pid)
        self._cpu_by_process: dict[tuple[int, float], float] = {}
        self.rss_peak_bytes = 0
        self.observations = 0
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Begin process sampling independently of PTY latency observations."""

        self._thread.start()

    def stop(self) -> None:
        """Stop and join the resource-sampling thread."""

        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample()
            except BaseException as exc:
                self.error = exc
                return
            self._stop.wait(_SAMPLE_INTERVAL_SECONDS)

    def _sample(self) -> None:
        try:
            processes = (self._root, *self._root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        rss = 0
        for process in processes:
            try:
                with process.oneshot():
                    cpu = process.cpu_times()
                    rss += process.memory_info().rss
                    identity = (process.pid, process.create_time())
                cpu_ms = (cpu.user + cpu.system) * 1_000
                self._cpu_by_process[identity] = max(
                    cpu_ms, self._cpu_by_process.get(identity, 0.0)
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        self.rss_peak_bytes = max(self.rss_peak_bytes, rss)
        self.observations += 1

    @property
    def cpu_ms(self) -> float:
        """Return the sum of the highest observed CPU counter per process."""

        return sum(self._cpu_by_process.values())


def validate_config(config: BenchmarkConfig) -> None:
    """Reject invalid or misleading benchmark configurations.

    Args:
        config: Requested paired-workload settings.

    Raises:
        ValueError: If the requested workload cannot support the probes.
        RuntimeError: If the current platform lacks a POSIX PTY.
    """

    from benchmarks.rust_tui_e2e import BenchmarkConfig as E2EConfig
    from benchmarks.rust_tui_e2e import validate_config as validate_e2e

    validate_e2e(
        E2EConfig(
            rust_binary=config.rust_binary,
            renderers=config.renderers,
            runs=config.runs,
            prompt_words=config.response_words,
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
    if config.stream_interval_ms < 1 or config.input_probes < 1:
        raise ValueError("stream interval and input probes must be positive")
    if config.response_words * config.stream_interval_ms < 5_000:
        raise ValueError("the fake response must stream for at least five seconds")
    # Each streaming probe waits 100 ms before the next one. Leave additional
    # room for paint, two navigation keys, and renderer scheduling variance.
    if config.response_words * config.stream_interval_ms < 3_000 + 150 * config.input_probes:
        raise ValueError("the fake response is too short for the configured input probes")
    if not math.isfinite(config.timeout_seconds):
        raise ValueError("timeout seconds must be finite")


def summarize_samples(samples: Sequence[SessionSample]) -> tuple[ConditionSummary, ...]:
    """Summarize per-renderer and per-history distributions.

    Args:
        samples: Complete session observations.

    Returns:
        Summaries in first-observed condition order.
    """

    if not samples:
        raise ValueError("at least one session sample is required")
    conditions = tuple(dict.fromkeys((s.renderer, s.history_messages) for s in samples))
    result = []
    for renderer, history_messages in conditions:
        selected = tuple(
            sample
            for sample in samples
            if sample.renderer == renderer and sample.history_messages == history_messages
        )
        result.append(
            ConditionSummary(
                renderer=renderer,
                history_messages=history_messages,
                sessions=len(selected),
                idle_input_visible_ms=_distribution(
                    [p.visible_ms for s in selected for p in s.probes if p.phase == "idle"]
                ),
                streaming_input_visible_ms=_distribution(
                    [p.visible_ms for s in selected for p in s.probes if p.phase == "streaming"]
                ),
                page_up_output_activity_ms=_distribution(
                    [
                        n.output_activity_ms
                        for s in selected
                        for n in s.navigation
                        if n.direction == "page_up"
                    ]
                ),
                page_down_output_activity_ms=_distribution(
                    [
                        n.output_activity_ms
                        for s in selected
                        for n in s.navigation
                        if n.direction == "page_down"
                    ]
                ),
                launch_to_ready_ms=_distribution([s.launch_to_ready_ms for s in selected]),
                observed_process_tree_cpu_ms=_distribution(
                    [s.observed_process_tree_cpu_ms for s in selected]
                ),
                observed_simultaneous_rss_peak_bytes=_distribution(
                    [float(s.observed_simultaneous_rss_peak_bytes) for s in selected]
                ),
            )
        )
    return tuple(result)


def run_benchmark(config: BenchmarkConfig | None = None) -> BenchmarkReport:
    """Run fresh and seeded-history sessions in alternating renderer order.

    Args:
        config: Workload settings, or defaults when omitted.

    Returns:
        Raw samples and per-condition distributions.

    Raises:
        RuntimeError: If a marker, live-stream check, clean exit, or terminal
            restoration fails.
    """

    selected = config or BenchmarkConfig()
    validate_config(selected)
    samples = []
    for run_index in range(selected.runs):
        histories = (
            selected.history_messages
            if run_index % 2 == 0
            else tuple(reversed(selected.history_messages))
        )
        for history_messages in histories:
            renderers = (
                selected.renderers if run_index % 2 == 0 else tuple(reversed(selected.renderers))
            )
            for order, renderer in enumerate(renderers, start=1):
                samples.append(
                    _run_sample(
                        selected,
                        renderer=renderer,
                        history_messages=history_messages,
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
    history_messages: int,
    run: int,
    order: int,
) -> SessionSample:
    prompt = START_TOKEN
    with tempfile.TemporaryDirectory(prefix="wisp-tui-interaction-") as temporary:
        root = Path(temporary)
        home, project_dir, session_dir = root / "home", root / "project", root / "sessions"
        for directory in (
            home,
            home / ".config",
            home / ".cache",
            home / ".local" / "share",
            project_dir,
            session_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        history_bytes = _seed_history(session_dir, history_messages) if history_messages else 0
        child_environment = _child_environment(config, renderer=renderer, home=home)
        child_environment["WISP_FAKE_STREAM_INTERVAL_MS"] = str(config.stream_interval_ms)
        child_environment["WISP_FAKE_RESPONSE_WORDS"] = str(config.response_words)
        child_environment["WISP_FAKE_RESPONSE_PREFIX"] = _RESPONSE_PREFIX
        child_environment["WISP_FAKE_RESPONSE_SUFFIX"] = _RESPONSE_SUFFIX
        child_environment["WISP_AUTO_COMPACTION"] = "0"
        command = [
            sys.executable,
            "-m",
            "wisp",
            "tui",
            "--renderer",
            renderer,
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
                os.execve(sys.executable, command, child_environment)
            except BaseException:
                os._exit(126)

        os.close(start_reader)
        observer: _TreeObserver | None = None
        output = bytearray()
        probe_output = bytearray()
        response_output = bytearray()
        settled_output = bytearray()
        terminal_output_bytes = 0
        context_redrawn = False
        history_marker_seen = False
        ready_ns: int | None = None
        submitted_ns: int | None = None
        first_response_ns: int | None = None
        final_response_ns: int | None = None
        settled_ns: int | None = None
        pending_probe: tuple[ProbePhase, int, str, int] | None = None
        probes: list[ProbeSample] = []
        next_probe_after_ns = 0
        navigation: list[NavigationSample] = []
        pending_navigation: tuple[NavigationDirection, int] | None = None
        next_navigation_after_ns = 0
        tail_sent = False
        quit_sent = False
        wait_result = None
        initial_terminal = None
        start_writer_open = True
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
                    if pending_probe is not None:
                        probe_output.extend(chunk)
                    if submitted_ns is not None:
                        response_output.extend(chunk)
                    if final_response_ns is not None:
                        settled_output.extend(chunk)
                now_ns = time.perf_counter_ns()
                if pending_navigation is not None and chunk:
                    direction, sent_ns = pending_navigation
                    if _response_has_final(_plain_text(response_output)):
                        raise RuntimeError(f"{renderer} stream ended before {direction} output")
                    navigation.append(NavigationSample(direction, _elapsed_ms(sent_ns, now_ns)))
                    pending_navigation = None
                    next_navigation_after_ns = now_ns + 50_000_000
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
                    _history_ready(startup_plain, renderer, history_marker_seen)
                    if history_messages
                    else _ready(startup_plain, renderer)
                )
                if context_redrawn and ready_ns is None and ready_visible:
                    ready_ns = now_ns
                    probe_output.clear()
                    pending_probe = _send_probe(terminal_fd, "idle", 0)
                elif ready_ns is not None:
                    probe_plain = _plain_text(probe_output)
                    if pending_probe is not None:
                        phase, index, marker, sent_ns = pending_probe
                        if _marker_painted(probe_plain, marker):
                            if phase == "streaming" and _response_has_final(
                                _plain_text(response_output)
                            ):
                                raise RuntimeError(
                                    f"{renderer} stream completed before probe {index} was visible"
                                )
                            probes.append(ProbeSample(phase, index, _elapsed_ms(sent_ns, now_ns)))
                            os.write(terminal_fd, b"\x7f" * len(marker))
                            next_probe_after_ns = now_ns + 100_000_000
                            probe_output.clear()
                            pending_probe = None
                    idle_count = sum(probe.phase == "idle" for probe in probes)
                    stream_count = sum(probe.phase == "streaming" for probe in probes)
                    if (
                        pending_probe is None
                        and submitted_ns is None
                        and now_ns >= next_probe_after_ns
                    ):
                        if idle_count < config.input_probes:
                            probe_output.clear()
                            pending_probe = _send_probe(terminal_fd, "idle", idle_count)
                        else:
                            submitted_ns = time.perf_counter_ns()
                            response_output.clear()
                            os.write(terminal_fd, f"{prompt}\r".encode())
                    elif submitted_ns is not None:
                        plain = _plain_text(response_output)
                        if first_response_ns is None and _response_prefix_visible(plain):
                            first_response_ns = now_ns
                        if (
                            first_response_ns is not None
                            and final_response_ns is None
                            and _response_has_final(plain)
                        ):
                            final_response_ns = now_ns
                            # Request a complete post-stream frame; the suffix may
                            # cross differential output writes.
                            fcntl.ioctl(
                                terminal_fd,
                                termios.TIOCSWINSZ,
                                struct.pack("HHHH", config.height, config.width + 1, 0, 0),
                            )
                            settled_output.clear()
                        if (
                            first_response_ns is not None
                            and pending_probe is None
                            and now_ns >= next_probe_after_ns
                        ):
                            if stream_count < config.input_probes:
                                if final_response_ns is not None:
                                    raise RuntimeError(
                                        "stream ended before all streaming probes were sent"
                                    )
                                probe_output.clear()
                                pending_probe = _send_probe(terminal_fd, "streaming", stream_count)
                        if (
                            stream_count == config.input_probes
                            and pending_probe is None
                            and pending_navigation is None
                            and len(navigation) < 2
                            and now_ns >= next_navigation_after_ns
                        ):
                            if final_response_ns is not None:
                                raise RuntimeError(
                                    "stream ended before both navigation keys were sent"
                                )
                            direction: NavigationDirection = (
                                "page_up" if not navigation else "page_down"
                            )
                            pending_navigation = _send_navigation(terminal_fd, direction)
                        if (
                            len(navigation) == 2
                            and not tail_sent
                            and now_ns >= next_navigation_after_ns
                        ):
                            # Restore live follow after the measured PageDown; new
                            # deltas may otherwise arrive below the scrolled view.
                            os.write(
                                terminal_fd,
                                b"\x1b[1;5F" if renderer == "rust" else b"\x1b[F",
                            )
                            tail_sent = True
                        if final_response_ns is not None and settled_ns is None:
                            settled_plain = _plain_text(settled_output)
                            if (
                                "idle" if renderer == "rust" else "Ask Wisp anything"
                            ) in settled_plain:
                                settled_ns = now_ns
                        if (
                            settled_ns is not None
                            and stream_count == config.input_probes
                            and len(navigation) == 2
                            and not quit_sent
                        ):
                            os.write(terminal_fd, b"/quit\r")
                            quit_sent = True

                wait_result = _wait_for_child(child_pid, block=False)
                if wait_result is not None:
                    trailing = _drain_available(terminal_fd)
                    terminal_output_bytes += len(trailing)
                    output.extend(trailing)
                    break
                if not chunk:
                    select.select([terminal_fd], [], [], 0.005)
            if wait_result is None:
                raise RuntimeError(
                    f"{renderer} history={history_messages} timed out; "
                    f"ready={ready_ns is not None} submitted={submitted_ns is not None} "
                    f"first={first_response_ns is not None} final={final_response_ns is not None} "
                    f"settled={settled_ns is not None} probes={len(probes)} "
                    f"navigation={len(navigation)} "
                    f"pending={pending_probe[:3] if pending_probe is not None else None}; "
                    f"response output tail={_plain_text(response_output)[-1000:]!r}; "
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
        if exit_code != 0:
            raise RuntimeError(
                f"{renderer} history={history_messages} exited {exit_code}; "
                f"terminal tail={_diagnostic_tail(output)!r}"
            )
        if any(
            value is None
            for value in (ready_ns, submitted_ns, first_response_ns, final_response_ns, settled_ns)
        ):
            raise RuntimeError(
                f"{renderer} exited before response and settlement markers were visible"
            )
        if len(probes) != config.input_probes * 2:
            raise RuntimeError(f"{renderer} exited before every typing probe was visible")
        if len(navigation) != 2:
            raise RuntimeError(f"{renderer} exited before both navigation keys produced output")
        if observer is None or observer.observations == 0 or observer.rss_peak_bytes == 0:
            raise RuntimeError(f"{renderer} process-tree sampling produced no resource evidence")
        if observer.error is not None:
            raise RuntimeError(f"{renderer} process-tree sampling failed") from observer.error
        if not restored:
            raise RuntimeError(f"{renderer} did not restore the terminal")
        ready, submitted, first, final = cast(
            tuple[int, int, int, int],
            (ready_ns, submitted_ns, first_response_ns, final_response_ns),
        )
        return SessionSample(
            renderer=renderer,
            history_messages=history_messages,
            history_bytes=history_bytes,
            run=run,
            order=order,
            launch_to_ready_ms=_elapsed_ms(launched_ns, ready),
            submit_to_first_response_ms=_elapsed_ms(submitted, first),
            submit_to_final_response_ms=_elapsed_ms(submitted, final),
            total_ms=_elapsed_ms(launched_ns, completed_ns),
            terminal_output_bytes=terminal_output_bytes,
            observed_process_tree_cpu_ms=observer.cpu_ms,
            observed_simultaneous_rss_peak_bytes=observer.rss_peak_bytes,
            resource_observations=observer.observations,
            probes=tuple(probes),
            navigation=tuple(navigation),
            clean_exit=True,
            terminal_restored=True,
        )


def _send_probe(
    terminal_fd: int, phase: ProbePhase, index: int
) -> tuple[ProbePhase, int, str, int]:
    if termios.tcgetattr(terminal_fd)[3] & termios.ECHO:
        raise RuntimeError("PTY echo is enabled; input marker timing would be invalid")
    # Unpredictable per-probe text cannot be assembled from prior probe repaints
    # and changing status digits, which would bias the paint timestamp early.
    marker = secrets.token_hex(8).upper()
    sent_ns = time.perf_counter_ns()
    os.write(terminal_fd, marker.encode())
    return phase, index, marker, sent_ns


def _send_navigation(
    terminal_fd: int, direction: NavigationDirection
) -> tuple[NavigationDirection, int]:
    if termios.tcgetattr(terminal_fd)[3] & termios.ECHO:
        raise RuntimeError("PTY echo is enabled; navigation output timing would be invalid")
    sequence = b"\x1b[5~" if direction == "page_up" else b"\x1b[6~"
    sent_ns = time.perf_counter_ns()
    os.write(terminal_fd, sequence)
    return direction, sent_ns


def _marker_painted(output: str, marker: str) -> bool:
    """Recognize a unique twelve-character marker suffix in terminal output.

    Args:
        output: Text extracted from output emitted after the input write.
        marker: Unique ASCII probe marker.

    Returns:
        Whether the random suffix appears contiguously in PTY text. The first
        characters may be split by differential repaint. This is a paint proxy,
        not viewport emulation.
    """

    return marker[-12:] in output


def _response_prefix_visible(output: str) -> bool:
    return _RESPONSE_PREFIX[-12:] in output


def _response_has_final(output: str) -> bool:
    return _RESPONSE_SUFFIX in output[-8192:]


def _history_ready(output: str, renderer: Renderer, history_marker_seen: bool) -> bool:
    if not history_marker_seen:
        return False
    if renderer == "textual":
        return _ready(output, renderer)
    compact = "".join(output.split())
    return (
        "AskWispanything" in compact and "~" in output and ("ctx" in output or "context" in output)
    )


def _parse_renderers(value: str) -> tuple[Renderer, ...]:
    selected = tuple(part.strip() for part in value.split(",") if part.strip())
    if not selected or any(part not in ("rust", "textual") for part in selected):
        raise argparse.ArgumentTypeError("renderers must be rust,textual or a nonempty subset")
    return cast(tuple[Renderer, ...], selected)


def _parse_history(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "history messages must be comma-separated integers"
        ) from exc


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the benchmark and write its JSON result.

    Args:
        arguments: Optional CLI arguments for embedded callers.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-binary", type=Path)
    parser.add_argument("--renderers", type=_parse_renderers, default=("rust", "textual"))
    parser.add_argument("--runs", type=int, default=BenchmarkConfig.runs)
    parser.add_argument("--history-messages", type=_parse_history, default=(0, 10_000))
    parser.add_argument("--response-words", type=int, default=BenchmarkConfig.response_words)
    parser.add_argument(
        "--stream-interval-ms", type=int, default=BenchmarkConfig.stream_interval_ms
    )
    parser.add_argument("--input-probes", type=int, default=BenchmarkConfig.input_probes)
    parser.add_argument("--width", type=int, default=BenchmarkConfig.width)
    parser.add_argument("--height", type=int, default=BenchmarkConfig.height)
    parser.add_argument("--timeout-seconds", type=float, default=BenchmarkConfig.timeout_seconds)
    parser.add_argument("--output", type=Path)
    parsed = parser.parse_args(arguments)
    report = run_benchmark(
        BenchmarkConfig(
            rust_binary=parsed.rust_binary,
            renderers=parsed.renderers,
            runs=parsed.runs,
            history_messages=parsed.history_messages,
            response_words=parsed.response_words,
            stream_interval_ms=parsed.stream_interval_ms,
            input_probes=parsed.input_probes,
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
