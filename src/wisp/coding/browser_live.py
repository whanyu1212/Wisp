"""Run an isolated Chromium tab and retain its latest CDP screencast frame."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import anyio
from anyio.abc import TaskGroup
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from wisp.coding.browser_control import BrowserControlError, BrowserInteraction
from wisp.events import BrowserViewportUpdated
from wisp.events.rpc import MAX_BROWSER_URL_CHARS, MAX_LIVE_BROWSER_FRAME_CHARS

_WIDTH = 900
_HEIGHT = 600
_STARTUP_TIMEOUT = 20
_TIMEOUT = 10
_CHROME_MAC = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
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


class LiveBrowser:
    """Own a single user-controlled tab for the lifetime of the RPC host."""

    def __init__(self, tasks: TaskGroup) -> None:
        self._tasks = tasks
        self._scope: anyio.CancelScope | None = None
        self._stopped: anyio.Event | None = None
        self._socket: ClientConnection | None = None
        self._target: str | None = None
        self._pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._next_id = 0
        self._first_frame = anyio.Event()
        self._navigation = anyio.Event()
        self._loader: str | None = None
        self._main_frame: str | None = None
        self._frame: str | None = None
        self._delivered: tuple[str, str | None] | None = None
        self._url: str | None = None

    async def open(self, url: str) -> BrowserViewportUpdated:
        """Replace the existing tab with one in a fresh Chromium profile.

        Args:
            url: HTTP or HTTPS address for the new tab.

        Returns:
            The first frame after navigation commits.

        Raises:
            BrowserControlError: The address, Chromium startup, or navigation failed.
            TimeoutError: The first frame did not arrive within the startup deadline.
        """
        url = validate_browser_url(url)
        await self.close()
        executable = _chrome_binary()
        self._first_frame = anyio.Event()
        try:
            with anyio.fail_after(_STARTUP_TIMEOUT):
                await self._tasks.start(self._run, executable)
                await self._command(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": _WIDTH, "height": _HEIGHT, "deviceScaleFactor": 1, "mobile": False},
                )
                await self._command("Page.enable", {})
                await self._command("Inspector.enable", {})
                await self._command("Page.bringToFront", {})
                result = await self._command("Page.navigate", {"url": url})
                if result.get("errorText") or result.get("isDownload"):
                    raise BrowserControlError("Chromium could not navigate to the requested page")
                loader = result.get("loaderId")
                if isinstance(loader, str):
                    while self._loader != loader and self._socket is not None:
                        await self._navigation.wait()
                        self._navigation = anyio.Event()
                await self._command("Page.startScreencast", {"format": "png"})
                await self._first_frame.wait()
                frame = self.take_frame()
                if frame is None:
                    raise BrowserControlError("Chromium did not return a usable first frame")
                return frame
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Stop capture and reap the browser before returning, even on cancellation."""
        self.request_close()
        if self._stopped is not None:
            with anyio.CancelScope(shield=True):
                await self._stopped.wait()

    def request_close(self) -> None:
        """Stop accepting input and arrange for cleanup in the owning task."""
        if self._scope is not None:
            self._scope.cancel()

    async def _run(self, executable: str, *, task_status: anyio.abc.TaskStatus[None]) -> None:
        self._stopped = stopped = anyio.Event()
        started = False
        try:
            with (
                anyio.CancelScope() as scope,
                TemporaryDirectory(prefix="wisp-browser-") as profile,
            ):
                self._scope = scope
                # The disposable macOS profile must not prompt for a login Keychain.
                keychain_args = ["--use-mock-keychain"] if sys.platform == "darwin" else []
                process = await anyio.open_process(
                    [
                        executable,
                        "--headless",
                        "--no-first-run",
                        "--no-default-browser-check",
                        *keychain_args,
                        "--remote-debugging-port=0",
                        "--remote-debugging-address=127.0.0.1",
                        f"--user-data-dir={profile}",
                        "about:blank",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={
                        key: os.environ[key]
                        for key in ("PATH", "HOME", "TMPDIR", "LANG")
                        if key in os.environ
                    },
                    cwd=profile,
                )
                try:
                    async with connect(
                        await _debugger_address(Path(profile), process),
                        proxy=None,
                        max_size=MAX_LIVE_BROWSER_FRAME_CHARS + 8192,
                        close_timeout=1,
                    ) as socket:
                        self._socket = socket
                        async with anyio.create_task_group() as reader:
                            reader.start_soon(self._read, socket)
                            await self._command("Browser.setDownloadBehavior", {"behavior": "deny"})
                            created = await self._command(
                                "Target.createTarget", {"url": "about:blank"}
                            )
                            attached = await self._command(
                                "Target.attachToTarget",
                                {"targetId": created.get("targetId"), "flatten": True},
                            )
                            session_id = attached.get("sessionId")
                            if not isinstance(session_id, str):
                                raise BrowserControlError(
                                    "Chromium did not attach to its browser tab"
                                )
                            self._target = session_id
                            task_status.started()
                            started = True
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
        except Exception:  # noqa: BLE001 - keep the RPC host alive after Chromium exits
            if not started:
                raise BrowserControlError("Could not start the isolated Chromium browser") from None
        finally:
            self._scope = None
            self._socket = None
            self._target = None
            self._frame = self._delivered = self._url = self._loader = self._main_frame = None
            self._navigation.set()
            self._first_frame.set()
            stopped.set()

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
                                self._main_frame = frame["id"]
                                self._loader = frame["loaderId"]
                                self._url = frame["url"]
                                self._navigation.set()
                        case "Page.navigatedWithinDocument":
                            if params["frameId"] == self._main_frame:
                                self._url = params["url"]
                        case "Page.screencastFrame":
                            self._accept_frame(params.get("data"))
                            await self._send(
                                "Page.screencastFrameAck", {"sessionId": params["sessionId"]}
                            )
        except WebSocketException:
            pass
        finally:
            for reply in self._pending.values():
                if not reply.done():
                    reply.set_result({"error": True})

    def _accept_frame(self, data: object) -> None:
        if isinstance(data, str) and 0 < len(data) <= MAX_LIVE_BROWSER_FRAME_CHARS:
            self._frame = data
            self._first_frame.set()

    async def _send(
        self, method: str, params: Mapping[str, object], *, expect_reply: bool = False
    ) -> asyncio.Future[dict[str, object]] | None:
        socket = self._socket
        if socket is None or self._scope is None:
            raise BrowserControlError("The live browser is closed; reopen it with /browser live")
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
            raise BrowserControlError("The live browser is closed; reopen it with /browser live")
        if self._frame is None or (self._frame, self._url) == self._delivered:
            return None
        self._delivered = (self._frame, self._url)
        return BrowserViewportUpdated(
            live_png=self._frame,
            url=self._url[:MAX_BROWSER_URL_CHARS] if self._url else None,
            title="Live Chromium",
            viewport_width=_WIDTH,
            viewport_height=_HEIGHT,
        )

    async def interact(self, request: BrowserInteraction) -> None:
        """Dispatch user input through Chromium's normal page input handling.

        Args:
            request: Validated click, scroll, text, or keyboard input.

        Raises:
            BrowserControlError: The request failed or the browser has closed.
        """
        if self._scope is None or self._scope.cancel_called:
            raise BrowserControlError("The live browser is closed; reopen it with /browser live")
        match request.action:
            case "click" | "scroll":
                await self._mouse(request)
            case "type":
                await self._command("Input.insertText", {"text": request.text})
            case "press_key":
                key = request.key or ""
                code = _KEY_CODES.get(
                    key, ord(key.upper()) if len(key) == 1 and key.isascii() else 0
                )
                params = {
                    "key": key,
                    "windowsVirtualKeyCode": code,
                    "modifiers": sum(
                        _MODIFIER_MASK[modifier] for modifier in request.modifiers or ()
                    ),
                }
                await self._command("Input.dispatchKeyEvent", {"type": "rawKeyDown", **params})
                await self._command("Input.dispatchKeyEvent", {"type": "keyUp", **params})
            case "reload":
                await self._command("Page.reload", {})
            case _:
                raise BrowserControlError("This action is unavailable in the live browser preview")

    async def _mouse(self, request: BrowserInteraction) -> None:
        x, y = request.x, request.y
        if x is None or y is None or not (0 <= x < _WIDTH and 0 <= y < _HEIGHT):
            raise BrowserControlError("Click or scroll inside the live browser frame")
        params = {"x": x, "y": y}
        if request.action == "scroll":
            await self._command(
                "Input.dispatchMouseEvent",
                {
                    **params,
                    "type": "mouseWheel",
                    "deltaX": request.delta_x or 0,
                    "deltaY": request.delta_y or 0,
                },
            )
            return
        button = request.button or "left"
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


async def _debugger_address(profile: Path, process: anyio.abc.Process) -> str:
    with anyio.fail_after(_STARTUP_TIMEOUT):
        while process.returncode is None:
            try:
                port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
                if port.isascii() and port.isdecimal() and path.startswith("/devtools/browser/"):
                    return f"ws://127.0.0.1:{port}{path}"
            except (FileNotFoundError, ValueError):
                pass
            await anyio.sleep(0.025)
    raise BrowserControlError("Chromium could not start its local debugging connection")
