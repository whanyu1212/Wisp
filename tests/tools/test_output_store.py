from __future__ import annotations

import asyncio
import errno
import os
import stat
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import anyio
import pytest

from tests.tools.support import run_tool
from wisp.tools import output_store as output_store_module
from wisp.tools.context import ToolContext
from wisp.tools.output_store import (
    OutputStore,
    SavedOutput,
    SavedStream,
    UnsavedOutput,
    saved_output_notice,
)
from wisp.tools.read_output import ReadOutputTool
from wisp.tools.result import ToolError

T = TypeVar("T")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "tool-output"


def run(scenario: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(scenario)


async def saved_stream(store: OutputStore, content: bytes, *, threshold: int = 4) -> SavedStream:
    stream = store.open_stream("stdout of `make`", spill_threshold=threshold)
    for start in range(0, len(content), 3):
        await stream.append(content[start : start + 3])
    stream.close()
    return stream


def saved_files(root: Path) -> list[Path]:
    return sorted(root.glob("runtime-*/*.log"))


def test_default_root_is_under_the_wisp_home() -> None:
    assert output_store_module.default_output_root() == Path.home() / ".wisp" / "tool-output"


@pytest.mark.parametrize(("size", "spills"), [(10, False), (11, True)])
def test_stream_spills_only_past_its_threshold(root: Path, size: int, spills: bool) -> None:
    async def scenario() -> object:
        stream = await saved_stream(OutputStore(root), b"x" * size, threshold=10)
        return stream.status()

    assert isinstance(run(scenario), SavedOutput) is spills
    assert root.exists() is spills


def test_spilled_stream_keeps_exact_bytes_split_across_chunks(root: Path) -> None:
    # Three-byte chunks split the two-byte "é" and the four-byte emoji.
    content = "first error\nsecond é\nthird 🚀\nno newline".encode()

    async def scenario() -> tuple[object, bytes]:
        store = OutputStore(root)
        stream = await saved_stream(store, content)
        return stream.status(), saved_files(root)[0].read_bytes()

    status, on_disk = run(scenario)

    assert on_disk == content
    assert status == SavedOutput(
        output_id=status.output_id,  # type: ignore[union-attr]
        saved_bytes=len(content),
        line_count=4,
        omitted_bytes=0,
        complete=True,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_saved_output_is_private(root: Path) -> None:
    async def scenario() -> None:
        await saved_stream(OutputStore(root), b"secret output\n")

    run(scenario)

    saved_file = saved_files(root)[0]
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(saved_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(saved_file.stat().st_mode) == 0o600


def test_save_writes_output_below_threshold(root: Path) -> None:
    async def scenario() -> object:
        store = OutputStore(root)
        stream = await saved_stream(store, b"a\nb\n", threshold=100)
        return await stream.save()

    status = run(scenario)

    assert isinstance(status, SavedOutput)
    assert (status.saved_bytes, status.line_count, status.complete) == (4, 2, True)
    assert saved_files(root)[0].read_bytes() == b"a\nb\n"


def test_save_of_empty_stream_reports_nothing(root: Path) -> None:
    async def scenario() -> object:
        stream = OutputStore(root).open_stream("stderr", spill_threshold=10)
        stream.close()
        return await stream.save()

    assert run(scenario) is None
    assert not root.exists()


def test_stream_cap_keeps_the_beginning_and_counts_the_rest(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_store_module, "MAX_SAVED_STREAM_BYTES", 10)

    async def scenario() -> object:
        stream = await saved_stream(OutputStore(root), b"0123456789abcdef")
        return stream.status()

    status = run(scenario)

    assert isinstance(status, SavedOutput)
    assert (status.saved_bytes, status.omitted_bytes) == (10, 6)
    assert saved_files(root)[0].read_bytes() == b"0123456789"


def test_total_cap_evicts_the_oldest_finished_output(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_store_module, "MAX_SAVED_TOTAL_BYTES", 20)

    async def scenario() -> tuple[object, object, object]:
        store = OutputStore(root)
        first = await saved_stream(store, b"first 1234\n")
        await first.save()  # reported, but finished, so it may still be evicted
        second = await saved_stream(store, b"second 123\n")
        with pytest.raises(ToolError, match="Unknown output_id"):
            await store.read_page(
                first.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
            )
        page = await store.read_page(
            second.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
        )
        return first.status(), second.status(), page.lines

    first, second, lines = run(scenario)

    assert first == UnsavedOutput("it was removed to make room for newer output")
    assert isinstance(second, SavedOutput)
    assert lines == ("second 123",)
    assert len(saved_files(root)) == 1


def test_running_stream_nobody_was_told_about_can_be_evicted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_store_module, "MAX_SAVED_OUTPUTS", 1)

    async def scenario() -> tuple[object, object]:
        store = OutputStore(root)
        unreported = store.open_stream("stdout of a dev server", spill_threshold=0)
        await unreported.append(b"server log\n")
        later = await saved_stream(store, b"later output\n")
        await unreported.append(b"more server log\n")
        return unreported.status(), later.status()

    unreported, later = run(scenario)

    assert unreported == UnsavedOutput("it was removed to make room for newer output")
    assert isinstance(later, SavedOutput)
    assert len(saved_files(root)) == 1


def test_running_stream_the_model_was_told_about_is_kept(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_store_module, "MAX_SAVED_OUTPUTS", 1)

    async def scenario() -> tuple[object, object]:
        store = OutputStore(root)
        reported = store.open_stream("stdout", spill_threshold=0)
        await reported.append(b"still running\n")
        await reported.save()
        later = await saved_stream(store, b"later output\n")
        return reported.status(), later.status()

    reported, later = run(scenario)

    assert isinstance(reported, SavedOutput)
    assert reported.complete is False
    assert later == UnsavedOutput("1 outputs are already saved and still being written")


def test_evicting_a_running_stream_returns_its_bytes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_store_module, "MAX_SAVED_TOTAL_BYTES", 30)

    async def scenario() -> list[object]:
        store = OutputStore(root)
        running = store.open_stream("stdout", spill_threshold=0)
        await running.append(b"x" * 20)
        await running.save()  # reported: kept while it runs
        statuses = []
        for name in ("a", "b", "c"):
            stream = await saved_stream(store, name.encode() * 9 + b"\n")
            statuses.append(stream.status())
        running.close()
        return statuses

    statuses = run(scenario)

    # Each 10-byte stream fits beside the running stream's 20 bytes only after the
    # previous one is evicted, so the store must have returned the evicted bytes.
    assert [
        (status.saved_bytes, status.omitted_bytes)
        for status in statuses
        if isinstance(status, SavedOutput)
    ] == [(10, 0), (10, 0), (10, 0)]
    assert sorted(path.stat().st_size for path in saved_files(root)) == [10, 20]


def test_total_cap_saves_what_fits_when_nothing_can_be_evicted(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(output_store_module, "MAX_SAVED_TOTAL_BYTES", 30)

    async def scenario() -> object:
        store = OutputStore(root)
        running = store.open_stream("stdout", spill_threshold=0)
        await running.append(b"x" * 20)
        await running.save()  # reported and still running: never evicted
        later = await saved_stream(store, b"y" * 15)
        return later.status()

    status = run(scenario)

    assert isinstance(status, SavedOutput)
    assert (status.saved_bytes, status.omitted_bytes) == (10, 5)


def test_write_failure_degrades_to_an_unsaved_status(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(_fd: int, _data: bytes) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), "/private/path")

    monkeypatch.setattr(output_store_module, "_write_all", disk_full)

    async def scenario() -> tuple[object, str]:
        store = OutputStore(root)
        stream = await saved_stream(store, b"lots of output\n")
        with pytest.raises(ToolError, match="Unknown output_id"):
            await store.read_page(
                stream.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
            )
        return stream.status(), saved_output_notice([("stdout", stream.status())]) or ""

    status, notice = run(scenario)

    assert status == UnsavedOutput(os.strerror(errno.ENOSPC))
    assert notice == f"[The full stdout could not be saved: {os.strerror(errno.ENOSPC)}.]"
    assert "/private/path" not in notice
    assert saved_files(root) == []


def test_discard_deletes_the_file(root: Path) -> None:
    async def scenario() -> None:
        store = OutputStore(root)
        stream = await saved_stream(store, b"discard me\n")
        assert saved_files(root)
        stream.discard()
        assert stream.status() is None
        with pytest.raises(ToolError, match="Unknown output_id"):
            await store.read_page(
                stream.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
            )

    run(scenario)

    assert saved_files(root) == []


def test_aclose_removes_the_runtime_directory_and_stops_saving(root: Path) -> None:
    async def scenario() -> object:
        store = OutputStore(root)
        stream = await saved_stream(store, b"before close\n")
        await store.aclose()
        await store.aclose()
        with pytest.raises(ToolError, match="Unknown output_id"):
            await store.read_page(
                stream.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
            )
        late = await saved_stream(store, b"after close\n")
        return late.status()

    assert run(scenario) == UnsavedOutput("the output store is closed")
    assert list(root.iterdir()) == []


def test_first_save_removes_only_abandoned_runtime_directories(root: Path) -> None:
    root.mkdir(mode=0o700)
    crashed = root / "runtime-crashed"
    crashed.mkdir()
    (crashed / ".lock").write_bytes(b"")
    (crashed / "old.log").write_bytes(b"left behind\n")
    starting = root / "runtime-starting"
    starting.mkdir()
    lockless_and_old = root / "runtime-lockless"
    lockless_and_old.mkdir()
    an_hour_ago = time.time() - 3600
    os.utime(lockless_and_old, (an_hour_ago, an_hour_ago))
    unrelated = root / "notes"
    unrelated.mkdir()

    async def scenario() -> None:
        live = OutputStore(root)
        await saved_stream(live, b"live runtime\n")
        live_dir = saved_files(root)[0].parent
        await saved_stream(OutputStore(root), b"another runtime\n")
        assert live_dir.exists()
        await live.aclose()

    run(scenario)

    assert not crashed.exists()
    assert not lockless_and_old.exists()
    assert starting.exists()
    assert unrelated.exists()


@pytest.mark.skipif(os.name != "posix", reason="symlink creation")
def test_symlinked_root_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = tmp_path / "tool-output"
    root.symlink_to(target, target_is_directory=True)

    async def scenario() -> object:
        stream = await saved_stream(OutputStore(root), b"output\n")
        return stream.status()

    status = run(scenario)

    assert isinstance(status, UnsavedOutput)
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="Windows cannot replace an open file")
def test_replacing_the_file_on_disk_does_not_change_what_is_read(root: Path) -> None:
    async def scenario() -> tuple[str, ...]:
        store = OutputStore(root)
        stream = await saved_stream(store, b"genuine\n")
        saved_file = saved_files(root)[0]
        saved_file.unlink()
        saved_file.write_bytes(b"substituted\n")
        page = await store.read_page(
            stream.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
        )
        return page.lines

    assert run(scenario) == ("genuine",)


def test_saving_stops_for_good_once_the_total_cap_drops_bytes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bytes dropped at the cap must not be followed by later bytes that fit after
    # space frees up, or the saved output would silently skip part of the stream.
    monkeypatch.setattr(output_store_module, "MAX_SAVED_TOTAL_BYTES", 30)

    async def scenario() -> tuple[object, tuple[str, ...]]:
        store = OutputStore(root)
        running = store.open_stream("stdout", spill_threshold=0)
        await running.append(b"x" * 20)
        await running.save()  # reported and running: holds 20 bytes
        capped = store.open_stream("stderr", spill_threshold=0)
        await capped.append(b"1234567890ab")
        await capped.save()
        running.close()  # finished, so its bytes can now be evicted
        await capped.append(b"cd\n")
        capped.close()
        page = await store.read_page(
            capped.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
        )
        return capped.status(), page.lines

    status, lines = run(scenario)

    assert lines == ("1234567890",)
    assert isinstance(status, SavedOutput)
    assert (status.saved_bytes, status.omitted_bytes) == (10, 5)


def test_running_stream_pages_show_buffered_output(root: Path) -> None:
    async def scenario() -> tuple[tuple[str, ...], bool]:
        store = OutputStore(root)
        stream = store.open_stream("stdout", spill_threshold=0)
        await stream.append(b"one\n")
        await stream.append(b"two\n")
        page = await store.read_page(
            stream.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
        )
        return page.lines, page.complete

    assert run(scenario) == (("one", "two"), False)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            SavedOutput("ab12", 52113, 1204, 0, True),
            "[Full output saved: stdout output_id=ab12 (52113 bytes, 1204 lines). "
            "Read it with read_output.]",
        ),
        (
            SavedOutput("ab12", 10, 2, 7, False),
            "[Full output saved: stdout output_id=ab12 (10 bytes, 2 lines; 7 later bytes "
            "not saved so far). Read it with read_output.]",
        ),
        (None, None),
    ],
)
def test_saved_output_notice(status: SavedOutput | None, expected: str | None) -> None:
    assert saved_output_notice([("stdout", status)]) == expected


class TestReadOutputTool:
    @staticmethod
    def saved(root: Path, content: bytes) -> tuple[ReadOutputTool, str]:
        store = OutputStore(root)

        async def scenario() -> str:
            return (await saved_stream(store, content)).output_id

        return ReadOutputTool(store), run(scenario)

    def test_pages_with_offset_and_continuation(self, root: Path) -> None:
        content = b"".join(f"line {number}\n".encode() for number in range(1, 11))
        tool, output_id = self.saved(root, content)
        context = ToolContext(cwd=root.parent)

        first = run_tool(tool, {"output_id": output_id, "offset": 3, "limit": 2}, context)
        last = run_tool(tool, {"output_id": output_id, "offset": 9}, context)

        assert first.text == (
            "[stdout of `make`: lines 3-4 of 10]\nline 3\nline 4\n"
            "[More lines follow. Use offset=5 to continue.]"
        )
        assert first.truncated is True
        assert first.data == {
            "output_id": output_id,
            "offset": 3,
            "limit": 2,
            "selected_count": 2,
            "line_count": 10,
        }
        assert last.text == "[stdout of `make`: lines 9-10 of 10]\nline 9\nline 10"
        assert last.truncated is False

    def test_marks_a_shortened_final_line_as_truncated(self, root: Path) -> None:
        tool, output_id = self.saved(root, b"x" * 2000)
        context = ToolContext(cwd=root.parent, max_output_bytes=1000)

        result = run_tool(tool, {"output_id": output_id}, context)

        assert result.text.endswith("x [line truncated]")
        assert result.truncated is True

    def test_shortens_a_line_larger_than_the_budget(self, root: Path) -> None:
        tool, output_id = self.saved(root, b"x" * 2000 + b"\nnext\n")
        context = ToolContext(cwd=root.parent, max_output_bytes=1000)

        result = run_tool(tool, {"output_id": output_id}, context)

        # The store gets the budget less 400 bytes kept for the header and notice; the
        # shortened line, its marker, and its newline fill the rest exactly.
        assert result.text.splitlines()[1] == "x" * 582 + " [line truncated]"
        assert result.text.endswith("[More lines follow. Use offset=2 to continue.]")

    def test_reports_output_lost_to_the_cap(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(output_store_module, "MAX_SAVED_STREAM_BYTES", 6)
        tool, output_id = self.saved(root, b"kept\nlost\n")

        result = run_tool(tool, {"output_id": output_id}, ToolContext(cwd=root.parent))

        assert result.text.splitlines()[1:] == [
            "kept",
            "l",
            "[4 later bytes were not saved because a saved-output limit was reached. "
            "The command's own result shows how the output ended.]",
        ]

    @pytest.mark.parametrize("output_id", ["../../etc/passwd", "missing", " "])
    def test_rejects_ids_it_did_not_issue(self, root: Path, output_id: str) -> None:
        tool, _ = self.saved(root, b"saved\n")

        with pytest.raises(ToolError):
            run_tool(tool, {"output_id": output_id}, ToolContext(cwd=root.parent))

    def test_rejects_an_offset_past_the_end(self, root: Path) -> None:
        tool, output_id = self.saved(root, b"only\n")

        with pytest.raises(ToolError, match="beyond the end"):
            run_tool(tool, {"output_id": output_id, "offset": 5}, ToolContext(cwd=root.parent))


def test_cancelled_caller_waits_for_file_work_to_finish() -> None:
    # A cancelled reader must not continue, and close its file, while a worker
    # thread is still writing to it.
    release = threading.Event()
    finished = threading.Event()

    def slow_write() -> None:
        release.wait(timeout=5)
        finished.set()

    async def scenario() -> bool:
        task = asyncio.create_task(output_store_module._run_in_thread(slow_write))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        still_waiting = not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return still_waiting and finished.is_set()

    assert run(scenario)


@pytest.mark.parametrize(("max_bytes", "expected"), [(10, ("aaaa", "bbbb")), (9, ("aaaa",))])
def test_pages_fill_the_byte_budget_exactly(
    root: Path, max_bytes: int, expected: tuple[str, ...]
) -> None:
    # Each line costs its bytes plus one for the newline that joins it.
    async def scenario() -> tuple[str, ...]:
        store = OutputStore(root)
        stream = await saved_stream(store, b"aaaa\nbbbb\ncccc\n")
        page = await store.read_page(
            stream.output_id, offset=1, limit=None, max_bytes=max_bytes, max_lines=10
        )
        return page.lines

    assert run(scenario) == expected


def test_final_line_without_newline_is_read_in_full(root: Path) -> None:
    async def scenario() -> tuple[str, ...]:
        store = OutputStore(root)
        stream = await saved_stream(store, b"first\nlast line")
        page = await store.read_page(
            stream.output_id, offset=1, limit=None, max_bytes=100, max_lines=10
        )
        return page.lines

    assert run(scenario) == ("first", "last line")
