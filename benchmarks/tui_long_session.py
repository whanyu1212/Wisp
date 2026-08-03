"""Measure current long-session TUI behavior before transcript windowing."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import anyio
import textual
from textual.pilot import Pilot

from wisp.agent.messages import Message
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionMessagePage
from wisp.tools.process_manager import ProcessSupervisor
from wisp.tui.history import history_entries_from_rpc_messages
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui
from wisp.tui.widgets import Transcript

_WORKER_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class ScenarioConfig:
    message_count: int = 2_000
    page_size: int = 100
    stream_chunks: int = 20


@dataclass(frozen=True)
class ScenarioReport:
    config: ScenarioConfig
    environment: dict[str, str]
    session_entry_count: int
    session_size_bytes: int
    newest_page_read_ms: float
    older_page_read_ms: tuple[float, ...]
    initial_render_ms: float
    prepend_render_ms: tuple[float, ...]
    mounted_widget_counts: tuple[int, ...]
    scroll_while_process_ms: float
    stream_following_tail_ms: float
    stream_scrolled_back_ms: float
    final_following: bool
    final_unseen_output_count: int
    process_state: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _milliseconds(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000


def _environment() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "textual": textual.__version__,
    }


def _cpu_command() -> str:
    program = "import time\nwhile True:\n    pass\n"
    arguments = [sys.executable, "-c", program]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


async def _wait_for(pilot: Pilot[None], predicate: Callable[[], bool]) -> None:
    """Wait for a Textual state transition without baking in a settle count."""

    for _ in range(40):
        if predicate():
            return
        await pilot.pause()
    raise RuntimeError("Textual benchmark scenario did not settle")


async def _append_messages(session: JsonlSession, count: int) -> None:
    for index in range(count):
        position = index % 5
        if position == 0:
            message = Message(role="user", content=f"prompt {index}")
        elif position == 4:
            message = Message(
                role="tool",
                content=f"tool output {index}\nbenchmark detail {index}\nbenchmark detail {index}",
                tool_call_id=f"benchmark-{index}",
                tool_name="bash",
            )
        else:
            message = Message(
                role="assistant",
                content=f"response {index}\nbenchmark detail {index}\nbenchmark detail {index}",
            )
        await session.append_message(message)


def _read_page(
    session: JsonlSession,
    *,
    limit: int,
    before_entry_id: str | None = None,
) -> tuple[SessionMessagePage, float]:
    started = time.perf_counter_ns()
    page = session.read_message_page(limit=limit, before_entry_id=before_entry_id)
    return page, _milliseconds(started)


async def _render_page(
    app: TextualTui,
    renderer: TextualTuiRenderer,
    pilot: Pilot[None],
    page: SessionMessagePage,
    *,
    prepend: bool,
) -> float:
    entries = history_entries_from_rpc_messages(page.messages)
    transcript = app.query_one("#transcript", Transcript)
    previous_count = len(transcript.children)
    started = time.perf_counter_ns()
    if prepend:
        renderer.prepend_history_entries(entries)
    else:
        renderer.replace_history_entries(entries, session_label="Long-session benchmark")
    await _wait_for(pilot, lambda: len(transcript.children) >= previous_count + len(entries))
    await app.wait_for_history_render()
    await pilot.pause()
    return _milliseconds(started)


async def run_scenario(config: ScenarioConfig) -> ScenarioReport:
    """Run one headless, end-to-end long-session baseline scenario."""

    if config.message_count < 1 or config.page_size < 1 or config.stream_chunks < 1:
        raise ValueError("message_count, page_size, and stream_chunks must be positive")
    with tempfile.TemporaryDirectory(prefix="wisp-tui-benchmark-") as temporary_directory:
        root = Path(temporary_directory)
        session = JsonlSessionStore(root).create()
        await _append_messages(session, config.message_count)
        newest_page, newest_page_read_ms = _read_page(session, limit=config.page_size)
        older_pages: list[tuple[SessionMessagePage, float]] = []
        cursor = newest_page.next_before_entry_id
        while cursor is not None:
            page, duration_ms = _read_page(
                session,
                limit=config.page_size,
                before_entry_id=cursor,
            )
            older_pages.append((page, duration_ms))
            cursor = page.next_before_entry_id

        app, renderer = create_textual_tui()
        assert isinstance(renderer, TextualTuiRenderer)
        supervisor = ProcessSupervisor()
        process_id: str | None = None
        try:
            async with app.run_test(size=(100, 12)) as pilot:
                initial_render_ms = await _render_page(
                    app,
                    renderer,
                    pilot,
                    newest_page,
                    prepend=False,
                )
                transcript = app.query_one("#transcript", Transcript)
                mounted_counts = [len(transcript.children)]
                prepend_render_ms: list[float] = []
                for page, _duration_ms in older_pages:
                    prepend_render_ms.append(
                        await _render_page(app, renderer, pilot, page, prepend=True)
                    )
                    mounted_counts.append(len(transcript.children))

                command = _cpu_command()
                process_id = await supervisor.start(
                    command, cwd=root, timeout=_WORKER_TIMEOUT_SECONDS
                )
                process_update = await supervisor.poll(process_id)
                if process_update.state != "running":
                    raise RuntimeError(
                        "Benchmark worker did not start: "
                        f"{process_update.state}: {process_update.stderr}"
                    )
                started = time.perf_counter_ns()
                transcript.scroll_end(animate=False)
                await _wait_for(pilot, lambda: transcript.max_scroll_y > 0)
                transcript.scroll_to(
                    y=transcript.max_scroll_y / 2,
                    animate=False,
                    immediate=True,
                )
                transcript.scroll_end(animate=False)
                await _wait_for(pilot, lambda: transcript.is_following)
                scroll_while_process_ms = _milliseconds(started)
                update = await supervisor.cancel(process_id)
                process_id = None
                process_state = update.state

                stream_chunks = tuple(
                    f"## Stream section {index}\n\n- benchmark item {index}"
                    for index in range(config.stream_chunks)
                )
                started = time.perf_counter_ns()
                for chunk in stream_chunks:
                    renderer.token_delta(f"{chunk}\n\n")
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                await _wait_for(pilot, lambda: transcript.is_following)
                stream_following_tail_ms = _milliseconds(started)

                await _wait_for(pilot, lambda: transcript.max_scroll_y > 0)
                transcript.scroll_to(
                    y=transcript.max_scroll_y / 2,
                    animate=False,
                    immediate=True,
                )
                await _wait_for(pilot, lambda: not transcript.is_following)
                started = time.perf_counter_ns()
                renderer.token_delta("\n\nscrolled-back stream output")
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                stream_scrolled_back_ms = _milliseconds(started)
                final_following = transcript.is_following
                final_unseen_output_count = len(app._unseen_output)

        finally:
            if process_id is not None:
                await supervisor.cancel(process_id)
            await supervisor.aclose()

        return ScenarioReport(
            config=config,
            environment=_environment(),
            session_entry_count=config.message_count,
            session_size_bytes=session.path.stat().st_size,
            newest_page_read_ms=newest_page_read_ms,
            older_page_read_ms=tuple(duration for _page, duration in older_pages),
            initial_render_ms=initial_render_ms,
            prepend_render_ms=tuple(prepend_render_ms),
            mounted_widget_counts=tuple(mounted_counts),
            scroll_while_process_ms=scroll_while_process_ms,
            stream_following_tail_ms=stream_following_tail_ms,
            stream_scrolled_back_ms=stream_scrolled_back_ms,
            final_following=final_following,
            final_unseen_output_count=final_unseen_output_count,
            process_state=process_state,
        )


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=ScenarioConfig.message_count)
    parser.add_argument("--page-size", type=int, default=ScenarioConfig.page_size)
    parser.add_argument("--stream-chunks", type=int, default=ScenarioConfig.stream_chunks)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    report = await run_scenario(
        ScenarioConfig(
            message_count=parsed.messages,
            page_size=parsed.page_size,
            stream_chunks=parsed.stream_chunks,
        )
    )
    print(report.to_json())
    if parsed.output is not None:
        parsed.output.write_text(f"{report.to_json()}\n", encoding="utf-8")


def main() -> None:
    anyio.run(_main)


if __name__ == "__main__":
    main()
