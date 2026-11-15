"""Listing the session's processes and reading their output for frontends."""

from __future__ import annotations

import asyncio
from pathlib import Path

import anyio
import pytest

from tests.tools.shell.supervisor_support import python_command
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import ProcessSupervisor, ProcessUpdate


def _printed(supervisor: ProcessSupervisor, process_id: str) -> str:
    _, tail = supervisor.output_tail(process_id)
    return "".join(chunk.text for chunk in tail.chunks)


async def _until_printed(supervisor: ProcessSupervisor, process_id: str, text: str) -> None:
    with anyio.fail_after(10):
        while text not in _printed(supervisor, process_id):
            await asyncio.sleep(0.01)


@pytest.mark.process
@pytest.mark.parametrize("ended", [False, True])
def test_read_leaves_poll_unchanged(tmp_path: Path, ended: bool) -> None:
    """Reading the tail drains nothing: the agent's poll still gets every line."""

    source = "print('one', flush=True)\n" + ("" if ended else "import time; time.sleep(30)\n")

    async def run() -> tuple[str, bool, ProcessUpdate]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(python_command(source), cwd=tmp_path, timeout=60)
            await _until_printed(supervisor, process_id, "one")
            if ended:
                with anyio.fail_after(10):
                    while supervisor.output_tail(process_id)[0].state == "running":
                        await asyncio.sleep(0.01)
            read = _printed(supervisor, process_id)
            reported_after_read = supervisor.is_terminal_reported(process_id)
            polled = await supervisor.poll(process_id)
            return read, reported_after_read, polled
        finally:
            await supervisor.aclose()

    read, reported_after_read, polled = anyio.run(run)

    assert read == "one\n"
    assert polled.stdout == "one\n"
    assert reported_after_read is False
    assert polled.state == ("completed" if ended else "running")


@pytest.mark.process
def test_listing_and_session_end(tmp_path: Path) -> None:
    """Ended processes stay listed with their outcome until the session ends."""

    async def run() -> None:
        supervisor = ProcessSupervisor()
        try:
            failing = await supervisor.start(
                python_command("raise SystemExit(3)"), cwd=tmp_path, timeout=60
            )
            sleeper = await supervisor.start(
                python_command("import time; time.sleep(30)"), cwd=tmp_path, timeout=60
            )
            with anyio.fail_after(10):
                while supervisor.output_tail(failing)[0].state == "running":
                    await asyncio.sleep(0.01)
            assert await supervisor.stop(sleeper) == "cancelled"

            listed = {process.process_id: process for process in supervisor.processes()}
            assert [process.process_id for process in supervisor.processes()] == [
                failing,
                sleeper,
            ]
            assert (listed[failing].state, listed[failing].exit_code) == ("completed", 3)
            assert listed[failing].ended_at is not None
            assert (listed[sleeper].state, listed[sleeper].stopped_by_user) == (
                "cancelled",
                True,
            )
            assert supervisor.running_processes() == ()

            supervisor.unlist_all()

            assert supervisor.processes() == ()
            with pytest.raises(ToolError, match=sleeper):
                supervisor.output_tail(sleeper)
            # The agent can still poll a process by id after its session ends.
            assert (await supervisor.poll(sleeper)).state == "cancelled"
        finally:
            await supervisor.aclose()

    anyio.run(run)


def test_unknown_process_names_id() -> None:
    with pytest.raises(ToolError, match="process-gone"):
        ProcessSupervisor().output_tail("process-gone")


@pytest.mark.process
def test_moved_process_keeps_earlier_output(tmp_path: Path) -> None:
    """A command moved from the foreground shows what it printed before the move."""

    marker = tmp_path / "moved"
    source = (
        "import os, time\n"
        "print('before', flush=True)\n"
        f"while not os.path.exists({str(marker)!r}): time.sleep(0.01)\n"
        "print('after', flush=True)\n"
    )

    async def run() -> str:
        supervisor = ProcessSupervisor()
        background = asyncio.Event()

        class _MoveOnOutput:
            def report(self, progress: object) -> None:
                if getattr(progress, "stdout_bytes", 0):
                    background.set()

        try:
            moved = await supervisor.run_to_completion(
                python_command(source),
                cwd=tmp_path,
                timeout=30,
                max_output_bytes=1_000_000,
                progress=_MoveOnOutput(),
                background=background,
            )
            assert isinstance(moved, ProcessUpdate)
            marker.touch()
            await _until_printed(supervisor, moved.process_id, "after")
            return _printed(supervisor, moved.process_id)
        finally:
            await supervisor.aclose()

    assert anyio.run(run) == "before\nafter\n"


@pytest.mark.process
def test_ended_process_stays_until_room_needed(tmp_path: Path) -> None:
    """A reported ended process stays listed until a new process needs its slot."""

    async def run() -> tuple[list[str], list[str], str, str]:
        supervisor = ProcessSupervisor(max_processes=2)
        try:
            done = await supervisor.start(python_command("pass"), cwd=tmp_path, timeout=60)
            await supervisor.poll(done, wait_seconds=10, until="exit")
            first = await supervisor.start(
                python_command("import time; time.sleep(30)"), cwd=tmp_path, timeout=60
            )
            # The agent's poll reported the final state, but a slot was still free.
            kept = [process.process_id for process in supervisor.processes()]
            await supervisor.cancel(first)
            second = await supervisor.start(
                python_command("import time; time.sleep(30)"), cwd=tmp_path, timeout=60
            )
            after = [process.process_id for process in supervisor.processes()]
            return kept, after, done, second
        finally:
            await supervisor.aclose()

    kept, after, done, second = anyio.run(run)

    assert kept[0] == done
    # Both slots were taken, so the oldest reported process made room.
    assert done not in after
    assert after[-1] == second


@pytest.mark.process
def test_tail_matches_agent_output(tmp_path: Path) -> None:
    """Interleaved streams reach the tail in order and the agent's poll in full."""

    source = (
        "import sys, time\n"
        "for i in range(3):\n"
        "    print(f'out {i}', flush=True)\n"
        "    time.sleep(0.05)\n"
        "    print(f'err {i}', file=sys.stderr, flush=True)\n"
        "    time.sleep(0.05)\n"
    )

    async def run() -> tuple[list[tuple[str, str]], ProcessUpdate]:
        supervisor = ProcessSupervisor()
        try:
            process_id = await supervisor.start(python_command(source), cwd=tmp_path, timeout=60)
            polled = await supervisor.poll(process_id, wait_seconds=10, until="exit")
            _, tail = supervisor.output_tail(process_id)
            return [(chunk.stream, chunk.text) for chunk in tail.chunks], polled
        finally:
            await supervisor.aclose()

    chunks, polled = anyio.run(run)

    assert chunks == [
        chunk for i in range(3) for chunk in (("stdout", f"out {i}\n"), ("stderr", f"err {i}\n"))
    ]
    assert "".join(text for stream, text in chunks if stream == "stdout") == polled.stdout
    assert "".join(text for stream, text in chunks if stream == "stderr") == polled.stderr
