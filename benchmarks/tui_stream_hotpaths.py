"""Measure Textual layout and rendering hotpaths during assistant streaming."""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import math
import statistics
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from unittest.mock import patch

import textual
from textual.geometry import Size
from textual.pilot import Pilot
from textual.screen import Screen

from benchmarks.support import environment
from benchmarks.tui_long_session import append_benchmark_messages
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.history import history_entries_from_rpc_messages
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui
from wisp.tui.widgets import StreamMessage, Transcript


@dataclass(frozen=True)
class TimingDistribution:
    """A compact distribution that remains useful for short benchmark samples."""

    sample_count: int
    total_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> TimingDistribution:
        ordered = sorted(samples)
        if not ordered:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return cls(
            sample_count=len(ordered),
            total_ms=sum(ordered),
            p50_ms=_nearest_rank(ordered, 0.50),
            p95_ms=_nearest_rank(ordered, 0.95),
            p99_ms=_nearest_rank(ordered, 0.99),
            max_ms=ordered[-1],
        )


@dataclass(frozen=True)
class BenchmarkConfig:
    message_count: int = 400
    mounted_history_entries: tuple[int, ...] = (75, 120, 300)
    stream_chunks: int = 100
    stream_interval_seconds: float = 0.02
    heartbeat_interval_seconds: float = 0.01
    viewport_width: int = 100
    viewport_height: int = 12
    runs: int = 5


@dataclass(frozen=True)
class StreamHotpathSample:
    run: int
    mounted_history_entries: int
    mounted_widget_count: int
    stream_total_ms: float
    stream_update_count: int
    event_loop_delay: TimingDistribution
    layout_passes: TimingDistribution
    compositor_renders: TimingDistribution
    final_following: bool
    final_at_tail: bool
    source_complete: bool


@dataclass(frozen=True)
class StreamHotpathSummary:
    mounted_history_entries: int
    sample_count: int
    stream_total_median_ms: float
    event_loop_p95_median_ms: float
    event_loop_max_median_ms: float
    layout_total_median_ms: float
    compositor_total_median_ms: float


@dataclass(frozen=True)
class StreamHotpathReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    summaries: tuple[StreamHotpathSummary, ...]
    samples: tuple[StreamHotpathSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass
class _TimingCollector:
    target_screen: Screen[object]
    layout_ms: list[float]
    compositor_ms: list[float]


def _nearest_rank(ordered: Sequence[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _milliseconds(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000


@contextmanager
def _measure_textual_hotpaths(collector: _TimingCollector) -> Iterator[None]:
    """Time private Textual seams only while one benchmark stream is active."""

    original_layout = Screen._refresh_layout
    original_compositor = Screen._compositor_refresh

    def refresh_layout(
        screen: Screen[object], size: Size | None = None, scroll: bool = False
    ) -> None:
        started = time.perf_counter_ns()
        try:
            original_layout(screen, size, scroll)
        finally:
            if screen is collector.target_screen:
                collector.layout_ms.append(_milliseconds(started))

    def compositor_refresh(screen: Screen[object]) -> None:
        started = time.perf_counter_ns()
        try:
            original_compositor(screen)
        finally:
            if screen is collector.target_screen:
                collector.compositor_ms.append(_milliseconds(started))

    with (
        patch.object(Screen, "_refresh_layout", refresh_layout),
        patch.object(Screen, "_compositor_refresh", compositor_refresh),
    ):
        yield


async def _heartbeat(
    interval_seconds: float,
    stopped: asyncio.Event,
    delays_ms: list[float],
) -> None:
    """Record delay from absolute deadlines without accumulating timer drift."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval_seconds
    while not stopped.is_set():
        try:
            await asyncio.wait_for(stopped.wait(), timeout=max(0.0, deadline - loop.time()))
        except TimeoutError:
            now = loop.time()
            delay = max(0.0, now - deadline)
            delays_ms.append(delay * 1_000)
            skipped_intervals = math.floor(delay / interval_seconds) + 1
            deadline += skipped_intervals * interval_seconds


async def run_benchmark(
    config: BenchmarkConfig | None = None,
    *,
    profile_output: Path | None = None,
) -> StreamHotpathReport:
    """Run the repeated mounted-history matrix through production TUI streaming."""

    selected = config or BenchmarkConfig()
    _validate_config(selected, profile_output=profile_output)
    samples: list[StreamHotpathSample] = []
    orders = _rotated_orders(selected.mounted_history_entries, selected.runs)
    for run, order in enumerate(orders, start=1):
        for mounted_history_entries in order:
            samples.append(
                await _run_sample(
                    selected,
                    run=run,
                    mounted_history_entries=mounted_history_entries,
                    profile_output=profile_output,
                )
            )
    report_environment = environment()
    report_environment["textual"] = textual.__version__
    return StreamHotpathReport(
        config=selected,
        environment=report_environment,
        summaries=_summarize(samples, selected.mounted_history_entries),
        samples=tuple(samples),
    )


async def _run_sample(
    config: BenchmarkConfig,
    *,
    run: int,
    mounted_history_entries: int,
    profile_output: Path | None,
) -> StreamHotpathSample:
    with tempfile.TemporaryDirectory(prefix="wisp-tui-hotpaths-") as temporary_directory:
        store = JsonlSessionStore(Path(temporary_directory))
        session = store.create()
        await append_benchmark_messages(session, config.message_count)
        page = session.read_message_page(limit=mounted_history_entries)
        entries = history_entries_from_rpc_messages(page.messages)
        app, renderer = create_textual_tui()
        assert isinstance(renderer, TextualTuiRenderer)
        async with app.run_test(size=(config.viewport_width, config.viewport_height)) as pilot:
            renderer.replace_history_entries(entries, session_label="Streaming hotpath benchmark")
            await app.wait_for_history_render()
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.return_to_latest()
            await pilot.pause()
            retained_count = renderer.retained_history_entry_count
            if retained_count != mounted_history_entries:
                raise RuntimeError(
                    "Streaming hotpath fixture mounted "
                    f"{retained_count} history entries instead of {mounted_history_entries}"
                )
            mounted_widget_count = len(transcript.children)
            return await _measure_stream(
                app,
                renderer,
                pilot,
                transcript,
                config=config,
                run=run,
                mounted_history_entries=mounted_history_entries,
                mounted_widget_count=mounted_widget_count,
                profile_output=profile_output,
            )


async def _measure_stream(
    app: TextualTui,
    renderer: TextualTuiRenderer,
    pilot: Pilot[None],
    transcript: Transcript,
    *,
    config: BenchmarkConfig,
    run: int,
    mounted_history_entries: int,
    mounted_widget_count: int,
    profile_output: Path | None,
) -> StreamHotpathSample:
    collector = _TimingCollector(app.screen, [], [])
    heartbeat_delays: list[float] = []
    heartbeat_stopped = asyncio.Event()
    profiler = cProfile.Profile() if profile_output is not None else None
    chunks = tuple(
        f"## Stream section {index}\n\n- benchmark item {index}\n\n"
        for index in range(config.stream_chunks)
    )
    heartbeat_task = asyncio.create_task(
        _heartbeat(config.heartbeat_interval_seconds, heartbeat_stopped, heartbeat_delays)
    )
    await asyncio.sleep(0)
    started = time.perf_counter_ns()
    try:
        with _measure_textual_hotpaths(collector):
            if profiler is not None:
                profiler.enable()
            try:
                for chunk in chunks:
                    renderer.token_delta(chunk)
                    await asyncio.sleep(config.stream_interval_seconds)
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                await pilot.pause()
            finally:
                if profiler is not None:
                    profiler.disable()
    finally:
        heartbeat_stopped.set()
        await heartbeat_task
    stream_total_ms = _milliseconds(started)
    if profiler is not None and profile_output is not None:
        profiler.dump_stats(profile_output)
    completed = app.stream_widget_for_completed_message()
    source_complete = isinstance(completed, StreamMessage) and completed.source == "".join(chunks)
    return StreamHotpathSample(
        run=run,
        mounted_history_entries=mounted_history_entries,
        mounted_widget_count=mounted_widget_count,
        stream_total_ms=stream_total_ms,
        stream_update_count=app.last_stream_write_count,
        event_loop_delay=TimingDistribution.from_samples(heartbeat_delays),
        layout_passes=TimingDistribution.from_samples(collector.layout_ms),
        compositor_renders=TimingDistribution.from_samples(collector.compositor_ms),
        final_following=transcript.is_following,
        final_at_tail=transcript.scroll_y >= transcript.max_scroll_y - 3,
        source_complete=source_complete,
    )


def _rotated_orders(values: tuple[int, ...], runs: int) -> tuple[tuple[int, ...], ...]:
    return tuple(
        values[(run - 1) % len(values) :] + values[: (run - 1) % len(values)]
        for run in range(1, runs + 1)
    )


def _summarize(
    samples: Sequence[StreamHotpathSample],
    conditions: tuple[int, ...],
) -> tuple[StreamHotpathSummary, ...]:
    summaries = []
    for condition in conditions:
        selected = [sample for sample in samples if sample.mounted_history_entries == condition]
        summaries.append(
            StreamHotpathSummary(
                mounted_history_entries=condition,
                sample_count=len(selected),
                stream_total_median_ms=statistics.median(
                    sample.stream_total_ms for sample in selected
                ),
                event_loop_p95_median_ms=statistics.median(
                    sample.event_loop_delay.p95_ms for sample in selected
                ),
                event_loop_max_median_ms=statistics.median(
                    sample.event_loop_delay.max_ms for sample in selected
                ),
                layout_total_median_ms=statistics.median(
                    sample.layout_passes.total_ms for sample in selected
                ),
                compositor_total_median_ms=statistics.median(
                    sample.compositor_renders.total_ms for sample in selected
                ),
            )
        )
    return tuple(summaries)


def _validate_config(config: BenchmarkConfig, *, profile_output: Path | None) -> None:
    positive_integers = (
        config.message_count,
        config.stream_chunks,
        config.viewport_width,
        config.viewport_height,
        config.runs,
    )
    if any(value < 1 for value in positive_integers):
        raise ValueError(
            "message_count, stream_chunks, viewport dimensions, and runs must be positive"
        )
    if not config.mounted_history_entries or any(
        count < 1 or count > config.message_count for count in config.mounted_history_entries
    ):
        raise ValueError(
            "mounted history entries must be positive and no larger than message_count"
        )
    if config.stream_interval_seconds <= 0 or config.heartbeat_interval_seconds <= 0:
        raise ValueError("stream and heartbeat intervals must be positive")
    if profile_output is not None and (
        config.runs != 1 or len(config.mounted_history_entries) != 1
    ):
        raise ValueError("profiling requires exactly one run and one mounted-history value")


def _parse_positive_csv(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("values must be comma-separated integers") from error
    if not values or any(item < 1 for item in values):
        raise argparse.ArgumentTypeError("values must be positive integers")
    return values


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=BenchmarkConfig.message_count)
    parser.add_argument(
        "--mounted-history",
        type=_parse_positive_csv,
        default=BenchmarkConfig.mounted_history_entries,
    )
    parser.add_argument("--stream-chunks", type=int, default=BenchmarkConfig.stream_chunks)
    parser.add_argument(
        "--stream-interval-seconds",
        type=float,
        default=BenchmarkConfig.stream_interval_seconds,
    )
    parser.add_argument(
        "--heartbeat-interval-seconds",
        type=float,
        default=BenchmarkConfig.heartbeat_interval_seconds,
    )
    parser.add_argument("--runs", type=int, default=BenchmarkConfig.runs)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--profile-output", type=Path)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = await run_benchmark(
        BenchmarkConfig(
            message_count=parsed.messages,
            mounted_history_entries=parsed.mounted_history,
            stream_chunks=parsed.stream_chunks,
            stream_interval_seconds=parsed.stream_interval_seconds,
            heartbeat_interval_seconds=parsed.heartbeat_interval_seconds,
            runs=parsed.runs,
        ),
        profile_output=parsed.profile_output,
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


def main() -> None:
    asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
