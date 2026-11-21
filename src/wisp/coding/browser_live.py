"""Run a Chromium tab on Wisp's saved profile and retain its latest CDP screencast frame."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import anyio
from anyio.abc import TaskGroup
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from wisp.coding.browser_control import BrowserButtonKind, BrowserControlError, BrowserInteraction
from wisp.coding.browser_profile import browser_profile
from wisp.config.settings import browser_profile_path
from wisp.events import (
    MAX_BROWSER_TITLE_CHARS,
    MAX_BROWSER_URL_CHARS,
    MAX_LIVE_BROWSER_FRAME_CHARS,
    MAX_LIVE_BROWSER_HEIGHT,
    MAX_LIVE_BROWSER_WIDTH,
    BrowserViewportUpdated,
    LiveBrowserViewport,
)

_MAX_CAPTURE_PIXELS = MAX_LIVE_BROWSER_WIDTH * MAX_LIVE_BROWSER_HEIGHT
_INITIAL_VIEWPORT = LiveBrowserViewport(width=900, height=600, device_scale_factor=1)
_STARTUP_TIMEOUT = 20
_TIMEOUT = 10
_CLOSED = "The live browser is closed; reopen it with /browser live"
_CHROME_MAC = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_CHROME_VERSION = re.compile(r"\b(\d+)\.\d+\.\d+\.\d+\b")
# User-agent reduction froze the platform token and the minor version fields.
_USER_AGENT_PLATFORM = (
    "Macintosh; Intel Mac OS X 10_15_7" if sys.platform == "darwin" else "X11; Linux x86_64"
)
_MOUSE_BUTTON_MASK = {"left": 1, "right": 2, "middle": 4}
_MODIFIER_MASK = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}
_KEY_CODES = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Delete": 46,
}


def validate_browser_url(url: str) -> str:
    """Validate a navigation address supplied by the user.

    Args:
        url: Address to open in the live tab.

    Returns:
        The original address if Chromium may navigate to it.

    Raises:
        BrowserControlError: The URL is invalid or contains embedded credentials.
    """
    try:
        parsed = urlsplit(url)
        # Reading the port raises ValueError unless it is a number from 0 to 65535.
        _ = parsed.port
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url)
        ):
            return url
    except ValueError:
        pass
    raise BrowserControlError("Enter an HTTP or HTTPS URL without embedded credentials")


def _chrome_binary() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", str(_CHROME_MAC)):
        if executable := shutil.which(name):
            return executable
    raise BrowserControlError("Install Chrome or put Chromium on PATH to use the live browser")


def is_ssh_session() -> bool:
    """Return whether Wisp is running inside an SSH session."""
    return any(os.environ.get(var) for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def live_browser_available() -> bool:
    """Return whether Wisp can launch its built-in live browser on this host."""
    if os.name != "posix":
        return False
    try:
        _chrome_binary()
    except BrowserControlError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LiveBrowserElement:
    """One visible interactive element discovered in a live browser page snapshot."""

    ref: str
    role: str
    name: str
    tag: str
    x: int
    y: int
    value: str | None = None


@dataclass(frozen=True, slots=True)
class LiveBrowserPageSnapshot:
    """Bounded text and interactive-element snapshot of the live browser page."""

    url: str | None
    title: str | None
    text: str
    elements: tuple[LiveBrowserElement, ...]
    window_mode: bool
    truncated: bool = False


_JS_READ_PAGE = """(maxChars, maxElements) => {
  const prev = document.querySelectorAll('[data-wisp-ref]');
  for (const el of prev) { el.removeAttribute('data-wisp-ref'); }
  const selector = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]',
    '[role="checkbox"]', '[role="radio"]', '[role="tab"]',
    '[role="menuitem"]', '[contenteditable="true"]'
  ].join(',');
  const nodes = document.querySelectorAll(selector);
  const elements = [];
  let idx = 0;
  for (const el of nodes) {
    if (elements.length >= maxElements) break;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    idx += 1;
    const ref = 'e' + idx;
    el.setAttribute('data-wisp-ref', ref);
    const tag = el.tagName.toLowerCase();
    const role = (
      el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' ? 'button' : tag)
    );
    const label = (
      el.getAttribute('aria-label')
      || el.getAttribute('placeholder')
      || el.getAttribute('title')
      || el.innerText
      || el.textContent
      || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, 120);
    let value = null;
    if (el instanceof HTMLInputElement) {
      value = el.type === 'password' ? '[password]' : (el.value || '').slice(0, 120);
    } else if (el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement) {
      value = (el.value || '').slice(0, 120);
    }
    elements.push({
      ref,
      role,
      name: label,
      tag,
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2),
      value,
    });
  }
  const rawText = (document.body ? (document.body.innerText || '') : '').trim();
  const truncated = rawText.length > maxChars;
  return {
    url: location.href,
    title: document.title || '',
    text: truncated ? rawText.slice(0, maxChars) : rawText,
    truncated,
    elements,
  };
}"""

_JS_RESOLVE_TARGET = """(ref, selector) => {
  const el = ref
    ? document.querySelector('[data-wisp-ref=' + JSON.stringify(ref) + ']')
    : document.querySelector(selector);
  if (!el) return null;
  if (typeof el.scrollIntoView === 'function') {
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); }
    catch (_e) { el.scrollIntoView(); }
  }
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  return {
    x: Math.max(0, Math.round(rect.left + rect.width / 2)),
    y: Math.max(0, Math.round(rect.top + rect.height / 2)),
  };
}"""

_JS_FOCUS_TARGET = """(ref, selector, clear) => {
  const el = ref
    ? document.querySelector('[data-wisp-ref=' + JSON.stringify(ref) + ']')
    : selector
      ? document.querySelector(selector)
      : document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return false;
  if (typeof el.scrollIntoView === 'function') {
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); }
    catch (_e) { el.scrollIntoView(); }
  }
  if (typeof el.focus === 'function') {
    el.focus();
  }
  if (clear) {
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && typeof desc.set === 'function') {
        desc.set.call(el, '');
      } else {
        el.value = '';
      }
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (el.isContentEditable) {
      el.textContent = '';
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }
  return true;
}"""

_JS_SMOOTH_SCROLL = """(x, y, dx, dy) => {
  if (typeof document === 'undefined' || typeof WheelEvent === 'undefined') return false;
  const key = Symbol.for('wisp.liveScroll');
  const S = (globalThis[key] ||= {
    rx: 0, ry: 0, cx: 0, cy: 0, x: 0, y: 0, raf: 0,
    resolveHit(px, py) {
      let doc = document, win = window, lx = px, ly = py;
      let el = doc.elementFromPoint(lx, ly);
      while (el) {
        while (el && el.shadowRoot && el.shadowRoot.elementFromPoint) {
          const deeper = el.shadowRoot.elementFromPoint(lx, ly);
          if (!deeper || deeper === el) break;
          el = deeper;
        }
        if (el && (el.tagName === 'EMBED' || el.tagName === 'OBJECT' || el.tagName === 'FRAME')) {
          return null;
        }
        if (el && el.tagName === 'IFRAME') {
          let childDoc;
          try { childDoc = el.contentDocument; } catch { return null; }
          if (!childDoc || !el.contentWindow) return null;
          const r = el.getBoundingClientRect();
          lx -= r.left; ly -= r.top;
          doc = childDoc; win = el.contentWindow;
          el = doc.elementFromPoint(lx, ly);
          continue;
        }
        break;
      }
      return { doc, win, el: el || doc.documentElement, lx, ly };
    },
    scrollChain(info, ddx, ddy) {
      let remX = ddx, remY = ddy;
      let win = info.win, doc = info.doc, el = info.el;
      const isScrollMode = (v) => v === 'auto' || v === 'scroll' || v === 'overlay';
      const isContained = (v) => v === 'contain' || v === 'none';
      const takeMoved = (rem, moved) => (
        Math.abs(moved - rem) < 0.75 ? 0 : rem - Math.round(moved)
      );
      while (win) {
        const scrollingEl = doc.scrollingElement || doc.documentElement;
        while (el && el !== doc.documentElement && el !== scrollingEl) {
          const cs = win.getComputedStyle(el);
          const isYBox = isScrollMode(cs.overflowY) && el.scrollHeight > el.clientHeight + 1;
          const isXBox = isScrollMode(cs.overflowX) && el.scrollWidth > el.clientWidth + 1;
          const canY = remY !== 0 && isYBox
            && (remY > 0 ? el.scrollTop + el.clientHeight < el.scrollHeight - 1 : el.scrollTop > 0);
          const canX = remX !== 0 && isXBox
            && (remX > 0 ? el.scrollLeft + el.clientWidth < el.scrollWidth - 1 : el.scrollLeft > 0);
          if (canX || canY) {
            const prevL = el.scrollLeft, prevT = el.scrollTop;
            el.scrollBy({ left: canX ? remX : 0, top: canY ? remY : 0, behavior: 'instant' });
            if (canX) remX = takeMoved(remX, el.scrollLeft - prevL);
            if (canY) remY = takeMoved(remY, el.scrollTop - prevT);
            if (remX === 0 && remY === 0) return;
          }
          if (remY !== 0 && isYBox && isContained(cs.overscrollBehaviorY)) remY = 0;
          if (remX !== 0 && isXBox && isContained(cs.overscrollBehaviorX)) remX = 0;
          if (remX === 0 && remY === 0) return;
          el = el.parentElement || (el.getRootNode && el.getRootNode().host) || null;
        }
        const htmlCs = doc.documentElement ? win.getComputedStyle(doc.documentElement) : null;
        const bodyCs = doc.body ? win.getComputedStyle(doc.body) : null;
        const vpOverflowY = htmlCs && htmlCs.overflowY !== 'visible'
          ? htmlCs.overflowY
          : (bodyCs ? bodyCs.overflowY : 'visible');
        const vpOverflowX = htmlCs && htmlCs.overflowX !== 'visible'
          ? htmlCs.overflowX
          : (bodyCs ? bodyCs.overflowX : 'visible');
        const vpLockY = vpOverflowY === 'hidden' || vpOverflowY === 'clip';
        const vpLockX = vpOverflowX === 'hidden' || vpOverflowX === 'clip';
        if (win === window) {
          if ((!vpLockX && remX !== 0) || (!vpLockY && remY !== 0)) {
            win.scrollBy({
              left: vpLockX ? 0 : remX,
              top: vpLockY ? 0 : remY,
              behavior: 'instant',
            });
          }
          return;
        }
        const canY = remY !== 0 && !vpLockY && scrollingEl
          && scrollingEl.scrollHeight > scrollingEl.clientHeight + 1
          && (remY > 0
            ? scrollingEl.scrollTop + scrollingEl.clientHeight < scrollingEl.scrollHeight - 1
            : scrollingEl.scrollTop > 0);
        const canX = remX !== 0 && !vpLockX && scrollingEl
          && scrollingEl.scrollWidth > scrollingEl.clientWidth + 1
          && (remX > 0
            ? scrollingEl.scrollLeft + scrollingEl.clientWidth < scrollingEl.scrollWidth - 1
            : scrollingEl.scrollLeft > 0);
        if (canX || canY) {
          const prevL = win.scrollX, prevT = win.scrollY;
          win.scrollBy({ left: canX ? remX : 0, top: canY ? remY : 0, behavior: 'instant' });
          if (canX) remX = takeMoved(remX, win.scrollX - prevL);
          if (canY) remY = takeMoved(remY, win.scrollY - prevT);
          if (remX === 0 && remY === 0) return;
        }
        const containY = (htmlCs && isContained(htmlCs.overscrollBehaviorY))
          || (bodyCs && isContained(bodyCs.overscrollBehaviorY));
        const containX = (htmlCs && isContained(htmlCs.overscrollBehaviorX))
          || (bodyCs && isContained(bodyCs.overscrollBehaviorX));
        if (remY !== 0 && containY) remY = 0;
        if (remX !== 0 && containX) remX = 0;
        if (remX === 0 && remY === 0) return;
        let fe = null;
        try { fe = win.frameElement; } catch { fe = null; }
        if (!fe || !win.parent || win.parent === win) return;
        win = win.parent;
        doc = win.document;
        el = fe.parentElement || (fe.getRootNode && fe.getRootNode().host) || null;
      }
    },
    step() {
      S.raf = 0;
      const take = (rem, carry) => {
        if (rem === 0) return [0, 0, 0];
        if (Math.abs(rem) <= 4) return [rem, 0, 0];
        const raw = rem * 0.45 + carry;
        let s = Math.trunc(raw);
        if (s === 0) s = Math.sign(rem);
        if (Math.abs(s) > Math.abs(rem)) s = rem;
        return [s, rem - s, raw - s];
      };
      const [sx, nrx, ncx] = take(S.rx, S.cx);
      const [sy, nry, ncy] = take(S.ry, S.cy);
      S.rx = nrx; S.cx = ncx; S.ry = nry; S.cy = ncy;
      if (sx !== 0 || sy !== 0) {
        const info = S.resolveHit(S.x, S.y);
        if (info) S.scrollChain(info, sx, sy);
      }
      if (S.rx !== 0 || S.ry !== 0) S.raf = requestAnimationFrame(S.step);
    }
  });
  const info = S.resolveHit(x, y);
  if (!info) return false;
  const ev = new info.win.WheelEvent('wheel', {
    clientX: info.lx, clientY: info.ly, deltaX: dx, deltaY: dy, deltaMode: 0,
    bubbles: true, cancelable: true, composed: true
  });
  if (info.el && !info.el.dispatchEvent(ev)) return true;
  S.x = x; S.y = y;
  if (S.ry !== 0 && dy !== 0 && Math.sign(S.ry) !== Math.sign(dy)) { S.ry = dy; S.cy = 0; }
  else { S.ry += dy; }
  if (S.rx !== 0 && dx !== 0 && Math.sign(S.rx) !== Math.sign(dx)) { S.rx = dx; S.cx = 0; }
  else { S.rx += dx; }
  if (!S.raf) S.raf = requestAnimationFrame(S.step);
  return true;
}"""

_JS_CANCEL_SMOOTH_SCROLL = """(() => {
  const S = globalThis[Symbol.for('wisp.liveScroll')];
  if (!S) return;
  S.rx = 0; S.ry = 0; S.cx = 0; S.cy = 0;
  if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; }
})()"""


class LiveBrowser:
    """Own a single user-controlled tab for the lifetime of the RPC host."""

    def __init__(self, tasks: TaskGroup, *, profile_dir: Path | None = None) -> None:
        """Prepare a closed browser.

        Args:
            tasks: Task group that owns the Chromium process.
            profile_dir: Saved Chromium profile, by default ``~/.wisp/browser-profile``.
                Only one browser may use it at a time.
        """
        self._profile_dir = profile_dir
        self._tasks = tasks
        self._scope: anyio.CancelScope | None = None
        self._stopped: anyio.Event | None = None
        self._socket: ClientConnection | None = None
        self._target: str | None = None
        self._pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._next_id = 0
        self._first_frame = anyio.Event()
        self._viewport: LiveBrowserViewport | None = None
        self._screencast_id = 0
        self._capture_loader: str | None = None
        self._navigation = anyio.Event()
        self._loader: str | None = None
        self._main_frame: str | None = None
        self._frame: str | None = None
        self._delivered: tuple[str, str | None] | None = None
        self._url: str | None = None
        self._title: str | None = None
        self._window_mode = False
        self._smooth_scroll_active = False
        # Held while capture state changes (open, resize, a fresh capture after a
        # document change) so a frame reader never takes the frame such a change
        # returns, and pointer input never sees the capture half-built.
        self._capture_lock = anyio.Lock()
        # Set and replaced whenever a frame, the page address, or the browser's
        # lifetime changes, waking every next_frame reader at once.
        self._changed = anyio.Event()

    @property
    def profile_dir(self) -> Path:
        """The saved Chromium profile directory this browser opens tabs on."""
        return self._profile_dir or browser_profile_path()

    @property
    def is_open(self) -> bool:
        """Whether a live Chromium tab is currently running."""
        return (
            self._socket is not None and self._scope is not None and not self._scope.cancel_called
        )

    @property
    def window_mode(self) -> bool:
        """Whether the live browser is currently running in a visible Chrome window."""
        return self._window_mode

    @property
    def current_url(self) -> str | None:
        """The current top-level page URL, or None while closed."""
        return self._url

    @property
    def current_title(self) -> str | None:
        """The last observed page title, or None when unknown."""
        return self._title

    def is_available(self) -> bool:
        """Return whether this host can launch the live browser."""
        return live_browser_available()

    async def open(self, url: str, *, window: bool = False) -> BrowserViewportUpdated:
        """Replace the existing tab with a new one on the saved Chromium profile.

        Args:
            url: HTTP or HTTPS address for the new tab.
            window: When True, launch Chrome as a visible desktop window instead
                of headless in the terminal pane. Unavailable over SSH.

        Returns:
            The first frame after navigation commits, or a window-mode viewport update.

        Raises:
            BrowserControlError: The address, Chromium startup, or navigation failed.
            TimeoutError: The first frame did not arrive within the startup deadline.
        """
        url = validate_browser_url(url)
        if window and is_ssh_session():
            raise BrowserControlError("Window mode is unavailable over SSH")
        async with self._capture_lock:
            return await self._open_locked(url, window=window)

    async def _open_locked(self, url: str, *, window: bool) -> BrowserViewportUpdated:
        await self._stop()
        executable = _chrome_binary()
        try:
            with anyio.fail_after(_STARTUP_TIMEOUT):
                await self._tasks.start(self._run, executable, window)
                self._window_mode = window
                if not window:
                    await self._set_metrics(_INITIAL_VIEWPORT)
                await self._command("Page.enable", {})
                await self._command("Inspector.enable", {})
                await self._command("Page.bringToFront", {})
                await self._navigate(url)
                await self._refresh_title()
                if window:
                    return self._window_viewport_event()
                return await self._start_capture(_INITIAL_VIEWPORT)
        except BaseException:
            await self._stop()
            raise

    async def switch_mode(self, *, window: bool, url: str | None = None) -> BrowserViewportUpdated:
        """Open or switch the live browser between terminal and window modes.

        Switching modes restarts Chrome on the same profile so cookies and
        sign-ins carry over, then reopens ``url`` (or the current page URL).

        Args:
            window: True for a visible Chrome window, False for the TUI pane.
            url: Optional HTTP/HTTPS URL to navigate to; defaults to the current URL.

        Returns:
            A viewport update describing the new frame or window-mode state.
        """
        validated_url = validate_browser_url(url) if url is not None else None
        if window and is_ssh_session():
            raise BrowserControlError("Window mode is unavailable over SSH")
        async with self._capture_lock:
            target = validated_url if validated_url is not None else self._url
            if target is None:
                raise BrowserControlError("Specify a URL to open the live browser")
            if self.is_open and self._window_mode == window:
                if validated_url is not None and target != self._url:
                    await self._navigate(target)
                    await self._refresh_title()
                if window:
                    return self._window_viewport_event()
                frame = await self._poll_frame()
                return frame if frame is not None else self._fallback_terminal_event()
            return await self._open_locked(target, window=window)

    async def navigate(self, url: str) -> None:
        """Navigate the existing tab to a validated HTTP or HTTPS address."""
        await self._navigate(validate_browser_url(url))

    async def _navigate(self, url: str) -> None:
        await self._cancel_smooth_scroll()
        result = await self._command("Page.navigate", {"url": url})
        if result.get("errorText") or result.get("isDownload"):
            raise BrowserControlError("Chromium could not navigate to the requested page")
        loader = result.get("loaderId")
        if isinstance(loader, str):
            with anyio.move_on_after(_TIMEOUT):
                while self._loader != loader and self._socket is not None:
                    await self._navigation.wait()
                    self._navigation = anyio.Event()
            if self._loader != loader:
                raise BrowserControlError(
                    "Chromium did not finish navigating to the requested page"
                )

    async def move_in_history(self, offset: Literal[-1, 1]) -> None:
        """Traverse one entry in this tab's history, staying put at either end."""
        await self._cancel_smooth_scroll()
        history = await self._command("Page.getNavigationHistory", {})
        current = history.get("currentIndex")
        entries = history.get("entries")
        if not isinstance(current, int) or not isinstance(entries, list):
            raise BrowserControlError("Chromium did not return its navigation history")
        index = current + offset
        if 0 <= index < len(entries):
            await self._change_document(
                "Page.navigateToHistoryEntry", {"entryId": entries[index]["id"]}
            )

    async def _change_document(self, method: str, params: Mapping[str, object]) -> None:
        await self._cancel_smooth_scroll()
        self._navigation = anyio.Event()
        await self._command(method, params)
        with anyio.move_on_after(_TIMEOUT):
            await self._navigation.wait()
            return
        raise BrowserControlError("Chromium did not finish the requested navigation")

    async def poll_frame(self) -> BrowserViewportUpdated | None:
        """Return the newest frame, starting a fresh capture after a document change."""
        async with self._capture_lock:
            return await self._poll_frame()

    async def _poll_frame(self) -> BrowserViewportUpdated | None:
        if self._window_mode:
            await self._refresh_title()
            return self._window_viewport_event()
        if self._viewport is not None and self._capture_loader != self._loader:
            return await self._restart_capture(self._viewport, resize=False)
        return self.take_frame()

    async def next_frame(self) -> BrowserViewportUpdated | None:
        """Wait until a frame or page address the caller has not seen exists, then return it.

        Delivery is latest-wins: frames Chromium produces while nobody waits
        replace each other, so a slow reader gets only the newest one. A
        document change starts a fresh capture first, as ``poll_frame`` does.
        Waiting costs nothing on a static page.

        Returns:
            The newest undelivered update, or None if the browser this wait
            began on was closed on request (``close``, ``request_close``, or a
            replacing ``open``).

        Raises:
            BrowserControlError: The browser is not open, Chromium exited on
                its own, or a fresh capture failed.
        """
        # Pinned before the first await: a replacing open may hold the capture lock,
        # and its new tab must not answer a read begun on the old one.
        scope = self._scope
        if self._socket is None or scope is None or scope.cancel_called:
            raise BrowserControlError(_CLOSED)
        if self._window_mode:
            return None
        while True:
            # Taken before the check, so a change during the check wakes the wait.
            changed = self._changed
            async with self._capture_lock:
                if scope.cancel_called or self._window_mode:
                    return None
                if self._scope is not scope:
                    raise BrowserControlError(_CLOSED)
                frame = await self._poll_frame()
            if frame is not None:
                return frame
            await changed.wait()

    @property
    def lifetime(self) -> anyio.CancelScope | None:
        """A token for the open tab, or None while closed; see ``is_current``."""
        return self._scope

    def is_current(self, lifetime: anyio.CancelScope | None) -> bool:
        """Whether the tab ``lifetime`` was taken from is still open and not closing.

        A close, ``request_close``, or a replacing ``open`` ends a lifetime for good.
        """
        return lifetime is not None and lifetime is self._scope and not lifetime.cancel_called

    def _signal_change(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()

    async def resize(self, viewport: LiveBrowserViewport) -> BrowserViewportUpdated:
        """Apply CSS layout and device scale, then return a frame from the new capture.

        Args:
            viewport: Bounded CSS dimensions and device pixel ratio.

        Returns:
            The first matching frame, including on a static page.
        """
        async with self._capture_lock:
            if self._window_mode:
                return self._window_viewport_event()
            return await self._restart_capture(viewport, resize=True)

    async def _restart_capture(
        self, viewport: LiveBrowserViewport, *, resize: bool
    ) -> BrowserViewportUpdated:
        # Stop retaining old frames before the first await. Chromium tags each new
        # screencast with a new sessionId, including scale-only changes.
        self._viewport = None
        self._frame = self._delivered = None
        try:
            await self._command("Page.stopScreencast", {})
            if resize:
                await self._set_metrics(viewport)
            return await self._start_capture(viewport)
        except BaseException:
            # Once capture has stopped its state is ambiguous on interruption.
            await self._stop()
            raise

    async def _set_metrics(self, viewport: LiveBrowserViewport) -> None:
        await self._command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": viewport.width,
                "height": viewport.height,
                "deviceScaleFactor": viewport.device_scale_factor,
                "mobile": False,
            },
        )

    async def _start_capture(self, viewport: LiveBrowserViewport) -> BrowserViewportUpdated:
        self._viewport = viewport
        max_width, max_height = _capture_size(viewport)
        # A redirect can commit during capture startup. Retire that generation
        # and retry within the same deadline to bind pixels to the new document.
        with anyio.move_on_after(_TIMEOUT):
            while True:
                self._capture_loader = self._loader
                self._first_frame = anyio.Event()
                self._screencast_id += 1
                # A CDP metrics or navigation acknowledgement can precede repaint.
                # A one-pixel surface copy waits for the compositor on static pages.
                await self._command(
                    "Page.captureScreenshot",
                    {
                        "captureBeyondViewport": False,
                        "clip": {"x": 0, "y": 0, "width": 1, "height": 1, "scale": 1},
                    },
                )
                await self._command(
                    "Page.startScreencast",
                    {"format": "png", "maxWidth": max_width, "maxHeight": max_height},
                )
                await self._first_frame.wait()
                if (frame := self.take_frame()) is not None:
                    return frame
                if self._capture_loader == self._loader:
                    break
                await self._command("Page.stopScreencast", {})
        raise BrowserControlError("Chromium did not return a frame for the requested viewport")

    async def close(self) -> None:
        """Stop capture and reap the browser before returning, even on cancellation.

        A capture change in progress, such as a reader's fresh capture after a
        document change, finishes first, so its frame is delivered before the close.
        """
        with anyio.CancelScope(shield=True):
            async with self._capture_lock:
                await self._stop()

    async def _stop(self) -> None:
        self.request_close()
        if self._stopped is not None:
            with anyio.CancelScope(shield=True):
                await self._stopped.wait()

    def request_close(self) -> None:
        """Stop accepting input and arrange for cleanup in the owning task."""
        if self._scope is not None:
            self._scope.cancel()

    async def _run(
        self,
        executable: str,
        window: bool = False,
        *,
        task_status: anyio.abc.TaskStatus[None],
    ) -> None:
        self._stopped = stopped = anyio.Event()
        started = False
        try:
            with (
                anyio.CancelScope() as scope,
                browser_profile(self.profile_dir) as profile,
            ):
                self._scope = scope
                env_keys = ["PATH", "HOME", "TMPDIR", "LANG"]
                if sys.platform.startswith("linux"):
                    # Let Chromium discover the desktop keyring and its session bus.
                    env_keys.extend(
                        (
                            "XDG_CURRENT_DESKTOP",
                            "XDG_RUNTIME_DIR",
                            "DBUS_SESSION_BUS_ADDRESS",
                            "KDE_SESSION_VERSION",
                        )
                    )
                    if window:
                        env_keys.extend(("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"))
                environment = {key: os.environ[key] for key in env_keys if key in os.environ}
                launch_args = [executable]
                if not window:
                    launch_args.extend(
                        [
                            "--headless",
                            # CDP captures the compositor surface. Emulating DPR alone
                            # changes page layout but leaves native frames at 1×.
                            "--force-device-scale-factor=2",
                        ]
                    )
                launch_args.extend(
                    [
                        "--no-first-run",
                        "--no-default-browser-check",
                        *await _desktop_identity_args(executable, environment),
                        "--remote-debugging-port=0",
                        "--remote-debugging-address=127.0.0.1",
                        f"--user-data-dir={profile.path}",
                        "about:blank",
                    ]
                )
                process = await anyio.open_process(
                    launch_args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    cwd=profile.path,
                    pass_fds=profile.pass_fds,
                )
                try:
                    async with connect(
                        await _debugger_address(profile.path, process),
                        proxy=None,
                        max_size=MAX_LIVE_BROWSER_FRAME_CHARS + 8192,
                        close_timeout=1,
                    ) as socket:
                        self._socket = socket
                        try:
                            async with anyio.create_task_group() as reader:
                                reader.start_soon(self._read, socket)
                                self._target = await self._attach_tab()
                                task_status.started()
                                started = True
                        finally:
                            if process.returncode is None:
                                await _close_saved_browser(socket, self._next_id + 1, process)
                finally:
                    with anyio.CancelScope(shield=True):
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.terminate()
                            with anyio.move_on_after(2):
                                await process.wait()
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.kill()
                        await process.aclose()
        except BrowserControlError:
            if not started:
                raise
        except Exception:  # noqa: BLE001 - keep the RPC host alive after Chromium exits
            if not started:
                raise BrowserControlError("Could not start the isolated Chromium browser") from None
        finally:
            self._scope = None
            self._socket = None
            self._target = None
            self._frame = self._delivered = self._url = self._loader = self._main_frame = None
            self._title = None
            self._window_mode = False
            self._smooth_scroll_active = False
            self._viewport = None
            self._screencast_id = 0
            self._capture_loader = None
            self._navigation.set()
            self._first_frame.set()
            self._signal_change()
            stopped.set()

    async def _attach_tab(self) -> str:
        await self._command("Browser.setDownloadBehavior", {"behavior": "deny"})
        created = await self._command("Target.createTarget", {"url": "about:blank"})
        attached = await self._command(
            "Target.attachToTarget", {"targetId": created.get("targetId"), "flatten": True}
        )
        session_id = attached.get("sessionId")
        if not isinstance(session_id, str):
            raise BrowserControlError("Chromium did not attach to its browser tab")
        return session_id

    async def _read(self, socket: ClientConnection) -> None:
        try:
            async for raw in socket:
                message = json.loads(raw)
                request_id = message.get("id")
                if request_id is not None:
                    if (reply := self._pending.get(request_id)) is not None and not reply.done():
                        reply.set_result(message)
                    continue
                params = message.get("params", {})
                if (
                    message.get("method") == "Target.detachedFromTarget"
                    and self._target is not None
                    and params.get("sessionId") == self._target
                ):
                    return
                if message.get("sessionId") == self._target:
                    match message.get("method"):
                        case "Inspector.targetCrashed" | "Inspector.detached":
                            return
                        case "Page.frameNavigated":
                            frame = params["frame"]
                            if "parentId" not in frame:
                                if self._loader != frame["loaderId"]:
                                    self._frame = self._delivered = None
                                    self._title = None
                                    self._smooth_scroll_active = False
                                    self._first_frame.set()
                                self._main_frame = frame["id"]
                                self._loader = frame["loaderId"]
                                self._url = frame["url"]
                                self._navigation.set()
                                self._signal_change()
                        case "Page.navigatedWithinDocument":
                            if params["frameId"] == self._main_frame:
                                if self._smooth_scroll_active:
                                    self._smooth_scroll_active = False
                                    with suppress(BrowserControlError):
                                        await self._send(
                                            "Runtime.evaluate",
                                            {
                                                "expression": _JS_CANCEL_SMOOTH_SCROLL,
                                                "returnByValue": False,
                                                "awaitPromise": False,
                                            },
                                            expect_reply=False,
                                        )
                                self._url = params["url"]
                                self._navigation.set()
                                self._signal_change()
                        case "Page.screencastFrame":
                            self._accept_frame(params)
                            await self._send(
                                "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
                            )
        except WebSocketException:
            pass
        finally:
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_result({"error": True})

    def _accept_frame(self, params: Mapping[str, object]) -> None:
        viewport = self._viewport
        capture_id = params.get("sessionId")
        metadata = params.get("metadata")
        data = params.get("data")
        if (
            viewport is not None
            and self._capture_loader == self._loader
            and capture_id == self._screencast_id
            and isinstance(metadata, dict)
            and metadata.get("deviceWidth") == viewport.width
            and metadata.get("deviceHeight") == viewport.height
            and isinstance(data, str)
            and len(data) <= MAX_LIVE_BROWSER_FRAME_CHARS
            and _png_matches_viewport(data, viewport)
        ):
            self._frame = data
            self._first_frame.set()
            self._signal_change()

    async def _send(
        self, method: str, params: Mapping[str, object], *, expect_reply: bool = False
    ) -> asyncio.Future[dict[str, object]] | None:
        socket = self._socket
        if socket is None or self._scope is None:
            raise BrowserControlError(_CLOSED)
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, object] = {"id": request_id, "method": method, "params": dict(params)}
        if self._target is not None:
            message["sessionId"] = self._target
        reply = asyncio.get_running_loop().create_future() if expect_reply else None
        if reply is not None:
            self._pending[request_id] = reply
        try:
            await socket.send(json.dumps(message))
            if reply is not None:
                with anyio.fail_after(_TIMEOUT):
                    await reply
            return reply
        except (TimeoutError, WebSocketException):
            raise BrowserControlError(
                "Lost connection to Chromium; reopen the live browser"
            ) from None
        finally:
            self._pending.pop(request_id, None)

    async def _command(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        reply = await self._send(method, params, expect_reply=True)
        assert reply is not None
        result = reply.result().get("result")
        if not isinstance(result, dict):
            raise BrowserControlError(f"Chromium rejected {method}")
        return result

    def take_frame(self) -> BrowserViewportUpdated | None:
        """Consume the newest frame, dropping intermediate frames.

        Returns:
            A viewport update if the image or page address changed.

        Raises:
            BrowserControlError: The browser has closed.
        """
        if self._socket is None or self._scope is None or self._scope.cancel_called:
            raise BrowserControlError(_CLOSED)
        if self._window_mode:
            return None
        viewport = self._viewport
        if viewport is None or self._frame is None or (self._frame, self._url) == self._delivered:
            return None
        self._delivered = (self._frame, self._url)
        return BrowserViewportUpdated(
            live_png=self._frame,
            url=self._url[:MAX_BROWSER_URL_CHARS] if self._url else None,
            title=self._sanitized_title(),
            viewport_width=viewport.width,
            viewport_height=viewport.height,
            live_device_scale=viewport.device_scale_factor,
        )

    def _sanitized_title(self) -> str:
        title = (self._title or "").strip()
        return title[:MAX_BROWSER_TITLE_CHARS] if title else "Live Chromium"

    def _window_viewport_event(self) -> BrowserViewportUpdated:
        return BrowserViewportUpdated(
            url=self._url[:MAX_BROWSER_URL_CHARS] if self._url else None,
            title=self._sanitized_title(),
            window_mode=True,
        )

    def _fallback_terminal_event(self) -> BrowserViewportUpdated:
        viewport = self._viewport
        return BrowserViewportUpdated(
            live_png=self._frame,
            url=self._url[:MAX_BROWSER_URL_CHARS] if self._url else None,
            title=self._sanitized_title(),
            viewport_width=viewport.width if viewport is not None else None,
            viewport_height=viewport.height if viewport is not None else None,
            live_device_scale=viewport.device_scale_factor if viewport is not None else None,
        )

    async def _evaluate(self, expression: str) -> object:
        result = await self._command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        if result.get("exceptionDetails") is not None:
            raise BrowserControlError("Could not inspect or target the page in the live browser")
        remote = result.get("result")
        if isinstance(remote, dict):
            return remote.get("value")
        return None

    async def _refresh_title(self) -> None:
        with suppress(BrowserControlError):
            title = await self._evaluate("document.title || ''")
            if isinstance(title, str):
                cleaned = title.strip()
                self._title = cleaned[:MAX_BROWSER_TITLE_CHARS] if cleaned else None

    async def read_page(
        self,
        *,
        max_chars: int = 12_000,
        max_elements: int = 60,
    ) -> LiveBrowserPageSnapshot:
        """Read the current page's URL, title, visible text, and interactive elements.

        Args:
            max_chars: Maximum characters of page text to return.
            max_elements: Maximum visible interactive elements to tag with refs.

        Returns:
            A bounded snapshot of the current page.

        Raises:
            BrowserControlError: The browser is closed or evaluation failed.
        """
        if not self.is_open:
            raise BrowserControlError(_CLOSED)
        expression = f"({_JS_READ_PAGE})({int(max_chars)}, {int(max_elements)})"
        raw = await self._evaluate(expression)
        if not isinstance(raw, dict):
            raise BrowserControlError("Could not read page state from the live browser")
        url = raw.get("url")
        if isinstance(url, str) and url:
            self._url = url
        title = raw.get("title")
        if isinstance(title, str):
            cleaned = title.strip()
            self._title = cleaned[:MAX_BROWSER_TITLE_CHARS] if cleaned else None
        raw_text = raw.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        truncated = bool(raw.get("truncated", False))
        elements: list[LiveBrowserElement] = []
        raw_elements = raw.get("elements")
        if isinstance(raw_elements, list):
            for item in raw_elements:
                if not isinstance(item, dict):
                    continue
                ref = item.get("ref")
                role = item.get("role")
                name = item.get("name")
                tag = item.get("tag")
                x = item.get("x")
                y = item.get("y")
                value = item.get("value")
                if (
                    isinstance(ref, str)
                    and isinstance(role, str)
                    and isinstance(name, str)
                    and isinstance(tag, str)
                    and isinstance(x, int)
                    and isinstance(y, int)
                ):
                    elements.append(
                        LiveBrowserElement(
                            ref=ref,
                            role=role,
                            name=name,
                            tag=tag,
                            x=x,
                            y=y,
                            value=value if isinstance(value, str) else None,
                        )
                    )
        return LiveBrowserPageSnapshot(
            url=self._url,
            title=self._title,
            text=text,
            elements=tuple(elements),
            window_mode=self._window_mode,
            truncated=truncated,
        )

    async def click_element(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        x: int | None = None,
        y: int | None = None,
        button: BrowserButtonKind = "left",
        double_click: bool = False,
    ) -> BrowserViewportUpdated | None:
        """Click an element by ref, CSS selector, or viewport coordinates."""
        if not self.is_open:
            raise BrowserControlError(_CLOSED)
        await self._cancel_smooth_scroll()
        if ref is not None or selector is not None:
            expression = f"({_JS_RESOLVE_TARGET})({json.dumps(ref)}, {json.dumps(selector)})"
            coords = await self._evaluate(expression)
            if not isinstance(coords, dict):
                target_desc = f"ref {ref!r}" if ref is not None else f"selector {selector!r}"
                raise BrowserControlError(
                    f"Element not found or not visible for {target_desc}; run browser_read first"
                )
            cx = coords.get("x")
            cy = coords.get("y")
            if not isinstance(cx, int) or not isinstance(cy, int):
                raise BrowserControlError("Could not resolve element coordinates")
            x, y = cx, cy
        if x is None or y is None or x < 0 or y < 0:
            raise BrowserControlError("Provide ref, selector, or non-negative x and y coordinates")
        await self._click_at(x, y, button=button, double_click=double_click)
        await anyio.sleep(0.05)
        await self._refresh_title()
        return await self._agent_viewport_event()

    async def type_text(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        clear: bool = False,
        press_enter: bool = False,
    ) -> BrowserViewportUpdated | None:
        """Type text into a targeted or currently focused element."""
        if not self.is_open:
            raise BrowserControlError(_CLOSED)
        if any((ord(ch) < 32 and ch not in "\t\n\r") or 0x7F <= ord(ch) <= 0x9F for ch in text):
            raise BrowserControlError("Text contains unsupported control characters")
        if ref is not None or selector is not None or clear:
            expression = (
                f"({_JS_FOCUS_TARGET})"
                f"({json.dumps(ref)}, {json.dumps(selector)}, {json.dumps(clear)})"
            )
            focused = await self._evaluate(expression)
            if (ref is not None or selector is not None) and focused is not True:
                target_desc = f"ref {ref!r}" if ref is not None else f"selector {selector!r}"
                raise BrowserControlError(
                    f"Element not found for {target_desc}; run browser_read first"
                )
        if text:
            await self._command("Input.insertText", {"text": text})
        if press_enter:
            params = {"key": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0}
            await self._command(
                "Input.dispatchKeyEvent",
                {"type": "keyDown", "text": "\r", "unmodifiedText": "\r", **params},
            )
            await self._command("Input.dispatchKeyEvent", {"type": "keyUp", **params})
        await anyio.sleep(0.05)
        await self._refresh_title()
        return await self._agent_viewport_event()

    async def capture_screenshot(self) -> tuple[bytes, str | None, str | None]:
        """Capture a PNG screenshot of the current live browser viewport."""
        if not self.is_open:
            raise BrowserControlError(_CLOSED)
        result = await self._command(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
        )
        data = result.get("data")
        if not isinstance(data, str) or not data or len(data) > MAX_LIVE_BROWSER_FRAME_CHARS:
            raise BrowserControlError("Chromium did not return a valid screenshot")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise BrowserControlError("Chromium returned malformed screenshot data") from exc
        await self._refresh_title()
        return raw, self._url, self._title

    async def _agent_viewport_event(self) -> BrowserViewportUpdated | None:
        if self._window_mode:
            return self._window_viewport_event()
        frame = await self.poll_frame()
        return frame if frame is not None else self._fallback_terminal_event()

    async def _click_at(
        self,
        x: int,
        y: int,
        *,
        button: BrowserButtonKind = "left",
        double_click: bool = False,
    ) -> None:
        params = {"x": x, "y": y}
        await self._command(
            "Input.dispatchMouseEvent", {**params, "type": "mouseMoved", "buttons": 0}
        )
        for count in range(1, 3 if double_click else 2):
            await self._command(
                "Input.dispatchMouseEvent",
                {
                    **params,
                    "type": "mousePressed",
                    "button": button,
                    "buttons": _MOUSE_BUTTON_MASK[button],
                    "clickCount": count,
                },
            )
            await self._command(
                "Input.dispatchMouseEvent",
                {
                    **params,
                    "type": "mouseReleased",
                    "button": button,
                    "buttons": 0,
                    "clickCount": count,
                },
            )

    async def interact(self, request: BrowserInteraction) -> None:
        """Dispatch user input through Chromium's normal page input handling.

        Args:
            request: Validated click, scroll, text, or keyboard input.

        Raises:
            BrowserControlError: The request failed or the browser has closed.
        """
        if self._scope is None or self._scope.cancel_called:
            raise BrowserControlError(_CLOSED)
        match request.action:
            case "click" | "scroll":
                await self._mouse(request)
            case "type":
                await self._command("Input.insertText", {"text": request.text})
            case "press_key":
                key = request.key or ""
                modifiers = request.modifiers or ()
                code = _KEY_CODES.get(
                    key, ord(key.upper()) if len(key) == 1 and key.isascii() else 0
                )
                params = {
                    "key": key,
                    "windowsVirtualKeyCode": code,
                    "modifiers": sum(_MODIFIER_MASK[modifier] for modifier in modifiers),
                }
                keydown = {"type": "rawKeyDown"}
                if key == "Enter" and modifiers in ((), ("Shift",)):
                    # Chromium needs the text-producing event for newline and form submission.
                    keydown = {"type": "keyDown", "text": "\r", "unmodifiedText": "\r"}
                await self._command("Input.dispatchKeyEvent", {**keydown, **params})
                await self._command("Input.dispatchKeyEvent", {"type": "keyUp", **params})
            case "back":
                await self.move_in_history(-1)
            case "reload":
                await self._change_document("Page.reload", {})
            case _:
                raise BrowserControlError("This action is unavailable in the live browser preview")

    async def _mouse(self, request: BrowserInteraction) -> None:
        x, y = request.x, request.y
        # Pointer coordinates are checked against the capture, so wait out a change.
        async with self._capture_lock:
            viewport = self._viewport
            frame = self._frame
        if (
            viewport is None
            or frame is None
            or x is None
            or y is None
            or not (0 <= x < viewport.width and 0 <= y < viewport.height)
        ):
            raise BrowserControlError("Click or scroll inside the live browser frame")
        params = {"x": x, "y": y}
        if request.action == "scroll":
            await self._scroll(x, y, request.delta_x or 0, request.delta_y or 0)
            return
        await self._cancel_smooth_scroll()
        button = request.button or "left"
        # A person's pointer arrives before it presses. Pages rely on the hover
        # this produces, and a press with no pointer history reads as scripted.
        await self._command(
            "Input.dispatchMouseEvent", {**params, "type": "mouseMoved", "buttons": 0}
        )
        for count in range(1, 3 if request.double_click else 2):
            await self._command(
                "Input.dispatchMouseEvent",
                {
                    **params,
                    "type": "mousePressed",
                    "button": button,
                    "buttons": _MOUSE_BUTTON_MASK[button],
                    "clickCount": count,
                },
            )
            await self._command(
                "Input.dispatchMouseEvent",
                {
                    **params,
                    "type": "mouseReleased",
                    "button": button,
                    "buttons": 0,
                    "clickCount": count,
                },
            )

    async def _cancel_smooth_scroll(self) -> None:
        if not self._smooth_scroll_active:
            return
        self._smooth_scroll_active = False
        with suppress(BrowserControlError):
            await self._command(
                "Runtime.evaluate",
                {
                    "expression": _JS_CANCEL_SMOOTH_SCROLL,
                    "returnByValue": False,
                    "awaitPromise": False,
                },
            )

    async def _scroll(self, x: int, y: int, delta_x: int, delta_y: int) -> None:
        args = f"{int(x)}, {int(y)}, {int(delta_x)}, {int(delta_y)}"
        result = await self._command(
            "Runtime.evaluate",
            {
                "expression": f"({_JS_SMOOTH_SCROLL})({args})",
                "returnByValue": True,
                "awaitPromise": False,
            },
        )
        remote = result.get("result")
        if (
            result.get("exceptionDetails") is None
            and isinstance(remote, dict)
            and remote.get("value") is True
        ):
            self._smooth_scroll_active = True
            return
        await self._send(
            "Input.dispatchMouseEvent",
            {
                "x": x,
                "y": y,
                "type": "mouseWheel",
                "deltaX": delta_x,
                "deltaY": delta_y,
            },
            expect_reply=False,
        )


def _capture_size(viewport: LiveBrowserViewport) -> tuple[int, int]:
    """Fit the device raster into the live frame's pixel budget.

    Args:
        viewport: Requested CSS dimensions and device pixel ratio.

    Returns:
        Maximum PNG width and height in physical pixels, scaled proportionally.
    """
    width = viewport.width * viewport.device_scale_factor
    height = viewport.height * viewport.device_scale_factor
    if width * height <= _MAX_CAPTURE_PIXELS:
        return width, height
    scale = math.sqrt(_MAX_CAPTURE_PIXELS / (width * height))
    return int(width * scale), int(height * scale)


def _png_matches_viewport(data: str, viewport: LiveBrowserViewport) -> bool:
    # CDP can describe new CSS bounds while encoding the previous surface.
    # Read just the signature and IHDR dimensions (24 bytes / 32 base64 chars).
    try:
        header = base64.b64decode(data[:32], validate=True)
    except ValueError:
        return False
    if len(header) != 24 or not header.startswith(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR"):
        return False
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    max_width, max_height = _capture_size(viewport)
    scale = min(max_width / viewport.width, max_height / viewport.height)
    # Chromium may fit twice: round the short edge to a whole pixel, then
    # fit the long edge to preserve aspect, yielding slightly fewer pixels.
    expected_short = min(viewport.width, viewport.height) * scale
    aspect_error = abs(width * viewport.height - height * viewport.width)
    return (
        0 < width <= max_width
        and 0 < height <= max_height
        and abs(min(width, height) - expected_short) <= 0.501
        and aspect_error <= max(viewport.width, viewport.height)
    )


async def _desktop_identity_args(executable: str, environment: Mapping[str, str]) -> list[str]:
    """Build flags that make the headless tab identify as the person's desktop Chrome.

    Headless Chrome names itself ``HeadlessChrome`` and sets ``navigator.webdriver``.
    Sites answer either signal with repeated bot challenges, although a person
    drives this tab. The flags apply to the whole browser, so cross-origin
    challenge frames see the same identity as the page.

    Args:
        executable: Chrome or Chromium binary that will be launched.
        environment: Environment for the version query.

    Returns:
        Launch flags, without a user-agent override when the build's version
        cannot be read.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    reported = await anyio.run_process(
        [executable, "--version"],
        check=False,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    if version := _CHROME_VERSION.search(reported.stdout.decode(errors="replace")):
        args.append(
            f"--user-agent=Mozilla/5.0 ({_USER_AGENT_PLATFORM}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version[1]}.0.0.0 Safari/537.36"
        )
    return args


async def _close_saved_browser(
    socket: ClientConnection, request_id: int, process: anyio.abc.Process
) -> None:
    # The reader may already be cancelled. Ask Chrome directly to flush cookies
    # and site storage before the socket closes, then fall back to termination.
    with anyio.move_on_after(3, shield=True), suppress(WebSocketException, OSError):
        await socket.send(json.dumps({"id": request_id, "method": "Browser.close"}))
        await process.wait()


async def _debugger_address(profile: Path, process: anyio.abc.Process) -> str:
    with anyio.fail_after(_STARTUP_TIMEOUT):
        while process.returncode is None:
            try:
                fd = os.open(
                    profile / "DevToolsActivePort",
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                )
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise BrowserControlError("Invalid Chromium debugging endpoint file")
                    port, path = stream.read(512).decode("ascii").splitlines()[:2]
                if port.isascii() and port.isdecimal() and path.startswith("/devtools/browser/"):
                    return f"ws://127.0.0.1:{port}{path}"
            except (FileNotFoundError, ValueError):
                pass
            await anyio.sleep(0.025)
    raise BrowserControlError("Chromium could not start its local debugging connection")
