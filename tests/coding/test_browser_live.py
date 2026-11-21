"""Exercise live browser operations against Chromium's CDP command boundary."""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest

from wisp.coding.browser_control import BrowserControlError, BrowserInteraction
from wisp.coding.browser_live import LiveBrowser, _desktop_identity_args, validate_browser_url
from wisp.coding.browser_profile import clear_browser_profile
from wisp.events import LiveBrowserViewport


def _png_header(width: int, height: int) -> str:
    return base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)
    ).decode("ascii")


@pytest.mark.parametrize(
    ("width", "height", "hd_size", "capture_limit"),
    [
        (1152, 360, (2304, 720), (2304, 720)),
        (360, 1152, (720, 2304), (720, 2304)),
        (1152, 603, (1914, 1002), (1915, 1002)),
    ],
)
def test_resize_discards_stale_frames(
    width: int,
    height: int,
    hd_size: tuple[int, int],
    capture_limit: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
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
                    metadata = {"deviceWidth": width, "deviceHeight": height}
                    raster = _png_header(width, height) if number == 1 else _png_header(*hd_size)
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
                viewport = LiveBrowserViewport(
                    width=width, height=height, device_scale_factor=scale
                )
                event = await browser.resize(viewport)
                assert (event.viewport_width, event.viewport_height) == (width, height)
                assert event.live_png == (
                    _png_header(width, height) if scale == 1 else _png_header(*hd_size)
                )
                assert event.live_device_scale == scale
                assert browser.take_frame() is None
            assert captures == [
                {"format": "png", "maxWidth": width, "maxHeight": height},
                {"format": "png", "maxWidth": capture_limit[0], "maxHeight": capture_limit[1]},
            ]
            assert metrics == [
                {"width": width, "height": height, "deviceScaleFactor": dpr, "mobile": False}
                for dpr in (1, 2)
            ]
            await browser.interact(
                BrowserInteraction(action="scroll", x=width - 1, y=height - 1, delta_y=1)
            )
            assert mouse == [
                {"x": width - 1, "y": height - 1, "type": "mouseWheel", "deltaX": 0, "deltaY": 1}
            ]
            mouse.clear()
            await browser.interact(BrowserInteraction(action="click", x=3, y=4))
            assert [(event["type"], event["x"], event["y"]) for event in mouse] == [
                ("mouseMoved", 3, 4),
                ("mousePressed", 3, 4),
                ("mouseReleased", 3, 4),
            ]
            with pytest.raises(BrowserControlError, match="inside"):
                await browser.interact(BrowserInteraction(action="click", x=width, y=height))

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


@pytest.mark.parametrize("url", ["http://localhost:65536", "https://host:abc"])
def test_invalid_port_rejected(url: str) -> None:
    with pytest.raises(BrowserControlError, match="HTTP or HTTPS"):
        validate_browser_url(url)


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


@pytest.mark.skipif(os.name != "posix", reason="remembered profiles require POSIX")
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_launch_failure_releases_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    profile = tmp_path / "browser-profile"
    used: list[Path] = []

    desktop = {
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "6",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=test",
    }
    for key, value in desktop.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WISP_TEST_UNRELATED", "exclude")

    async def failed_launch(
        args: list[str], *, cwd: Path, env: dict[str, str], **kwargs: object
    ) -> None:
        fds = kwargs["pass_fds"]
        assert len(fds) == 1
        assert os.path.samestat(os.fstat(fds[0]), (cwd / ".wisp-browser-profile").stat())
        assert f"--user-data-dir={cwd}" in args
        # Saved cookies use the real Keychain, and the tab identifies as desktop Chrome.
        assert "--use-mock-keychain" not in args
        assert "--identity-flag" in args
        assert {key: env[key] for key in desktop if key in env} == (
            desktop if platform == "linux" else {}
        )
        assert "WISP_TEST_UNRELATED" not in env
        assert cwd.is_dir()
        used.append(cwd)
        raise FileNotFoundError("simulated missing executable")

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks, profile_dir=profile)
            monkeypatch.setattr("wisp.coding.browser_live._chrome_binary", lambda: "chrome")

            async def identity(executable: str, environment: object) -> list[str]:
                return ["--identity-flag"]

            monkeypatch.setattr("wisp.coding.browser_live._desktop_identity_args", identity)
            monkeypatch.setattr("wisp.coding.browser_live.sys.platform", platform)
            monkeypatch.setattr("wisp.coding.browser_live.anyio.open_process", failed_launch)
            with pytest.raises(BrowserControlError, match="Could not start"):
                await browser.open("https://example.org")

    anyio.run(scenario)
    assert used == [profile] and profile.exists()
    clear_browser_profile(profile)
    assert not (profile / "Default").exists()


@pytest.mark.parametrize(
    ("reported", "major"), [("Google Chrome 158.0.8127.36 ", "158"), ("unknown build", None)]
)
def test_identity_hides_headless_markers(reported: str, major: str | None, tmp_path: Path) -> None:
    """The override names the launched build's major version, or is left out."""
    chrome = tmp_path / "chrome"
    chrome.write_text(f"#!/bin/sh\necho '{reported}'\n")
    chrome.chmod(0o755)

    args = anyio.run(_desktop_identity_args, str(chrome), {})

    assert args[0] == "--disable-blink-features=AutomationControlled"
    user_agents = [arg for arg in args[1:] if arg.startswith("--user-agent=")]
    assert len(user_agents) == len(args) - 1
    if major is None:
        assert user_agents == []
    else:
        platform = "Macintosh" if sys.platform == "darwin" else "X11; Linux"
        assert f" Chrome/{major}.0.0.0 " in user_agents[0]
        assert platform in user_agents[0]
        assert "Headless" not in user_agents[0]


def _open_capture(browser: LiveBrowser, monkeypatch: pytest.MonkeyPatch) -> anyio.CancelScope:
    """Give the browser the state an open tab has after its first capture started."""
    scope = anyio.CancelScope()
    monkeypatch.setattr(browser, "_socket", object())
    monkeypatch.setattr(browser, "_scope", scope)
    monkeypatch.setattr(
        browser, "_viewport", LiveBrowserViewport(width=100, height=60, device_scale_factor=1)
    )
    browser._loader = browser._capture_loader = "first"
    browser._main_frame = "main"
    browser._screencast_id = 1
    return scope


def _frame(capture_id: int, label: str) -> dict[str, object]:
    return {
        "sessionId": capture_id,
        "metadata": {"deviceWidth": 100, "deviceHeight": 60},
        "data": _png_header(100, 60) + label,
    }


async def _cdp_events(*events: tuple[str, dict[str, object]]) -> AsyncIterator[str]:
    for method, params in events:
        yield json.dumps({"method": method, "params": params})


def _end_run(browser: LiveBrowser) -> None:
    """Do what the browser's run task does as Chromium goes away."""
    browser._scope = None
    browser._socket = None
    browser._signal_change()


def test_reader_gets_newest_frame_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frames are never queued: a waiting reader gets the latest, then waits again."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            _open_capture(browser, monkeypatch)
            browser._accept_frame(_frame(1, "a"))
            first = await browser.next_frame()
            assert first is not None and first.live_png == _png_header(100, 60) + "a"

            received: list[str | None] = []

            async def read() -> None:
                update = await browser.next_frame()
                received.append(update.live_png if update else None)

            tasks.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            assert received == []
            # Chromium outpaces the reader; only the newest frame reaches it.
            browser._accept_frame(_frame(1, "b"))
            browser._accept_frame(_frame(1, "c"))
            await anyio.wait_all_tasks_blocked()
            assert received == [_png_header(100, 60) + "c"]

            # Nothing new has arrived, so a further read waits.
            tasks.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            assert received == [_png_header(100, 60) + "c"]
            tasks.cancel_scope.cancel()

    anyio.run(scenario)


def test_reader_follows_page_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """An address change without new pixels, then a new document, each wake the reader."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            _open_capture(browser, monkeypatch)
            browser._accept_frame(_frame(1, "old"))
            assert await browser.next_frame() is not None
            received: list[tuple[str | None, str | None]] = []

            async def read() -> None:
                update = await browser.next_frame()
                assert update is not None
                received.append((update.url, update.live_png))

            tasks.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            await browser._read(
                _cdp_events(
                    (
                        "Page.navigatedWithinDocument",
                        {"frameId": "main", "url": "https://a.test/#x"},
                    )
                )  # type: ignore[arg-type]
            )
            await anyio.wait_all_tasks_blocked()
            assert received == [("https://a.test/#x", _png_header(100, 60) + "old")]

            async def command(method: str, params: Mapping[str, object]) -> dict[str, object]:
                if method == "Page.startScreencast":
                    browser._accept_frame(_frame(2, "new"))
                return {}

            monkeypatch.setattr(browser, "_command", command)
            tasks.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            frame = {"id": "main", "loaderId": "next", "url": "https://a.test/next"}
            await browser._read(
                _cdp_events(("Page.frameNavigated", {"frame": frame}))  # type: ignore[arg-type]
            )
            await anyio.wait_all_tasks_blocked()
            assert received[1] == ("https://a.test/next", _png_header(100, 60) + "new")

    anyio.run(scenario)


def test_resize_keeps_its_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A waiting reader never takes the first frame a resize returns."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            _open_capture(browser, monkeypatch)
            received: list[object] = []

            async def command(method: str, params: Mapping[str, object]) -> dict[str, object]:
                if method == "Page.startScreencast":
                    browser._accept_frame(_frame(browser._screencast_id, "resized"))
                # Let the woken reader run while the resize is still capturing.
                await anyio.wait_all_tasks_blocked()
                return {}

            monkeypatch.setattr(browser, "_command", command)

            async def read() -> None:
                received.append(await browser.next_frame())

            tasks.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            resized = await browser.resize(
                LiveBrowserViewport(width=100, height=60, device_scale_factor=1)
            )
            assert resized.live_png == _png_header(100, 60) + "resized"
            await anyio.wait_all_tasks_blocked()
            assert received == []
            browser._accept_frame(_frame(browser._screencast_id, "next"))
            await anyio.wait_all_tasks_blocked()
            assert len(received) == 1
            tasks.cancel_scope.cancel()

    anyio.run(scenario)


@pytest.mark.parametrize("requested", [True, False])
def test_waiting_reader_ends_with_browser(monkeypatch: pytest.MonkeyPatch, requested: bool) -> None:
    """A close on request ends a wait quietly; Chromium exiting on its own is an error."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            scope = _open_capture(browser, monkeypatch)
            outcome: list[object] = []

            async def read() -> None:
                try:
                    outcome.append(await browser.next_frame())
                except BrowserControlError as exc:
                    outcome.append(exc)

            tasks.start_soon(read)
            await anyio.wait_all_tasks_blocked()
            if requested:
                await browser.close()
                assert scope.cancel_called
            _end_run(browser)
            await anyio.wait_all_tasks_blocked()
            if requested:
                assert outcome == [None]
            else:
                assert len(outcome) == 1 and isinstance(outcome[0], BrowserControlError)
                assert "closed" in str(outcome[0])
            # A browser that is not open fails a read at once.
            with pytest.raises(BrowserControlError, match="closed"):
                await browser.next_frame()

    anyio.run(scenario)


def test_read_during_replacement_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read begun on the old tab never binds to the tab a replacing open starts."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            old = _open_capture(browser, monkeypatch)
            outcome: list[object] = []

            async def read() -> None:
                outcome.append(await browser.next_frame())

            # A replacing open holds the capture lock while it swaps the tab.
            async with browser._capture_lock:
                tasks.start_soon(read)
                await anyio.wait_all_tasks_blocked()
                old.cancel()
                browser._scope = anyio.CancelScope()
                browser._screencast_id = 2
            await anyio.wait_all_tasks_blocked()
            assert outcome == [None]
            # The new tab's frames go to readers that began on it.
            browser._accept_frame(_frame(2, "new"))
            update = await browser.next_frame()
            assert update is not None and update.live_png == _png_header(100, 60) + "new"

    anyio.run(scenario)


@pytest.mark.parametrize("ending", ["close", "replace"])
def test_lifetime_ends_for_good(monkeypatch: pytest.MonkeyPatch, ending: str) -> None:
    """A close or a replacing tab ends a lifetime; a closed browser has none."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            assert browser.lifetime is None and not browser.is_current(None)
            old = _open_capture(browser, monkeypatch)
            lifetime = browser.lifetime
            assert lifetime is old and browser.is_current(lifetime)
            if ending == "close":
                browser.request_close()
            else:
                browser._scope = anyio.CancelScope()
                assert browser.is_current(browser.lifetime)
            assert not browser.is_current(lifetime)

    anyio.run(scenario)


def test_close_survives_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller already being cancelled, such as a quitting host, still stops Chromium."""

    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            scope = _open_capture(browser, monkeypatch)
            with anyio.CancelScope() as caller:
                caller.cancel()
                await browser.close()
            assert scope.cancel_called
            # Closing has begun but the run has not ended: reads fail at once.
            with pytest.raises(BrowserControlError, match="closed"):
                await browser.next_frame()

    anyio.run(scenario)


def test_click_needs_a_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with anyio.create_task_group() as tasks:
            browser = LiveBrowser(tasks)
            _open_capture(browser, monkeypatch)
            with pytest.raises(BrowserControlError, match="inside"):
                await browser.interact(BrowserInteraction(action="click", x=3, y=4))

    anyio.run(scenario)
