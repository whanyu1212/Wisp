"""PTY E2E tests for interactive browser takeover, coordinate mapping, and panel expansion."""

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

from tests.tui_e2e.support import complete_frames, context_stats_ready, screen_rows, screen_shows
from tests.tui_e2e.test_readiness import _rust_binary, _TuiProcess
from wisp import __version__

_FAKE_BROWSER_SERVER = r"""from __future__ import annotations

import base64
import json
import os
import struct
import zlib
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

server = MCPServer("playwright")
_LOG_PATH = Path(os.environ["WISP_FAKE_BROWSER_LOG"])
_STATE = {"url": "https://app.example/login", "title": "Sign In", "clicks": 0}


def _make_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", crc)

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, level=1))
        + chunk(b"IEND", b"")
    )


_PNG_B64 = base64.b64encode(_make_png(320, 180, (44, 118, 210))).decode("ascii")


def _record(tool: str, **args: object) -> None:
    with _LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tool": tool, "args": args}) + "\n")


def _frame_result() -> list[TextContent | ImageContent]:
    return [
        TextContent(
            type="text",
            text=f"- Page URL: {_STATE['url']}\n- Page Title: {_STATE['title']}\n",
        ),
        ImageContent(type="image", data=_PNG_B64, mimeType="image/png"),
    ]


@server.tool()
def browser_take_screenshot() -> list[TextContent | ImageContent]:
    _record("browser_take_screenshot")
    return _frame_result()


@server.tool()
def browser_snapshot() -> str:
    _record("browser_snapshot")
    return f"- Page URL: {_STATE['url']}\n- Page Title: {_STATE['title']}\n"


@server.tool()
def browser_mouse_click_xy(x: int, y: int, button: str = "left") -> str:
    _STATE["clicks"] = int(_STATE["clicks"]) + 1
    _STATE["title"] = f"Clicked {x},{y}"
    _record("browser_mouse_click_xy", x=x, y=y, button=button)
    return f"clicked ({x}, {y})"


@server.tool()
def browser_mouse_wheel(deltaX: int = 0, deltaY: int = 0) -> str:
    _record("browser_mouse_wheel", deltaX=deltaX, deltaY=deltaY)
    return "scrolled"


@server.tool()
def browser_type(text: str) -> str:
    _STATE["title"] = "Typed Input"
    _record("browser_type", text=text)
    return "typed"


@server.tool()
def browser_press_key(key: str) -> str:
    _STATE["title"] = f"Key {key}"
    _record("browser_press_key", key=key)
    return f"pressed {key}"


@server.tool()
def browser_navigate_back() -> str:
    _STATE["url"] = "https://app.example/home"
    _STATE["title"] = "Home"
    _record("browser_navigate_back")
    return "navigated back"


if __name__ == "__main__":
    server.run()
"""


def _start_wide_tui(
    tmp_path: Path,
    home: Path,
    *,
    width: int = 160,
    height: int = 36,
    environment: dict[str, str] | None = None,
) -> _TuiProcess:
    backend = tmp_path / "backend.py"
    backend.write_text(
        """import sys
from wisp.cli.application import main

sys.argv = [
    "wisp", "--mode", "rpc", "--provider", "fake", "--all-tools", "--session-dir", sys.argv[1]
]
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
            "WISP_TUI_IMAGES": "halfblock",
            **(environment or {}),
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
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
    return _TuiProcess(child, fd, termios.tcgetattr(fd))


def _wait_screen(
    tui: _TuiProcess,
    marker: str,
    failure: str,
    *,
    timeout: float = 25,
) -> None:
    compact = "".join(marker.split())

    def shown(output: bytes) -> bool:
        rows = screen_rows(complete_frames(output))
        return any(compact in "".join(row.split()) for row in rows)

    tui.wait_until(shown, timeout=timeout, failure=failure)


def _read_log(log_path: Path) -> list[dict[str, object]]:
    if not log_path.exists():
        return []
    lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _wait_log_tool(
    tui: _TuiProcess,
    log_path: Path,
    tool: str,
    *,
    timeout: float = 20,
) -> dict[str, object]:
    found: dict[str, object] | None = None

    def arrived(_output: bytes) -> bool:
        nonlocal found
        for entry in _read_log(log_path):
            if entry.get("tool") == tool:
                found = entry
                return True
        return False

    try:
        tui.wait_until(
            arrived,
            timeout=timeout,
            failure=f"fake browser MCP server never received {tool!r}",
        )
    except BaseException:
        if found is None:
            pytest.fail(
                f"fake browser MCP server never received {tool!r}; log={_read_log(log_path)!r}"
            )
        raise
    assert found is not None
    return found


@pytest.mark.process
def test_browser_takeover_click_type_key_and_panel_expand_over_pty(tmp_path: Path) -> None:
    """Real PTY flow: connect fake browser MCP, enter takeover, click/type/key, expand panel."""
    home = tmp_path / "home"
    (home / ".wisp").mkdir(parents=True)
    server_script = tmp_path / "fake_browser_mcp.py"
    server_script.write_text(_FAKE_BROWSER_SERVER, encoding="utf-8")
    log_path = tmp_path / "browser_actions.jsonl"
    (home / ".wisp" / "settings.json").write_text(
        json.dumps(
            {
                "mcp_servers": {
                    "playwright": {
                        "command": sys.executable,
                        "args": ["-u", str(server_script)],
                        "env": {"WISP_FAKE_BROWSER_LOG": str(log_path)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    width, height = 160, 36
    tui = _start_wide_tui(tmp_path, home, width=width, height=height)
    try:
        # Wait past startup hydration at wide docked size (keep >=148 cols so panel docks).
        tui.wait_until(
            lambda output: (
                context_stats_ready(output)
                and screen_shows(output, "─ fake ")
                and not screen_shows(output, "ctx ~?")
                and not screen_shows(output, "ctx ~…")
                and not screen_shows(output, "Refreshing context")
            ),
            timeout=25,
            failure="Rust TUI did not finish startup hydration",
        )
        # Wait for the fake Playwright MCP server to finish connecting before takeover.
        tui.send(b"/mcp\r")
        _wait_screen(
            tui,
            "playwright connected",
            "fake playwright MCP server did not connect",
            timeout=30,
        )
        tui.send(b"\x1b")
        tui.wait_until(
            lambda output: not screen_shows(output, "tools (read = read-only"),
            timeout=15,
            failure="/mcp overlay did not close",
        )

        # Open Browser tab, focus side panel (F8), pull an immediate screenshot with `r`, then `i`.
        tui.send(b"/browser\r")
        _wait_screen(tui, "No browser frame yet.", "Browser tab did not open")
        tui.send(b"\x1b[19~")
        _wait_screen(tui, "i interact", "Side panel focus footer did not render")
        tui.send(b"r")
        _wait_log_tool(tui, log_path, "browser_take_screenshot", timeout=20)
        _wait_screen(tui, "Sign In", "Initial browser screenshot title did not render")
        tui.send(b"i")
        _wait_screen(tui, "INTERACT", "Browser INTERACT badge did not render")

        # Click near the center of the docked right-hand browser viewport (SGR left click).
        click_col = 136
        click_row = 16
        tui.send(f"\x1b[<0;{click_col};{click_row}M\x1b[<0;{click_col};{click_row}m".encode())
        click_entry = _wait_log_tool(tui, log_path, "browser_mouse_click_xy")
        click_args = click_entry["args"]
        assert isinstance(click_args, dict)
        assert 0 <= int(click_args["x"]) < 320
        assert 0 <= int(click_args["y"]) < 180
        _wait_screen(
            tui,
            f"Clicked {click_args['x']},{click_args['y']}",
            "Updated title after mapped click did not render",
        )

        # Type a secret string and press Enter; typed text must reach MCP without leaking to screen.
        secret_text = "hunter2-e2e"
        tui.send(secret_text.encode() + b"\r")
        type_entry = _wait_log_tool(tui, log_path, "browser_type")
        assert type_entry["args"] == {"text": secret_text}
        key_entry = _wait_log_tool(tui, log_path, "browser_press_key")
        assert key_entry["args"] == {"key": "Enter"}
        _wait_screen(tui, "Key Enter", "Updated title after Enter key did not render")
        assert secret_text.encode() not in tui.output

        # Exit takeover with Esc, focus the side panel with F8, and toggle expanded overlay with f.
        tui.send(b"\x1b")
        tui.wait_until(
            lambda output: not screen_shows(output, "INTERACT"),
            timeout=15,
            failure="Esc did not exit browser takeover",
        )
        tui.send(b"\x1b[19~")
        _wait_screen(tui, "[Expand]", "Side panel Expand control did not render")
        tui.send(b"f")
        _wait_screen(tui, "[Restore]", "Side panel did not expand into large overlay")
        tui.send(b"f")
        _wait_screen(tui, "[Expand]", "Side panel did not restore from expanded overlay")
        tui.send(b"\x1b")
        tui.quit()
    finally:
        tui.close()
