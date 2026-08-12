"""Compare production Rich Markdown streaming with its literal-text floor.

This is an experiment harness, not an alternate production renderer. ``rich`` runs
the real ``StreamMessage`` path. ``plain`` temporarily replaces only its internal
render step so the same controller, transcript, pacing, and follow behavior update
literal text. The patch is removed after every scenario.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Sequence
from dataclasses import asdict
from typing import Literal
from unittest.mock import patch

import anyio
from textual.content import Content
from tui_long_session import ScenarioConfig, ScenarioReport, run_scenario

from wisp.tui.transcript_window import TUI_TRANSCRIPT_WINDOW_SIZE
from wisp.tui.widgets import StreamMessage

StreamMode = Literal["rich", "plain"]

_TIMING_FIELDS = (
    "stream_following_tail_ms",
    "stream_max_event_loop_stall_ms",
    "stream_page_up_ms",
    "stream_scrolled_back_ms",
)


async def _run_plain_scenario(config: ScenarioConfig) -> ScenarioReport:
    """Measure the closest literal-text comparison to production rendering."""

    def render_plain(widget: StreamMessage) -> None:
        widget.update(Content(widget.source))

    with patch.object(StreamMessage, "_render_source", render_plain):
        return await run_scenario(config)


async def _run_mode(mode: StreamMode, config: ScenarioConfig) -> ScenarioReport:
    if mode == "plain":
        return await _run_plain_scenario(config)
    return await run_scenario(config)


def _sample(mode: StreamMode, run: int, report: ScenarioReport) -> dict[str, object]:
    return {
        "mode": mode,
        "run": run,
        "environment": report.environment,
        "stream_following_tail_ms": report.stream_following_tail_ms,
        "stream_max_event_loop_stall_ms": report.stream_max_event_loop_stall_ms,
        "stream_page_up_ms": report.stream_page_up_ms,
        "stream_scrolled_back_ms": report.stream_scrolled_back_ms,
        # The report keeps its historical field name, but this counts paced
        # StreamMessage updates for both modes.
        "stream_update_count": report.stream_markdown_writes,
        "final_following": report.final_following,
        "final_unseen_output_count": report.final_unseen_output_count,
    }


def _summarize(samples: list[dict[str, object]]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for field in _TIMING_FIELDS:
        medians = {
            mode: statistics.median(
                float(sample[field]) for sample in samples if sample["mode"] == mode
            )
            for mode in ("rich", "plain")
        }
        rich_median = medians["rich"]
        plain_ratio = medians["plain"] / rich_median if rich_median else None
        summary[field] = {
            "rich_median_ms": rich_median,
            "plain_median_ms": medians["plain"],
            "plain_over_rich_ratio": plain_ratio,
            "rich_over_plain_overhead_percent": (
                None if plain_ratio in {None, 0} else (1 / plain_ratio - 1) * 100
            ),
        }
    return summary


async def run_comparison(config: ScenarioConfig, *, runs: int) -> dict[str, object]:
    """Run both scenarios, rotating order to reduce warm-up bias."""

    if runs < 1:
        raise ValueError("runs must be positive")

    samples: list[dict[str, object]] = []
    for run in range(1, runs + 1):
        modes: tuple[StreamMode, ...] = ("rich", "plain") if run % 2 else ("plain", "rich")
        for mode in modes:
            report = await _run_mode(mode, config)
            samples.append(_sample(mode, run, report))

    return {
        "config": asdict(config),
        "runs_per_mode": runs,
        "summary": _summarize(samples),
        "samples": samples,
    }


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=2_000)
    parser.add_argument("--page-size", type=int, default=TUI_TRANSCRIPT_WINDOW_SIZE)
    parser.add_argument("--stream-chunks", type=int, default=100)
    parser.add_argument("--stream-interval-seconds", type=float, default=0.02)
    parser.add_argument("--runs", type=int, default=3)
    return parser.parse_args(arguments)


async def _main(parsed: argparse.Namespace) -> None:
    config = ScenarioConfig(
        message_count=parsed.messages,
        page_size=parsed.page_size,
        stream_chunks=parsed.stream_chunks,
        stream_interval_seconds=parsed.stream_interval_seconds,
    )
    comparison = await run_comparison(config, runs=parsed.runs)
    print(json.dumps(comparison, indent=2, sort_keys=True))


def main(arguments: Sequence[str] | None = None) -> None:
    anyio.run(_main, _parse_args(arguments))


if __name__ == "__main__":
    main()
