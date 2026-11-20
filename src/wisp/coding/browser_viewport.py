"""Browser MCP detection, metadata extraction, and viewport event helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from wisp.events import (
    MAX_BROWSER_SOURCE_TOOL_CHARS,
    MAX_BROWSER_TITLE_CHARS,
    MAX_BROWSER_URL_CHARS,
    MAX_BROWSER_VIEWPORT_SIDE,
    BrowserViewportUpdated,
    ImagePart,
)
from wisp.mcp.discovery import McpCatalogTool
from wisp.tools.base import Tool

_AUTO_SCREENSHOT_ENV = "WISP_TUI_BROWSER_AUTO_SCREENSHOT"
_AUTO_SCREENSHOT_DISABLED = frozenset({"0", "false", "no", "off"})

_BROWSER_SERVER_TOKENS = (
    "playwright",
    "browser",
    "chrome",
    "chromium",
    "devtools",
    "puppeteer",
    "cdp",
    "firefox",
    "webkit",
    "selenium",
)

# Ranked in preference order when choosing a sibling screenshot tool for auto-capture.
_SCREENSHOT_TOOL_NAMES = (
    "browser_take_screenshot",
    "take_screenshot",
    "screenshot",
    "capture_screenshot",
)

# Normalized base names (with any leading ``browser_`` removed) for browser tools.
_MUTATING_BASE_NAMES = frozenset(
    {
        "back",
        "click",
        "close_page",
        "dblclick",
        "drag",
        "drop",
        "emulate",
        "emulate_media",
        "evaluate",
        "evaluate_script",
        "file_upload",
        "fill",
        "fill_form",
        "forward",
        "goto",
        "handle_dialog",
        "hover",
        "navigate",
        "navigate_back",
        "navigate_page",
        "new_page",
        "open",
        "press",
        "press_key",
        "reload",
        "resize",
        "resize_page",
        "run_code",
        "run_code_unsafe",
        "select_option",
        "select_page",
        "tabs",
        "type",
        "type_text",
        "upload_file",
        "wait_for",
    }
)
_KNOWN_BROWSER_BASE_NAMES = _MUTATING_BASE_NAMES | frozenset(
    {
        "console_messages",
        "find",
        "get_console_message",
        "get_css_styles",
        "get_network_request",
        "lighthouse_audit",
        "list_console_messages",
        "list_network_requests",
        "list_pages",
        "network_request",
        "network_requests",
        "performance_analyze_insight",
        "performance_start_trace",
        "performance_stop_trace",
        "snapshot",
        "take_heapsnapshot",
        "take_screenshot",
        "capture_screenshot",
        "screenshot",
    }
)

_URL_LINE_RE = re.compile(r"(?im)^(?:[-*]\s*)?(?:page\s+)?url\s*:\s*(\S.+?)\s*$")
_TITLE_LINE_RE = re.compile(r"(?im)^(?:[-*]\s*)?(?:page\s+)?title\s*:\s*(.+?)\s*$")
_WHITESPACE_CONTROL_RE = re.compile(r"[\t\n\r\x0b\x0c]+")
_BIDI_AND_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]+")


@dataclass(frozen=True, slots=True)
class BrowserMetadata:
    """Bounded, display-safe browser metadata extracted from a tool invocation."""

    url: str | None = None
    title: str | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None


def _normalized_base_name(remote_name: str) -> str:
    lower = remote_name.strip().lower()
    return lower.removeprefix("browser_")


def auto_screenshot_enabled() -> bool:
    """Return whether post-action auto-screenshot capture is enabled."""

    raw = os.environ.get(_AUTO_SCREENSHOT_ENV, "1").strip().lower()
    return raw not in _AUTO_SCREENSHOT_DISABLED


def is_screenshot_tool_name(remote_name: str) -> bool:
    """Return whether ``remote_name`` is a browser screenshot tool."""

    return remote_name.strip().lower() in _SCREENSHOT_TOOL_NAMES


def is_mutating_browser_tool_name(remote_name: str) -> bool:
    """Return whether ``remote_name`` mutates or navigates browser state."""

    return _normalized_base_name(remote_name) in _MUTATING_BASE_NAMES


def is_browser_mcp_tool(tool: Tool, sibling_tools: Iterable[Tool] = ()) -> bool:
    """Return whether ``tool`` is an MCP tool belonging to a browser automation server.

    Args:
        tool (Tool): Executed tool instance.
        sibling_tools (Iterable[Tool]): Tools exposed in the same run, used to
            identify browser toolsets even when the server alias is custom.

    Returns:
        bool: True when ``tool`` belongs to a browser MCP server.
    """

    if not isinstance(tool, McpCatalogTool):
        return False
    server_lower = tool.server_name.strip().lower()
    if any(token in server_lower for token in _BROWSER_SERVER_TOKENS):
        return True
    remote_lower = tool.remote_name.strip().lower()
    if remote_lower.startswith("browser_"):
        return True
    if _normalized_base_name(remote_lower) not in _KNOWN_BROWSER_BASE_NAMES:
        return False
    return any(
        isinstance(candidate, McpCatalogTool)
        and candidate.server_name == tool.server_name
        and (
            is_screenshot_tool_name(candidate.remote_name)
            or _normalized_base_name(candidate.remote_name)
            in {"navigate", "navigate_page", "goto", "new_page"}
        )
        for candidate in sibling_tools
    )


def find_auto_screenshot_tool(
    tool: McpCatalogTool, candidates: Iterable[Tool]
) -> McpCatalogTool | None:
    """Find an argument-free screenshot tool on the same MCP server as ``tool``.

    Args:
        tool (McpCatalogTool): Mutating browser MCP tool that just finished.
        candidates (Iterable[Tool]): Allowed, connected tools in this run.

    Returns:
        McpCatalogTool | None: Highest-priority zero-required-arg screenshot tool
        from the same server, or None when unavailable.
    """

    by_remote: dict[str, McpCatalogTool] = {}
    for candidate in candidates:
        if not isinstance(candidate, McpCatalogTool):
            continue
        if candidate.server_name != tool.server_name:
            continue
        remote_lower = candidate.remote_name.strip().lower()
        if remote_lower not in _SCREENSHOT_TOOL_NAMES:
            continue
        required = candidate.input_schema.get("required")
        if isinstance(required, list) and required:
            continue
        by_remote.setdefault(remote_lower, candidate)

    for preferred in _SCREENSHOT_TOOL_NAMES:
        match = by_remote.get(preferred)
        if match is not None:
            return match
    return None


def extract_browser_metadata(
    *,
    arguments: Mapping[str, object],
    output: str,
    structured: object,
    image: ImagePart | None,
) -> BrowserMetadata:
    """Extract sanitized URL, title, and viewport dimensions from a tool run."""

    structured_map = structured if isinstance(structured, Mapping) else None

    url = _first_clean_str(
        structured_map,
        ("url", "page_url", "pageUrl", "current_url", "currentUrl"),
        MAX_BROWSER_URL_CHARS,
    )
    if url is None:
        url = _match_line(_URL_LINE_RE, output, MAX_BROWSER_URL_CHARS)
    if url is None:
        url = _first_clean_str(arguments, ("url", "uri"), MAX_BROWSER_URL_CHARS)

    title = _first_clean_str(
        structured_map,
        ("title", "page_title", "pageTitle"),
        MAX_BROWSER_TITLE_CHARS,
    )
    if title is None:
        title = _match_line(_TITLE_LINE_RE, output, MAX_BROWSER_TITLE_CHARS)

    width = _extract_dimension(structured_map, ("viewport_width", "viewportWidth", "width"))
    height = _extract_dimension(structured_map, ("viewport_height", "viewportHeight", "height"))
    if width is None or height is None:
        nested_viewport = structured_map.get("viewport") if structured_map is not None else None
        if isinstance(nested_viewport, Mapping):
            width = width or _positive_dimension(nested_viewport.get("width"))
            height = height or _positive_dimension(nested_viewport.get("height"))
    if width is None or height is None:
        arg_viewport = arguments.get("viewport")
        if isinstance(arg_viewport, Mapping):
            width = width or _positive_dimension(arg_viewport.get("width"))
            height = height or _positive_dimension(arg_viewport.get("height"))
    if width is None:
        width = _positive_dimension(arguments.get("width"))
    if height is None:
        height = _positive_dimension(arguments.get("height"))
    if (width is None or height is None) and image is not None:
        width = width or min(image.width, MAX_BROWSER_VIEWPORT_SIDE)
        height = height or min(image.height, MAX_BROWSER_VIEWPORT_SIDE)

    return BrowserMetadata(
        url=url,
        title=title,
        viewport_width=width,
        viewport_height=height,
    )


def build_browser_viewport_event(
    *,
    tool_name: str,
    call_id: str,
    image: ImagePart | None,
    metadata: BrowserMetadata,
) -> BrowserViewportUpdated | None:
    """Build a ``BrowserViewportUpdated`` event when frame or page metadata changed."""

    if image is None and metadata.url is None and metadata.title is None:
        return None
    source_tool = _sanitize_line(tool_name, MAX_BROWSER_SOURCE_TOOL_CHARS)
    clean_call_id = _sanitize_line(call_id, 256)
    return BrowserViewportUpdated(
        image=image,
        url=metadata.url,
        title=metadata.title,
        viewport_width=metadata.viewport_width,
        viewport_height=metadata.viewport_height,
        source_tool=source_tool,
        call_id=clean_call_id,
    )


def _sanitize_line(value: object, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _WHITESPACE_CONTROL_RE.sub(" ", value)
    cleaned = _BIDI_AND_CONTROL_RE.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[:max_chars].strip() or None


def _first_clean_str(
    mapping: Mapping[str, object] | None,
    keys: tuple[str, ...],
    max_chars: int,
) -> str | None:
    if mapping is None:
        return None
    for key in keys:
        candidate = _sanitize_line(mapping.get(key), max_chars)
        if candidate is not None:
            return candidate
    return None


def _match_line(pattern: re.Pattern[str], text: str, max_chars: int) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return _sanitize_line(match.group(1), max_chars)


def _positive_dimension(value: object) -> int | None:
    if type(value) is int and 1 <= value <= MAX_BROWSER_VIEWPORT_SIDE:
        return value
    return None


def _extract_dimension(mapping: Mapping[str, object] | None, keys: tuple[str, ...]) -> int | None:
    if mapping is None:
        return None
    for key in keys:
        found = _positive_dimension(mapping.get(key))
        if found is not None:
            return found
    return None
