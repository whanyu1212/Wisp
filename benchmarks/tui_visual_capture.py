"""Export synthetic conversation screens for visual review, without a backend.

Run the ignored Rust capture test first, then pass its output directory here.
Artifacts are review evidence, not pixel-equality tests between renderers.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pydantic import BaseModel
from rich.cells import cell_len
from rich.console import Console
from rich.style import Style
from rich.text import Text

from wisp.events import (
    MessageCompleted,
    MessageStarted,
    ToolApprovalRequested,
    ToolCallRequested,
    ToolResultReady,
)
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import PromptEditor, ToolCard, WorkingIndicator


class Cell(BaseModel):
    text: str
    fg: str
    bg: str
    bold: bool
    italic: bool
    underline: bool
    reverse: bool
    strike: bool


class Screen(BaseModel):
    width: int
    height: int
    cells: list[Cell]


class Fixture(BaseModel):
    prompt: str
    reply: str
    tool_name: str
    tool_arguments: dict[str, str]
    tool_output: str
    draft: str


def export_rust(path: Path) -> None:
    """Write an SVG beside a Rust cell capture, retaining wide-cell geometry.

    Args:
        path (Path): JSON buffer emitted by the ignored Rust capture test.
    """
    screen = Screen.model_validate_json(path.read_text())
    if len(screen.cells) != screen.width * screen.height:
        raise ValueError("cell count does not match screen geometry")
    text = Text()
    for y in range(screen.height):
        next_column = 0
        for x in range(screen.width):
            if x < next_column:
                continue
            cell = screen.cells[y * screen.width + x]
            text.append(
                cell.text,
                Style(
                    color=cell.fg,
                    bgcolor=cell.bg,
                    bold=cell.bold,
                    italic=cell.italic,
                    underline=cell.underline,
                    reverse=cell.reverse,
                    strike=cell.strike,
                ),
            )
            next_column = x + max(1, cell_len(cell.text))
        text.append("\n")
    console = Console(
        width=screen.width,
        height=screen.height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        record=True,
    )
    # Keep each cell positioned independently: SVG renderers disagree on the
    # advance of nonbreaking spaces inside a merged text run.
    console.print(text, end="", soft_wrap=True)
    path.with_suffix(".svg").write_text(console.export_svg(title=path.stem))


async def export_textual(output: Path) -> None:
    """Capture settled Textual reference screens from the shared synthetic fixture.

    Args:
        output (Path): Destination for reference SVGs; no session data is read.
    """
    fixture_path = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/tui_visual_conversation.json"
    )
    fixture = Fixture.model_validate_json(fixture_path.read_text())
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    for width, height in [(100, 30), (80, 24)]:
        for variant, theme in [("dark", "wisp"), ("light", "wisp-light")]:
            for scenario in ["conversation", "tools", "working", "approval"]:
                app, renderer = create_textual_tui()
                async with app.run_test(size=(width, height)) as pilot:
                    app.theme = theme
                    await pilot.pause()
                    renderer.prompt_submitted(fixture.prompt)
                    renderer.event(
                        ToolCallRequested(
                            call_id="visual-tool",
                            name=fixture.tool_name,
                            arguments=dict(fixture.tool_arguments),
                            timestamp=timestamp,
                        )
                    )
                    renderer.event(
                        ToolResultReady(
                            call_id="visual-tool",
                            name=fixture.tool_name,
                            output=fixture.tool_output,
                            is_error=False,
                            exit_code=0,
                            timestamp=timestamp,
                        )
                    )
                    renderer.event(
                        MessageCompleted(
                            turn=1,
                            content=fixture.reply,
                            finish_reason="stop",
                            timestamp=timestamp,
                        )
                    )
                    app.query_one(PromptEditor).load_text(fixture.draft)
                    await pilot.pause()
                    if scenario == "tools":
                        card = app.query_one(ToolCard)
                        card.action_toggle_expand()
                        card.focus()
                        await pilot.pause()
                    elif scenario == "working":
                        renderer.event(MessageStarted(turn=2, timestamp=timestamp))
                        renderer.token_delta("Checking the remaining edge cases…")
                        await pilot.pause()
                    elif scenario == "approval":
                        renderer.approval_request(
                            ToolApprovalRequested(
                                call_id="visual-approval",
                                name="bash",
                                arguments=dict(fixture.tool_arguments),
                                safety="command",
                                timestamp=timestamp,
                            )
                        )
                        await pilot.pause()
                    name = f"textual-{scenario}-{variant}-{width}x{height}"
                    (output / f"{name}.svg").write_text(app.export_screenshot(title=name))


def main() -> None:
    """Export existing Rust captures and optional Textual reference screens."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--textual", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for path in sorted(args.output.glob("rust-*.json")):
        export_rust(path)
    if args.textual:
        # Reference colors must not inherit the invoking agent's NO_COLOR setting.
        with patch.dict(os.environ), patch.object(WorkingIndicator, "presentation_clock_tick"):
            os.environ.pop("NO_COLOR", None)
            asyncio.run(export_textual(args.output))


if __name__ == "__main__":
    main()
