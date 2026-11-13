"""Moving a running foreground command to the background, at the supervisor."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import python_command
from wisp.tools.progress import OutputProgress
from wisp.tools.result import ToolError
from wisp.tools.shell import supervisor as supervisor_module
from wisp.tools.shell.process import ProcessResult
from wisp.tools.shell.supervisor import ProcessExit, ProcessSupervisor, ProcessUpdate

# Prints 0..1999, one per line, flushing each so the output arrives in many chunks.
_TICKER = python_command(
    "import sys, time\n"
    "for i in range(2000):\n"
    "    print(i, flush=True)\n"
    "    if i % 100 == 99: time.sleep(0.01)\n"
)
_SLEEPER = python_command("import time; time.sleep(30)")


class _MoveOnOutput:
    """Progress that asks for the move as soon as the command has printed."""

    def __init__(self, background: asyncio.Event) -> None:
        self._background = background

    def report(self, progress: OutputProgress) -> None:
        if progress.stdout_bytes:
            self._background.set()


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _move(
    supervisor: ProcessSupervisor, command: str, tmp_path: Path, *, on_output: bool = False
) -> ProcessUpdate:
    background = asyncio.Event()
    if not on_output:
        background.set()
    outcome = await supervisor.run_to_completion(
        command,
        cwd=tmp_path,
        timeout=30,
        max_output_bytes=1_000_000,
        progress=_MoveOnOutput(background) if on_output else None,
        background=background,
    )
    assert isinstance(outcome, ProcessUpdate)
    return outcome


@pytest.mark.process
def test_move_mid_burst_loses_nothing(tmp_path: Path) -> None:
    """The output before the move, then every poll after it, is the whole stream once."""

    heard: list[ProcessExit] = []

    async def run() -> tuple[ProcessUpdate, str, int]:
        supervisor = ProcessSupervisor()
        supervisor.set_exit_listener(heard.append)
        try:
            moved = await _move(supervisor, _TICKER, tmp_path, on_output=True)
            final = await supervisor.poll(moved.process_id, wait_seconds=20, until="exit")
            saved = supervisor._managed[moved.process_id].stdout_saved  # noqa: SLF001
            saved_bytes = saved._saved_bytes + len(saved._buffer)  # noqa: SLF001
            return moved, final.stdout, saved_bytes
        finally:
            await supervisor.aclose()

    moved, rest, saved_bytes = anyio.run(run)

    expected = "".join(f"{i}\n" for i in range(2000))
    assert moved.state == "running"
    assert moved.stdout + rest == expected
    assert saved_bytes == len(expected)
    assert [(e.state, e.exit_code, e.notify) for e in heard] == [("completed", 0, True)]


@pytest.mark.process
def test_move_after_exit_returns_result(tmp_path: Path) -> None:
    """A move asked for once the command has finished gives its ordinary result."""

    async def run() -> tuple[ProcessResult | ProcessUpdate, int, int]:
        supervisor = ProcessSupervisor()
        try:
            background = asyncio.Event()
            task = asyncio.create_task(
                supervisor.run_to_completion(
                    python_command("print('done')"),
                    cwd=tmp_path,
                    timeout=30,
                    max_output_bytes=1_000,
                    background=background,
                )
            )
            # Ask at the moment the command is exiting; either side may win.
            while not supervisor._one_shot:  # noqa: SLF001
                await asyncio.sleep(0)
            (process,) = supervisor._one_shot  # noqa: SLF001
            await process.wait()
            background.set()
            outcome = await task
            return outcome, len(supervisor._one_shot), len(supervisor._managed)  # noqa: SLF001
        finally:
            await supervisor.aclose()

    outcome, one_shot, managed = anyio.run(run)

    assert one_shot == 0
    if isinstance(outcome, ProcessResult):
        assert (outcome.exit_code, outcome.stdout, managed) == (0, "done\n", 0)
    else:
        assert managed == 1


@pytest.mark.process
def test_move_during_cancel_leaves_nothing(tmp_path: Path) -> None:
    """Cancelling the call as the move is asked for still stops the process tree."""

    async def run() -> tuple[int, int, int]:
        supervisor = ProcessSupervisor()
        try:
            background = asyncio.Event()
            task = asyncio.create_task(
                supervisor.run_to_completion(
                    _SLEEPER,
                    cwd=tmp_path,
                    timeout=30,
                    max_output_bytes=1_000,
                    background=background,
                )
            )
            while not supervisor._one_shot:  # noqa: SLF001
                await asyncio.sleep(0)
            (process,) = supervisor._one_shot  # noqa: SLF001
            background.set()
            task.cancel()
            try:
                outcome = await task
            except asyncio.CancelledError:
                outcome = None
            if isinstance(outcome, ProcessUpdate):
                # The move won; the managed process owns the tree now.
                await supervisor.cancel(outcome.process_id)
            return process.pid, len(supervisor._one_shot), supervisor._owned_process_count()  # noqa: SLF001
        finally:
            await supervisor.aclose()

    pid, one_shot, _owned = anyio.run(run)

    assert one_shot == 0
    assert _gone(pid)


@pytest.mark.process
def test_move_keeps_slot_count(tmp_path: Path) -> None:
    """A moved command keeps its slot, so a full supervisor stays full."""

    async def run() -> str:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            await _move(supervisor, _SLEEPER, tmp_path)
            with pytest.raises(ToolError, match="limit") as caught:
                await supervisor.start(_SLEEPER, cwd=tmp_path, timeout=30)
            return str(caught.value)
        finally:
            await supervisor.aclose()

    assert "limit" in anyio.run(run)


@pytest.mark.process
@pytest.mark.parametrize("ending", ["cancel", "stop", "cancel_all", "aclose", "lifetime"])
def test_moved_process_still_cleaned_up(tmp_path: Path, ending: str) -> None:
    """Every way a managed process ends stops a moved command's process tree."""

    async def run() -> int:
        supervisor = ProcessSupervisor()
        background = asyncio.Event()
        background.set()
        outcome = await supervisor.run_to_completion(
            _SLEEPER,
            cwd=tmp_path,
            timeout=30,
            max_output_bytes=1_000,
            background=background,
            background_lifetime=0.2 if ending == "lifetime" else 30,
        )
        assert isinstance(outcome, ProcessUpdate)
        pid = supervisor._managed[outcome.process_id].process.pid  # noqa: SLF001
        try:
            if ending == "cancel":
                await supervisor.cancel(outcome.process_id)
            elif ending == "stop":
                await supervisor.stop(outcome.process_id)
            elif ending == "cancel_all":
                await supervisor.cancel_all()
            elif ending == "lifetime":
                update = await supervisor.poll(outcome.process_id, wait_seconds=10, until="exit")
                assert update.state == "timed_out"
        finally:
            await supervisor.aclose()
        return pid

    assert _gone(anyio.run(run))


@pytest.mark.process
def test_cancel_after_capture_still_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel that lands as a finished command's readers stop still frees its slot."""

    stopping = asyncio.Event()
    release = asyncio.Event()
    original = supervisor_module._stop_readers

    async def slow_stop(readers: tuple[asyncio.Task[None], ...]) -> None:
        stopping.set()
        await release.wait()
        await original(readers)

    monkeypatch.setattr(supervisor_module, "_stop_readers", slow_stop)

    async def run() -> tuple[int, int]:
        supervisor = ProcessSupervisor(max_processes=1)
        try:
            task = asyncio.create_task(
                supervisor.run_to_completion(
                    python_command("print('done')"),
                    cwd=tmp_path,
                    timeout=30,
                    max_output_bytes=1_000,
                )
            )
            await stopping.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            return len(supervisor._one_shot), supervisor._owned_process_count()  # noqa: SLF001
        finally:
            await supervisor.aclose()

    assert anyio.run(run) == (0, 0)


@pytest.mark.parametrize(
    ("data", "complete", "partial"),
    [
        (b"ok\n", b"ok\n", b""),
        ("café".encode(), "café".encode(), b""),
        ("café".encode()[:-1], b"caf", b"\xc3"),
        ("€".encode()[:2], b"", "€".encode()[:2]),
        ("𝄞".encode()[:3], b"", "𝄞".encode()[:3]),
        (b"\x80\x80", b"\x80\x80", b""),
    ],
)
def test_split_incomplete_utf8(data: bytes, complete: bytes, partial: bytes) -> None:
    assert supervisor_module._split_incomplete_utf8(data) == (complete, partial)  # noqa: SLF001
