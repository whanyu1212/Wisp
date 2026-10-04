"""Commands and polling helpers shared by the process supervisor tests."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import anyio

from wisp.tools.shell.supervisor import (
    ProcessSupervisor,
    ProcessUpdate,
)


def python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def wait_for_pid_file(path: Path) -> int:
    with anyio.fail_after(3):
        while True:
            try:
                text = path.read_text().strip()
            except FileNotFoundError:
                text = ""
            if text:
                return int(text)
            await anyio.sleep(0.01)


async def poll_until_terminal(
    supervisor: ProcessSupervisor,
    process_id: str,
    *,
    timeout: float = 3,
) -> tuple[ProcessUpdate, ...]:
    updates: list[ProcessUpdate] = []
    with anyio.fail_after(timeout):
        while True:
            update = await supervisor.poll(process_id, wait_seconds=0.1)
            updates.append(update)
            if update.state != "running":
                return tuple(updates)
