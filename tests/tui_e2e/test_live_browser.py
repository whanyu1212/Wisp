"""Exercise Chromium capture, Kitty output, and real page input through the TUI."""

from __future__ import annotations

import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.tui_e2e.support import context_stats_ready
from tests.tui_e2e.test_browser_takeover import _start_wide_tui, _wait_screen

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
<script>
  const field = document.querySelector('input');
  field.addEventListener('click', ev => {
    if (ev.isTrusted) {
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


@pytest.mark.process
@pytest.mark.skipif(
    os.environ.get("WISP_TEST_LIVE_BROWSER") != "1",
    reason="set WISP_TEST_LIVE_BROWSER=1 on a host with Chrome or Chromium",
)
def test_live_browser_renders_and_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page animates without input and accepts a trusted click and typed text."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    clicked = threading.Event()
    typed = threading.Event()
    submitted = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b""
            if self.path == "/":
                self.send_response(302)
                self.send_header("Location", "/page")
                self.end_headers()
                return
            if self.path == "/page":
                body = _PAGE
            elif self.path == "/clicked":
                clicked.set()
            elif self.path == "/typed":
                typed.set()
            elif self.path == "/submitted":
                submitted.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
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
                "WISP_TUI_IMAGES": "kitty-unicode",
            },
        )
        # Fork the PTY before starting the HTTP server thread.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            try:
                tui.wait_until(context_stats_ready, timeout=15, failure="TUI did not start")
                opened_at = time.monotonic()
                tui.send(f"/browser live http://127.0.0.1:{server.server_port}/\r".encode())
                _wait_screen(tui, "Live Chromium", "Browser did not show its first frame")
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
                assert list(tmp_path.glob("wisp-browser-*"))
                tui.send(b"\x03/browser\r")
                tui.wait_until(
                    lambda _: not list(tmp_path.glob("wisp-browser-*")),
                    timeout=6,
                    failure="Hiding the pane did not clean up Chrome's profile",
                )
                tui.quit()
            finally:
                tui.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
