"""Wheel and trackpad scrolling reaches earlier turns through a real pseudo-terminal (HY-246).

A scripted backend streams two turns. The test scrolls up with SGR wheel reports
until the first turn's opening row is painted again, while idle and while a run
keeps emitting turn events. Finally one short gesture, sent together with a
resize and with the pointer over the composer, must still scroll; before HY-246
wheel input outside the transcript rows was dropped.
"""

from __future__ import annotations

import errno
import fcntl
import itertools
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.tui_e2e.support import (
    CONTEXT_STATS_BACKEND,
    complete_logged_commands,
    reap,
    rendered_without_spacing,
    welcome_ready,
)
from wisp import __version__

# The first turn's opening row, off screen once the turn has streamed.
EARLIEST = b"alpha-row-00"
# SGR wheel-up at column 10, row 8 (one-based), inside the transcript.
WHEEL_UP = b"\x1b[<64;10;8M"
# The same report with the pointer resting on the composer row, where the
# cursor sits after typing a prompt.
WHEEL_UP_OVER_COMPOSER = b"\x1b[<64;10;22M"
CTRL_END = b"\x1b[1;5F"

BACKEND = (
    CONTEXT_STATS_BACKEND
    + """
import json
import sys
import time
from pathlib import Path
from wisp.events import (
    MessageStarted, MessageCompleted, RpcCommandFinished, TurnStarted,
    RpcConnectionCatalogReported, RpcConnectionCatalogSnapshot, RpcMessagesReported,
)

log_path = Path(sys.argv[1])
stop_path = Path(sys.argv[2])
def emit(event):
    print(event.model_dump_json(), flush=True)
def finish(command):
    emit(RpcCommandFinished(command_id=command["id"], command_type=command["type"], ok=True))
def rows(turn, label):
    for index in range(40):
        emit(MessageStarted(turn=turn))
        emit(MessageCompleted(turn=turn, content=f"{label}-row-{index:02d}",
            message_entry_id=f"{label}-{index}", finish_reason="stop"))
request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type":"rpc.handshake.accepted", "backend_package_version":request["frontend_version"],
    "capabilities":[],
    "limits":{"max_client_frame_bytes":67108864,"max_server_frame_bytes":67108864},
}), flush=True)
for line in sys.stdin:
    command = json.loads(line)
    with log_path.open("a") as log:
        log.write(json.dumps(command) + "\\n")
    kind = command["type"]
    if kind == "get_session_stats":
        report_stats(command)
    elif kind == "get_skills":
        report_skills(command)
    elif kind == "get_connection_catalog":
        emit(RpcConnectionCatalogReported(
            command_id=command["id"], catalog=RpcConnectionCatalogSnapshot()))
        finish(command)
    elif kind == "get_messages":
        # No persisted session: every history read is empty.
        emit(RpcMessagesReported(command_id=command["id"], session_id=command.get("session_id")))
        finish(command)
    elif kind == "prompt" and command["prompt"] == "first":
        rows(1, "alpha")
        finish(command)
    elif kind == "prompt":
        rows(2, "beta")
        # Keep the run busy with events that change what a click would target,
        # until the test has scrolled back to the first turn.
        turn = 3
        deadline = time.monotonic() + 20
        while not stop_path.exists() and time.monotonic() < deadline:
            emit(TurnStarted(turn=turn))
            turn += 1
            time.sleep(0.02)
        finish(command)
    else:
        finish(command)
        if kind == "shutdown":
            break
"""
)


@pytest.mark.process
def test_wheel_reaches_earlier_turns_idle_during_a_run_and_after_a_resize(
    tmp_path: Path,
) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)
    backend = tmp_path / "wheel_backend.py"
    backend.write_text(BACKEND, encoding="utf-8")
    command_log = tmp_path / "wheel_commands.jsonl"
    stop = tmp_path / "stop"
    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        environment = {**os.environ}
        environment.pop("WISP_TUI_MOUSE", None)
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                str(command_log),
                str(stop),
            ],
            environment,
        )

    def resize(width: int) -> None:
        fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))

    widths = itertools.cycle(range(100, 120))

    def full_frame() -> None:
        # Output is differential, so a moved row may repaint only the cells that
        # changed. A resize requests a full frame in which rows appear whole.
        resize(next(widths))

    def prompts() -> list[object]:
        return [
            item["prompt"]
            for item in complete_logged_commands(command_log)
            if item["type"] == "prompt"
        ]

    resize(90)
    output = bytearray()
    phase = "startup"
    reached: list[str] = []
    target = b""
    bursts = 0
    busy_since = 0.0
    next_wheel = 0.0
    settle_until = 0.0
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline and phase != "done":
            readable, _, _ = select.select([terminal_fd], [], [], 0.02)
            if readable:
                try:
                    output.extend(os.read(terminal_fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            now = time.monotonic()
            painted = rendered_without_spacing(output)
            if phase == "startup" and b"ctx" in output and b"~" in output:
                # Differential output need not repeat the welcome text; a resize
                # requests a full frame.
                resize(91)
                output.clear()
                phase = "ready"
            elif phase == "ready" and welcome_ready(output):
                os.write(terminal_fd, b"first\r")
                phase = "first turn"
            elif phase == "first turn" and _read_after_prompt(command_log, 1):
                # The first run finished: the frontend reads history after a prompt.
                output.clear()
                phase = "idle scroll"
            elif phase in {"idle scroll", "busy scroll"}:
                if EARLIEST in painted:
                    reached.append(phase)
                    output.clear()
                    if phase == "idle scroll":
                        os.write(terminal_fd, CTRL_END + b"second\r")
                        phase = "second turn"
                    else:
                        # Reached while the run was still emitting events.
                        assert not stop.exists()
                        assert now - busy_since < 15
                        stop.touch()
                        phase = "busy settle"
                elif now >= next_wheel:
                    os.write(terminal_fd, WHEEL_UP * 20)
                    bursts += 1
                    if bursts % 5 == 0:
                        full_frame()
                    next_wheel = now + 0.1
            elif phase == "second turn" and b"beta-row-" in painted and b"working" in painted:
                # The footer stays working until the command finishes; a completed
                # reply no longer brings back the Working row. Differential output
                # need not repeat a whole row, so any second-turn row will do.
                assert prompts() == ["first", "second"]
                assert not _read_after_prompt(command_log, 2)
                busy_since = now
                output.clear()
                phase = "busy scroll"
            elif phase == "busy settle" and _read_after_prompt(command_log, 2):
                os.write(terminal_fd, CTRL_END)
                settle_until = now + 0.5
                phase = "tail"
            elif phase == "tail" and now >= settle_until:
                # A full frame shows which rows the tail holds.
                full_frame()
                output.clear()
                settle_until = now + 0.5
                phase = "tail frame"
            elif phase == "tail frame" and now >= settle_until:
                visible = sorted(int(row) for row in re.findall(rb"beta-row-(\d\d)", painted))
                if not visible:
                    # The frame may still be arriving on a slow runner.
                    assert now < settle_until + 5, f"no beta rows: {bytes(output)[-2000:]!r}"
                    continue
                target = f"beta-row-{visible[0] - 1:02d}".encode()
                output.clear()
                # One short gesture over the composer, sent with a resize. It is
                # never repeated: a dropped gesture fails the test.
                resize(92)
                os.write(terminal_fd, WHEEL_UP_OVER_COMPOSER * 10)
                settle_until = now + 0.5
                phase = "resize scroll"
            elif phase == "resize scroll" and now >= settle_until:
                # The gesture has been handled; check the scrolled frame whole.
                output.clear()
                full_frame()
                settle_until = now + 2.5
                phase = "resize check"
            elif phase == "resize check":
                if target in painted:
                    reached.append("resize scroll")
                    phase = "done"
                elif now >= settle_until:
                    break
        assert reached == ["idle scroll", "busy scroll", "resize scroll"], (
            f"stopped in {phase!r}: {bytes(output)[-2000:]!r}"
        )
    finally:
        stop.touch()
        reap(child_pid, terminal_fd)


def _read_after_prompt(command_log: Path, count: int) -> bool:
    """Return whether the frontend read history after its ``count``-th prompt finished."""
    commands = complete_logged_commands(command_log)
    prompts = [index for index, item in enumerate(commands) if item["type"] == "prompt"]
    return len(prompts) == count and any(
        item["type"] == "get_messages" for item in commands[prompts[-1] + 1 :]
    )
