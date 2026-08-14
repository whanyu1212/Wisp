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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from unittest.mock import patch
from weakref import WeakKeyDictionary

import textual
from rich.console import Console, ConsoleOptions, RenderResult
from textual.geometry import Region, Size
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widget import Widget

from benchmarks.support import environment
from benchmarks.tui_long_session import append_benchmark_messages
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tui.history import history_entries_from_rpc_messages
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui
from wisp.tui.widgets import (
    StreamMessage,
    Transcript,
    WorkingIndicator,
    _SafeAssistantMarkdown,
)


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
    retained_history_entries: tuple[int, ...] = (75, 120, 300)
    stream_chunks: int = 100
    stream_interval_seconds: float = 0.02
    heartbeat_interval_seconds: float = 0.01
    viewport_width: int = 100
    viewport_height: int = 12
    runs: int = 5


@dataclass(frozen=True)
class MarkdownRenderCounts:
    """Rich Markdown visual renders split by mutable and settled widgets."""

    active: int
    settled: int
    total: int


@dataclass(frozen=True)
class StreamHotpathSample:
    run: int
    retained_history_entries: int
    mounted_history_entries: int
    mounted_widget_count: int
    stream_total_ms: float
    stream_cpu_ms: float
    stream_update_count: int
    layout_request_count: int
    layout_requests: dict[str, int]
    layout_passes_per_stream_update: float
    content_height_call_count: int
    content_height_calls: dict[str, int]
    markdown_renders: MarkdownRenderCounts
    markdown_source_rebuild_count: int
    event_loop_delay: TimingDistribution
    layout_passes: TimingDistribution
    compositor_renders: TimingDistribution
    working_indicator_active: bool
    final_following: bool
    final_at_tail: bool
    source_complete: bool


@dataclass(frozen=True)
class StreamHotpathSummary:
    retained_history_entries: int
    mounted_history_entries: int
    sample_count: int
    stream_total_median_ms: float
    stream_cpu_median_ms: float
    layout_request_count_median: float
    layout_passes_per_stream_update_median: float
    content_height_call_count_median: float
    active_markdown_render_median: float
    settled_markdown_render_median: float
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
class _HotpathCollector:
    target_screen: Screen[object]
    settled_stream_messages: set[StreamMessage]
    layout_ms: list[float] = field(default_factory=list)
    compositor_ms: list[float] = field(default_factory=list)
    layout_requests: dict[str, int] = field(default_factory=dict)
    content_height_calls: dict[str, int] = field(default_factory=dict)
    markdown_owners: WeakKeyDictionary[_SafeAssistantMarkdown, StreamMessage] = field(
        default_factory=WeakKeyDictionary
    )
    active_markdown_renders: int = 0
    settled_markdown_renders: int = 0
    markdown_source_rebuild_count: int = 0


def _nearest_rank(ordered: Sequence[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _milliseconds(started_ns: int) -> float:
    return (time.perf_counter_ns() - started_ns) / 1_000_000


def _is_on_target_screen(widget: Widget, target_screen: Screen[object]) -> bool:
    return widget.is_mounted and widget.screen is target_screen


@contextmanager
def _measure_textual_hotpaths(collector: _HotpathCollector) -> Iterator[None]:
    """Measure private Textual seams only while one benchmark stream is active."""

    original_layout = Screen._refresh_layout
    original_compositor = Screen._compositor_refresh
    original_refresh = Widget.refresh
    original_content_height = Widget.get_content_height
    original_markdown_render = _SafeAssistantMarkdown.__rich_console__
    original_source_render = StreamMessage._render_source

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

    def refresh(
        widget: Widget,
        *regions: Region,
        repaint: bool = True,
        layout: bool = False,
        recompose: bool = False,
    ) -> Widget:
        if layout and _is_on_target_screen(widget, collector.target_screen):
            widget_name = type(widget).__name__
            collector.layout_requests[widget_name] = (
                collector.layout_requests.get(widget_name, 0) + 1
            )
        return original_refresh(
            widget,
            *regions,
            repaint=repaint,
            layout=layout,
            recompose=recompose,
        )

    def get_content_height(
        widget: Widget,
        container: Size,
        viewport: Size,
        width: int,
    ) -> int:
        if _is_on_target_screen(widget, collector.target_screen):
            widget_name = type(widget).__name__
            collector.content_height_calls[widget_name] = (
                collector.content_height_calls.get(widget_name, 0) + 1
            )
        return original_content_height(widget, container, viewport, width)

    def render_markdown(
        markdown: _SafeAssistantMarkdown,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        widget = collector.markdown_owners.get(markdown)
        if widget is not None and _is_on_target_screen(widget, collector.target_screen):
            if widget in collector.settled_stream_messages:
                collector.settled_markdown_renders += 1
            else:
                collector.active_markdown_renders += 1
        yield from original_markdown_render(markdown, console, options)

    def render_source(widget: StreamMessage) -> None:
        if _is_on_target_screen(widget, collector.target_screen):
            collector.markdown_source_rebuild_count += 1
        original_source_render(widget)
        visual = widget._selection_visual
        if visual is not None and isinstance(visual._markdown_renderable, _SafeAssistantMarkdown):
            collector.markdown_owners[visual._markdown_renderable] = widget

    with (
        patch.object(Screen, "_refresh_layout", refresh_layout),
        patch.object(Screen, "_compositor_refresh", compositor_refresh),
        patch.object(Widget, "refresh", refresh),
        patch.object(Widget, "get_content_height", get_content_height),
        patch.object(_SafeAssistantMarkdown, "__rich_console__", render_markdown),
        patch.object(StreamMessage, "_render_source", render_source),
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
    orders = _rotated_orders(selected.retained_history_entries, selected.runs)
    for run, order in enumerate(orders, start=1):
        for retained_history_entries in order:
            samples.append(
                await _run_sample(
                    selected,
                    run=run,
                    retained_history_entries=retained_history_entries,
                    profile_output=profile_output,
                )
            )
    report_environment = environment()
    report_environment["textual"] = textual.__version__
    return StreamHotpathReport(
        config=selected,
        environment=report_environment,
        summaries=_summarize(samples, selected.retained_history_entries),
        samples=tuple(samples),
    )


async def _run_sample(
    config: BenchmarkConfig,
    *,
    run: int,
    retained_history_entries: int,
    profile_output: Path | None,
) -> StreamHotpathSample:
    with tempfile.TemporaryDirectory(prefix="wisp-tui-hotpaths-") as temporary_directory:
        store = JsonlSessionStore(Path(temporary_directory))
        session = store.create()
        await append_benchmark_messages(session, config.message_count)
        page = session.read_message_page(limit=retained_history_entries)
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
            if retained_count != retained_history_entries:
                raise RuntimeError(
                    "Streaming hotpath fixture retained "
                    f"{retained_count} history entries instead of {retained_history_entries}"
                )
            # Match the production command lifecycle: TuiShell calls running()
            # before delivering tokens, leaving this 80 ms heartbeat mounted at
            # the transcript tail for the complete streaming phase.
            renderer.running()
            await pilot.pause()
            indicator = app.query_one(WorkingIndicator)
            if indicator.parent is not transcript or transcript.children[-1] is not indicator:
                raise RuntimeError("Working indicator did not settle at the transcript tail")
            mounted_widget_count = len(transcript.children)
            # This fixture has exactly one session marker and one working indicator
            # outside its mounted historical messages.
            mounted_history_entries = mounted_widget_count - 2
            if mounted_history_entries < 1 or mounted_history_entries > retained_count:
                raise RuntimeError(
                    "Streaming hotpath fixture mounted an invalid history count: "
                    f"{mounted_history_entries} of {retained_count} retained entries"
                )
            try:
                return await _measure_stream(
                    app,
                    renderer,
                    pilot,
                    transcript,
                    indicator,
                    config=config,
                    run=run,
                    retained_history_entries=retained_history_entries,
                    mounted_history_entries=mounted_history_entries,
                    mounted_widget_count=mounted_widget_count,
                    profile_output=profile_output,
                )
            finally:
                app.hide_working_indicator()
                await pilot.pause()


async def _measure_stream(
    app: TextualTui,
    renderer: TextualTuiRenderer,
    pilot: Pilot[None],
    transcript: Transcript,
    indicator: WorkingIndicator,
    *,
    config: BenchmarkConfig,
    run: int,
    retained_history_entries: int,
    mounted_history_entries: int,
    mounted_widget_count: int,
    profile_output: Path | None,
) -> StreamHotpathSample:
    settled_stream_messages = set(transcript.query(StreamMessage))
    collector = _HotpathCollector(
        target_screen=app.screen,
        settled_stream_messages=settled_stream_messages,
    )
    for widget in settled_stream_messages:
        visual = widget._selection_visual
        if visual is not None and isinstance(visual._markdown_renderable, _SafeAssistantMarkdown):
            collector.markdown_owners[visual._markdown_renderable] = widget
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
    started_cpu = time.process_time_ns()
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
        stream_cpu_ms = (time.process_time_ns() - started_cpu) / 1_000_000
        heartbeat_stopped.set()
        await heartbeat_task
    stream_total_ms = _milliseconds(started)
    if profiler is not None and profile_output is not None:
        profiler.dump_stats(profile_output)
    completed = app.stream_widget_for_completed_message()
    source_complete = isinstance(completed, StreamMessage) and completed.source == "".join(chunks)
    stream_update_count = app.last_stream_write_count
    return StreamHotpathSample(
        run=run,
        retained_history_entries=retained_history_entries,
        mounted_history_entries=mounted_history_entries,
        mounted_widget_count=mounted_widget_count,
        stream_total_ms=stream_total_ms,
        stream_cpu_ms=stream_cpu_ms,
        stream_update_count=stream_update_count,
        layout_request_count=sum(collector.layout_requests.values()),
        layout_requests=dict(sorted(collector.layout_requests.items())),
        layout_passes_per_stream_update=(
            len(collector.layout_ms) / stream_update_count if stream_update_count else 0.0
        ),
        content_height_call_count=sum(collector.content_height_calls.values()),
        content_height_calls=dict(sorted(collector.content_height_calls.items())),
        markdown_renders=MarkdownRenderCounts(
            active=collector.active_markdown_renders,
            settled=collector.settled_markdown_renders,
            total=collector.active_markdown_renders + collector.settled_markdown_renders,
        ),
        markdown_source_rebuild_count=collector.markdown_source_rebuild_count,
        event_loop_delay=TimingDistribution.from_samples(heartbeat_delays),
        layout_passes=TimingDistribution.from_samples(collector.layout_ms),
        compositor_renders=TimingDistribution.from_samples(collector.compositor_ms),
        working_indicator_active=(
            indicator.is_mounted
            and indicator.parent is transcript
            and app._transcript_controller.working_indicator is indicator
        ),
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
        selected = [sample for sample in samples if sample.retained_history_entries == condition]
        mounted_counts = {sample.mounted_history_entries for sample in selected}
        if len(mounted_counts) != 1:
            raise RuntimeError(
                f"Retained-history condition {condition} produced inconsistent mounted counts"
            )
        summaries.append(
            StreamHotpathSummary(
                retained_history_entries=condition,
                mounted_history_entries=mounted_counts.pop(),
                sample_count=len(selected),
                stream_total_median_ms=statistics.median(
                    sample.stream_total_ms for sample in selected
                ),
                stream_cpu_median_ms=statistics.median(sample.stream_cpu_ms for sample in selected),
                layout_request_count_median=statistics.median(
                    sample.layout_request_count for sample in selected
                ),
                layout_passes_per_stream_update_median=statistics.median(
                    sample.layout_passes_per_stream_update for sample in selected
                ),
                content_height_call_count_median=statistics.median(
                    sample.content_height_call_count for sample in selected
                ),
                active_markdown_render_median=statistics.median(
                    sample.markdown_renders.active for sample in selected
                ),
                settled_markdown_render_median=statistics.median(
                    sample.markdown_renders.settled for sample in selected
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
    if not config.retained_history_entries or any(
        count < 1 or count > config.message_count for count in config.retained_history_entries
    ):
        raise ValueError(
            "retained history entries must be positive and no larger than message_count"
        )
    if config.stream_interval_seconds <= 0 or config.heartbeat_interval_seconds <= 0:
        raise ValueError("stream and heartbeat intervals must be positive")
    if profile_output is not None and (
        config.runs != 1 or len(config.retained_history_entries) != 1
    ):
        raise ValueError("profiling requires exactly one run and one retained-history value")


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
        "--retained-history",
        "--mounted-history",
        dest="retained_history",
        type=_parse_positive_csv,
        default=BenchmarkConfig.retained_history_entries,
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
            retained_history_entries=parsed.retained_history,
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
