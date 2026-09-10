"""Compare paragraph wrapping reuse with Rich's uncached paragraph renderer.

This is a synchronous source-preparation and height-measurement microbenchmark,
not a frame, input-latency, or terminal-emulator benchmark. Both modes keep Wisp's
parser, code-fence cache, theme, and single-widget rendering unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Literal
from unittest.mock import patch

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Paragraph
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from wisp.tui.widgets import StreamMessage, _AssistantMarkdown, _AssistantParagraph

type Workload = Literal["prose", "open_fence"]


@dataclass(frozen=True)
class Sample:
    """Numeric work and timing for one fixed-size synthetic document."""

    run: int
    workload: Workload
    cached: bool
    updates: int
    preparation_ms: float
    measurement_ms: float
    paragraph_wraps: int
    final_height: int
    source_complete: bool


class _MarkdownApp(App[None]):
    def compose(self) -> ComposeResult:
        # Mount the measured widget only after startup. The benchmark drives
        # its visual explicitly without interleaving unrelated screen layouts.
        yield Static()


async def _sample(*, run: int, workload: Workload, cached: bool, updates: int) -> Sample:
    app = _MarkdownApp()
    chunks = (
        tuple(
            f"Paragraph {index}: **styled text** and [a link](https://example.com). "
            + "Some wrapping prose to measure. " * 5
            + "\n\n"
            for index in range(updates)
        )
        if workload == "prose"
        else ("```python\n", *(f"value_{index} = {index}\n" for index in range(updates - 1)))
    )
    wraps = 0
    original_render = Text.__rich_console__

    def count_wraps(text: Text, console: Console, options: ConsoleOptions) -> RenderResult:
        nonlocal wraps
        if text.plain.startswith("Paragraph "):
            wraps += 1
        yield from original_render(text, console, options)

    async with app.run_test(size=(80, 24)):
        stream = StreamMessage()
        await app.mount(stream)
        preparation_ms = measurement_ms = 0.0
        height = 0
        paragraph_type = _AssistantParagraph if cached else Paragraph
        with (
            patch.dict(_AssistantMarkdown.elements, paragraph_open=paragraph_type),
            patch.object(Text, "__rich_console__", count_wraps),
        ):
            for chunk in chunks:
                started = perf_counter()
                await stream.append_markdown(chunk)
                preparation_ms += (perf_counter() - started) * 1_000
                visual = stream._selection_visual
                assert visual is not None
                started = perf_counter()
                height = visual.get_height(stream.styles, 80)
                measurement_ms += (perf_counter() - started) * 1_000
        return Sample(
            run=run,
            workload=workload,
            cached=cached,
            updates=len(chunks),
            preparation_ms=preparation_ms,
            measurement_ms=measurement_ms,
            paragraph_wraps=wraps,
            final_height=height,
            source_complete=stream.source == "".join(chunks),
        )


async def _run(*, runs: int, updates: int) -> list[Sample]:
    samples: list[Sample] = []
    workloads: tuple[Workload, ...] = ("prose", "open_fence")
    for run in range(runs):
        modes = (False, True) if run % 2 == 0 else (True, False)
        for workload in workloads:
            for cached in modes:
                samples.append(
                    await _sample(run=run + 1, workload=workload, cached=cached, updates=updates)
                )
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--updates", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 1 or args.updates < 2:
        parser.error("runs must be positive and updates must be at least two")
    samples = asyncio.run(_run(runs=args.runs, updates=args.updates))
    payload = json.dumps([asdict(sample) for sample in samples], indent=2)
    print(payload)
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
