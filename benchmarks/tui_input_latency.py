"""Measure Textual interactive input latency while assistant output streams."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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
from wisp.tui.widgets import PromptEditor, Transcript

type BenchmarkCondition = Literal["idle", "streaming"]


@dataclass(frozen=True)
class BenchmarkConfig:
    runs: int = 5
    stream_chunks: int = 100
    stream_interval_seconds: float = 0.02
    action_interval_seconds: float = 0.03
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
class InputLatencyReport:
    config: BenchmarkConfig
    environment: dict[str, str]
    summaries: tuple[InputLatencySummary, ...]
    samples: tuple[InputLatencySample, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass
class _InputCollector:
    samples: list[InputLatencyDiagnostic] = field(default_factory=list)

    def record_input_latency(self, diagnostic: InputLatencyDiagnostic) -> None:
        self.samples.append(diagnostic)

    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        return

    def record_display_update(self, _diagnostic: DisplayUpdateDiagnostic) -> None:
        return


async def run_benchmark(config: BenchmarkConfig | None = None) -> InputLatencyReport:
    selected = config or BenchmarkConfig()
    _validate_config(selected)
    samples: list[InputLatencySample] = []
    for run in range(1, selected.runs + 1):
        conditions: tuple[BenchmarkCondition, ...] = (
            ("idle", "streaming") if run % 2 else ("streaming", "idle")
        )
        for condition in conditions:
            samples.extend(await _run_condition(selected, run=run, condition=condition))
    report_environment = environment()
    report_environment["textual"] = textual.__version__
    return InputLatencyReport(
        config=selected,
        environment=report_environment,
        summaries=_summarize(samples),
        samples=tuple(samples),
    )


async def _run_condition(
    config: BenchmarkConfig,
    *,
    run: int,
    condition: BenchmarkCondition,
) -> tuple[InputLatencySample, ...]:
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
        producer: asyncio.Task[None] | None = None
        stream_stop: asyncio.Event | None = None
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
            producer = asyncio.create_task(_stream(renderer, config, stream_stop))
        try:
            await _exercise_inputs(app, renderer, pilot, input_widget, transcript, config)
        finally:
            if producer is not None and stream_stop is not None:
                stream_stop.set()
                await producer
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
        await pilot.pause()
    return tuple(
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
    )


async def _stream(
    renderer: TextualTuiRenderer,
    config: BenchmarkConfig,
    stop: asyncio.Event,
) -> None:
    index = 0
    while not stop.is_set():
        section = index % config.stream_chunks
        renderer.token_delta(f"## streamed section {section}\n\nbenchmark output\n\n")
        index += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.stream_interval_seconds)
        except TimeoutError:
            pass


async def _exercise_inputs(
    app: TextualTui,
    renderer: TextualTuiRenderer,
    pilot: Pilot[object],
    input_widget: PromptEditor,
    transcript: Transcript,
    config: BenchmarkConfig,
) -> None:
    delay = config.action_interval_seconds
    for key in "latency":
        await pilot.press(key)
        await asyncio.sleep(delay)
    await pilot.press("left", "right")
    await asyncio.sleep(delay)
    await pilot.press("pageup")
    await asyncio.sleep(delay)
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
    await asyncio.sleep(delay)
    transcript.scroll_end(animate=False)
    await pilot.pause()
    input_widget.value = "steer benchmark"
    await pilot.press("enter")
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
    await pilot.press("down")
    await asyncio.sleep(delay)
    app.hide_decision()
    await pilot.pause()

    async def acknowledge_cancellation() -> None:
        await asyncio.sleep(0)
        renderer.cancelling("benchmark cancellation requested")

    cancellation = asyncio.create_task(acknowledge_cancellation())
    await pilot.press("escape")
    await cancellation
    await pilot.pause()


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
    if config.runs < 1 or config.stream_chunks < 1:
        raise ValueError("runs and stream_chunks must be positive")
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
