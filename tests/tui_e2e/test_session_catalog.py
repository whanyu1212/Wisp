"""Persisted catalog discovery through the built TUI and real RPC host."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import sys
import termios
from pathlib import Path

import anyio
import pytest

from tests.tui_e2e.test_readiness import _rust_binary, _TuiProcess
from wisp import __version__
from wisp.agent.messages import Message
from wisp.sessions.jsonl import JsonlSessionStore


@pytest.mark.process
def test_search_and_page_real_persisted_catalog(tmp_path: Path) -> None:
    binary = _rust_binary()
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "sessions"
    store = JsonlSessionStore(root)
    sessions = [store.create() for _ in range(63)]

    async def seed() -> None:
        for index, session in enumerate(sessions):
            await session.append_message(Message(role="user", content=f"saved prompt {index}"))
            await session.set_name("old needle" if index == 0 else f"Catalog item {index:02}")
            os.utime(session.path, ns=(1_800_000_000_000_000_000 + index,) * 2)

    anyio.run(seed)
    log = tmp_path / "catalog.jsonl"
    backend = tmp_path / "backend.py"
    backend.write_text(
        """import sys
from pathlib import Path
from wisp.cli.application import main
from wisp.cli import rpc
log = Path(sys.argv[1])
sessions = sys.argv[2]
original = rpc._write_json_event
def record(event):
    original(event)
    if event.type in {"rpc.sessions", "rpc.session.selected", "rpc.command.finished"}:
        with log.open("a") as stream:
            stream.write(event.model_dump_json() + "\\n")
rpc._write_json_event = record
sys.argv = ["wisp", "--mode", "rpc", "--provider", "fake", "--session-dir", sessions]
main()
""",
        encoding="utf-8",
    )

    def reports(kind: str) -> list[dict[str, object]]:
        if not log.exists():
            return []
        return [
            record
            for line in log.read_text().splitlines(keepends=True)
            if line.endswith("\n") and (record := json.loads(line))["type"] == kind
        ]

    child, fd = pty.fork()
    if child == 0:
        environment = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "WISP_TRUST": "1",
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
                str(root),
            ],
            environment,
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 110, 0, 0))
    tui = _TuiProcess(child, fd, termios.tcgetattr(fd))
    width = 112

    def wait_page(count: int, label: bytes, *, query: str = "") -> dict[str, object]:
        nonlocal width
        tui.wait_until(
            lambda _: len(reports("rpc.sessions")) >= count, failure="missing catalog page"
        )
        report = reports("rpc.sessions")[-1]
        assert report.get("query", "") == query, report
        assert report["sessions"], report
        tui.wait_until(
            lambda _: any(
                event["command_id"] == report["command_id"]
                for event in reports("rpc.command.finished")
            ),
            failure="catalog command did not finish",
        )
        offset = len(tui.output)
        width += 1
        tui.resize(width=width)
        # The query header can paint before the matching result. Wait for its
        # identity too, so Enter tests a rendered row rather than the stale guard.
        identity = f"[{report['sessions'][0]['session_id'][:12]}]".encode()
        tui.wait_for(label, identity, since=offset, failure="catalog result did not paint")
        return report

    try:
        tui.wait_ready()
        tui.send(b"/resume\r")
        first = wait_page(1, b"Catalog item 62")
        assert len(first["sessions"]) == 50
        assert first["next_cursor"]
        tui.send(b"\x1b[1;5C")  # Ctrl+Right: next backend page, not a local row jump.
        older = wait_page(2, b"Catalog item 12")
        assert len(older["sessions"]) == 13
        assert older.get("next_cursor") is None
        tui.send(b"\x1b[1;5D")
        wait_page(3, b"Catalog item 62")
        tui.send(b"\x1b[200~old needle\x1b[201~")
        found = wait_page(4, b"old needle", query="old needle")
        assert [row["session_id"] for row in found["sessions"]] == [sessions[0].session_id]
        # Dismissal does not submit the search text as a prompt.
        offset = len(tui.output)
        tui.send(b"\x1b")
        # Escape has a terminal decoding delay. Wait for the UI acknowledgement
        # before sending '/', which could otherwise be decoded as Alt+/.
        width += 1
        tui.resize(width=width)
        tui.wait_for(b"Session selection cancelled.", since=offset, failure="picker did not close")
        assert not reports("rpc.session.selected")
        tui.send(b"/resume\r")
        wait_page(5, b"Catalog item 62")
        tui.send(b"\x1b[200~old needle\x1b[201~")
        wait_page(6, b"old needle", query="old needle")
        tui.send(b"\r")
        tui.wait_until(
            lambda _: bool(reports("rpc.session.selected")), failure="older session not selected"
        )
        assert reports("rpc.session.selected")[-1]["session_id"] == sessions[0].session_id
        tui.wait_for(b"saved prompt 0", failure="selected session was not hydrated")
        tui.quit()
    finally:
        tui.close()
