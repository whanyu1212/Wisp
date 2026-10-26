"""grep, find and ls stop at the call's deadline and return what they found."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.tools.support import run_tool
from wisp.tools.base import Tool
from wisp.tools.builtin import FindTool, GrepTool, LsTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.search import tools as search_module

_DEADLINE_MARKER = (
    "[truncated] Stopped at the 30-second search limit, so results are incomplete. "
    "Search a narrower path to see the rest."
)


@pytest.fixture
def reached() -> threading.Event:
    """Expire the deadline only once the search sets the returned event."""

    return threading.Event()


@pytest.fixture(autouse=True)
def _deadline_on_signal(monkeypatch: MonkeyPatch, reached: threading.Event) -> None:
    async def wait_for_signal() -> None:
        await anyio.to_thread.run_sync(reached.wait, 5, abandon_on_cancel=True)

    monkeypatch.setattr(search_module, "_wait_for_search_deadline", wait_for_signal)


def _capture_stop_event(monkeypatch: MonkeyPatch, worker: str) -> list[threading.Event]:
    """Record the stop event the tool hands its worker."""

    events: list[threading.Event] = []
    original = getattr(search_module, worker)

    def recording(**kwargs: object) -> ToolResult:
        stop = kwargs["cancel_event"]
        assert isinstance(stop, threading.Event)
        events.append(stop)
        return original(**kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(search_module, worker, recording)
    return events


@pytest.mark.parametrize(
    ("tool", "arguments", "first"),
    [
        pytest.param(GrepTool(), {"pattern": "needle"}, "a.txt:1:needle", id="grep"),
        pytest.param(FindTool(), {}, "a.txt", id="find"),
    ],
)
def test_returns_results_found_so_far(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    reached: threading.Event,
    tool: Tool,
    arguments: dict[str, object],
    first: str,
) -> None:
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")

    def walk_one_file(
        *_args: object, cancel_event: threading.Event, **_kwargs: object
    ) -> Iterator[search_module._WalkedFile]:
        yield search_module._WalkedFile(tmp_path / "a.txt")
        reached.set()
        assert cancel_event.wait(5)

    monkeypatch.setattr(search_module, "_iter_walked_files", walk_one_file)

    result = run_tool(tool, arguments, ToolContext(cwd=tmp_path))

    assert result.text == f"{first}\n{_DEADLINE_MARKER}"
    assert result.data["count"] == 1
    assert result.truncated


def test_keeps_matches_in_current_file(
    tmp_path: Path, monkeypatch: MonkeyPatch, reached: threading.Event
) -> None:
    (tmp_path / "log.txt").write_text("unused\n", encoding="utf-8")
    events = _capture_stop_event(monkeypatch, "_python_grep")

    def two_blocks(_descriptor: int) -> Iterator[list[str]]:
        yield ["needle one"]
        reached.set()
        assert events[0].wait(5)
        yield ["needle two"]

    monkeypatch.setattr(search_module, "_iter_utf8_line_blocks", two_blocks)

    result = run_tool(
        GrepTool(), {"pattern": "needle", "path": "log.txt"}, ToolContext(cwd=tmp_path)
    )

    assert result.text == f"log.txt:1:needle one\n{_DEADLINE_MARKER}"


def test_ls_lists_entries_seen_so_far(
    tmp_path: Path, monkeypatch: MonkeyPatch, reached: threading.Event
) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).touch()
    events = _capture_stop_event(monkeypatch, "_python_ls")

    def expire_after_first(*_args: object) -> bool:
        if not reached.is_set():
            reached.set()
            assert events[0].wait(5)
        return False

    monkeypatch.setattr(search_module, "_is_protected_walked_file", expire_after_first)

    result = run_tool(LsTool(), {}, ToolContext(cwd=tmp_path))

    assert result.data["entry_count"] == 1
    assert result.text.endswith(f"\n{_DEADLINE_MARKER}")
    assert result.truncated


@pytest.mark.parametrize(
    ("tool", "arguments", "worker"),
    [
        pytest.param(GrepTool(), {"pattern": "needle"}, "_python_grep", id="grep"),
        pytest.param(FindTool(), {}, "_python_find", id="find"),
        pytest.param(LsTool(), {}, "_python_ls", id="ls"),
    ],
)
def test_abandons_blocked_worker(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    reached: threading.Event,
    tool: Tool,
    arguments: dict[str, object],
    worker: str,
) -> None:
    release = threading.Event()

    def blocked(**_kwargs: object) -> ToolResult:
        # Like a read on an unresponsive network mount, this ignores the stop event.
        reached.set()
        release.wait(5)
        return ToolResult(text="late")

    monkeypatch.setattr(search_module, worker, blocked)
    monkeypatch.setattr(search_module, "_SEARCH_DEADLINE_GRACE_SECONDS", 0)

    async def scenario() -> ToolError:
        with anyio.fail_after(2), pytest.raises(ToolError) as caught:
            await tool.run(arguments, ToolContext(cwd=tmp_path))
        return caught.value

    try:
        error = anyio.run(scenario)
    finally:
        release.set()

    assert error.failure_code == "timeout"
    assert "a filesystem call may be blocked" in str(error)


def test_keeps_native_matches_in_current_file(
    tmp_path: Path, monkeypatch: MonkeyPatch, reached: threading.Event
) -> None:
    (tmp_path / "log.txt").write_text("needle one\nneedle two\n", encoding="utf-8")

    class Cancellation:
        def __init__(self) -> None:
            self.cancelled = threading.Event()

        def cancel(self) -> None:
            self.cancelled.set()

    class StoppedScan:
        lines = ["log.txt:1:needle one"]
        byte_count = len(lines[0])
        match_count = 1
        had_extra_match = False
        exhausted = False
        status = "cancelled"

    class Native:
        def GrepCancellation(self) -> Cancellation:
            return Cancellation()

        def scan_literal_fd(
            self, *_args: object, cancellation: Cancellation, **_kwargs: object
        ) -> StoppedScan:
            reached.set()
            assert cancellation.cancelled.wait(5)
            return StoppedScan()

    monkeypatch.setattr(search_module, "_NATIVE_GREP", Native())

    result = run_tool(
        GrepTool(_scanner_backend="native"),
        {"pattern": "needle", "path": "log.txt", "literal": True},
        ToolContext(cwd=tmp_path),
    )

    assert result.text == f"log.txt:1:needle one\n{_DEADLINE_MARKER}"
    assert result.data["count"] == 1
