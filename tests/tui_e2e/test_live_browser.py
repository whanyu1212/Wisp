"""Exercise Chromium capture, Kitty output, and real page input through the TUI."""

from __future__ import annotations

import base64
import fcntl
import os
import re
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import pytest

from tests.support.live_browser import keychain_safe_chrome_path
from tests.tui_e2e.support import (
    complete_frames,
    context_stats_ready,
    rendered_without_spacing,
    screen_rows,
)
from tests.tui_e2e.test_browser_takeover import _start_wide_tui, _wait_screen
from tests.tui_e2e.test_readiness import _TuiProcess
from wisp.coding.browser_control import BrowserInteraction
from wisp.coding.browser_live import LiveBrowser
from wisp.events import LiveBrowserViewport

_PAGE = b"""<!doctype html>
<style>
  body { margin: 0 }
  input {
    box-sizing: border-box;
    width: 100vw;
    height: 100vh;
    font: 80px sans-serif;
    text-align: center;
    background: #ee1444;
    animation: pulse 600ms step-end infinite;
  }
  @keyframes pulse { 50% { background: #002fee } }
</style>
<form><input aria-label="Type here"></form>
<div style="height: 100vh; background: #444"></div>
<script>
  const field = document.querySelector('input');
  const reportSize = () => fetch('/size?' + innerWidth + ',' + innerHeight);
  window.addEventListener('resize', reportSize);
  reportSize();
  window.addEventListener('scroll', () => {
    if (scrollY > 0) fetch('/scrolled');
  });
  field.addEventListener('click', ev => {
    if (ev.isTrusted && ev.clientX > innerWidth * .15 && ev.clientX < innerWidth * .85) {
      field.style.animation = 'none';
      fetch('/clicked');
    }
  });
  field.addEventListener('input', ev => {
    if (ev.isTrusted && field.value === 'wisp') {
      field.style.background = '#11ee22';
      fetch('/typed');
    }
  });
  field.form.addEventListener('submit', ev => {
    ev.preventDefault();
    if (ev.isTrusted) fetch('/submitted');
  });
</script>
"""
_KITTY_FRAME = re.compile(rb"\x1b_Ga=T,f=100,[^;]*U=1,[^;]+;([^\x1b]+)\x1b\\")


def _has_frame_size(output: bytes, offset: int, size: tuple[int, int]) -> bool:
    for frame in _KITTY_FRAME.finditer(output, offset):
        header = base64.b64decode(frame[1][:32])
        if len(header) == 24 and struct.unpack(">II", header[16:24]) == size:
            return True
    return False


def _profile_released(home: Path) -> bool:
    """Whether no live browser holds the saved profile under ``home``."""
    marker = home / ".wisp" / "browser-profile" / ".wisp-browser-profile"
    with marker.open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _click_tab(tui: _TuiProcess, label: str) -> None:
    """Click the top bar's ``label`` tab where the last complete frame painted it."""
    top_bar = screen_rows(complete_frames(tui.output))[0]
    column = top_bar.rfind(f" {label}")
    assert column >= 0, f"No {label} tab in {top_bar!r}"
    tui.send(f"\x1b[<0;{column + 2};1M\x1b[<0;{column + 2};1m".encode())


def _recent_listed(output: bytes, offset: int, address: str) -> bool:
    """Whether output since ``offset`` painted "Recent" and ``address`` as a whole entry.

    Kitty image payloads confuse the screen replay, so this reads the painted text.
    """
    painted = rendered_without_spacing(output[offset:])
    return b"Recent" in painted and bool(
        re.search(re.escape(address.encode()) + rb"(?!other)", painted)
    )


@pytest.mark.process
def test_browser_tab_start_page(tmp_path: Path) -> None:
    """The Browser tab opens a focused address field without starting Chrome."""
    home = tmp_path / "home"
    home.mkdir()
    tui = _start_wide_tui(tmp_path, home)
    try:
        tui.wait_until(context_stats_ready, timeout=15, failure="TUI did not start")
        tui.send(b"draft for the agent")
        _wait_screen(tui, "draft for the agent", "The composer did not show the draft")
        _click_tab(tui, "Browser")
        _wait_screen(tui, "Search or enter address", "The Browser tab showed no address field")
        _wait_screen(tui, "Enter open", "The address field did not take focus")
        tui.send(b"example")
        _wait_screen(tui, "[example", "Typing did not reach the address field")
        _wait_screen(tui, "draft for the agent", "Opening the Browser tab lost the draft")
        assert not (home / ".wisp" / "browser-profile").exists()
        tui.send(b"\x1b")
        _wait_screen(tui, "Ctrl+L address", "Esc did not return focus to the prompt")
        tui.quit()
    finally:
        tui.close()


def _click_control(tui: _TuiProcess, label: str) -> None:
    for row, text in enumerate(screen_rows(complete_frames(tui.output)), 1):
        if (column := text.find(label)) >= 0:
            tui.send(f"\x1b[<0;{column + 2};{row}M\x1b[<0;{column + 2};{row}m".encode())
            return
    pytest.fail(f"No {label} button in the painted browser pane")


@pytest.mark.process
@pytest.mark.skipif(
    os.environ.get("WISP_TEST_LIVE_BROWSER") != "1",
    reason="set WISP_TEST_LIVE_BROWSER=1 on a host with Chrome or Chromium",
)
def test_live_browser_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live page animates, accepts input, navigates, reflows, closes, and keeps its cookies."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    clicked = threading.Event()
    typed = threading.Event()
    submitted = threading.Event()
    scrolled = threading.Event()
    marked = threading.Event()
    remembered = threading.Event()
    sizes: list[tuple[int, int]] = []
    opened_pages: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            body = b""
            if "mark=kept" in self.headers.get("Cookie", ""):
                (remembered if url.query == "reopened" else marked).set()
            if url.path == "/":
                self.send_response(302)
                self.send_header("Location", "/page")
                self.end_headers()
                return
            if url.path in {"/page", "/other"}:
                opened_pages.append(url.path)
                body = _PAGE
            elif url.path == "/size":
                width, height = url.query.split(",")
                sizes.append((int(width), int(height)))
            elif url.path == "/clicked":
                clicked.set()
            elif url.path == "/typed":
                typed.set()
            elif url.path == "/submitted":
                submitted.set()
            elif url.path == "/scrolled":
                scrolled.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            if url.query == "mark":
                self.send_header("Set-Cookie", "mark=kept; Max-Age=3600")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        home = tmp_path / "home"
        home.mkdir()
        tui = _start_wide_tui(
            tmp_path,
            home,
            environment={
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "TMPDIR": str(tmp_path),
                "PATH": keychain_safe_chrome_path(tmp_path, os.environ["PATH"]),
                "WISP_TUI_IMAGES": "kitty-unicode",
            },
        )
        # Fork the PTY before starting the HTTP server thread.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                tui.wait_until(context_stats_ready, timeout=15, failure="TUI did not start")
                first_address = f"http://127.0.0.1:{server.server_port}/"
                _click_tab(tui, "Browser")
                _wait_screen(tui, "Enter open", "The Browser tab did not focus its address field")
                opened_at = time.monotonic()
                tui.send(f"{first_address}\r".encode())
                _wait_screen(
                    tui,
                    f"127.0.0.1:{server.server_port}/page",
                    "Browser did not show the committed address",
                )
                tui.wait_until(
                    lambda output: bool(_KITTY_FRAME.search(output)),
                    timeout=5,
                    failure="Browser did not transmit its first Kitty PNG",
                )
                print(f"First Kitty frame observed after {time.monotonic() - opened_at:.2f}s")
                tui.wait_until(
                    lambda _: bool(sizes and sizes[-1][0] < 900),
                    timeout=5,
                    failure="Chromium did not reflow to the docked pane",
                )
                docked_size = sizes[-1]
                start = len(tui.output)
                tui.wait_until(
                    lambda output: len(set(_KITTY_FRAME.findall(output, start))) >= 2,
                    timeout=10,
                    failure="Chromium animation did not reach Kitty output",
                )
                tui.send(b"\x1b[<0;136;16M\x1b[<0;136;16m")
                tui.wait_until(
                    lambda _: clicked.is_set(), timeout=5, failure="Chromium did not receive click"
                )
                _wait_screen(tui, "INTERACT", "Browser did not take keyboard focus")
                scroll_offset = len(tui.output)
                wheel_at = time.monotonic()
                tui.send(b"\x1b[<65;136;16M")
                tui.wait_until(
                    lambda output: (
                        scrolled.is_set() and bool(_KITTY_FRAME.search(output, scroll_offset))
                    ),
                    timeout=5,
                    failure="Chromium scroll did not repaint",
                )
                print(f"Scroll and repaint observed after {time.monotonic() - wheel_at:.2f}s")
                first_byte = len(tui.output)
                typed_at = time.monotonic()
                tui.send(b"wisp")
                tui.wait_until(
                    lambda _: typed.is_set(), timeout=5, failure="Chromium did not receive typing"
                )
                tui.wait_until(
                    lambda output: bool(_KITTY_FRAME.search(output, first_byte)),
                    timeout=5,
                    failure="Typed state did not repaint in Kitty output",
                )
                print(f"Typed input and repaint observed after {time.monotonic() - typed_at:.2f}s")
                tui.send(b"\r")
                tui.wait_until(
                    lambda _: submitted.is_set(),
                    timeout=5,
                    failure="Enter did not submit Chromium's form",
                )
                assert not list((tmp_path / "sessions" / "images").glob("*"))
                assert not _profile_released(home)

                tui.send(b"\x0c")
                _wait_screen(tui, "Enter go", "Ctrl+L did not focus the address bar")
                tui.send(
                    f"\x1b[200~http://127.0.0.1:{server.server_port}/other\x1b[201~\r".encode()
                )
                _wait_screen(tui, "/other", "The address bar did not navigate the live tab")
                _click_control(tui, "[‹]")
                _wait_screen(tui, "/page", "Back did not select the previous address")
                _click_control(tui, "[›]")
                _wait_screen(tui, "/other", "Forward did not select the next address")
                before_reload = opened_pages.count("/other")
                _click_control(tui, "[↻]")
                tui.wait_until(
                    lambda _: opened_pages.count("/other") > before_reload,
                    timeout=5,
                    failure="Reload did not request the current page",
                )
                resize_offset = len(sizes)
                frame_offset = len(tui.output)
                tui.resize(width=190)
                tui.wait_until(
                    lambda _: any(size != docked_size for size in sizes[resize_offset:]),
                    timeout=5,
                    failure="Terminal resize did not update Chromium's CSS viewport",
                )
                resized = next(size for size in sizes[resize_offset:] if size != docked_size)
                tui.wait_until(
                    lambda output: _has_frame_size(output, frame_offset, resized),
                    timeout=5,
                    failure="The resized browser frame did not reach the terminal",
                )
                # At 190 columns the dock is 52 columns wide. Hold its left
                # edge at an intermediate width, then release at a different one.
                resize_offset = len(sizes)
                drag_offset = len(tui.output)
                tui.send(b"\x1b[<0;139;12M\x1b[<32;132;12M\x1b[<32;120;12M")
                tui.wait_until(
                    lambda output: any(
                        row[119:120] == "│"
                        for row in screen_rows(complete_frames(output[drag_offset:]))
                    ),
                    timeout=5,
                    failure="The dock border did not follow the held pointer",
                )
                assert not sizes[resize_offset:]
                frame_offset = len(tui.output)
                tui.send(b"\x1b[<0;115;12m")
                # The PTY reports no physical metrics, so Wisp uses 8px per column.
                final_size = (resized[0] + (139 - 115) * 8, resized[1])
                tui.wait_until(
                    lambda output: _has_frame_size(output, frame_offset, final_size),
                    timeout=5,
                    failure="Chromium did not repaint at the released dock width",
                )
                assert sizes[resize_offset:] == [final_size]
                clicked.clear()
                # The same form still receives a center click at its shifted docked position.
                tui.send(b"\x1b[<0;166;12M\x1b[<0;166;12m")
                tui.wait_until(
                    lambda _: clicked.is_set(), timeout=5, failure="Resized click missed the page"
                )
                # Ctrl+C leaves the page's input, then the active tab hides the pane.
                tui.send(b"\x03")
                _click_tab(tui, "Browser")
                tui.wait_until(
                    lambda _: _profile_released(home),
                    timeout=6,
                    failure="Hiding the pane did not stop Chrome and release its profile",
                )
                # The address typed into the start page is remembered for the next visit.
                start_page_offset = len(tui.output)
                _click_tab(tui, "Browser")
                tui.wait_until(
                    lambda output: _recent_listed(output, start_page_offset, first_address),
                    timeout=5,
                    failure="The reopened start page did not list the first address",
                )
                tui.send(b"\x1b")
                _wait_screen(tui, "Ctrl+L address", "Esc did not leave the address field")
                # Chromium writes cookies on a timer, so one set moments before the pane
                # closes reaches the next visit only if Chromium exits cleanly.
                address = f"http://127.0.0.1:{server.server_port}/page"
                reopened_offset = len(tui.output)
                tui.send(f"/browser live {address}?mark\r".encode())
                # The open command must finish before the composer accepts another.
                tui.wait_until(
                    lambda output: (
                        marked.is_set() and bool(_KITTY_FRAME.search(output, reopened_offset))
                    ),
                    timeout=10,
                    failure="Chromium did not store the cookie and paint the page",
                )
                _click_tab(tui, "Browser")
                tui.wait_until(
                    lambda _: _profile_released(home),
                    timeout=6,
                    failure="Hiding the pane a second time did not stop Chrome",
                )
                tui.send(f"/browser live {address}?reopened\r".encode())
                tui.wait_until(
                    lambda _: remembered.is_set(),
                    timeout=10,
                    failure="A reopened live browser did not send the cookie from its last visit",
                )
                tui.quit()
            finally:
                tui.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.process
@pytest.mark.skipif(
    os.environ.get("WISP_TEST_LIVE_BROWSER") != "1",
    reason="set WISP_TEST_LIVE_BROWSER=1 on a host with Chrome or Chromium",
)
def test_live_browser_smooth_scroll_dom_cases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise _JS_SMOOTH_SCROLL in real Chromium across DOM, iframe, and CSS edge cases."""
    monkeypatch.setenv("PATH", keychain_safe_chrome_path(tmp_path, os.environ["PATH"]))

    child_doc = b"<!doctype html><body style='margin:0;height:400px;background:#88c'>child</body>"
    dom_page = (
        b"<!doctype html><body style='margin:0;height:2000px'>"
        b"<div id='contain' style='position:fixed;left:0;top:0;width:120px;height:100px;"
        b"overflow-y:auto;overscroll-behavior-y:contain'><div style='height:150px'>c</div></div>"
        b"<div id='prevent' style='position:fixed;left:120px;top:0;width:120px;height:100px;"
        b"overflow-y:auto'><div style='height:300px'>p</div></div>"
        b"<div id='host' style='position:fixed;left:240px;top:0;width:120px;height:100px'></div>"
        b"<div id='wrap' style='position:fixed;left:0;top:110px;width:180px;height:120px;"
        b"overflow-y:auto'><iframe id='same' src='/child' "
        b"style='width:160px;height:90px;border:0'></iframe><div style='height:300px'></div></div>"
        b"<iframe id='xorigin' sandbox='' srcdoc='<body style=height:400px>x</body>' "
        b"style='position:fixed;left:190px;top:110px;width:150px;height:100px;border:0'></iframe>"
        b"<div id='rev' style='position:fixed;left:0;top:240px;width:120px;height:100px;"
        b"overflow-y:auto;display:flex;flex-direction:column-reverse'>"
        b"<div style='flex:0 0 300px'>r</div></div>"
        b"<div id='rtl' style='position:fixed;left:130px;top:240px;width:120px;height:100px;"
        b"overflow-x:auto;direction:rtl'><div style='width:300px;height:50px'>rtl</div></div>"
        b"<script>"
        b"document.getElementById('prevent').addEventListener('wheel',e=>e.preventDefault(),{passive:false});"
        b"const sh=document.getElementById('host').attachShadow({mode:'open'});"
        b'sh.innerHTML=\'<div id=s style="width:120px;height:100px;overflow-y:auto">'
        b"<div style=height:400px>shadow</div></div>';"
        b"</script></body>"
    )
    body_scroll_page = (
        b"<!doctype html><html style='overflow:hidden;height:100%'>"
        b"<body style='margin:0;overflow-y:auto;height:100%'><div style='height:1000px'>b</div>"
        b"</body></html>"
    )
    locked_page = (
        b"<!doctype html><html style='overflow:hidden'><body style='margin:0;height:2000px'>"
        b"locked</body></html>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            body = {
                "/dom": dom_page,
                "/child": child_doc,
                "/body-scroll": body_scroll_page,
                "/locked": locked_page,
            }.get(path, b"")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    async def scenario(port: int) -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks, profile_dir=tmp_path / "profile")
            await browser.open(f"http://127.0.0.1:{port}/dom")
            await browser.resize(LiveBrowserViewport(width=400, height=400, device_scale_factor=1))
            for _ in range(50):
                if (
                    await browser._evaluate(
                        "Boolean(document.getElementById('same')"
                        "?.contentDocument?.body?.scrollHeight > 100)"
                    )
                    is True
                ):
                    break
                await anyio.sleep(0.02)

            # 1. overscroll-behavior: contain scrolls inner box and blocks window chain.
            await browser.interact(BrowserInteraction(action="scroll", x=50, y=50, delta_y=120))
            await anyio.sleep(0.25)
            assert await browser._evaluate(
                "[document.getElementById('contain').scrollTop, Math.round(window.scrollY)]"
            ) == [50, 0]

            # 2. preventDefault() suppresses scrolling while still succeeding.
            await browser.interact(BrowserInteraction(action="scroll", x=170, y=50, delta_y=120))
            await anyio.sleep(0.2)
            assert await browser._evaluate(
                "[document.getElementById('prevent').scrollTop, Math.round(window.scrollY)]"
            ) == [0, 0]

            # 3. Shadow DOM scrollable container scrolls cleanly.
            await browser.interact(BrowserInteraction(action="scroll", x=290, y=50, delta_y=90))
            await anyio.sleep(0.2)
            assert (
                await browser._evaluate(
                    "document.getElementById('host').shadowRoot.getElementById('s').scrollTop"
                )
                == 90
            )

            # 4. Same-origin iframe chains excess scroll to parent #wrap div.
            await browser.interact(BrowserInteraction(action="scroll", x=70, y=150, delta_y=360))
            await anyio.sleep(0.25)
            child_y, wrap_y = await browser._evaluate(
                "[Math.round(document.getElementById('same').contentWindow.scrollY),"
                " Math.round(document.getElementById('wrap').scrollTop)]"
            )  # type: ignore[misc]
            assert child_y == 310 and wrap_y == 50

            # 5. Cross-origin (sandbox='') iframe falls back to mouseWheel without hanging/error.
            await browser.interact(BrowserInteraction(action="scroll", x=220, y=150, delta_y=60))
            await anyio.sleep(0.1)

            # 5b. Negative scroll ranges (column-reverse vertical & RTL horizontal) scroll from 0.
            await browser.interact(BrowserInteraction(action="scroll", x=50, y=280, delta_y=-90))
            await anyio.sleep(0.25)
            await browser.interact(BrowserInteraction(action="scroll", x=180, y=280, delta_x=-90))
            await anyio.sleep(0.25)
            rev_y, rtl_x = await browser._evaluate(
                "[Math.round(document.getElementById('rev').scrollTop),"
                " Math.round(document.getElementById('rtl').scrollLeft)]"
            )  # type: ignore[misc]
            assert rev_y == -90 and rtl_x == -90

            # 6. Direction reversal cancels remaining positive tail immediately.
            await browser._evaluate("window.scrollTo(0, 500)")
            await browser.interact(BrowserInteraction(action="scroll", x=380, y=380, delta_y=240))
            await browser.interact(BrowserInteraction(action="scroll", x=380, y=380, delta_y=-60))
            await anyio.sleep(0.25)
            reversed_y = await browser._evaluate("Math.round(window.scrollY)")
            assert isinstance(reversed_y, int) and reversed_y < 500

            # 7. html { overflow: hidden } body { overflow-y: auto } scrolls body, not window.
            await browser.navigate(f"http://127.0.0.1:{port}/body-scroll")
            await browser.poll_frame()
            await browser.interact(BrowserInteraction(action="scroll", x=50, y=50, delta_y=120))
            await anyio.sleep(0.2)
            assert await browser._evaluate(
                "[Math.round(document.body.scrollTop), Math.round(window.scrollY)]"
            ) == [120, 0]

            # 8. html { overflow: hidden } modal scroll lock leaves window at 0.
            await browser.navigate(f"http://127.0.0.1:{port}/locked")
            await browser.poll_frame()
            await browser.interact(BrowserInteraction(action="scroll", x=50, y=50, delta_y=120))
            await anyio.sleep(0.2)
            assert await browser._evaluate("Math.round(window.scrollY)") == 0

            await browser.close()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            anyio.run(scenario, server.server_port)
        finally:
            server.shutdown()
            thread.join(timeout=2)
