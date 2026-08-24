"""Measure Textual interactive input latency while assistant output streams."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import textual
from textual import events
from textual.pilot import Pilot

from benchmarks.support import environment
from benchmarks.tui_stream_hotpaths import TimingDistribution
from wisp.events import ToolApprovalRequested
from wisp.tui.diagnostics import (
    DisplayUpdateDiagnostic,
    InputEventCategory,
    InputLatencyDiagnostic,
    MarkdownDrainDiagnostic,
)
from wisp.tui.rendering import TuiViewSnapshot
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui
from wisp.tui.widgets import PromptEditor, StreamMessage, Transcript

type BenchmarkCondition = Literal["idle", "streaming"]

_INPUT_SAMPLE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class BenchmarkConfig:
    runs: int = 5
    stream_chunks: int = 100
    stream_interval_seconds: float = 0.02
    action_interval_seconds: float = 0.03
    gesture_repetitions: int = 5
    viewport_width: int = 100
    viewport_height: int = 24


@dataclass(frozen=True)
class InputLatencySample:
    run: int
    condition: BenchmarkCondition
    category: InputEventCategory
    handler_ms: float
    queued_ms: float
    display_ms: float
    total_ms: float
    display_kind: str


@dataclass(frozen=True)
class InputLatencySummary:
    condition: BenchmarkCondition
    category: InputEventCategory
    handler: TimingDistribution
    queued: TimingDistribution
    display: TimingDistribution
    total: TimingDistribution


@dataclass(frozen=True)
class StreamingRunSample:
    run: int
    stream_total_ms: float
    stream_flush_ms: float
    produced_chunk_count: int
    expected_source_chars: int
    rendered_source_chars: int
    stream_write_count: int
    source_complete: bool
    reader_remained_parked: bool
    final_following: bool
    final_at_tail: bool


@dataclass(frozen=True)
class InputLatencyReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    summaries: tuple[InputLatencySummary, ...]
    samples: tuple[InputLatencySample, ...]
    streaming_runs: tuple[StreamingRunSample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass
class _InputCollector:
    samples: list[InputLatencyDiagnostic] = field(default_factory=list)
    _sample_added: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def record_input_latency(self, diagnostic: InputLatencyDiagnostic) -> None:
        self.samples.append(diagnostic)
        self._sample_added.set()

    def sample_count(self, category: InputEventCategory) -> int:
        return sum(sample.category == category for sample in self.samples)

    async def wait_for_next(
        self,
        category: InputEventCategory,
        *,
        previous_count: int,
        action: str,
    ) -> None:
        """Wait until an action reaches its first emitted-frame diagnostic."""

        try:
            async with asyncio.timeout(_INPUT_SAMPLE_TIMEOUT_SECONDS):
                while self.sample_count(category) <= previous_count:
                    self._sample_added.clear()
                    if self.sample_count(category) > previous_count:
                        break
                    await self._sample_added.wait()
        except TimeoutError as error:
            raise RuntimeError(
                f"timed out waiting for {category} input diagnostic after {action}"
            ) from error

    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        return

    def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
        return


async def run_benchmark(config: BenchmarkConfig | None = None) -> InputLatencyReport:
    selected = config or BenchmarkConfig()
    _validate_config(selected)
    samples: list[InputLatencySample] = []
    streaming_runs: list[StreamingRunSample] = []
    for run in range(1, selected.runs + 1):
        conditions: tuple[BenchmarkCondition, ...] = (
            ("idle", "streaming") if run % 2 else ("streaming", "idle")
        )
        for condition in conditions:
            condition_samples, streaming_run = await _run_condition(
                selected,
                run=run,
                condition=condition,
            )
            samples.extend(condition_samples)
            if streaming_run is not None:
                streaming_runs.append(streaming_run)
    report_environment = environment()
    report_environment["textual"] = textual.__version__
    return InputLatencyReport(
        config=selected,
        environment=report_environment,
        summaries=_summarize(samples),
        samples=tuple(samples),
        streaming_runs=tuple(streaming_runs),
    )


async def _run_condition(
    config: BenchmarkConfig,
    *,
    run: int,
    condition: BenchmarkCondition,
) -> tuple[tuple[InputLatencySample, ...], StreamingRunSample | None]:
    collector = _InputCollector()
    app, renderer = create_textual_tui(diagnostics=collector)
    assert isinstance(renderer, TextualTuiRenderer)
    async with app.run_test(size=(config.viewport_width, config.viewport_height)) as pilot:
        input_widget = app.query_one("#input", PromptEditor)
        transcript = app.query_one("#transcript", Transcript)
        for index in range(40):
            app.write_message(f"benchmark history row {index}", role="assistant")
        await pilot.pause()
        transcript.scroll_end(animate=False)
        await pilot.pause()
        producer: asyncio.Task[tuple[str, ...]] | None = None
        stream_stop: asyncio.Event | None = None
        minimum_streamed: asyncio.Event | None = None
        stream_started: int | None = None
        if condition == "streaming":
            renderer.view_updated(
                TuiViewSnapshot(
                    status="running",
                    input_hint="wisp(running)> ",
                    input_mode="running",
                )
            )
            renderer.running()
            stream_stop = asyncio.Event()
            minimum_streamed = asyncio.Event()
            stream_started = time.perf_counter_ns()
            producer = asyncio.create_task(_stream(renderer, config, stream_stop, minimum_streamed))
        reader_remained_parked = False
        streaming_run: StreamingRunSample | None = None
        try:
            reader_remained_parked = await _exercise_inputs(
                app,
                renderer,
                pilot,
                input_widget,
                transcript,
                collector,
                config,
            )
        finally:
            if (
                producer is not None
                and stream_stop is not None
                and minimum_streamed is not None
                and stream_started is not None
            ):
                await minimum_streamed.wait()
                flush_started = time.perf_counter_ns()
                stream_stop.set()
                chunks = await producer
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                await pilot.pause()
                completed = app.stream_widget_for_completed_message()
                expected_source = "".join(chunks)
                rendered_source = completed.source if isinstance(completed, StreamMessage) else ""
                streaming_run = StreamingRunSample(
                    run=run,
                    stream_total_ms=(time.perf_counter_ns() - stream_started) / 1_000_000,
                    stream_flush_ms=(time.perf_counter_ns() - flush_started) / 1_000_000,
                    produced_chunk_count=len(chunks),
                    expected_source_chars=len(expected_source),
                    rendered_source_chars=len(rendered_source),
                    stream_write_count=app.last_stream_write_count,
                    source_complete=(
                        isinstance(completed, StreamMessage) and rendered_source == expected_source
                    ),
                    reader_remained_parked=reader_remained_parked,
                    final_following=transcript.is_following,
                    final_at_tail=transcript.scroll_y >= transcript.max_scroll_y - 3,
                )
        await pilot.pause()
    _validate_condition_samples(collector.samples, config)
    return (
        tuple(
            InputLatencySample(
                run=run,
                condition=condition,
                category=sample.category,
                handler_ms=sample.handler_seconds * 1_000,
                queued_ms=sample.queued_seconds * 1_000,
                display_ms=sample.display_seconds * 1_000,
                total_ms=sample.total_seconds * 1_000,
                display_kind=sample.display_kind,
            )
            for sample in collector.samples
        ),
        streaming_run,
    )


async def _stream(
    renderer: TextualTuiRenderer,
    config: BenchmarkConfig,
    stop: asyncio.Event,
    minimum_streamed: asyncio.Event,
) -> tuple[str, ...]:
    index = 0
    chunks: list[str] = []
    while not stop.is_set():
        section = index % config.stream_chunks
        chunk = f"## streamed section {section}\n\nbenchmark output\n\n"
        renderer.token_delta(chunk)
        chunks.append(chunk)
        index += 1
        if index >= config.stream_chunks:
            minimum_streamed.set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.stream_interval_seconds)
        except TimeoutError:
            pass
    return tuple(chunks)


async def _exercise_inputs(
    app: TextualTui,
    renderer: TextualTuiRenderer,
    pilot: Pilot[object],
    input_widget: PromptEditor,
    transcript: Transcript,
    collector: _InputCollector,
    config: BenchmarkConfig,
) -> bool:
    delay = config.action_interval_seconds
    for key in "latency":
        await _press_and_wait(pilot, collector, key=key, category="typing")
        await asyncio.sleep(delay)
    reader_remained_parked = True
    for _ in range(config.gesture_repetitions):
        await _press_and_wait(pilot, collector, key="left", category="cursor")
        await asyncio.sleep(delay)
        await _press_and_wait(pilot, collector, key="right", category="cursor")
        await asyncio.sleep(delay)

        await _press_and_wait(pilot, collector, key="pageup", category="navigation")
        await asyncio.sleep(delay)
        page_up_following = transcript.is_following
        transcript.scroll_end(animate=False)
        await pilot.pause()

        await _wheel_up_and_wait(app, transcript, collector)
        await asyncio.sleep(delay)
        wheel_following = transcript.is_following
        reader_remained_parked = reader_remained_parked and _reader_remained_parked(
            page_up_following=page_up_following,
            wheel_following=wheel_following,
        )
        transcript.scroll_end(animate=False)
        await pilot.pause()

    input_widget.value = "steer benchmark"
    await _press_and_wait(pilot, collector, key="enter", category="submission")
    await asyncio.sleep(delay)
    renderer.approval_request(
        ToolApprovalRequested(
            call_id="benchmark-approval",
            name="bash",
            arguments={"command": "printf benchmark"},
            safety="command",
        )
    )
    await pilot.pause()
    await _press_and_wait(pilot, collector, key="down", category="approval")
    await asyncio.sleep(delay)
    app.hide_decision()
    await pilot.pause()

    previous_cancellations = collector.sample_count("cancellation")
    await pilot.press("escape")
    renderer.cancelling("benchmark cancellation requested")
    await collector.wait_for_next(
        "cancellation",
        previous_count=previous_cancellations,
        action="escape",
    )
    await pilot.pause()
    return reader_remained_parked


async def _press_and_wait(
    pilot: Pilot[object],
    collector: _InputCollector,
    *,
    key: str,
    category: InputEventCategory,
) -> None:
    previous_count = collector.sample_count(category)
    await pilot.press(key)
    await collector.wait_for_next(
        category,
        previous_count=previous_count,
        action=key,
    )


async def _wheel_up_and_wait(
    app: TextualTui,
    transcript: Transcript,
    collector: _InputCollector,
) -> None:
    previous_count = collector.sample_count("wheel")
    origin = transcript.region.offset
    wheel = events.MouseScrollUp(
        widget=transcript,
        x=origin.x,
        y=origin.y,
        delta_x=0,
        delta_y=0,
        button=0,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=origin.x,
        screen_y=origin.y,
    )
    wheel.set_sender(app)
    await app.on_event(wheel)
    await collector.wait_for_next(
        "wheel",
        previous_count=previous_count,
        action="wheel up",
    )


def _reader_remained_parked(*, page_up_following: bool, wheel_following: bool) -> bool:
    return not page_up_following and not wheel_following


def _validate_condition_samples(
    samples: Sequence[InputLatencyDiagnostic],
    config: BenchmarkConfig,
) -> None:
    expected: Counter[InputEventCategory] = Counter(
        {
            "typing": len("latency"),
            "cursor": config.gesture_repetitions * 2,
            "navigation": config.gesture_repetitions,
            "wheel": config.gesture_repetitions,
            "submission": 1,
            "approval": 1,
            "cancellation": 1,
        }
    )
    observed: Counter[InputEventCategory] = Counter(sample.category for sample in samples)
    if observed != expected:
        raise RuntimeError(
            "unexpected input diagnostic counts: "
            f"expected {dict(expected)}, observed {dict(observed)}"
        )


def _summarize(samples: Sequence[InputLatencySample]) -> tuple[InputLatencySummary, ...]:
    summaries: list[InputLatencySummary] = []
    conditions: tuple[BenchmarkCondition, ...] = ("idle", "streaming")
    categories: tuple[InputEventCategory, ...] = (
        "typing",
        "cursor",
        "navigation",
        "wheel",
        "submission",
        "approval",
        "cancellation",
        "paste",
    )
    for condition in conditions:
        for category in categories:
            selected = [
                sample
                for sample in samples
                if sample.condition == condition and sample.category == category
            ]
            if not selected:
                continue
            summaries.append(
                InputLatencySummary(
                    condition=condition,
                    category=category,
                    handler=TimingDistribution.from_samples(
                        tuple(sample.handler_ms for sample in selected)
                    ),
                    queued=TimingDistribution.from_samples(
                        tuple(sample.queued_ms for sample in selected)
                    ),
                    display=TimingDistribution.from_samples(
                        tuple(sample.display_ms for sample in selected)
                    ),
                    total=TimingDistribution.from_samples(
                        tuple(sample.total_ms for sample in selected)
                    ),
                )
            )
    return tuple(summaries)


def _validate_config(config: BenchmarkConfig) -> None:
    if config.runs < 1 or config.stream_chunks < 1 or config.gesture_repetitions < 1:
        raise ValueError("runs, stream_chunks, and gesture_repetitions must be positive")
    if config.stream_interval_seconds <= 0 or config.action_interval_seconds <= 0:
        raise ValueError("stream and action intervals must be positive")
    if config.viewport_width < 1 or config.viewport_height < 1:
        raise ValueError("viewport dimensions must be positive")


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=BenchmarkConfig.runs)
    parser.add_argument("--stream-chunks", type=int, default=BenchmarkConfig.stream_chunks)
    parser.add_argument(
        "--stream-interval-seconds",
        type=float,
        default=BenchmarkConfig.stream_interval_seconds,
    )
    parser.add_argument(
        "--action-interval-seconds",
        type=float,
        default=BenchmarkConfig.action_interval_seconds,
    )
    parser.add_argument(
        "--gesture-repetitions",
        type=int,
        default=BenchmarkConfig.gesture_repetitions,
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = await run_benchmark(
        BenchmarkConfig(
            runs=parsed.runs,
            stream_chunks=parsed.stream_chunks,
            stream_interval_seconds=parsed.stream_interval_seconds,
            action_interval_seconds=parsed.action_interval_seconds,
            gesture_repetitions=parsed.gesture_repetitions,
        )
    )
    payload = report.to_json()
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


def main() -> None:
    asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
