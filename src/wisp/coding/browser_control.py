"""MCP-native browser takeover controller, handoff tracking, and prompt guidance."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

import anyio
from typing_extensions import TypeAliasType

from wisp.coding.browser_viewport import (
    BrowserMetadata,
    build_browser_viewport_event,
    extract_browser_metadata,
    is_browser_mcp_tool,
    is_screenshot_tool_name,
)
from wisp.events import BrowserViewportUpdated, ImagePart
from wisp.images import ImageRejectedError
from wisp.mcp.discovery import McpCatalogTool
from wisp.sessions.errors import SessionError
from wisp.tool_types import DEFAULT_BROWSER_DEFAULT, BrowserDefault
from wisp.tools.base import Tool, ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolError, ToolResult

_logger = logging.getLogger(__name__)

_DEFAULT_BROWSER_ACTION_TIMEOUT_SECONDS = 10.0
_SCREENSHOT_TOOL_ORDER = (
    "browser_take_screenshot",
    "take_screenshot",
    "screenshot",
    "capture_screenshot",
)
_SUPPORTED_SCREENSHOT_REQUIRED = frozenset({"pageid", "scale"})
_PAGE_LINE_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?(\d+)\s*:\s+.+$")
_PAGE_ID_RE = re.compile(r"(?i)\bpage_?id\s*[:=]\s*(\d+)\b")

_JS_CLICK_TEMPLATE = """() => {{
  const x = {x};
  const y = {y};
  const button = {button};
  const doubleClick = {double_click};
  const target = document.elementFromPoint(x, y);
  if (!target) {{
    return {{ clicked: false, url: location.href, title: document.title }};
  }}
  if (typeof target.focus === "function") {{
    try {{ target.focus({{ preventScroll: true }}); }} catch (_e) {{ target.focus(); }}
  }}
  const common = {{
    bubbles: true,
    cancelable: true,
    view: window,
    clientX: x,
    clientY: y,
    button,
    buttons: button === 2 ? 2 : button === 1 ? 4 : 1,
  }};
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {{
    const Ctor = type.startsWith("pointer") && typeof PointerEvent === "function"
      ? PointerEvent
      : MouseEvent;
    target.dispatchEvent(new Ctor(type, common));
  }}
  if (button === 2) {{
    target.dispatchEvent(new MouseEvent("contextmenu", common));
  }} else if (button === 0) {{
    if (typeof target.click === "function") {{
      target.click();
    }} else {{
      target.dispatchEvent(new MouseEvent("click", common));
    }}
    if (doubleClick) {{
      target.dispatchEvent(new MouseEvent("dblclick", {{ ...common, detail: 2 }}));
    }}
  }} else {{
    target.dispatchEvent(new MouseEvent("auxclick", common));
  }}
  return {{ clicked: true, url: location.href, title: document.title }};
}}"""

_JS_SCROLL_TEMPLATE = """() => {{
  const x = {x};
  const y = {y};
  const dx = {delta_x};
  const dy = {delta_y};
  let el = document.elementFromPoint(x, y);
  while (el && el !== document.documentElement && el !== document.body) {{
    const style = getComputedStyle(el);
    const canY =
      dy !== 0 &&
      el.scrollHeight > el.clientHeight &&
      /(auto|scroll|overlay)/.test(style.overflowY);
    const canX =
      dx !== 0 &&
      el.scrollWidth > el.clientWidth &&
      /(auto|scroll|overlay)/.test(style.overflowX);
    if (canY || canX) {{
      el.scrollBy({{ left: dx, top: dy, behavior: "instant" }});
      return {{ scrolled: true, url: location.href, title: document.title }};
    }}
    el = el.parentElement;
  }}
  window.scrollBy({{ left: dx, top: dy, behavior: "instant" }});
  return {{ scrolled: true, url: location.href, title: document.title }};
}}"""

_JS_TYPE_ACTIVE_TEMPLATE = """() => {{
  const text = {text_json};
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) {{
    return {{ typed: false, url: location.href, title: document.title }};
  }}
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {{
    const start = typeof el.selectionStart === "number" ? el.selectionStart : el.value.length;
    const end = typeof el.selectionEnd === "number" ? el.selectionEnd : el.value.length;
    const next = el.value.slice(0, start) + text + el.value.slice(end);
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
    if (descriptor && typeof descriptor.set === "function") {{
      descriptor.set.call(el, next);
    }} else {{
      el.value = next;
    }}
    if (typeof el.setSelectionRange === "function") {{
      const caret = start + text.length;
      try {{ el.setSelectionRange(caret, caret); }} catch (_e) {{}}
    }}
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    el.dispatchEvent(new Event("change", {{ bubbles: true }}));
    return {{ typed: true, url: location.href, title: document.title }};
  }}
  if (el.isContentEditable) {{
    if (typeof document.execCommand === "function") {{
      document.execCommand("insertText", false, text);
    }} else {{
      el.textContent = (el.textContent || "") + text;
    }}
    el.dispatchEvent(new Event("input", {{ bubbles: true }}));
    return {{ typed: true, url: location.href, title: document.title }};
  }}
  return {{ typed: false, url: location.href, title: document.title }};
}}"""

_JS_PRESS_KEY_TEMPLATE = """() => {{
  const key = {key_json};
  const mods = {modifiers_json};
  const target = document.activeElement || document.body || document.documentElement;
  const init = {{
    key,
    bubbles: true,
    cancelable: true,
    altKey: Boolean(mods.alt),
    ctrlKey: Boolean(mods.ctrl),
    metaKey: Boolean(mods.meta),
    shiftKey: Boolean(mods.shift),
  }};
  target.dispatchEvent(new KeyboardEvent("keydown", init));
  if (key === "Enter" && target instanceof HTMLInputElement && target.form) {{
    if (typeof target.form.requestSubmit === "function") {{
      target.form.requestSubmit();
    }} else {{
      target.form.submit();
    }}
  }} else if (
    key === "Backspace" &&
    (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)
  ) {{
    const start =
      typeof target.selectionStart === "number" ? target.selectionStart : target.value.length;
    const end = typeof target.selectionEnd === "number" ? target.selectionEnd : target.value.length;
    if (end > 0 || start !== end) {{
      const next = start !== end
        ? target.value.slice(0, start) + target.value.slice(end)
        : target.value.slice(0, start - 1) + target.value.slice(end);
      target.value = next;
      const caret = start !== end ? start : Math.max(0, start - 1);
      try {{ target.setSelectionRange(caret, caret); }} catch (_e) {{}}
      target.dispatchEvent(new Event("input", {{ bubbles: true }}));
    }}
  }}
  target.dispatchEvent(new KeyboardEvent("keyup", init));
  return {{ pressed: true, url: location.href, title: document.title }};
}}"""

_JS_BACK = "() => { window.history.back(); return { url: location.href, title: document.title }; }"
_JS_RELOAD = (
    "() => { window.location.reload(); return { url: location.href, title: document.title }; }"
)

BrowserImageStore = Callable[[bytes], Awaitable[ImagePart]]
BrowserConnectedCheck = Callable[[str], bool]

BrowserActionKind = TypeAliasType(
    "BrowserActionKind",
    Literal["click", "scroll", "type", "press_key", "back", "reload", "refresh"],
)
BrowserButtonKind = TypeAliasType(
    "BrowserButtonKind",
    Literal["left", "right", "middle"],
)
BrowserModifierKind = TypeAliasType(
    "BrowserModifierKind",
    Literal["Alt", "Control", "Meta", "Shift"],
)


@dataclass(frozen=True, slots=True)
class BrowserInteraction:
    """Coding-layer request for one user-initiated browser takeover interaction."""

    action: BrowserActionKind
    interaction_id: str | None = None
    x: int | None = None
    y: int | None = None
    button: BrowserButtonKind | None = None
    double_click: bool | None = None
    delta_x: int | None = None
    delta_y: int | None = None
    text: str | None = field(default=None, repr=False)
    key: str | None = None
    modifiers: tuple[BrowserModifierKind, ...] | None = None

    def __repr__(self) -> str:
        text_part = f", text=<{len(self.text)} chars>" if self.text is not None else ""
        return (
            f"BrowserInteraction(interaction_id={self.interaction_id!r}, "
            f"action={self.action!r}, x={self.x!r}, y={self.y!r}, "
            f"button={self.button!r}, double_click={self.double_click!r}, "
            f"delta_x={self.delta_x!r}, delta_y={self.delta_y!r}, "
            f"key={self.key!r}, modifiers={self.modifiers!r}{text_part})"
        )

    __str__ = __repr__


class BrowserControlError(RuntimeError):
    """Raised when a user-initiated browser takeover action cannot complete."""


@dataclass(frozen=True, slots=True)
class BrowserInteractionResult:
    """Result of one completed user-initiated browser takeover interaction."""

    viewport_event: BrowserViewportUpdated | None
    server_name: str
    source_tool: str
    metadata: BrowserMetadata


@dataclass(frozen=True, slots=True)
class BrowserHandoffSnapshot:
    """Counted, secret-free summary of human takeover actions since the last model turn."""

    clicks: int = 0
    scrolls: int = 0
    key_presses: int = 0
    text_entries: int = 0
    navigations: int = 0
    last_url: str | None = None
    last_title: str | None = None

    @property
    def total_actions(self) -> int:
        return self.clicks + self.scrolls + self.key_presses + self.text_entries + self.navigations


class BrowserHandoffTracker:
    """Track active browser MCP target and human takeover interactions per session."""

    def __init__(self) -> None:
        self.active_server_name: str | None = None
        self.last_page_id: int | str | None = None
        self.last_url: str | None = None
        self.last_title: str | None = None
        self._clicks = 0
        self._scrolls = 0
        self._key_presses = 0
        self._text_entries = 0
        self._navigations = 0

    def select_server(self, server_name: str) -> None:
        """Select ``server_name`` and discard any cached page ID from another server."""

        if server_name:
            if self.active_server_name != server_name:
                self.last_page_id = None
            self.active_server_name = server_name

    def observe_agent_tool(
        self,
        server_name: str,
        arguments: Mapping[str, object],
        output: str,
        structured: object,
    ) -> None:
        """Remember the MCP server and page ID observed from an agent browser call."""

        self.select_server(server_name)
        page_id = _extract_page_id_from_mapping(arguments)
        if page_id is None:
            page_id = _extract_page_id_from_result(output, structured)
        if page_id is not None:
            self.last_page_id = page_id

    def record_user_action(
        self,
        action: str,
        *,
        server_name: str,
        metadata: BrowserMetadata,
    ) -> None:
        """Record one mutating user interaction without retaining typed text or key names."""

        self.select_server(server_name)
        if metadata.url is not None:
            self.last_url = metadata.url
        if metadata.title is not None:
            self.last_title = metadata.title
        if action == "click":
            self._clicks += 1
        elif action == "scroll":
            self._scrolls += 1
        elif action == "press_key":
            self._key_presses += 1
        elif action == "type":
            self._text_entries += 1
        elif action in {
            "back",
            "reload",
            "live_open",
            "live_window",
            "live_navigate",
            "live_forward",
        }:
            self._navigations += 1

    def pending_snapshot(self) -> BrowserHandoffSnapshot | None:
        """Return a snapshot of undelivered human interactions, or None when none occurred."""

        snapshot = BrowserHandoffSnapshot(
            clicks=self._clicks,
            scrolls=self._scrolls,
            key_presses=self._key_presses,
            text_entries=self._text_entries,
            navigations=self._navigations,
            last_url=self.last_url,
            last_title=self.last_title,
        )
        return snapshot if snapshot.total_actions > 0 else None

    def mark_delivered(self, snapshot: BrowserHandoffSnapshot | None) -> None:
        """Consume only the counts delivered to a persisted user prompt."""

        if snapshot is None:
            return
        self._clicks = max(0, self._clicks - snapshot.clicks)
        self._scrolls = max(0, self._scrolls - snapshot.scrolls)
        self._key_presses = max(0, self._key_presses - snapshot.key_presses)
        self._text_entries = max(0, self._text_entries - snapshot.text_entries)
        self._navigations = max(0, self._navigations - snapshot.navigations)

    def restore(self, snapshot: BrowserHandoffSnapshot | None) -> None:
        """Restore previously delivered counts when a prompt is rolled back."""

        if snapshot is None:
            return
        self._clicks += snapshot.clicks
        self._scrolls += snapshot.scrolls
        self._key_presses += snapshot.key_presses
        self._text_entries += snapshot.text_entries
        self._navigations += snapshot.navigations
        if self.last_url is None and snapshot.last_url is not None:
            self.last_url = snapshot.last_url
        if self.last_title is None and snapshot.last_title is not None:
            self.last_title = snapshot.last_title

    def clear(self) -> None:
        """Discard session-local browser tracking when resetting or switching sessions."""

        self.active_server_name = None
        self.last_page_id = None
        self.last_url = None
        self.last_title = None
        self._clicks = 0
        self._scrolls = 0
        self._key_presses = 0
        self._text_entries = 0
        self._navigations = 0

    @staticmethod
    def format_note(snapshot: BrowserHandoffSnapshot | None) -> str | None:
        """Render a concise bracketed user-turn handoff note, free of secret inputs."""

        if snapshot is None or snapshot.total_actions <= 0:
            return None
        parts: list[str] = []
        if snapshot.clicks:
            parts.append(f"{snapshot.clicks} click" + ("s" if snapshot.clicks != 1 else ""))
        if snapshot.scrolls:
            parts.append(f"{snapshot.scrolls} scroll" + ("s" if snapshot.scrolls != 1 else ""))
        if snapshot.key_presses:
            parts.append(
                f"{snapshot.key_presses} key press" + ("es" if snapshot.key_presses != 1 else "")
            )
        if snapshot.text_entries:
            parts.append(
                "text entry"
                if snapshot.text_entries == 1
                else f"text entry ({snapshot.text_entries}x)"
            )
        if snapshot.navigations:
            parts.append(
                "navigation" if snapshot.navigations == 1 else f"{snapshot.navigations} navigations"
            )
        summary = ", ".join(parts)
        location = _format_page_location(snapshot.last_url, snapshot.last_title)
        location_clause = f"; current page: {location}" if location is not None else ""
        return (
            f"[The user interacted with the live browser pane ({summary}){location_clause}. "
            "Re-snapshot the page before acting because page state, focus, URL, or element "
            "refs may have changed.]"
        )


class BrowserController(Protocol):
    """Transport-neutral boundary for applying user browser takeover interactions."""

    async def execute(
        self,
        request: BrowserInteraction,
        *,
        tools: Iterable[Tool],
        context: ToolContext,
        policy: ToolPolicy,
        image_store: BrowserImageStore,
        tracker: BrowserHandoffTracker,
        connected: BrowserConnectedCheck | None = None,
    ) -> BrowserInteractionResult: ...


class McpBrowserController:
    """Apply user takeover interactions through the active connected browser MCP server."""

    def __init__(self, *, timeout_seconds: float = _DEFAULT_BROWSER_ACTION_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds

    async def execute(
        self,
        request: BrowserInteraction,
        *,
        tools: Iterable[Tool],
        context: ToolContext,
        policy: ToolPolicy,
        image_store: BrowserImageStore,
        tracker: BrowserHandoffTracker,
        connected: BrowserConnectedCheck | None = None,
    ) -> BrowserInteractionResult:
        """Execute ``request`` on the selected browser MCP server and capture a fresh frame."""

        all_tools = tuple(tools)
        connected_mcp = tuple(
            tool
            for tool in all_tools
            if isinstance(tool, McpCatalogTool)
            and (connected is None or connected(tool.server_name))
        )
        server_name, server_tools = _select_browser_server(
            connected_mcp,
            active_server=tracker.active_server_name,
        )
        tracker.select_server(server_name)
        tools_by_remote = {tool.remote_name.strip().lower(): tool for tool in server_tools}

        page_id = tracker.last_page_id
        call_id = request.interaction_id or f"browser-interact-{request.action}"
        deadline = anyio.current_time() + self._timeout_seconds
        if request.action == "refresh":
            with anyio.move_on_after(self._timeout_seconds) as cancel_scope:
                screenshot_tool = _find_control_screenshot_tool(tools_by_remote)
                if screenshot_tool is None:
                    raise BrowserControlError(
                        f"Browser MCP server {server_name!r} exposes no screenshot tool"
                    )
                page_id = await _ensure_page_id_if_needed(
                    screenshot_tool,
                    tools_by_remote=tools_by_remote,
                    current_page_id=page_id,
                    context=context,
                    policy=policy,
                    tracker=tracker,
                )
                shot_args = _screenshot_arguments(screenshot_tool, page_id=page_id)
                result = await _invoke_tool(
                    screenshot_tool, shot_args, context=context, policy=policy
                )
                image = await _store_result_image(result, image_store)
                metadata = extract_browser_metadata(
                    arguments=shot_args,
                    output=result.text,
                    structured=result.structured,
                    image=image,
                )
                event = build_browser_viewport_event(
                    tool_name=f"browser_interact:{request.action}",
                    call_id=call_id,
                    image=image,
                    metadata=metadata,
                )
                return BrowserInteractionResult(
                    viewport_event=event,
                    server_name=server_name,
                    source_tool=screenshot_tool.name,
                    metadata=metadata,
                )
            if cancel_scope.cancelled_caught:
                raise BrowserControlError(
                    f"Browser {request.action} timed out after {self._timeout_seconds:.0f}s"
                )
            raise BrowserControlError(f"Browser {request.action} did not produce a result")

        primary_tool, raw_args = _resolve_action_call(request, tools_by_remote)
        if primary_tool is None:
            raise BrowserControlError(
                f"Browser MCP server {server_name!r} has no supported tool for {request.action!r}"
            )
        primary_result: ToolResult | None = None
        primary_args: dict[str, object] = {}
        with anyio.move_on_after(self._timeout_seconds) as primary_scope:
            page_id = await _ensure_page_id_if_needed(
                primary_tool,
                tools_by_remote=tools_by_remote,
                current_page_id=page_id,
                context=context,
                policy=policy,
                tracker=tracker,
            )
            primary_args = _filter_schema_arguments(primary_tool, raw_args, page_id=page_id)
            primary_result = await _invoke_tool(
                primary_tool, primary_args, context=context, policy=policy
            )
        if primary_scope.cancelled_caught:
            raise BrowserControlError(
                f"Browser {request.action} timed out after {self._timeout_seconds:.0f}s"
            )
        if primary_result is None:
            raise BrowserControlError(f"Browser {request.action} did not produce a result")

        # The primary action has already mutated the browser; isolate post-mutation
        # screenshot/pageId follow-ups so failures or cancellations still record handoff.
        metadata = extract_browser_metadata(
            arguments=primary_args,
            output=primary_result.text,
            structured=primary_result.structured,
            image=None,
        )
        image = None
        try:
            image = await _store_result_image(primary_result, image_store)
            if image is not None:
                metadata = extract_browser_metadata(
                    arguments=primary_args,
                    output=primary_result.text,
                    structured=primary_result.structured,
                    image=image,
                )
            if image is None:
                screenshot_tool = _find_control_screenshot_tool(tools_by_remote)
                remaining = max(0.0, deadline - anyio.current_time())
                if (
                    screenshot_tool is not None
                    and policy.allows(screenshot_tool)
                    and remaining > 0.0
                ):
                    with anyio.move_on_after(remaining) as shot_scope:
                        try:
                            page_id = await _ensure_page_id_if_needed(
                                screenshot_tool,
                                tools_by_remote=tools_by_remote,
                                current_page_id=page_id,
                                context=context,
                                policy=policy,
                                tracker=tracker,
                            )
                            shot_args = _screenshot_arguments(screenshot_tool, page_id=page_id)
                            shot_result = await _invoke_tool(
                                screenshot_tool, shot_args, context=context, policy=policy
                            )
                            image = await _store_result_image(shot_result, image_store)
                            shot_meta = extract_browser_metadata(
                                arguments=shot_args,
                                output=shot_result.text,
                                structured=shot_result.structured,
                                image=image,
                            )
                            metadata = BrowserMetadata(
                                url=shot_meta.url or metadata.url,
                                title=shot_meta.title or metadata.title,
                                viewport_width=shot_meta.viewport_width or metadata.viewport_width,
                                viewport_height=(
                                    shot_meta.viewport_height or metadata.viewport_height
                                ),
                            )
                        except BrowserControlError:
                            _logger.debug(
                                "Post-interaction screenshot failed on %s",
                                server_name,
                                exc_info=True,
                            )
                    if shot_scope.cancelled_caught:
                        _logger.debug(
                            "Post-interaction screenshot timed out on %s",
                            server_name,
                        )
        except anyio.get_cancelled_exc_class():
            tracker.record_user_action(
                request.action,
                server_name=server_name,
                metadata=metadata,
            )
            raise

        event = build_browser_viewport_event(
            tool_name=f"browser_interact:{request.action}",
            call_id=call_id,
            image=image,
            metadata=metadata,
        )
        return BrowserInteractionResult(
            viewport_event=event,
            server_name=server_name,
            source_tool=primary_tool.name,
            metadata=metadata,
        )


def browser_prompt_guidance(
    tools: Iterable[Tool],
    *,
    connected: BrowserConnectedCheck | None = None,
    live_browser_available: bool = False,
    browser_default: BrowserDefault = DEFAULT_BROWSER_DEFAULT,
) -> str:
    """Return Wisp model guidance when the live browser or a browser MCP server is available."""

    tool_list = tuple(tools)
    connected_browser_servers = sorted(
        {
            tool.server_name
            for tool in tool_list
            if isinstance(tool, McpCatalogTool)
            and (connected is None or connected(tool.server_name))
            and is_browser_mcp_tool(tool, tool_list)
        }
    )
    if not live_browser_available and not connected_browser_servers:
        return ""

    sections: list[str] = []
    if live_browser_available:
        live_lines = [
            "[LIVE BROWSER WORKFLOW]",
            "Wisp has a built-in live browser in the terminal (browser_navigate, browser_read, "
            "browser_click, browser_type, browser_screenshot), rendered in the user's /browser "
            "pane.",
            "- Prefer browser_read over browser_screenshot to conserve image context budget.",
            "- For sign-in, 2FA/OTP codes, or CAPTCHAs, pause and ask the user to take over in "
            "the /browser pane (press i or click the viewport; Esc to exit) rather than asking "
            "them to paste passwords or secrets into chat.",
            "- If a site blocks headless Chrome or requires an OS prompt (passkey, Touch ID, "
            "file picker, camera), suggest switching to window mode (browser_navigate with "
            "window=true, or /browser window) or a user takeover instead of retrying.",
            "- After a user takeover handoff note appears, call browser_read before acting "
            "because page state, focus, URL, or element refs may have changed.",
        ]
        if connected_browser_servers and browser_default == "live":
            live_lines.insert(
                1,
                "- Default browser: Wisp's live browser (browser_* tools). Use the user's own "
                "Chrome (via the connected browser MCP server) when the task needs existing "
                "sign-ins, extensions, or open tabs, or when the user asks for it.",
            )
        elif connected_browser_servers and browser_default == "mcp":
            live_lines.insert(
                1,
                "- Default browser: the user's own Chrome (via the connected browser MCP server). "
                "Wisp's live browser (browser_* tools) is also available in the terminal.",
            )
        sections.append("\n".join(live_lines))

    if connected_browser_servers:
        servers = ", ".join(connected_browser_servers)
        mcp_lines = [
            "[BROWSER MCP WORKFLOW]",
            f"Connected browser MCP server(s): {servers}.",
            "- Prefer accessibility/DOM snapshots (such as browser_snapshot or take_snapshot) "
            "to inspect page state and target elements by ref instead of guessing selectors.",
            "- Use screenshots selectively to conserve image context budget; Wisp automatically "
            "mirrors live browser updates into the user's /browser viewport pane.",
            "- Inspect browser console messages and network requests when diagnosing UI or "
            "client-side failures.",
            "- For sign-in, 2FA/OTP codes, CAPTCHAs, or manual visual checks, pause and ask the "
            "user to take over in the /browser pane (press i or click the viewport; Esc to exit) "
            "rather than asking them to paste passwords or secrets into chat.",
            "- After a user takeover handoff note appears, take a fresh snapshot before clicking "
            "or typing because URL, focus, or element refs may have changed.",
        ]
        sections.append("\n".join(mcp_lines))
    return "\n\n".join(sections)


def _select_browser_server(
    connected_tools: tuple[McpCatalogTool, ...],
    *,
    active_server: str | None,
) -> tuple[str, tuple[McpCatalogTool, ...]]:
    by_server: dict[str, list[McpCatalogTool]] = {}
    for tool in connected_tools:
        if is_browser_mcp_tool(tool, connected_tools):
            by_server.setdefault(tool.server_name, []).append(tool)
    if not by_server:
        raise BrowserControlError("No connected browser MCP server is available")
    if active_server is not None and active_server in by_server:
        return active_server, tuple(by_server[active_server])
    if len(by_server) == 1:
        server_name, tools = next(iter(by_server.items()))
        return server_name, tuple(tools)
    names = ", ".join(sorted(by_server))
    raise BrowserControlError(
        f"Multiple browser MCP servers are connected ({names}); "
        "use one first to select the active browser"
    )


def _find_control_screenshot_tool(
    tools_by_remote: Mapping[str, McpCatalogTool],
) -> McpCatalogTool | None:
    for remote_name in _SCREENSHOT_TOOL_ORDER:
        tool = tools_by_remote.get(remote_name)
        if tool is None or not is_screenshot_tool_name(tool.remote_name):
            continue
        required = tool.input_schema.get("required")
        if isinstance(required, list) and any(
            not isinstance(name, str) or name.lower() not in _SUPPORTED_SCREENSHOT_REQUIRED
            for name in required
        ):
            continue
        return tool
    return None


def _screenshot_arguments(tool: McpCatalogTool, *, page_id: int | str | None) -> dict[str, object]:
    properties = _schema_properties(tool)
    args: dict[str, object] = {}
    if page_id is not None and "pageId" in properties:
        args["pageId"] = page_id
    if "scale" in properties and "scale" in _schema_required(tool):
        args["scale"] = "css"
    return args


def _supports_required_arguments(
    tool: McpCatalogTool,
    raw_args: Mapping[str, object],
    *,
    allow_page_id: bool = True,
) -> bool:
    required = _schema_required(tool)
    if not required:
        return True
    properties = _schema_properties(tool)
    if properties:
        provided = {key for key in raw_args if key in properties}
        if allow_page_id and "pageId" in properties:
            provided.add("pageId")
    else:
        provided = set(raw_args)
        if allow_page_id:
            provided.add("pageId")
    return required.issubset(provided)


def _resolve_action_call(
    request: BrowserInteraction,
    tools_by_remote: Mapping[str, McpCatalogTool],
) -> tuple[McpCatalogTool | None, dict[str, object]]:
    action = request.action
    if action == "click":
        assert request.x is not None and request.y is not None
        args: dict[str, object] = {"x": request.x, "y": request.y}
        if request.button is not None:
            args["button"] = request.button
        if request.double_click:
            args["clickCount"] = 2
            args["doubleClick"] = True
        for native in ("browser_mouse_click_xy", "mouse_click_xy"):
            tool = tools_by_remote.get(native)
            if tool is not None and _supports_required_arguments(tool, args):
                return tool, args
        if (eval_tool := _find_eval_tool(tools_by_remote)) is not None:
            button_code = {"left": 0, "middle": 1, "right": 2}[request.button or "left"]
            script = _JS_CLICK_TEMPLATE.format(
                x=request.x,
                y=request.y,
                button=button_code,
                double_click="true" if request.double_click else "false",
            )
            return eval_tool, _eval_arguments(eval_tool, script)
        return None, {}

    if action == "scroll":
        assert request.x is not None and request.y is not None
        delta_x = request.delta_x or 0
        delta_y = request.delta_y or 0
        args = {
            "deltaX": delta_x,
            "deltaY": delta_y,
            "x": request.x,
            "y": request.y,
        }
        for native in ("browser_mouse_wheel", "mouse_wheel"):
            tool = tools_by_remote.get(native)
            if tool is not None and _supports_required_arguments(tool, args):
                return tool, args
        if (eval_tool := _find_eval_tool(tools_by_remote)) is not None:
            script = _JS_SCROLL_TEMPLATE.format(
                x=request.x, y=request.y, delta_x=delta_x, delta_y=delta_y
            )
            return eval_tool, _eval_arguments(eval_tool, script)
        return None, {}

    if action == "type":
        assert request.text is not None
        args = {"text": request.text}
        for native in ("type_text", "browser_type"):
            tool = tools_by_remote.get(native)
            if tool is not None and _supports_required_arguments(tool, args):
                return tool, args
        if (eval_tool := _find_eval_tool(tools_by_remote)) is not None:
            script = _JS_TYPE_ACTIVE_TEMPLATE.format(text_json=json.dumps(request.text))
            return eval_tool, _eval_arguments(eval_tool, script)
        return None, {}

    if action == "press_key":
        assert request.key is not None
        modifiers = request.modifiers or ()
        wire_key = "Space" if request.key == " " else request.key
        if modifiers:
            wire_key = "+".join([*modifiers, wire_key])
        args = {"key": wire_key}
        for native in ("browser_press_key", "press_key"):
            tool = tools_by_remote.get(native)
            if tool is not None and _supports_required_arguments(tool, args):
                return tool, args
        if (eval_tool := _find_eval_tool(tools_by_remote)) is not None:
            dom_key = " " if request.key == "Space" else request.key
            mod_flags = {
                "alt": "Alt" in modifiers,
                "ctrl": "Control" in modifiers,
                "meta": "Meta" in modifiers,
                "shift": "Shift" in modifiers,
            }
            script = _JS_PRESS_KEY_TEMPLATE.format(
                key_json=json.dumps(dom_key),
                modifiers_json=json.dumps(mod_flags),
            )
            return eval_tool, _eval_arguments(eval_tool, script)
        return None, {}

    if action == "back":
        tool = tools_by_remote.get("browser_navigate_back")
        if tool is not None and _supports_required_arguments(tool, {}):
            return tool, {}
        tool = tools_by_remote.get("navigate_page")
        if tool is not None and _supports_required_arguments(tool, {"type": "back"}):
            return tool, {"type": "back"}
        if (eval_tool := _find_eval_tool(tools_by_remote)) is not None:
            return eval_tool, _eval_arguments(eval_tool, _JS_BACK)
        return None, {}

    if action == "reload":
        tool = tools_by_remote.get("navigate_page")
        if tool is not None and _supports_required_arguments(tool, {"type": "reload"}):
            return tool, {"type": "reload"}
        if (eval_tool := _find_eval_tool(tools_by_remote)) is not None:
            return eval_tool, _eval_arguments(eval_tool, _JS_RELOAD)
        return None, {}

    return None, {}


def _find_eval_tool(tools_by_remote: Mapping[str, McpCatalogTool]) -> McpCatalogTool | None:
    for remote_name in ("browser_evaluate", "evaluate_script", "evaluate"):
        tool = tools_by_remote.get(remote_name)
        if tool is not None and "function" in _schema_properties(tool):
            if _supports_required_arguments(tool, _eval_arguments(tool, "")):
                return tool
    return None


def _eval_arguments(tool: McpCatalogTool, script: str) -> dict[str, object]:
    args: dict[str, object] = {"function": script}
    if "format" in _schema_properties(tool):
        args["format"] = "function"
    return args


def _schema_properties(tool: McpCatalogTool) -> Mapping[str, object]:
    properties = tool.input_schema.get("properties")
    return properties if isinstance(properties, Mapping) else {}


def _schema_required(tool: McpCatalogTool) -> set[str]:
    required = tool.input_schema.get("required")
    if not isinstance(required, list):
        return set()
    return {name for name in required if isinstance(name, str)}


def _filter_schema_arguments(
    tool: McpCatalogTool,
    raw_args: Mapping[str, object],
    *,
    page_id: int | str | None,
) -> dict[str, object]:
    properties = _schema_properties(tool)
    if properties:
        filtered = {key: value for key, value in raw_args.items() if key in properties}
    else:
        filtered = dict(raw_args)
    if page_id is not None and "pageId" in properties:
        filtered["pageId"] = page_id
    return filtered


async def _ensure_page_id_if_needed(
    tool: McpCatalogTool,
    *,
    tools_by_remote: Mapping[str, McpCatalogTool],
    current_page_id: int | str | None,
    context: ToolContext,
    policy: ToolPolicy,
    tracker: BrowserHandoffTracker,
) -> int | str | None:
    required = _schema_required(tool)
    if "pageId" not in required or current_page_id is not None:
        return current_page_id
    list_pages = tools_by_remote.get("list_pages")
    if list_pages is None:
        raise BrowserControlError(
            f"{tool.name} requires a pageId and no list_pages tool is available"
        )
    result = await _invoke_tool(list_pages, {}, context=context, policy=policy)
    page_id = _extract_page_id_from_result(result.text, result.structured)
    if page_id is None:
        raise BrowserControlError(f"Could not determine an active browser pageId for {tool.name}")
    tracker.last_page_id = page_id
    return page_id


async def _invoke_tool(
    tool: McpCatalogTool,
    arguments: ToolArguments,
    *,
    context: ToolContext,
    policy: ToolPolicy,
) -> ToolResult:
    if not policy.allows(tool):
        raise BrowserControlError(f"Tool policy denies {tool.name}")
    try:
        return await tool.run(arguments, context)
    except ToolError:
        # Never forward remote `is_error` text, which may echo typed secrets or scripts.
        raise BrowserControlError(f"{tool.name} failed") from None
    except Exception as exc:  # noqa: BLE001 - isolate untrusted MCP errors without leaking payloads
        raise BrowserControlError(f"{tool.name} failed: {type(exc).__name__}") from None


async def _store_result_image(
    result: ToolResult, image_store: BrowserImageStore
) -> ImagePart | None:
    if not result.images:
        return None
    try:
        return await image_store(result.images[-1])
    except (ImageRejectedError, SessionError):
        _logger.debug("Browser interaction image skipped after validation/store error")
        return None


def _extract_page_id_from_mapping(mapping: object) -> int | str | None:
    if not isinstance(mapping, Mapping):
        return None
    for key in ("pageId", "page_id", "selectedPageId", "selected_page_id"):
        value = mapping.get(key)
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.strip():
            return int(value) if value.strip().isdigit() else value.strip()
    return None


def _extract_page_id_from_result(output: str, structured: object) -> int | str | None:
    direct = _extract_page_id_from_mapping(structured)
    if direct is not None:
        return direct
    pages: object = None
    if isinstance(structured, list):
        pages = structured
    elif isinstance(structured, Mapping):
        pages = structured.get("pages")
    if isinstance(pages, list) and pages:
        for preferred in (
            *(
                item
                for item in pages
                if isinstance(item, Mapping) and (item.get("selected") or item.get("active"))
            ),
            *pages,
        ):
            if isinstance(preferred, Mapping):
                for key in ("pageId", "page_id", "id", "index"):
                    value = preferred.get(key)
                    if type(value) is int and value >= 0:
                        return value
                    if isinstance(value, str) and value.strip():
                        return int(value) if value.strip().isdigit() else value.strip()
    selected_match = None
    first_match = None
    for line in output.splitlines():
        line_match = _PAGE_LINE_RE.match(line)
        if line_match is not None:
            candidate = int(line_match.group(1))
            if first_match is None:
                first_match = candidate
            if "[selected]" in line.lower() or "(selected)" in line.lower():
                selected_match = candidate
                break
        id_match = _PAGE_ID_RE.search(line)
        if id_match is not None:
            candidate = int(id_match.group(1))
            if first_match is None:
                first_match = candidate
            if "[selected]" in line.lower() or "(selected)" in line.lower():
                selected_match = candidate
                break
    return selected_match if selected_match is not None else first_match


def _format_page_location(url: str | None, title: str | None) -> str | None:
    if url is not None and title is not None:
        return f'{url} ("{title}")'
    if url is not None:
        return url
    if title is not None:
        return f'"{title}"'
    return None
