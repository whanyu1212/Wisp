from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import sys
import termios
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.rust_tui.test_rust_tui_readiness import _rust_binary, _TuiProcess
from wisp import __version__


@pytest.mark.process
def test_queue_management_against_blocked_real_rpc_host(tmp_path: Path) -> None:
    binary = _rust_binary()
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "queue.jsonl"
    backend = tmp_path / "backend.py"
    backend.write_text(
        """import json
import sys
import anyio
from pathlib import Path
from wisp.providers.fake import FakeProvider
from wisp.providers.events import ProviderResponseStarted, ProviderTextDelta
from wisp.rpc import execution
from wisp.cli.application import main
from wisp.cli import rpc

log = Path(sys.argv[1])
sessions = sys.argv[2]
async def blocked(self, messages, **kwargs):
    yield ProviderResponseStarted(model="fake")
    yield ProviderTextDelta(delta="blocked-provider-ready")
    await anyio.Event().wait()
FakeProvider.stream = blocked
original = execution.handle_rpc_queue_command
async def recorded(command, **kwargs):
    await original(command, **kwargs)
    state = kwargs["agent"].queue_state(kwargs["session"])
    with log.open("a") as stream:
        record = {
            "command": command.model_dump(mode="json", exclude_none=True),
            "state": state.model_dump(mode="json"),
        }
        stream.write(json.dumps(record) + "\\n")
execution.handle_rpc_queue_command = recorded
write_event = rpc._write_json_event
def record_finished(event):
    write_event(event)
    if event.type == "rpc.command.finished":
        with log.open("a") as stream:
            record = {"command": {"type": "finished:" + event.command_type}}
            stream.write(json.dumps(record) + "\\n")
rpc._write_json_event = record_finished
sys.argv = ["wisp", "--mode", "rpc", "--provider", "fake", "--session-dir", sessions]
main()
""",
        encoding="utf-8",
    )

    def reports(kind: str) -> list[dict[str, object]]:
        if not log.exists():
            return []
        result = []
        for line in log.read_text().splitlines(keepends=True):
            if line.endswith("\n"):
                record = json.loads(line)
                if record["command"]["type"] == kind:
                    result.append(record)
        return result

    child, fd = pty.fork()
    if child == 0:
        environment = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "WISP_TRUST": "1",
            "WISP_RUST_TUI_BINDINGS_JSON": '{"queue.manage":["F7"]}',
        }
        for key in ("WISP_PROVIDER", "WISP_MODEL", "WISP_FAKE_STREAM_INTERVAL_MS"):
            environment.pop(key, None)
        os.execve(
            str(binary),
            [
                str(binary),
                "--expected-backend-version",
                __version__,
                "--",
                sys.executable,
                str(backend),
                str(log),
                str(tmp_path / "sessions"),
            ],
            environment,
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    tui = _TuiProcess(child, fd, termios.tcgetattr(fd))
    width = 102

    def full_repaint_after(output: bytes, offset: int) -> bytes | None:
        # A resize clears the screen inside the next synchronized frame.
        start = output.find(b"\x1b[?2026h\x1b[2J", offset)
        end = output.find(b"\x1b[?2026l", start) if start >= 0 else -1
        return output[start:end] if end >= 0 else None

    idle_footer = "↑↓ select".encode()

    def wait_for_full_repaint(accept: Callable[[bytes], bool], *, failure: str) -> None:
        """Force full repaints until one shows the expected current state.

        Incremental frames can split text across cursor moves, so each resize is
        checked as one complete repaint. A resize can be handled before earlier
        input or backend events, so request another until one is accepted.

        Args:
            accept (Callable[[bytes], bool]): Checks the bytes of one full repaint.
            failure (str): Message used if no repaint is accepted in time.
        """
        nonlocal width
        deadline = time.monotonic() + 15
        while True:
            offset = len(tui.output)
            width = 103 if width == 102 else 102
            tui.resize(width=width)

            def repainted(output: bytes, start: int = offset) -> bool:
                return full_repaint_after(output, start) is not None

            output = tui.wait_until(repainted, failure="TUI did not repaint after resize")
            frame = full_repaint_after(output, offset)
            assert frame is not None
            if accept(frame):
                return
            if time.monotonic() >= deadline:
                pytest.fail(f"{failure}; last full repaint={frame!r}")

    def settle() -> None:
        """Wait until the TUI has consumed every event of the last queue action.

        The TUI discards input that arrives while workflow events are still queued,
        so input must not race the previous action's results. Actions mark
        themselves pending before sending a command, so once the backend has
        reported the result, a repaint forced after that point shows the idle footer
        only after the TUI has applied the result.
        """
        wait_for_full_repaint(
            lambda frame: idle_footer in frame, failure="queue manager did not settle"
        )

    def select_row(index: int, label: bytes) -> int:
        """Navigate to a row and activate it.

        The current selection must differ from ``index``: the highlighted target row
        is then painted only after every navigation key was applied, and Enter is
        ignored until the new choice has been painted. No resize happens between
        that paint and Enter, because a resize also invalidates the painted choice.

        Returns:
            int: Output offset from just before Enter, for waiting on its effect.
        """
        settle()
        offset = len(tui.output)
        tui.send(b"\x1b[H" + b"\x1b[B" * index)
        tui.wait_for("› ".encode() + label, since=offset, failure="queue choice did not paint")
        offset = len(tui.output)
        tui.send(b"\r")
        return offset

    def wait_report(kind: str, count: int) -> None:
        tui.wait_until(lambda _: len(reports(kind)) >= count, failure=f"missing {kind} result")

    try:
        tui.wait_ready()
        tui.send(b"hold this run\r")
        tui.wait_for(b"blocked-provider-ready", failure="provider did not block")
        tui.send(b"first steering\r")
        wait_report("steer", 1)
        tui.send(b"later follow-up\x1b\r")
        wait_report("follow_up", 1)
        tui.send(b"draft-kept\x1b[18~")
        tui.wait_for(b"Switch queue", failure="queue manager did not open")

        select_row(1, b"Change drain mode")
        wait_report("set_queue_mode", 1)
        assert reports("set_queue_mode")[-1]["state"]["steering_mode"] == "all"
        assert reports("set_queue_mode")[-1]["command"]["expected_token"]

        chosen = select_row(3, b"Clear this queue")
        tui.wait_for("› Cancel".encode(), since=chosen, failure="confirmation did not open")
        select_row(1, b"Confirm clear")
        wait_report("clear_queue", 1)
        cleared = reports("clear_queue")[-1]
        assert cleared["command"]["kind"] == "steering"
        assert cleared["state"]["steering"] == []
        assert cleared["state"]["follow_up"] == ["later follow-up"]

        chosen = select_row(4, b"Clear both queues")
        tui.wait_for("› Cancel".encode(), since=chosen, failure="confirmation did not open")
        tui.send(b"\r")  # Cancel is the default; no destructive command.
        before = len(reports("get_queue_state"))
        select_row(5, b"Refresh")
        wait_report("get_queue_state", before + 1)
        assert len(reports("clear_queue")) == 1

        select_row(4, b"Clear both queues")
        select_row(1, b"Confirm clear")
        wait_report("clear_queue", 2)
        cleared = reports("clear_queue")[-1]
        assert "kind" not in cleared["command"]
        assert cleared["state"]["steering"] == cleared["state"]["follow_up"] == []

        settle()
        tui.send(b"\x1b")
        # The draft is visible under the overlay too, so wait until the overlay is
        # gone: an unread Esc followed by Alt+Enter parses as Esc and a plain Enter.
        wait_for_full_repaint(
            lambda frame: b"draft-kept" in frame and idle_footer not in frame,
            failure="queue overlay lost the composer draft",
        )
        # Keep work across cancellation, then restore it on the first attempt
        # in a replacement run without opening the manager or manually refreshing.
        tui.send(b"\x1b\r")
        wait_report("follow_up", 2)
        old_token = reports("follow_up")[-1]["state"]["token"]
        metadata_before_cancel = {
            kind: len(reports(f"finished:{kind}")) for kind in ("get_session_stats", "get_messages")
        }
        offset = len(tui.output)
        tui.send(b"\x03")
        tui.wait_for(b"idle", since=offset, failure="run did not cancel")
        # Statistics and branch metadata refresh independently after a prompt.
        # Idle status (or statistics alone) does not mean both have completed.
        for kind, count in metadata_before_cancel.items():
            wait_report(f"finished:{kind}", count + 1)
        offset = len(tui.output)
        tui.send(b"replacement run\r")
        tui.wait_for(
            b"blocked-provider-ready", since=offset, failure="replacement run did not start"
        )
        tui.send(b"\x1b[1;3A")
        wait_report("pop_queue", 1)
        restored = reports("pop_queue")[-1]
        assert restored["command"]["expected_token"] != old_token
        assert restored["state"]["follow_up"] == []
        offset = len(tui.output)
        tui.resize(width=width + 2)
        tui.wait_for(
            b"draft-kept", since=offset, failure="first restore did not recover retained work"
        )
        offset = len(tui.output)
        tui.send(b"\x03")
        tui.wait_for(b"idle", since=offset, failure="replacement run did not cancel")
        tui.quit()
    finally:
        tui.close()
