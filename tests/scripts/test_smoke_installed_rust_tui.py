from __future__ import annotations

import struct
from collections import deque
from pathlib import Path

import pytest

from scripts import smoke_installed_rust_tui as smoke


# A fresh session is ready at the welcome screen's action list, which paints its
# words in separate cells; a continued one at its saved history.
@pytest.mark.parametrize(
    ("history_messages", "ready_text", "reported_messages"),
    [
        (10_000, smoke._HISTORY_READY + b" idle", 10_001),
        (0, b"Browse\x1b[14;26Hcommands idle", 0),
    ],
)
@pytest.mark.parametrize("working_repaints", [0, 2])
@pytest.mark.parametrize("premature_exit", [False, True])
def test_smoke_finishes_when_idle_is_only_incrementally_repainted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    history_messages: int,
    ready_text: bytes,
    reported_messages: int,
    working_repaints: int,
    premature_exit: bool,
) -> None:
    """Replay the macOS failure: response finishes but idle is painted as 'dle'."""
    (tmp_path / "wisp-tui").write_bytes(b"binary")
    packets: deque[bytes] = deque()
    sent: list[bytes] = []
    clock = 0.0
    repaints = 0

    def frame(text: bytes) -> bytes:
        return b"\x1b[?2026h\x1b[2J" + text + b"\x1b[?2026l"

    def resize(_fd: int, _operation: int, dimensions: bytes) -> None:
        nonlocal repaints
        width = struct.unpack("HHHH", dimensions)[1]
        if width == 81:
            packets.append(frame(b"ctx ~42%"))
        elif not sent:
            packets.append(frame(ready_text))
        else:
            repaints += 1
            state = b"working" if repaints <= working_repaints else b"idle"
            # Include a partial frame first: the script must wait for its end.
            complete = frame(b"fake response to: installed wheel smoke " + state)
            packets.extend([complete[:-8], complete[-8:]])

    def write(_fd: int, data: bytes) -> int:
        sent.append(data)
        if data.endswith(b"\r"):
            packets.append(b"working fake response to: installed wheel smoke\x1b[24;26Hdle")
        return len(data)

    def monotonic() -> float:
        nonlocal clock
        clock += 0.05
        return clock

    monkeypatch.setattr(smoke, "_seed_history", lambda *_: 100)
    monkeypatch.setattr(smoke.pty, "fork", lambda: (123, 456))
    monkeypatch.setattr(smoke.fcntl, "ioctl", resize)
    monkeypatch.setattr(smoke.termios, "tcgetattr", lambda _: ["original attributes"])
    monkeypatch.setattr(smoke.select, "select", lambda *_: ([456] if packets else [], [], []))
    monkeypatch.setattr(smoke.os, "read", lambda *_: packets.popleft())
    monkeypatch.setattr(smoke.os, "write", write)
    monkeypatch.setattr(
        smoke.os,
        "waitpid",
        lambda *_: (123, 0) if b"\x03" in sent or (premature_exit and sent) else (0, 0),
    )
    monkeypatch.setattr(smoke.os, "close", lambda _: None)
    monkeypatch.setattr(smoke.os, "kill", lambda *_: None)
    monkeypatch.setattr(smoke.os, "killpg", lambda *_: None)
    monkeypatch.setattr(smoke.os, "tcgetpgrp", lambda _: 123)
    monkeypatch.setattr(smoke.time, "monotonic", monotonic)

    if premature_exit:
        with pytest.raises(RuntimeError, match="smoke failed"):
            smoke.run(tmp_path / "wisp", tmp_path / "history", history_messages)
        assert b"\x03" not in sent
        return

    result = smoke.run(tmp_path / "wisp", tmp_path / "history", history_messages)

    assert sent == [b"installed wheel smoke\r", b"\x03"]
    assert repaints > working_repaints
    assert result["history_messages"] == reported_messages
    assert result["total_seconds"] < 10
