"""Exercise live browser operations against Chromium's CDP command boundary."""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import AsyncIterator, Mapping
from typing import Any

import anyio
import pytest

from wisp.coding.browser_control import BrowserControlError, BrowserInteraction
from wisp.coding.browser_live import LiveBrowser
from wisp.events import LiveBrowserViewport


def _png_header(width: int, height: int) -> str:
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)
    ).decode("ascii")


@pytest.mark.parametrize(
    ("height", "hd_size", "height_cap"), [(603, (1600, 837), 1200), (45, (1587, 62), 90)]
)
def test_resize_discards_stale_frames(
    height: int, hd_size: tuple[int, int], height_cap: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            monkeypatch.setattr(browser, "_socket", object())
            monkeypatch.setattr(browser, "_scope", tasks.cancel_scope)
            captures: list[Mapping[str, object]] = []
            metrics: list[Mapping[str, object]] = []
            mouse: list[Mapping[str, object]] = []

            async def command(method: str, params: Mapping[str, object]) -> dict[str, object]:
                if method == "Emulation.setDeviceMetricsOverride":
                    metrics.append(params)
                elif method == "Page.captureScreenshot":
                    assert params["clip"] == {"x": 0, "y": 0, "width": 1, "height": 1, "scale": 1}
                elif method == "Page.startScreencast":
                    captures.append(params)
                    number = len(captures)
                    metadata = {"deviceWidth": 1152, "deviceHeight": height}
                    raster = _png_header(1152, height) if number == 1 else _png_header(*hd_size)
                    if number > 1:
                        # A scale-only change has identical CSS dimensions.
                        browser._accept_frame(
                            {"sessionId": number - 1, "metadata": metadata, "data": raster}
                        )
                        assert browser.take_frame() is None
                    browser._accept_frame(
                        {
                            "sessionId": number,
                            "metadata": {"deviceWidth": 900, "deviceHeight": 600},
                            "data": raster,
                        }
                    )
                    assert browser.take_frame() is None
                    # Chromium can report the new CSS bounds while its compositor
                    # still encodes pixels from the previous aspect ratio.
                    browser._accept_frame(
                        {"sessionId": number, "metadata": metadata, "data": _png_header(900, 600)}
                    )
                    assert browser.take_frame() is None
                    browser._accept_frame(
                        {"sessionId": number, "metadata": metadata, "data": raster}
                    )
                elif method == "Input.dispatchMouseEvent":
                    mouse.append(params)
                else:
                    assert method == "Page.stopScreencast"
                    assert browser.take_frame() is None
                return {}

            monkeypatch.setattr(browser, "_command", command)
            for scale in (1, 2):
                viewport = LiveBrowserViewport(width=1152, height=height, device_scale_factor=scale)
                event = await browser.resize(viewport)
                assert (event.viewport_width, event.viewport_height) == (1152, height)
                assert event.live_png == (
                    _png_header(1152, height) if scale == 1 else _png_header(*hd_size)
                )
                assert event.live_device_scale == scale
                assert browser.take_frame() is None
            assert captures == [
                {"format": "png", "maxWidth": 1152, "maxHeight": height},
                {"format": "png", "maxWidth": 1600, "maxHeight": height_cap},
            ]
            assert metrics == [
                {"width": 1152, "height": height, "deviceScaleFactor": dpr, "mobile": False}
                for dpr in (1, 2)
            ]
            await browser.interact(
                BrowserInteraction(action="scroll", x=1151, y=height - 1, delta_y=1)
            )
            assert mouse == [
                {"x": 1151, "y": height - 1, "type": "mouseWheel", "deltaX": 0, "deltaY": 1}
            ]
            with pytest.raises(BrowserControlError, match="inside"):
                await browser.interact(BrowserInteraction(action="click", x=1152, y=height))

    anyio.run(scenario)


def test_navigate_validates_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            dispatched: list[tuple[str, Mapping[str, object]]] = []

            async def command(method: str, params: Mapping[str, object]) -> dict[str, object]:
                dispatched.append((method, params))
                return {}

            monkeypatch.setattr(browser, "_command", command)
            for url in (
                "file:///etc/hosts",
                "https://user:password@example.org",
                "javascript:void(0)",
            ):
                with pytest.raises(BrowserControlError, match="HTTP or HTTPS"):
                    await browser.navigate(url)
            await browser.navigate("https://example.org/page")
            assert dispatched == [("Page.navigate", {"url": "https://example.org/page"})]

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("current", "offset", "expected"), [(0, -1, None), (1, 1, None), (1, -1, 10), (0, 1, 20)]
)
def test_history_obeys_bounds(
    current: int, offset: int, expected: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            visited: list[Mapping[str, object]] = []

            async def command(method: str, params: Mapping[str, object]) -> dict[str, Any]:
                if method == "Page.getNavigationHistory":
                    return {"currentIndex": current, "entries": [{"id": 10}, {"id": 20}]}
                assert method == "Page.navigateToHistoryEntry"
                visited.append(params)
                browser._navigation.set()
                return {}

            monkeypatch.setattr(browser, "_command", command)
            await browser.move_in_history(offset)
            assert visited == ([] if expected is None else [{"entryId": expected}])

    anyio.run(scenario)


@pytest.mark.parametrize("cancel", [False, True])
def test_interrupted_resize_closes_browser(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            monkeypatch.setattr(browser, "_socket", object())
            monkeypatch.setattr(browser, "_scope", anyio.CancelScope())
            with anyio.CancelScope() as caller:

                async def command(method: str, params: Mapping[str, object]) -> dict[str, object]:
                    if method == "Page.stopScreencast":
                        return {}
                    assert method == "Emulation.setDeviceMetricsOverride"
                    if cancel:
                        caller.cancel()
                        await anyio.lowlevel.checkpoint()
                    raise BrowserControlError("metrics rejected")

                monkeypatch.setattr(browser, "_command", command)
                with pytest.raises(BrowserControlError, match="metrics rejected"):
                    await browser.resize(
                        LiveBrowserViewport(width=100, height=100, device_scale_factor=1)
                    )
            assert caller.cancelled_caught == cancel
            with pytest.raises(BrowserControlError, match="closed"):
                browser.take_frame()

    anyio.run(scenario)


def test_navigation_retires_old_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            monkeypatch.setattr(browser, "_socket", object())
            monkeypatch.setattr(browser, "_scope", tasks.cancel_scope)
            viewport = LiveBrowserViewport(width=100, height=60, device_scale_factor=1)
            monkeypatch.setattr(browser, "_viewport", viewport)
            browser._loader = browser._capture_loader = "first"
            browser._main_frame = "main"
            browser._screencast_id = 1
            png = _png_header(100, 60)

            def frame(capture_id: int, label: str) -> dict[str, object]:
                return {
                    "sessionId": capture_id,
                    "metadata": {"deviceWidth": 100, "deviceHeight": 60},
                    "data": png + label,
                }

            async def event(method: str, params: dict[str, object]) -> AsyncIterator[str]:
                yield json.dumps({"method": method, "params": params})

            browser._accept_frame(frame(1, "old"))
            await browser._read(
                event(
                    "Page.navigatedWithinDocument",
                    {"frameId": "main", "url": "https://example.org/#section"},
                )
            )  # type: ignore[arg-type]
            same = browser.take_frame()
            assert same is not None and (same.url, same.live_png) == (
                "https://example.org/#section",
                png + "old",
            )

            await browser._read(
                event(
                    "Page.frameNavigated",
                    {
                        "frame": {
                            "id": "main",
                            "loaderId": "next",
                            "url": "https://example.org/next",
                        }
                    },
                )
            )  # type: ignore[arg-type]
            browser._accept_frame(frame(1, "late"))
            assert browser.take_frame() is None

            async def command(method: str, params: Mapping[str, object]) -> dict[str, object]:
                if method == "Page.startScreencast":
                    browser._accept_frame(frame(2, "new"))
                return {}

            monkeypatch.setattr(browser, "_command", command)
            updated = await browser.poll_frame()
            assert updated is not None and (updated.url, updated.live_png) == (
                "https://example.org/next",
                png + "new",
            )

            assert await browser.poll_frame() is None

    anyio.run(scenario)
