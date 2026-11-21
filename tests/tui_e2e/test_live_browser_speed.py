"""Measure how smoothly a live Chromium page reaches a Retina-sized terminal.

The test prints frame rates, frame intervals, input latency, and frame sizes for
``pytest -s``. It asserts that frames arrive, including while the wheel turns, and
that the page sees all of the input, but sets no machine-dependent rate targets.
"""

from __future__ import annotations

import base64
import errno
import math
import os
import select
import statistics
import struct
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.support.live_browser import keychain_safe_chrome_path
from tests.tui_e2e.support import context_stats_ready
from tests.tui_e2e.test_browser_takeover import _start_wide_tui, _wait_screen
from tests.tui_e2e.test_live_browser import _click_tab, _profile_released
from tests.tui_e2e.test_readiness import _TuiProcess

# A Retina terminal cell, so the docked pane takes the device-scale-2 capture path.
_CELL_PIXELS = (17, 34)
_COLUMNS, _ROWS = 190, 50
# Points inside the docked pane (columns 139-190): on the tall input, and below it.
_INPUT_CELL = (165, 10)
_CONTENT_CELL = (165, 34)
_WINDOW = 3.0
_WHEEL_RATE = 60
_WHEEL_DELTA = 120
_TYPED = "abcdefghijklmnopqrstuvwxyzabcd"

_PAGE = b"""<!doctype html>
<style>
  body { margin: 0; font: 15px/20px Georgia, serif; color: #222; background: #fafaf7 }
  input {
    box-sizing: border-box; width: 100%; height: 360px; border: 0;
    font: 40px sans-serif; background: #dde3f0; caret-color: transparent;
  }
  #stage {
    position: fixed; right: 0; bottom: 0; left: 0; height: 160px;
    background: #123; color: #fff; font: 28px monospace;
  }
  #block { position: absolute; top: 60px; width: 80px; height: 80px; background: #f80 }
  .band { height: 400px }
  p { margin: 0 16px }
</style>
<input aria-label="Type here">
<main></main>
<div id="stage"><div id="block"></div><span id="count">frame 0</span></div>
<script>
  const field = document.querySelector('input');
  const main = document.querySelector('main');
  const colours = ['#c33', '#3a3', '#33c', '#cc3', '#3cc', '#c3c'];
  for (let line = 0; line < 600; line++) {
    const text = document.createElement('p');
    text.textContent = `Line ${line}: the quick brown fox jumps over the lazy dog.`;
    main.append(text);
    if (line % 15 === 0) {
      const band = document.createElement('div');
      band.className = 'band';
      band.style.background = colours[(line / 15) % colours.length];
      main.append(band);
    }
  }
  const block = document.getElementById('block');
  const count = document.getElementById('count');
  let frames = 0;
  let running = true;
  const tick = () => {
    if (!running) return;
    frames++;
    block.style.left = (frames * 7) % Math.max(1, innerWidth - 80) + 'px';
    count.textContent = 'frame ' + frames;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  // Focusing the field stops the animation, so later frames come only from input.
  field.addEventListener('focus', () => {
    running = false;
    fetch('/focused');
  });
  field.addEventListener('input', () => fetch('/typed?' + field.value.length));
  addEventListener('scrollend', () => {
    const limit = document.documentElement.scrollHeight - innerHeight;
    fetch('/scrolled?' + Math.round(scrollY) + ',' + limit);
  });
</script>
"""


@dataclass
class _KittyFrame:
    """One complete Kitty PNG transmission and when its first and last bytes were read."""

    started: float
    width: int
    height: int
    finished: float = 0.0
    transmission_bytes: int = 0
    base64_chars: int = 0
    checksum: int = 1


class _LiveTerminal:
    """Read the TUI's output, timing each Kitty transmission and discarding painted text.

    Frames run to megabytes, so the output is not kept in ``_TuiProcess.output``:
    replaying it would cost more than the frames being measured.
    """

    def __init__(self, tui: _TuiProcess) -> None:
        self.tui = tui
        self.frames: list[_KittyFrame] = []
        self._pending = bytearray()
        self._current: _KittyFrame | None = None
        self._tail = b""

    def pump(self, seconds: float) -> None:
        """Read output as it arrives for ``seconds``."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select([self.tui.fd], [], [], min(remaining, 0.05))
            if readable:
                try:
                    chunk = os.read(self.tui.fd, 1 << 20)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                self._feed(chunk, time.monotonic())
            waited_pid, waited_status = os.waitpid(self.tui.pid, os.WNOHANG)
            if waited_pid == self.tui.pid:
                self.tui.status = waited_status
                pytest.fail(f"The TUI exited; terminal tail={self._tail!r}")

    def wait(self, condition: Callable[[], bool], *, timeout: float, failure: str) -> None:
        """Read output until ``condition`` holds, failing after ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                pytest.fail(f"{failure}; terminal tail={self._tail!r}")
            self.pump(0.01)

    def next_frame(self, seen: int, *, timeout: float, failure: str) -> _KittyFrame:
        """Return the first frame completed after the first ``seen`` frames."""
        self.wait(lambda: len(self.frames) > seen, timeout=timeout, failure=failure)
        return self.frames[seen]

    def finished_between(self, start: float, end: float) -> list[_KittyFrame]:
        return [frame for frame in self.frames if start <= frame.finished < end]

    def _feed(self, chunk: bytes, now: float) -> None:
        pending = self._pending
        pending += chunk
        position = 0
        while True:
            start = pending.find(b"\x1b_G", position)
            if start < 0:
                # Keep a trailing partial introducer for the next read.
                keep = 2 if pending.endswith(b"\x1b_") else int(pending.endswith(b"\x1b"))
                self._text(pending[position : len(pending) - keep])
                position = len(pending) - keep
                break
            self._text(pending[position:start])
            end = pending.find(b"\x1b\\", start + 3)
            if end < 0:
                position = start
                break
            self._command(bytes(pending[start : end + 2]), now)
            position = end + 2
        del pending[:position]

    def _text(self, text: bytes | bytearray) -> None:
        if text:
            self._tail = (self._tail + bytes(text))[-4000:]

    def _command(self, command: bytes, now: float) -> None:
        header, _, payload = command[3:-2].partition(b";")
        keys = dict(item.split(b"=", 1) for item in header.split(b",") if b"=" in item)
        if keys.get(b"a") == b"T" and keys.get(b"f") == b"100":
            # The PNG signature and IHDR length and type fill 16 bytes; width and height follow.
            ihdr = base64.b64decode(payload[:32])
            width, height = struct.unpack(">II", ihdr[16:24])
            self._current = _KittyFrame(started=now, width=width, height=height)
        elif not header.startswith(b"m=") or self._current is None:
            return
        frame = self._current
        frame.transmission_bytes += len(command)
        frame.base64_chars += len(payload)
        frame.checksum = zlib.adler32(payload, frame.checksum)
        if keys.get(b"m") == b"0":
            frame.finished = now
            self.frames.append(frame)
            self._current = None


def _spread(values: list[float]) -> str:
    """Format median, 95th percentile (nearest rank), and maximum of millisecond values."""
    if not values:
        return "no samples"
    ordered = sorted(values)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
    return (
        f"median {statistics.median(ordered):.1f} ms, p95 {p95:.1f} ms, "
        f"max {ordered[-1]:.1f} ms (n={len(ordered)})"
    )


def _report_rate(label: str, frames: list[_KittyFrame], seconds: float) -> None:
    distinct = len({frame.checksum for frame in frames})
    intervals = [
        (later.finished - earlier.finished) * 1000
        for earlier, later in zip(frames, frames[1:], strict=False)
    ]
    print(
        f"{label}: {distinct / seconds:.1f} distinct frames/s "
        f"({len(frames)} transmissions in {seconds:.2f}s); interval {_spread(intervals)}"
    )


def _cell_press(column: int, row: int) -> bytes:
    return f"\x1b[<0;{column};{row}M\x1b[<0;{column};{row}m".encode()


@pytest.mark.process
@pytest.mark.skipif(
    os.environ.get("WISP_TEST_LIVE_BROWSER") != "1",
    reason="set WISP_TEST_LIVE_BROWSER=1 on a host with Chrome or Chromium",
)
def test_live_browser_speed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Print frame rate, input latency, and frame size for a Retina-sized live page."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    page_opened = threading.Event()
    focused = threading.Event()
    typed_lengths: list[int] = []
    # (scrollY, maximum scrollY) at each scrollend.
    scroll_ends: list[tuple[int, int]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            body = b""
            if url.path == "/page":
                page_opened.set()
                body = _PAGE
            elif url.path == "/focused":
                focused.set()
            elif url.path == "/typed":
                typed_lengths.append(int(url.query))
            elif url.path == "/scrolled":
                position, limit = url.query.split(",")
                scroll_ends.append((int(position), int(limit)))
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
            width=_COLUMNS,
            height=_ROWS,
            cell_pixels=_CELL_PIXELS,
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
                _click_tab(tui, "Browser")
                _wait_screen(tui, "Enter open", "The Browser tab did not focus its address field")
                live = _LiveTerminal(tui)
                opened_at = time.monotonic()
                tui.send(f"http://127.0.0.1:{server.server_port}/page\r".encode())
                live.wait(lambda: bool(live.frames), timeout=10, failure="No frame arrived")
                assert page_opened.is_set()
                print(
                    f"\nOpen to first frame: {(live.frames[0].finished - opened_at) * 1000:.0f} ms"
                )

                live.pump(1.0)
                start = time.monotonic()
                live.pump(_WINDOW)
                animation = live.finished_between(start, time.monotonic())
                assert len(animation) >= 2, "The animation did not reach the terminal"
                _report_rate("Animation", animation, _WINDOW)
                sizes = sorted({(frame.width, frame.height) for frame in animation})
                transmissions = [frame.transmission_bytes for frame in animation]
                png_bytes = [frame.base64_chars * 3 // 4 for frame in animation]
                print(
                    f"Frame pixels {sizes}; Kitty transmission median "
                    f"{statistics.median(transmissions) / 1024:.0f} KiB, max "
                    f"{max(transmissions) / 1024:.0f} KiB; PNG median "
                    f"{statistics.median(png_bytes) / 1024:.0f} KiB, max "
                    f"{max(png_bytes) / 1024:.0f} KiB"
                )

                tui.send(_cell_press(*_INPUT_CELL))
                live.wait(focused.is_set, timeout=5, failure="The page input did not take focus")
                # Let the last animation frame and the focus change settle.
                live.pump(0.5)
                latencies: list[float] = []
                for index, character in enumerate(_TYPED):
                    sent_at = time.monotonic()
                    seen = len(live.frames)
                    tui.send(character.encode())
                    painted = live.next_frame(
                        seen,
                        timeout=2,
                        failure=f"Typing {character!r} (key {index + 1}) did not repaint",
                    )
                    latencies.append((painted.finished - sent_at) * 1000)
                    live.pump(sent_at + 0.1 - time.monotonic())
                live.wait(
                    lambda: bool(typed_lengths) and typed_lengths[-1] == len(_TYPED),
                    timeout=5,
                    failure=f"The page did not receive every key; lengths={typed_lengths[-3:]}",
                )
                late = sum(latency > 100 for latency in latencies)
                print(f"Key to frame: {_spread(latencies)}; {late} keys took over 100 ms")

                live.pump(0.5)
                wheel = "\x1b[<65;{};{}M".format(*_CONTENT_CELL).encode()
                wheel_count = round(_WINDOW * _WHEEL_RATE)
                start = time.monotonic()
                for index in range(wheel_count):
                    live.pump(start + index / _WHEEL_RATE - time.monotonic())
                    tui.send(wheel)
                end = time.monotonic()
                during = live.finished_between(start, end)
                _report_rate("Scrolling", during, end - start)
                assert during, "No frame was painted while the wheel turned"
                live.wait(
                    lambda: live.frames[-1].finished > start,
                    timeout=5,
                    failure="Scrolling did not repaint",
                )
                first_repaint = next(frame for frame in live.frames if frame.finished > start)
                # A scroll the page reports and then a second without another means it settled.
                live.wait(lambda: bool(scroll_ends), timeout=5, failure="The page did not scroll")
                settled_at = time.monotonic()
                reported = len(scroll_ends)
                while time.monotonic() - settled_at < 1.0 and time.monotonic() - end < 8:
                    live.pump(0.1)
                    if len(scroll_ends) != reported:
                        reported, settled_at = len(scroll_ends), time.monotonic()
                position, limit = scroll_ends[-1]
                expected = min(wheel_count * _WHEEL_DELTA, limit)
                print(
                    f"Scroll ended at {position}px of {expected}px expected "
                    f"({wheel_count} wheel events, page limit {limit}px); first repaint "
                    f"{(first_repaint.finished - start) * 1000:.0f} ms after the first wheel, "
                    f"last {(live.frames[-1].finished - end) * 1000:.0f} ms after the last"
                )
                # Held-back wheel input is summed, never dropped or sent twice.
                assert position == expected

                # Ctrl+C leaves the page's input, then the active tab hides the pane.
                tui.send(b"\x03")
                live.pump(0.3)
                _click_tab(tui, "Browser")
                live.wait(
                    lambda: _profile_released(home),
                    timeout=6,
                    failure="Hiding the pane did not stop Chrome",
                )
                tui.quit()
            finally:
                tui.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
