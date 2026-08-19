"""Measure complete history hydration and rendering for long Textual sessions."""

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
from textual import events
from textual.pilot import Pilot

from wisp.agent.messages import Message
from wisp.events import ToolCallSnapshot
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionMessagePage
from wisp.tools.process_manager import ProcessSupervisor
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    HistoricalTranscriptEntry,
    history_entries_from_rpc_messages,
    represented_history_entry_ids,
)
from wisp.tui.textual_app import TextualTui, TextualTuiRenderer, create_textual_tui
from wisp.tui.widgets import Transcript

_WORKER_TIMEOUT_SECONDS = 60.0
DEFAULT_SCENARIO_MESSAGE_COUNTS = (2_000, 5_000)


@dataclass(frozen=True)
class ScenarioConfig:
    message_count: int = 2_000
    page_size: int = TUI_HISTORY_MESSAGE_LIMIT
    stream_chunks: int = 20
    stream_interval_seconds: float = 0.02


@dataclass(frozen=True)
class ScenarioReport:
    config: ScenarioConfig
    environment: dict[str, str]
    session_entry_count: int
    session_size_bytes: int
    newest_page_read_ms: float
    warm_newest_page_read_ms: float
    older_page_read_ms: tuple[float, ...]
    complete_history_convert_ms: float
    complete_history_mount_ms: float
    persisted_message_count: int
    represented_row_count: int
    hydrated_entry_count: int
    mounted_widget_count: int
    retained_entry_count: int
    persisted_rows_per_widget: float
    first_wheel_up_ms: float
    first_wheel_up_rows: float
    first_wheel_up_attempts: int
    scroll_while_process_ms: float
    stream_following_tail_ms: float
    stream_page_up_ms: float
    stream_scrolled_back_ms: float
    stream_max_event_loop_stall_ms: float
    stream_markdown_writes: int
    settled_live_widget_count: int
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


async def append_benchmark_messages(session: JsonlSession, count: int) -> None:
    """Populate a session with the shared deterministic TUI benchmark fixture."""

    for index in range(count):
        position = index % 5
        if position == 0:
            message = Message(role="user", content=f"prompt {index}")
        elif position == 4:
            message = Message(
                role="tool",
                content=(
                    "Process benchmark-process is still running\n"
                    f"stdout:\nbenchmark poll output {index}\n"
                ),
                tool_call_id=f"benchmark-{index}",
                tool_name="bash",
            )
        elif position == 3:
            message = Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCallSnapshot(
                        call_id=f"benchmark-{index + 1}",
                        name="bash",
                        arguments={
                            "operation": "poll",
                            "process_id": "benchmark-process",
                        },
                    ),
                ),
                finish_reason="tool_calls",
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
    page = session.read_message_page(
        limit=limit,
        before_entry_id=before_entry_id,
        complete_structure=True,
    )
    return page, _milliseconds(started)


async def _hydrate_history(
    app: TextualTui,
    renderer: TextualTuiRenderer,
    pilot: Pilot[None],
    entries: tuple[HistoricalTranscriptEntry, ...],
) -> float:
    started = time.perf_counter_ns()
    await renderer.hydrate_history_entries(
        entries,
        session_label="Long-session benchmark",
    )
    await app.wait_for_history_render()
    await pilot.pause()
    return _milliseconds(started)


async def run_scenario(config: ScenarioConfig) -> ScenarioReport:
    """Run one headless, end-to-end long-session baseline scenario."""

    if config.message_count < 1 or config.page_size < 1 or config.stream_chunks < 1:
        raise ValueError("message_count, page_size, and stream_chunks must be positive")
    if config.stream_interval_seconds <= 0:
        raise ValueError("stream_interval_seconds must be positive")
    with tempfile.TemporaryDirectory(prefix="wisp-tui-benchmark-") as temporary_directory:
        root = Path(temporary_directory)
        store = JsonlSessionStore(root)
        session = store.create()
        await append_benchmark_messages(session, config.message_count)
        session = store.load(session.path)
        newest_page, newest_page_read_ms = _read_page(session, limit=config.page_size)
        _warm_newest_page, warm_newest_page_read_ms = _read_page(session, limit=config.page_size)
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

        pages = (newest_page, *(page for page, _duration_ms in older_pages))
        messages = tuple(message for page in reversed(pages) for message in page.messages)
        started = time.perf_counter_ns()
        history_entries = history_entries_from_rpc_messages(messages)
        represented_row_count = len(represented_history_entry_ids(history_entries))
        if represented_row_count != len(messages):
            raise RuntimeError("Complete history conversion did not represent every message row")
        complete_history_convert_ms = _milliseconds(started)

        app, renderer = create_textual_tui()
        assert isinstance(renderer, TextualTuiRenderer)
        supervisor = ProcessSupervisor()
        process_id: str | None = None
        try:
            async with app.run_test(size=(100, 12)) as pilot:
                complete_history_mount_ms = await _hydrate_history(
                    app,
                    renderer,
                    pilot,
                    history_entries,
                )
                transcript = app.query_one("#transcript", Transcript)
                mounted_widget_count = len(transcript.children)
                retained_entry_count = renderer.retained_history_entry_count
                if retained_entry_count != len(history_entries):
                    raise RuntimeError("Complete hydration did not retain every history entry")

                await _wait_for(pilot, lambda: transcript.max_scroll_y > 0)
                transcript.return_to_latest()
                await pilot.pause()
                initial_scroll_y = transcript.scroll_y
                started = time.perf_counter_ns()
                first_wheel_up_attempts = 0
                while transcript.scroll_y >= initial_scroll_y and first_wheel_up_attempts < 2:
                    first_wheel_up_attempts += 1
                    await pilot._post_mouse_events(
                        [events.MouseScrollUp],
                        widget=transcript,
                        times=1,
                    )
                    await pilot.pause()
                first_wheel_up_ms = _milliseconds(started)
                first_wheel_up_rows = initial_scroll_y - transcript.scroll_y
                if first_wheel_up_rows <= 0:
                    raise RuntimeError("Complete history did not respond to upward wheel input")
                transcript.return_to_latest()
                await pilot.pause()

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
                await _wait_for(pilot, lambda: transcript.max_scroll_y > 0)
                transcript.scroll_to(
                    y=transcript.max_scroll_y / 2,
                    animate=False,
                    immediate=True,
                )
                await pilot.pause()
                await _wait_for(pilot, lambda: 0 < transcript.scroll_y < transcript.max_scroll_y)
                started = time.perf_counter_ns()
                transcript.scroll_to(y=transcript.max_scroll_y, animate=False, immediate=True)
                await pilot.pause()
                await _wait_for(
                    pilot,
                    lambda: (
                        transcript.is_following
                        and transcript.scroll_y >= transcript.max_scroll_y - 3
                    ),
                )
                scroll_while_process_ms = _milliseconds(started)
                update = await supervisor.cancel(process_id)
                if update.state != "cancelled":
                    raise RuntimeError(
                        f"Benchmark worker did not cancel: {update.state}: {update.stderr}"
                    )
                process_id = None
                process_state = update.state

                stream_chunks = tuple(
                    f"## Stream section {index}\n\n- benchmark item {index}"
                    for index in range(config.stream_chunks)
                )
                started = time.perf_counter_ns()
                stream_max_event_loop_stall_ms = 0.0
                for chunk in stream_chunks:
                    chunk_started = time.perf_counter_ns()
                    renderer.token_delta(f"{chunk}\n\n")
                    await anyio.sleep(config.stream_interval_seconds)
                    stream_max_event_loop_stall_ms = max(
                        stream_max_event_loop_stall_ms,
                        max(
                            0.0,
                            _milliseconds(chunk_started) - config.stream_interval_seconds * 1_000,
                        ),
                    )
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                await pilot.pause()
                await _wait_for(
                    pilot,
                    lambda: (
                        transcript.is_following
                        and transcript.scroll_y >= transcript.max_scroll_y - 3
                    ),
                )
                stream_following_tail_ms = _milliseconds(started)
                stream_markdown_writes = app.last_stream_write_count

                await _wait_for(pilot, lambda: transcript.max_scroll_y > 0)
                transcript.return_to_latest()
                renderer.token_delta("streaming page-up latency probe")
                await pilot.pause()
                started = time.perf_counter_ns()
                transcript.page_up()
                await pilot.pause()
                stream_page_up_ms = _milliseconds(started)
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                transcript.return_to_latest()
                await pilot.pause()

                transcript.page_up()
                await pilot.pause()
                await _wait_for(pilot, lambda: not transcript.is_following)
                started = time.perf_counter_ns()
                renderer.token_delta("\n\nscrolled-back stream output")
                renderer.end_token_stream()
                await app.wait_for_stream_idle()
                await pilot.pause()
                stream_scrolled_back_ms = _milliseconds(started)
                final_following = transcript.is_following
                final_unseen_output_count = app._transcript_controller.unseen_output_count
                settled_live_widget_count = app._transcript_controller.settled_widget_count

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
            warm_newest_page_read_ms=warm_newest_page_read_ms,
            older_page_read_ms=tuple(duration for _page, duration in older_pages),
            complete_history_convert_ms=complete_history_convert_ms,
            complete_history_mount_ms=complete_history_mount_ms,
            persisted_message_count=len(messages),
            represented_row_count=represented_row_count,
            hydrated_entry_count=len(history_entries),
            mounted_widget_count=mounted_widget_count,
            retained_entry_count=retained_entry_count,
            persisted_rows_per_widget=(
                represented_row_count / mounted_widget_count if mounted_widget_count else 0.0
            ),
            first_wheel_up_ms=first_wheel_up_ms,
            first_wheel_up_rows=first_wheel_up_rows,
            first_wheel_up_attempts=first_wheel_up_attempts,
            scroll_while_process_ms=scroll_while_process_ms,
            stream_following_tail_ms=stream_following_tail_ms,
            stream_page_up_ms=stream_page_up_ms,
            stream_scrolled_back_ms=stream_scrolled_back_ms,
            stream_max_event_loop_stall_ms=stream_max_event_loop_stall_ms,
            stream_markdown_writes=stream_markdown_writes,
            settled_live_widget_count=settled_live_widget_count,
            final_following=final_following,
            final_unseen_output_count=final_unseen_output_count,
            process_state=process_state,
        )


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--messages",
        type=int,
        action="append",
        help="Message count to benchmark; repeat for multiple scenarios.",
    )
    parser.add_argument("--page-size", type=int, default=ScenarioConfig.page_size)
    parser.add_argument("--stream-chunks", type=int, default=ScenarioConfig.stream_chunks)
    parser.add_argument(
        "--stream-interval-seconds",
        type=float,
        default=ScenarioConfig.stream_interval_seconds,
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(arguments)


async def _main(arguments: Sequence[str] | None = None) -> None:
    parsed = _parse_args(arguments)
    message_counts = tuple(parsed.messages or DEFAULT_SCENARIO_MESSAGE_COUNTS)
    reports: list[ScenarioReport] = []
    for message_count in message_counts:
        reports.append(
            await run_scenario(
                ScenarioConfig(
                    message_count=message_count,
                    page_size=parsed.page_size,
                    stream_chunks=parsed.stream_chunks,
                    stream_interval_seconds=parsed.stream_interval_seconds,
                )
            )
        )
    payload = json.dumps([asdict(report) for report in reports], indent=2, sort_keys=True)
    print(payload)
    if parsed.output is not None:
        parsed.output.write_text(f"{payload}\n", encoding="utf-8")


def main() -> None:
    anyio.run(_main)


if __name__ == "__main__":
    main()
