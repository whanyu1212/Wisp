"""PTY test: add an MCP server from `/mcp` by pasting its config snippet."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import struct
import sys
import termios
from pathlib import Path

import pytest

from tests.support.paths import FIXTURES_DIR
from tests.tui_e2e.support import complete_frames, rendered_without_spacing, screen_rows
from tests.tui_e2e.test_readiness import _rust_binary, _TuiProcess
from wisp import __version__

SECRET = "sentinel-typed-value-42"


def _start_tui(tmp_path: Path, home: Path, environment: dict[str, str]) -> _TuiProcess:
    """Run the Rust TUI over the real backend in a PTY, with ``home`` as its home folder."""

    backend = tmp_path / "backend.py"
    backend.write_text(
        """import sys
from wisp.cli.application import main

sys.argv = ["wisp", "--mode", "rpc", "--provider", "fake", "--session-dir", sys.argv[1]]
main()
""",
        encoding="utf-8",
    )
    binary = _rust_binary()
    child, fd = pty.fork()
    if child == 0:
        child_environment = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "WISP_TRUST": "1",
            **environment,
        }
        for key in ("WISP_PROVIDER", "WISP_MODEL", "WISP_FAKE_STREAM_INTERVAL_MS"):
            child_environment.pop(key, None)
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
            child_environment,
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    return _TuiProcess(child, fd, termios.tcgetattr(fd))


def _wait_painted(tui: _TuiProcess, text: str, failure: str, timeout: float = 20) -> None:
    """Wait until the replayed screen shows ``text`` on one row.

    Incremental frames send only changed cells, so a phrase that replaces
    similar text is not contiguous in the output. Forcing full repaints with
    resizes is no substitute: each resize makes the TUI ignore a choice key
    until its next frame, so the key sent after the wait could be dropped.

    Args:
        tui (_TuiProcess): The running TUI.
        text (str): Text a single screen row must contain, compared without spacing.
        failure (str): Message used if the screen never shows the text.
        timeout (float): Seconds to wait.
    """
    marker = "".join(text.split())

    def shown(output: bytes) -> bool:
        rows = screen_rows(complete_frames(output))
        return any(marker in "".join(row.split()) for row in rows)

    tui.wait_until(shown, timeout=timeout, failure=failure)


@pytest.mark.process
def test_configured_server_connects_after_startup(tmp_path: Path) -> None:
    """A server still starting when the TUI opens shows as connecting, then connected."""

    home = tmp_path / "home"
    (home / ".wisp").mkdir(parents=True)
    # The server starts only once the gate file exists, which is after `/mcp` has
    # shown it connecting. Nothing asks for status again afterwards, so only the
    # backend's update can show it connected.
    gate = tmp_path / "start-server"
    gated_start = (
        "import os, runpy, time\n"
        f"while not os.path.exists({str(gate)!r}): time.sleep(0.05)\n"
        f"runpy.run_path({str(FIXTURES_DIR / 'mcp_stdio_server.py')!r}, run_name='__main__')"
    )
    (home / ".wisp" / "settings.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "fixture": {"command": sys.executable, "args": ["-u", "-c", gated_start]}
                }
            }
        ),
        encoding="utf-8",
    )
    tui = _start_tui(tmp_path, home, {})
    try:
        tui.wait_ready()
        tui.send(b"/mcp\r")
        _wait_painted(tui, "fixture connecting", "the server was not shown connecting")
        gate.touch()
        # The connect allows the server 10 s from when the backend started serving.
        _wait_painted(tui, "fixture connected · 2 tools", "the server was not shown connected")
    finally:
        # The first Ctrl+C closes the open `/mcp` view.
        tui.send(b"\x03")
        tui.quit()


@pytest.mark.process
def test_pasted_snippet_adds_a_connected_server_without_showing_values(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    closed_file = tmp_path / "fixture-closed"
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
    # Forwarded to the server by the choice made below.
    tui = _start_tui(tmp_path, home, {"WISP_MCP_TEST_CLOSED_FILE": str(closed_file)})

    def wait_painted(text: str, failure: str, timeout: float = 20) -> None:
        _wait_painted(tui, text, failure, timeout)

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
        # Starting the server and listing its tools is slow on a loaded machine.
        wait_painted("fixture connected · 2 tools", "server did not connect", timeout=40)

        settings = json.loads((home / ".wisp" / "settings.json").read_text(encoding="utf-8"))
        assert settings["mcp_servers"]["fixture"]["env"] == {"WISP_MCP_TEST_LITERAL": SECRET}
        assert settings["mcp_servers"]["fixture"]["env_from"] == ["WISP_MCP_TEST_CLOSED_FILE"]
        assert SECRET.encode() not in tui.output
        assert b"<value>" not in rendered_without_spacing(tui.output)
    finally:
        # The first Ctrl+C closes the open `/mcp` view.
        tui.send(b"\x03")
        tui.quit()
