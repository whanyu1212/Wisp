"""Agent tools for driving Wisp's built-in live browser."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from wisp.coding.browser_control import BrowserButtonKind, BrowserControlError
from wisp.coding.browser_live import (
    LiveBrowser,
    LiveBrowserPageSnapshot,
    validate_browser_url,
)
from wisp.events import BrowserViewportUpdated
from wisp.tool_types import ToolSafety
from wisp.tools.base import Tool, ToolArguments, ToolInputSchema
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolArgumentError, ToolError, ToolResult

LIVE_BROWSER_TOOL_NAMES = frozenset(
    {
        "browser_navigate",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    }
)

ViewportPublisher = Callable[[BrowserViewportUpdated], Awaitable[None]]


def _require_trusted(is_trusted: Callable[[], bool]) -> None:
    if not is_trusted():
        raise ToolError(
            "Wisp's live browser requires a trusted project.",
            failure_code="policy_denied",
        )


def _format_snapshot(snapshot: LiveBrowserPageSnapshot) -> str:
    mode_label = "window" if snapshot.window_mode else "terminal"
    lines = [
        f"URL: {snapshot.url or '(none)'}",
        f"Title: {snapshot.title or '(untitled)'}",
        f"Mode: {mode_label}",
    ]
    if snapshot.elements:
        lines.append("")
        lines.append("Interactive elements:")
        for el in snapshot.elements:
            parts = [f"[{el.ref}] {el.role}"]
            if el.name:
                parts.append(repr(el.name))
            if el.value:
                parts.append(f"value={el.value!r}")
            parts.append(f"({el.x}, {el.y})")
            lines.append("  " + " ".join(parts))
    if snapshot.text:
        lines.append("")
        lines.append("Page text:")
        lines.append(snapshot.text)
        if snapshot.truncated:
            lines.append("[... page text truncated ...]")
    return "\n".join(lines)


async def _publish_viewport(
    publish: ViewportPublisher | None,
    event: BrowserViewportUpdated | None,
    *,
    tool_name: str,
) -> None:
    if publish is None or event is None:
        return
    await publish(event.model_copy(update={"source_tool": tool_name}))


class BrowserNavigateTool:
    """Open, navigate, or switch terminal/window mode in Wisp's built-in live browser."""

    name: str = "browser_navigate"
    safety: ToolSafety = "mutating"
    description: str = (
        "Open or navigate Wisp's built-in live browser (not the user's own Chrome). "
        "Opens the Browser panel in Wisp automatically. Set window=true to switch Wisp's "
        "live browser to a visible Chrome window (sharing Wisp's profile cookies) when a "
        "site blocks headless Chrome or needs an OS prompt; set window=false to dock it "
        "back in the terminal."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "HTTP or HTTPS URL to open. Optional when switching window mode on an "
                    "already-open page."
                ),
            },
            "window": {
                "type": "boolean",
                "description": (
                    "When true, open Wisp's live browser in a visible desktop Chrome window; "
                    "when false (default), run inside the Wisp terminal Browser pane."
                ),
            },
        },
        "additionalProperties": False,
    }

    def __init__(
        self,
        browser: LiveBrowser,
        *,
        is_trusted: Callable[[], bool],
        publish_viewport: ViewportPublisher | None = None,
    ) -> None:
        self._browser = browser
        self._is_trusted = is_trusted
        self._publish_viewport = publish_viewport

    def check_arguments(self, arguments: ToolArguments) -> str | None:
        url = arguments.get("url")
        window = arguments.get("window")
        if url is not None:
            if not isinstance(url, str) or not url.strip():
                return "url must be a non-empty HTTP or HTTPS URL"
            try:
                validate_browser_url(url.strip())
            except BrowserControlError as exc:
                return str(exc)
        if window is not None and type(window) is not bool:
            return "window must be a boolean"
        if url is None and window is None:
            return "Provide url, window, or both"
        return None

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        del context
        _require_trusted(self._is_trusted)
        if problem := self.check_arguments(arguments):
            raise ToolArgumentError(problem)
        raw_url = arguments.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else None
        raw_window = arguments.get("window")
        window = raw_window if isinstance(raw_window, bool) else self._browser.window_mode
        try:
            event = await self._browser.switch_mode(window=window, url=url)
        except BrowserControlError as exc:
            raise ToolError(str(exc)) from exc
        await _publish_viewport(self._publish_viewport, event, tool_name=self.name)
        mode_label = "window" if self._browser.window_mode else "terminal"
        current_url = self._browser.current_url or url or ""
        title = self._browser.current_title or ""
        summary = f"Live browser ({mode_label}) navigated to {current_url}"
        if title:
            summary += f" — {title}"
        return ToolResult(
            text=summary,
            data={"url": current_url, "title": title, "window_mode": self._browser.window_mode},
        )


class BrowserReadTool:
    """Read the current page's URL, title, visible text, and interactive elements."""

    name: str = "browser_read"
    safety: ToolSafety = "read"
    description: str = (
        "Read the current page in Wisp's built-in live browser (not the user's own Chrome). "
        "Returns the URL, title, mode, visible text, and interactive elements with refs "
        "(e1, e2, ...) that can be passed to browser_click or browser_type. Prefer this over "
        "browser_screenshot to save tokens."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "maximum": 32000,
                "description": "Maximum characters of page text to return (default 12000).",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, browser: LiveBrowser, *, is_trusted: Callable[[], bool]) -> None:
        self._browser = browser
        self._is_trusted = is_trusted

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        del context
        _require_trusted(self._is_trusted)
        raw_max = arguments.get("max_chars", 12_000)
        if type(raw_max) is not int or not (200 <= raw_max <= 32_000):
            raise ToolArgumentError("max_chars must be an integer between 200 and 32000")
        try:
            snapshot = await self._browser.read_page(max_chars=raw_max)
        except BrowserControlError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(
            text=_format_snapshot(snapshot),
            truncated=snapshot.truncated,
            data={
                "url": snapshot.url or "",
                "title": snapshot.title or "",
                "window_mode": snapshot.window_mode,
                "element_count": len(snapshot.elements),
            },
        )


class BrowserClickTool:
    """Click an element or coordinate in Wisp's built-in live browser."""

    name: str = "browser_click"
    safety: ToolSafety = "mutating"
    description: str = (
        "Click an element in Wisp's built-in live browser (not the user's own Chrome). "
        "Target an element by ref from browser_read (preferred), CSS selector, or x/y "
        "viewport coordinates."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Element ref from browser_read (e.g. 'e1').",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for the element to click.",
            },
            "x": {"type": "integer", "minimum": 0, "description": "Viewport X coordinate."},
            "y": {"type": "integer", "minimum": 0, "description": "Viewport Y coordinate."},
            "button": {
                "type": "string",
                "enum": ["left", "right", "middle"],
                "description": "Mouse button (default 'left').",
            },
            "double_click": {
                "type": "boolean",
                "description": "Whether to double-click (default false).",
            },
        },
        "additionalProperties": False,
    }

    def __init__(
        self,
        browser: LiveBrowser,
        *,
        is_trusted: Callable[[], bool],
        publish_viewport: ViewportPublisher | None = None,
    ) -> None:
        self._browser = browser
        self._is_trusted = is_trusted
        self._publish_viewport = publish_viewport

    def check_arguments(self, arguments: ToolArguments) -> str | None:
        ref = arguments.get("ref")
        selector = arguments.get("selector")
        x = arguments.get("x")
        y = arguments.get("y")
        has_coords = x is not None or y is not None
        if ref is None and selector is None and not has_coords:
            return "Provide ref, selector, or x and y coordinates"
        if has_coords and (type(x) is not int or type(y) is not int or x < 0 or y < 0):
            return "x and y must both be non-negative integers"
        if ref is not None and (not isinstance(ref, str) or not ref.strip()):
            return "ref must be a non-empty string"
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            return "selector must be a non-empty string"
        button = arguments.get("button")
        if button is not None and button not in {"left", "right", "middle"}:
            return "button must be 'left', 'right', or 'middle'"
        double_click = arguments.get("double_click")
        if double_click is not None and type(double_click) is not bool:
            return "double_click must be a boolean"
        return None

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        del context
        _require_trusted(self._is_trusted)
        if problem := self.check_arguments(arguments):
            raise ToolArgumentError(problem)
        ref = arguments.get("ref")
        selector = arguments.get("selector")
        x = arguments.get("x")
        y = arguments.get("y")
        button = cast(BrowserButtonKind, arguments.get("button", "left"))
        double_click = bool(arguments.get("double_click", False))
        try:
            event = await self._browser.click_element(
                ref=ref.strip() if isinstance(ref, str) else None,
                selector=selector.strip() if isinstance(selector, str) else None,
                x=x if isinstance(x, int) else None,
                y=y if isinstance(y, int) else None,
                button=button,
                double_click=double_click,
            )
        except BrowserControlError as exc:
            raise ToolError(str(exc)) from exc
        await _publish_viewport(self._publish_viewport, event, tool_name=self.name)
        current_url = self._browser.current_url or ""
        title = self._browser.current_title or ""
        return ToolResult(
            text=f"Clicked in live browser. Current page: {current_url}"
            + (f" — {title}" if title else ""),
            data={"url": current_url, "title": title},
        )


class BrowserTypeTool:
    """Type text into an element in Wisp's built-in live browser."""

    name: str = "browser_type"
    safety: ToolSafety = "mutating"
    description: str = (
        "Type text into an input or focused element in Wisp's built-in live browser "
        "(not the user's own Chrome). Optionally target an element by ref from "
        "browser_read or CSS selector, clear existing text first, and press Enter."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type."},
            "ref": {
                "type": "string",
                "description": "Optional element ref from browser_read to focus first.",
            },
            "selector": {
                "type": "string",
                "description": "Optional CSS selector to focus first.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear existing input text before typing (default false).",
            },
            "press_enter": {
                "type": "boolean",
                "description": "Press Enter after typing (default false).",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        browser: LiveBrowser,
        *,
        is_trusted: Callable[[], bool],
        publish_viewport: ViewportPublisher | None = None,
    ) -> None:
        self._browser = browser
        self._is_trusted = is_trusted
        self._publish_viewport = publish_viewport

    def check_arguments(self, arguments: ToolArguments) -> str | None:
        text = arguments.get("text")
        if not isinstance(text, str):
            return "text must be a string"
        if any((ord(ch) < 32 and ch not in "\t\n\r") or 0x7F <= ord(ch) <= 0x9F for ch in text):
            return "text contains unsupported control characters"
        ref = arguments.get("ref")
        if ref is not None and (not isinstance(ref, str) or not ref.strip()):
            return "ref must be a non-empty string"
        selector = arguments.get("selector")
        if selector is not None and (not isinstance(selector, str) or not selector.strip()):
            return "selector must be a non-empty string"
        for flag in ("clear", "press_enter"):
            val = arguments.get(flag)
            if val is not None and type(val) is not bool:
                return f"{flag} must be a boolean"
        return None

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        del context
        _require_trusted(self._is_trusted)
        if problem := self.check_arguments(arguments):
            raise ToolArgumentError(problem)
        text = cast(str, arguments["text"])
        ref = arguments.get("ref")
        selector = arguments.get("selector")
        clear = bool(arguments.get("clear", False))
        press_enter = bool(arguments.get("press_enter", False))
        try:
            event = await self._browser.type_text(
                text=text,
                ref=ref.strip() if isinstance(ref, str) else None,
                selector=selector.strip() if isinstance(selector, str) else None,
                clear=clear,
                press_enter=press_enter,
            )
        except BrowserControlError as exc:
            raise ToolError(str(exc)) from exc
        await _publish_viewport(self._publish_viewport, event, tool_name=self.name)
        current_url = self._browser.current_url or ""
        title = self._browser.current_title or ""
        return ToolResult(
            text=f"Typed {len(text)} character(s) in live browser."
            + (f" Current page: {current_url}" if current_url else ""),
            data={"url": current_url, "title": title, "chars": len(text)},
        )


class BrowserScreenshotTool:
    """Capture an opt-in PNG screenshot of Wisp's built-in live browser."""

    name: str = "browser_screenshot"
    safety: ToolSafety = "read"
    description: str = (
        "Capture a PNG screenshot of the current page in Wisp's built-in live browser "
        "(not the user's own Chrome). Use selectively when visual inspection is needed; "
        "prefer browser_read for routine page inspection."
    )
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, browser: LiveBrowser, *, is_trusted: Callable[[], bool]) -> None:
        self._browser = browser
        self._is_trusted = is_trusted

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        del arguments, context
        _require_trusted(self._is_trusted)
        try:
            png_bytes, url, title = await self._browser.capture_screenshot()
        except BrowserControlError as exc:
            raise ToolError(str(exc)) from exc
        label = f"Captured live browser screenshot of {url or '(unknown URL)'}"
        if title:
            label += f" — {title}"
        return ToolResult(
            text=label,
            images=(png_bytes,),
            data={"url": url or "", "title": title or ""},
        )


def build_live_browser_tools(
    browser: LiveBrowser,
    *,
    is_trusted: Callable[[], bool],
    publish_viewport: ViewportPublisher | None = None,
) -> tuple[Tool, ...]:
    """Return the five agent tools bound to ``browser`` in stable registration order."""
    return (
        BrowserNavigateTool(
            browser,
            is_trusted=is_trusted,
            publish_viewport=publish_viewport,
        ),
        BrowserReadTool(browser, is_trusted=is_trusted),
        BrowserClickTool(
            browser,
            is_trusted=is_trusted,
            publish_viewport=publish_viewport,
        ),
        BrowserTypeTool(
            browser,
            is_trusted=is_trusted,
            publish_viewport=publish_viewport,
        ),
        BrowserScreenshotTool(browser, is_trusted=is_trusted),
    )


__all__ = [
    "BrowserClickTool",
    "BrowserNavigateTool",
    "BrowserReadTool",
    "BrowserScreenshotTool",
    "BrowserTypeTool",
    "LIVE_BROWSER_TOOL_NAMES",
    "build_live_browser_tools",
]
