"""PTY test: add an MCP server from `/mcp` by pasting its config snippet."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import sys
import termios
import time
from pathlib import Path

import pytest

from tests.support.paths import FIXTURES_DIR
from tests.tui_e2e.support import rendered_without_spacing
from tests.tui_e2e.test_readiness import _rust_binary, _TuiProcess
from wisp import __version__

SECRET = "sentinel-typed-value-42"


def _full_repaint_after(output: bytes, offset: int) -> bytes | None:
    # A resize clears the screen inside the next synchronized frame.
    start = output.find(b"\x1b[?2026h\x1b[2J", offset)
    end = output.find(b"\x1b[?2026l", start) if start >= 0 else -1
    return output[start:end] if end >= 0 else None


@pytest.mark.process
def test_pasted_snippet_adds_a_connected_server_without_showing_values(tmp_path: Path) -> None:
    binary = _rust_binary()
    home = tmp_path / "home"
    home.mkdir()
    closed_file = tmp_path / "fixture-closed"
    backend = tmp_path / "backend.py"
    backend.write_text(
        """import sys
from wisp.cli.application import main

sys.argv = ["wisp", "--mode", "rpc", "--provider", "fake", "--session-dir", sys.argv[1]]
main()
""",
        encoding="utf-8",
    )
    # Published snippets carry placeholder values; the TUI asks for each again.
    snippet = json.dumps(
        {
            "mcpServers": {
                "fixture": {
                    "command": sys.executable,
                    "args": ["-u", str(FIXTURES_DIR / "mcp_stdio_server.py")],
                    "env": {
                        "WISP_MCP_TEST_CLOSED_FILE": "<path>",
                        "WISP_MCP_TEST_LITERAL": "<value>",
                    },
                }
            }
        }
    )

    child, fd = pty.fork()
    if child == 0:
        environment = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "WISP_TRUST": "1",
            # Forwarded to the server by the choice made below.
            "WISP_MCP_TEST_CLOSED_FILE": str(closed_file),
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
                str(tmp_path / "sessions"),
            ],
            environment,
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    tui = _TuiProcess(child, fd, termios.tcgetattr(fd))

    # `wait_ready` leaves the terminal 101 columns wide.
    width = 101

    def wait_painted(text: str, failure: str) -> None:
        """Force full repaints until one shows ``text``.

        Incremental frames send only changed cells, so a phrase that replaces
        similar text is not contiguous in the output. A resize repaints the whole
        screen inside one synchronized frame.

        Args:
            text (str): Text the repaint must contain, compared without spacing.
            failure (str): Message used if no repaint shows the text in time.
        """
        nonlocal width
        marker = "".join(text.split()).encode()
        deadline = time.monotonic() + 15
        while True:
            offset = len(tui.output)
            width = 101 if width == 100 else 100
            tui.resize(width=width)

            def repainted(output: bytes, start: int = offset) -> bool:
                return _full_repaint_after(output, start) is not None

            output = tui.wait_until(repainted, failure="TUI did not repaint after resize")
            frame = _full_repaint_after(output, offset)
            assert frame is not None
            if marker in rendered_without_spacing(frame):
                return
            if time.monotonic() >= deadline:
                pytest.fail(f"{failure}; last full repaint={rendered_without_spacing(frame)!r}")

    try:
        tui.wait_ready()
        tui.send(b"/mcp\r")
        wait_painted("No MCP servers configured.", "/mcp did not open")

        tui.send(b"a")
        wait_painted("Paste the server's config snippet.", "add panel did not open")

        tui.send(b"\x1b[200~" + snippet.encode() + b"\x1b[201~")
        wait_painted("fixture needs WISP_MCP_TEST_CLOSED_FILE (1 of 2)", "no env step")

        tui.send(b"1")
        wait_painted("fixture needs WISP_MCP_TEST_LITERAL (2 of 2)", "no second step")

        tui.send(b"2")
        wait_painted("Value for WISP_MCP_TEST_LITERAL.", "value entry did not open")
        tui.send(SECRET.encode())
        # Enter submits only after the typed value has been painted.
        wait_painted("•" * len(SECRET), "typed value was not shown masked")
        tui.send(b"\r")
        wait_painted("Add MCP server fixture", "confirmation did not open")
        wait_painted("forwards: WISP_MCP_TEST_CLOSED_FILE", "forwarding not shown")

        tui.send(b"1")
        wait_painted("fixture connected · 2 tools", "server did not connect")

        settings = json.loads((home / ".wisp" / "settings.json").read_text(encoding="utf-8"))
        assert settings["mcp_servers"]["fixture"]["env"] == {"WISP_MCP_TEST_LITERAL": SECRET}
        assert settings["mcp_servers"]["fixture"]["env_from"] == ["WISP_MCP_TEST_CLOSED_FILE"]
        assert SECRET.encode() not in tui.output
        assert b"<value>" not in rendered_without_spacing(tui.output)
    finally:
        # The first Ctrl+C closes the open `/mcp` view.
        tui.send(b"\x03")
        tui.quit()
