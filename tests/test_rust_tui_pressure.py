from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from wisp import __version__

_PRESSURE_BACKEND = r"""
import json
import os
import sys
import time
from pathlib import Path

from wisp.events import (
    EVENT_SCHEMA_VERSION,
    AgentCompleted,
    AgentStarted,
    MessageCompleted,
    MessageDelta,
    MessageStarted,
    RpcCommandFinished,
    RpcMessagesReported,
)
from wisp.rpc.protocol import LIVE_RPC_PROTOCOL_VERSION


mode = sys.argv[1]
pid_path = Path(sys.argv[2])
release_path = Path(sys.argv[3])
burst_done_path = Path(sys.argv[4])
pid_path.write_text(str(os.getpid()), encoding="utf-8")


def emit(event):
    sys.stdout.write(event.model_dump_json() + "\n")
    sys.stdout.flush()


request = json.loads(sys.stdin.readline())
print(json.dumps({
    "type": "rpc.handshake.accepted",
    "backend_package_version": request["frontend_version"],
    "protocol_version": LIVE_RPC_PROTOCOL_VERSION,
    "event_schema_version": EVENT_SCHEMA_VERSION,
    "min_protocol_version": LIVE_RPC_PROTOCOL_VERSION,
    "max_protocol_version": LIVE_RPC_PROTOCOL_VERSION,
    "capabilities": [],
    "limits": {
        "max_client_frame_bytes": 67108864,
        "max_server_frame_bytes": 67108864,
    },
}), flush=True)

for line in sys.stdin:
    command = json.loads(line)
    command_type = command["type"]
    command_id = command["id"]
    if command_type == "get_messages":
        emit(RpcMessagesReported(command_id=command_id))
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=True,
        ))
    elif command_type == "prompt":
        emit(AgentStarted(session_id="pressure-session"))
        emit(MessageStarted(turn=1))
        if mode in {"burst", "signal-burst"}:
            # One bounded write makes the producer substantially outrun terminal
            # rendering and exercises admission beyond the 64-event queue.
            frames = []
            event_count = 192 if mode == "burst" else 1024
            prefix = "BURST" if mode == "burst" else "SIGNAL-BURST"
            for index in range(event_count):
                marker = f"{prefix}-{index:04d}:"
                delta = marker + ("x" * (1024 - len(marker)))
                frames.append(MessageDelta(turn=1, delta=delta).model_dump_json() + "\n")
            frames.extend([
                MessageCompleted(
                    turn=1,
                    content="BURST-FIRST BURST-LAST",
                    finish_reason="stop",
                ).model_dump_json() + "\n",
                AgentCompleted(
                    session_id="pressure-session",
                    turns=1,
                    outcome="completed",
                ).model_dump_json() + "\n",
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="prompt",
                    ok=True,
                ).model_dump_json() + "\n",
            ])
            sys.stdout.write("".join(frames))
            sys.stdout.flush()
            burst_done_path.touch()
        else:
            emit(MessageDelta(turn=1, delta="PARTIAL-BEFORE-FAULT"))
            while not release_path.exists():
                time.sleep(0.01)
            if mode == "eof":
                raise SystemExit(23)
            sys.stdout.buffer.write(b'{"type": definitely-not-json}\n')
            sys.stdout.buffer.flush()
            # The frontend must terminate a backend that remains alive after a
            # fatal protocol error.
            while True:
                time.sleep(1)
    elif command_type == "shutdown":
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type="shutdown",
            ok=True,
        ))
        break
    else:
        emit(RpcCommandFinished(
            command_id=command_id,
            command_type=command_type,
            ok=True,
        ))
"""


def _rust_binary() -> Path:
    value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    return Path(value).resolve(strict=True)


@dataclass
class _TuiProcess:
    pid: int
    fd: int
    initial_terminal: list[int | list[bytes]]
    output: bytearray = field(default_factory=bytearray)
    status: int | None = None

    def send(self, value: bytes) -> None:
        os.write(self.fd, value)

    def wait_until(
        self,
        predicate: Callable[[bytes], bool],
        *,
        timeout: float = 20,
        failure: str,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.fd], [], [], 0.05)
            if readable:
                try:
                    self.output.extend(os.read(self.fd, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            if self.status is None:
                waited_pid, waited_status = os.waitpid(self.pid, os.WNOHANG)
                if waited_pid == self.pid:
                    self.status = waited_status
            if predicate(bytes(self.output)):
                return bytes(self.output)
            if self.status is not None:
                break
        pytest.fail(f"{failure}; terminal tail={bytes(self.output[-8000:])!r}")

    def wait_for(self, *markers: bytes, since: int = 0, failure: str) -> bytes:
        return self.wait_until(
            lambda output: all(marker in output[since:] for marker in markers),
            failure=failure,
        )

    def wait_ready(self) -> None:
        self.wait_for(
            b"Type a prompt below to start.",
            failure="Rust TUI did not finish startup",
        )

    def wait_for_exit(self, *, failure: str) -> int:
        self.wait_until(lambda _output: self.status is not None, failure=failure)
        assert self.status is not None
        return os.waitstatus_to_exitcode(self.status)

    def close(self) -> None:
        if self.status is None:
            try:
                os.killpg(os.tcgetpgrp(self.fd), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
        os.close(self.fd)


def _launch(tmp_path: Path, *, mode: str) -> tuple[_TuiProcess, Path, Path, Path]:
    binary = _rust_binary()
    backend = tmp_path / "pressure_backend.py"
    backend.write_text(_PRESSURE_BACKEND, encoding="utf-8")
    backend_pid = tmp_path / "backend.pid"
    release = tmp_path / "release-fault"
    burst_done = tmp_path / "burst-done"

    child_pid, terminal_fd = pty.fork()
    if child_pid == 0:
        os.chdir(tmp_path)
        environment = {**os.environ, "WISP_TUI_MOUSE": "0"}
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                mode,
                str(backend_pid),
                str(release),
                str(burst_done),
            ],
            environment,
        )

    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    return (
        _TuiProcess(
            pid=child_pid,
            fd=terminal_fd,
            initial_terminal=termios.tcgetattr(terminal_fd),
        ),
        backend_pid,
        release,
        burst_done,
    )


def _backend_process_has_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.process
def test_finite_burst_beyond_event_queue_capacity_drains_in_order(tmp_path: Path) -> None:
    tui, backend_pid_path, _release, burst_done = _launch(tmp_path, mode="burst")
    try:
        tui.wait_ready()
        assert backend_pid_path.exists()
        offset = len(tui.output)
        tui.send(b"pressure burst\r")
        tui.wait_until(
            lambda output: (
                b"BURST-FIRST" in output[offset:]
                and b"BURST-LAST" in output[offset:]
                and output.rfind(b"idle") > output.rfind(b"running")
            ),
            failure="bounded burst did not reach its final event",
        )
        assert b"frontend cannot keep up" not in tui.output
        assert burst_done.exists()
        tui.send(b"\x03")
        assert tui.wait_for_exit(failure="Rust TUI did not exit after the burst") == 0
        assert termios.tcgetattr(tui.fd) == tui.initial_terminal
    finally:
        tui.close()

    backend_pid = int(backend_pid_path.read_text(encoding="utf-8"))
    assert _backend_process_has_exited(backend_pid)


@pytest.mark.process
def test_external_sigint_interrupts_a_sustained_finite_burst(tmp_path: Path) -> None:
    tui, backend_pid_path, _release, burst_done = _launch(tmp_path, mode="signal-burst")
    try:
        tui.wait_ready()
        assert backend_pid_path.exists()
        offset = len(tui.output)
        tui.send(b"interrupt pressure\r")
        tui.wait_for(
            b"SIGNAL-BURST-",
            since=offset,
            failure="sustained burst did not begin rendering",
        )
        assert not burst_done.exists(), "backend finished before SIGINT exercised pressure"
        signal_offset = len(tui.output)
        os.kill(tui.pid, signal.SIGINT)
        tui.wait_until(
            lambda output: all(
                marker in output[signal_offset:]
                for marker in (b"Cancelling", b"current", b"prompt")
            ),
            timeout=5,
            failure="external SIGINT was starved by the burst",
        )
        tui.wait_until(
            lambda output: (
                burst_done.exists()
                and b"BURST-LAST" in output[signal_offset:]
                and output.rfind(b"idle") > output.rfind(b"running")
            ),
            failure="finite burst did not settle after cancellation",
        )
        os.kill(tui.pid, signal.SIGINT)
        assert tui.wait_for_exit(failure="Rust TUI did not exit after the second SIGINT") == 0
        assert termios.tcgetattr(tui.fd) == tui.initial_terminal
    finally:
        tui.close()

    backend_pid = int(backend_pid_path.read_text(encoding="utf-8"))
    assert _backend_process_has_exited(backend_pid)


@pytest.mark.process
@pytest.mark.parametrize("fault", ["eof", "malformed"])
def test_transport_fault_restores_terminal_and_cleans_up_backend(
    tmp_path: Path,
    fault: str,
) -> None:
    tui, backend_pid_path, release, _burst_done = _launch(tmp_path, mode=fault)
    try:
        tui.wait_ready()
        assert backend_pid_path.exists()
        offset = len(tui.output)
        tui.send(b"trigger fault\r")
        tui.wait_for(
            b"PARTIAL-BEFORE-FAULT",
            since=offset,
            failure="partial response did not render before the fault",
        )
        release.touch()
        assert tui.wait_for_exit(failure=f"Rust TUI did not exit after {fault}") != 0
        assert termios.tcgetattr(tui.fd) == tui.initial_terminal
    finally:
        tui.close()

    output = bytes(tui.output)
    assert b"PARTIAL-BEFORE-FAULT" in output
    if fault == "eof":
        assert b"backend stream ended unexpectedly" in output
        assert b"partial assistant response" in output
        assert b"backend exited unsuccessfully" in output
    else:
        assert b"invalid UTF-8 JSON RPC object" in output
        assert b"abandoned queued events=" in output

    backend_pid = int(backend_pid_path.read_text(encoding="utf-8"))
    assert _backend_process_has_exited(backend_pid)
