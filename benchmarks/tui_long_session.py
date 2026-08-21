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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import anyio
import textual
from textual import events
from textual._compositor import Compositor, CompositorMap
from textual.geometry import Size
from textual.pilot import Pilot
from textual.widget import Widget

from wisp.agent.messages import Message
from wisp.events import ToolCallSnapshot
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore, SessionMessagePage
from wisp.tools.process_manager import ProcessSupervisor
from wisp.tui.diagnostics import DisplayUpdateDiagnostic, MarkdownDrainDiagnostic
from wisp.tui.history import (
    TUI_HISTORY_MESSAGE_LIMIT,
    HistoricalTranscriptEntry,
    HistoricalTranscriptMessage,
    history_entries_from_rpc_messages,
    represented_history_entry_ids,
)
from wisp.tui.textual_app import (
    TextualTui,
    TextualTuiRenderer,
    _transcript_child_layout_pending,
    create_textual_tui,
)
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
    cached_newest_page_read_ms: float
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
    wheel_up_ms: tuple[float, ...]
    wheel_up_complete_arrangement_count: int
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
    display_updates: dict[str, int]
    display_frame_fail_open_count: int
    history_prepend_probe_exercised: bool
    history_prepend_suppressed_update_count: int
    history_prepend_escaped_update_count: int
    first_uncovered_at_tail: bool
    first_uncovered_has_pending_layout: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


@dataclass
class _ScenarioDiagnostics:
    app: TextualTui | None = None
    watch_uncovered_frame: bool = False
    display_updates: dict[str, int] = field(default_factory=dict)
    display_frame_fail_open_count: int = 0
    history_prepend_suppressed_update_count: int = 0
    history_prepend_escaped_update_count: int = 0
    first_uncovered_at_tail: bool | None = None
    first_uncovered_has_pending_layout: bool | None = None

    def record_markdown_drain(self, _diagnostic: MarkdownDrainDiagnostic) -> None:
        return

    def record_display_update(self, diagnostic: DisplayUpdateDiagnostic) -> None:
        self.display_updates[diagnostic.kind] = self.display_updates.get(diagnostic.kind, 0) + 1
        if diagnostic.fail_open:
            self.display_frame_fail_open_count += 1
        if diagnostic.history_prepend_suppressed:
            self.history_prepend_suppressed_update_count += 1
        if diagnostic.history_prepend_unsettled and not diagnostic.history_prepend_suppressed:
            self.history_prepend_escaped_update_count += 1
        if not self.watch_uncovered_frame or self.first_uncovered_at_tail is not None:
            return
        emitted_frame = diagnostic.kind == "layout" or (
            diagnostic.kind == "chops" and diagnostic.emitted_spans > 0
        )
        app = self.app
        if not emitted_frame or app is None:
            return
        indicator = app._operation_indicator
        transcript = app.transcript
        if indicator is None or indicator.is_open or transcript is None:
            return
        self.first_uncovered_at_tail = (
            transcript.is_following and transcript.scroll_y == transcript.max_scroll_y
        )
        self.first_uncovered_has_pending_layout = any(
            _transcript_child_layout_pending(child) for child in transcript.children
        )


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


@contextmanager
def _count_complete_arrangements() -> Iterator[Callable[[], int]]:
    """Count whole-tree compositor arrangements performed inside the block.

    Textual arranges only the visible widgets on its scroll fast path. Anything that
    invalidates the complete compositor map forces the next widget geometry lookup to
    re-arrange *every* mounted widget instead, so this count — unlike a wall-clock
    timing — grows with transcript size and is stable across machines. Routine
    scrolling must not trigger it at all.
    """

    count = 0
    original = Compositor._arrange_root

    def counting_arrange_root(
        self: Compositor, root: Widget, size: Size, visible_only: bool = True
    ) -> tuple[CompositorMap, set[Widget]]:
        nonlocal count
        if not visible_only:
            count += 1
        return original(self, root, size, visible_only=visible_only)

    Compositor._arrange_root = counting_arrange_root  # type: ignore[method-assign]
    try:
        yield lambda: count
    finally:
        Compositor._arrange_root = original  # type: ignore[method-assign]


async def _measure_wheel_up_response(
    transcript: Transcript,
    diagnostics: _ScenarioDiagnostics,
) -> tuple[float, float]:
    """Measure production-style wheel dispatch through the first displayed response.

    ``Pilot._post_mouse_events`` surrounds dispatch with ``Pilot.pause()``. That test
    helper drains every mounted widget, so its cost scales with transcript size even
    when the production scroll path does not. Dispatch through the screen as Textual's
    runtime does, then yield only to the event loop until both the viewport and display
    boundary have observed the scroll.
    """

    initial_scroll_y = transcript.scroll_y
    initial_display_updates = sum(diagnostics.display_updates.values())
    origin = transcript.region.offset
    event = events.MouseScrollUp(
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
    started = time.perf_counter_ns()
    transcript.screen._forward_event(event)
    with anyio.fail_after(5):
        while (
            transcript.scroll_y >= initial_scroll_y
            or sum(diagnostics.display_updates.values()) <= initial_display_updates
        ):
            await anyio.sleep(0)
    return _milliseconds(started), initial_scroll_y - transcript.scroll_y


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
        # A fresh handle has no in-process index, so this is what a resume pays: it
        # measures the newest-page cache rather than the warm index above.
        _cached_page, cached_newest_page_read_ms = _read_page(
            store.load(session.path), limit=config.page_size
        )
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

        diagnostics = _ScenarioDiagnostics()
        app, renderer = create_textual_tui(diagnostics=diagnostics)
        diagnostics.app = app
        assert isinstance(renderer, TextualTuiRenderer)
        supervisor = ProcessSupervisor()
        process_id: str | None = None
        try:
            async with app.run_test(size=(100, 12)) as pilot:
                renderer.history_hydration_started()
                await pilot.pause()
                complete_history_mount_ms = await _hydrate_history(
                    app,
                    renderer,
                    pilot,
                    history_entries,
                )
                diagnostics.watch_uncovered_frame = True
                renderer.history_hydration_finished()
                await _wait_for(
                    pilot,
                    lambda: (
                        app._operation_indicator is not None
                        and not app._operation_indicator.is_open
                    ),
                )
                await pilot.pause()
                diagnostics.watch_uncovered_frame = False
                transcript = app.query_one("#transcript", Transcript)
                mounted_widget_count = len(transcript.children)
                retained_entry_count = renderer.retained_history_entry_count
                if retained_entry_count != len(history_entries):
                    raise RuntimeError("Complete hydration did not retain every history entry")

                await _wait_for(pilot, lambda: transcript.max_scroll_y > 0)
                transcript.return_to_latest()
                await pilot.pause()
                first_wheel_up_attempts = 1
                with _count_complete_arrangements() as complete_arrangements:
                    first_wheel_up_ms, first_wheel_up_rows = await _measure_wheel_up_response(
                        transcript,
                        diagnostics,
                    )
                    wheel_up_ms = [first_wheel_up_ms]
                    for _ in range(19):
                        if transcript.scroll_y <= 0:
                            break
                        duration_ms, rows = await _measure_wheel_up_response(
                            transcript,
                            diagnostics,
                        )
                        if rows <= 0:
                            raise RuntimeError("Complete history stopped responding to wheel input")
                        wheel_up_ms.append(duration_ms)
                    wheel_up_complete_arrangement_count = complete_arrangements()
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

                suppressed_before = diagnostics.history_prepend_suppressed_update_count
                escaped_before = diagnostics.history_prepend_escaped_update_count
                renderer.replace_history_entries(
                    tuple(
                        HistoricalTranscriptMessage(
                            role="assistant",
                            content=f"stability current {index}",
                        )
                        for index in range(30)
                    ),
                    session_label="Display stability probe",
                )
                await app.wait_for_history_render()
                transcript = app.query_one("#transcript", Transcript)
                transcript.scroll_to(y=8, animate=False)
                await pilot.pause()
                renderer.prepend_history_entries(
                    tuple(
                        HistoricalTranscriptMessage(
                            role="user",
                            content=f"stability older {index}",
                        )
                        for index in range(12)
                    )
                )
                with anyio.fail_after(5):
                    while (
                        app._history_prepend_anchor is not None
                        or app._history_prepend_paint_suppressed
                    ):
                        await pilot.pause()
                await pilot.pause()
                history_prepend_probe_exercised = True
                history_prepend_suppressed_update_count = (
                    diagnostics.history_prepend_suppressed_update_count - suppressed_before
                )
                history_prepend_escaped_update_count = (
                    diagnostics.history_prepend_escaped_update_count - escaped_before
                )

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
            cached_newest_page_read_ms=cached_newest_page_read_ms,
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
            wheel_up_ms=tuple(wheel_up_ms),
            wheel_up_complete_arrangement_count=wheel_up_complete_arrangement_count,
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
            display_updates=dict(sorted(diagnostics.display_updates.items())),
            display_frame_fail_open_count=diagnostics.display_frame_fail_open_count,
            history_prepend_probe_exercised=history_prepend_probe_exercised,
            history_prepend_suppressed_update_count=history_prepend_suppressed_update_count,
            history_prepend_escaped_update_count=history_prepend_escaped_update_count,
            first_uncovered_at_tail=diagnostics.first_uncovered_at_tail is True,
            first_uncovered_has_pending_layout=(
                diagnostics.first_uncovered_has_pending_layout is True
            ),
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
