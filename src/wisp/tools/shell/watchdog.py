"""Stop commands' processes when the backend that started them dies.

Commands run in their own sessions, so nothing stops them if the backend is killed
outright or crashes before its cleanup runs. One watchdog process per backend closes
that gap. The backend registers each POSIX command with it over a pipe whose write end
only the backend holds, and deregisters the command once its cleanup succeeds. When the
backend dies for any reason, the watchdog reads end of file and stops every command
still registered, the same way the backend's own cleanup would, then exits.

The watchdog is a safety net: if it cannot start or stops responding, commands still
run, and a warning is logged once.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

# Two watchdogs that would not start or exited early mean a third would fail too.
_MAX_WATCHDOG_STARTS = 2


@dataclass(frozen=True, slots=True)
class WatchedCommand:
    """What the watchdog needs to stop one command's processes.

    Attributes:
        pid (int): The command's shell leader, which is also its process group id.
        token (str): The command token every process the command starts inherits.
        started_at (float): Wall-clock start time, which bounds the detached-process scan.
        jobs_file (str): The file the command's shell records its background jobs in.
        leader_start (int | None): When the leader started, in a platform unit, so a pid
            reused after the leader exited is not mistaken for it. ``None`` when it could
            not be read, in which case the leader is treated as gone.
    """

    pid: int
    token: str
    started_at: float
    jobs_file: str
    leader_start: int | None


class _Watchdog:
    """The backend's handle on its watchdog process, started with the first command."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._commands: dict[int, WatchedCommand] = {}
        self._starts = 0
        self._warned = False

    def watch(self, command: WatchedCommand) -> None:
        with self._lock:
            self._commands[command.pid] = command
            self._send({"watch": asdict(command)})

    def release(self, pid: int) -> None:
        with self._lock:
            if self._commands.pop(pid, None) is not None:
                self._send({"release": pid})

    def pid(self) -> int | None:
        """Return the watchdog's pid, or ``None`` when none is running."""

        with self._lock:
            return None if self._process is None else self._process.pid

    def _send(self, message: dict[str, object]) -> None:
        line = (json.dumps(message) + "\n").encode()
        while True:
            process = self._process
            if process is None:
                process = self._start()
                if process is None:
                    return
                # A new watchdog learns every live command, including this one.
                if not self._write(process, *self._watch_lines(self._commands.values())):
                    continue
                return
            if self._write(process, line):
                return

    def _start(self) -> subprocess.Popen[bytes] | None:
        if self._starts >= _MAX_WATCHDOG_STARTS:
            return None
        self._starts += 1
        try:
            process = _start_watchdog_process()
        except OSError as exc:
            self._warn(f"could not start: {exc}")
            return None
        assert process.stdin is not None
        # A watchdog that stops reading must not block the event loop.
        os.set_blocking(process.stdin.fileno(), False)
        self._process = process
        return process

    def _write(self, process: subprocess.Popen[bytes], *lines: bytes) -> bool:
        """Write ``lines`` to the watchdog, discarding it if that fails."""

        assert process.stdin is not None
        try:
            for line in lines:
                # Each line is shorter than PIPE_BUF, so a write is never partial.
                os.write(process.stdin.fileno(), line)
        except OSError as exc:
            self._warn(f"stopped responding: {exc}")
            self._discard(process)
            return False
        return True

    def _discard(self, process: subprocess.Popen[bytes]) -> None:
        # Kill it before closing its pipe: end of file would make it stop live commands.
        process.kill()
        process.wait()
        assert process.stdin is not None
        process.stdin.close()
        self._process = None

    def _warn(self, problem: str) -> None:
        if not self._warned:
            self._warned = True
            logger.warning(
                "The process watchdog %s. Commands still run, but a backend crash would "
                "leave them running.",
                problem,
            )

    @staticmethod
    def _watch_lines(commands: Iterable[WatchedCommand]) -> list[bytes]:
        return [(json.dumps({"watch": asdict(command)}) + "\n").encode() for command in commands]


def _start_watchdog_process() -> subprocess.Popen[bytes]:
    # Its own session keeps the terminal's hangup from reaching it, and its output goes
    # nowhere so it never holds the backend's stdout open after the backend exits.
    return subprocess.Popen(
        [sys.executable, "-m", "wisp.tools.shell.watchdog"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd="/",
        start_new_session=True,
    )


_WATCHDOG = _Watchdog()


def watch_command(command: WatchedCommand) -> None:
    """Have the watchdog stop ``command`` if the backend dies before its cleanup."""

    _WATCHDOG.watch(command)


def release_command(pid: int) -> None:
    """Stop watching the command led by ``pid``, once its cleanup has succeeded."""

    _WATCHDOG.release(pid)


def _read_watched_commands(lines: Iterable[bytes]) -> list[WatchedCommand]:
    """Return the commands still registered once the backend has stopped writing."""

    commands: dict[int, WatchedCommand] = {}
    for line in lines:
        # A backend killed mid-write leaves a partial last line.
        if not line.endswith(b"\n"):
            break
        message = json.loads(line)
        if "watch" in message:
            command = WatchedCommand(**message["watch"])
            commands[command.pid] = command
        elif "release" in message:
            commands.pop(message["release"], None)
    return list(commands.values())


def main() -> None:
    """Run the watchdog: wait for the backend to die, then stop its commands."""

    # Imported here because the process module registers commands through this one.
    from wisp.tools.shell.process import stop_watched_command

    for command in _read_watched_commands(sys.stdin.buffer):
        stop_watched_command(command)


if __name__ == "__main__":
    main()
