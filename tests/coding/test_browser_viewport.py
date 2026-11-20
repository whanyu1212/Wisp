"""Tests for browser MCP image capture, auto-screenshot, and BrowserViewportUpdated."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpToolDefinition

from wisp.coding.tool_execution import ConfiguredToolExecutor, ImageStore
from wisp.events import BrowserViewportUpdated, ImagePart, ToolExecutionEnded, WispEvent
from wisp.mcp.config import McpServerConfig
from wisp.mcp.discovery import McpCallTool, McpCatalog
from wisp.mcp.tool import adapt_mcp_tool
from wisp.providers.events import ToolCall
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.images import SessionImageStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolResult

PNG = (Path(__file__).parents[1] / "fixtures" / "images" / "pixel.png").read_bytes()
PNG_B64 = base64.b64encode(PNG).decode("ascii")


class _ScriptedMcpClient:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        result = self._handler(name, arguments)
        if isinstance(result, Exception):
            raise result
        return result


class _LocalImageTool:
    name = "read"
    safety = "read"
    description = "Non-browser tool returning an image."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    async def run(self, arguments: object, context: object) -> ToolResult:
        return ToolResult(text="loaded", images=(PNG,))


def _mcp_tool(
    client: _ScriptedMcpClient,
    *,
    server_name: str = "playwright",
    remote_name: str,
    safety: str = "read",
    required: list[str] | None = None,
) -> Any:
    server = McpServerConfig(
        name=server_name,
        command="playwright-mcp",
        tool_safety={remote_name: safety},  # type: ignore[arg-type]
    )
    definition = McpToolDefinition(
        name=remote_name,
        description=f"Browser tool {remote_name}",
        inputSchema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            **({"required": required} if required else {}),
        },
    )
    return adapt_mcp_tool(server=server, tool=definition, client=client)


def _store_in(directory: Path) -> tuple[SessionImageStore, ImageStore]:
    store = SessionImageStore(directory)

    async def store_image(data: bytes) -> ImagePart:
        return store.store(data)

    return store, store_image


def _run_tool(
    executor: ConfiguredToolExecutor,
    call: ToolCall,
) -> list[Any]:
    async def run() -> list[Any]:
        return [event async for event in executor.execute(call)]

    return anyio.run(run)


def test_browser_screenshot_emits_viewport_update(tmp_path: Path) -> None:
    store, store_image = _store_in(tmp_path / "images")
    live_events: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live_events.append(event)

    client = _ScriptedMcpClient(
        lambda name, args: CallToolResult(
            content=[
                TextContent(text="- Page URL: https://example.test/docs\n- Page Title: Docs Home"),
                ImageContent(data=PNG_B64, mimeType="image/png"),
            ],
            structuredContent={"viewport": {"width": 1280, "height": 800}},
        )
    )
    shot = _mcp_tool(client, remote_name="browser_take_screenshot", safety="read")
    registry = ToolRegistry()
    registry.register(shot)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=sink,
        image_store=store_image,
    )

    events = _run_tool(
        executor,
        ToolCall("call-shot", shot.name, {}),
    )
    ended = next(e for e in events if isinstance(e, ToolExecutionEnded))
    viewport = next(e for e in live_events if isinstance(e, BrowserViewportUpdated))

    assert ended.is_error is False
    assert len(ended.images) == 1
    assert viewport.image == ended.images[0]
    assert store.read(viewport.image) == PNG
    assert viewport.url == "https://example.test/docs"
    assert viewport.title == "Docs Home"
    assert (viewport.viewport_width, viewport.viewport_height) == (1280, 800)
    assert viewport.source_tool == shot.name
    assert viewport.call_id == "call-shot"


def test_non_browser_tool_images_skip_viewport_event(tmp_path: Path) -> None:
    _, store_image = _store_in(tmp_path / "images")
    live_events: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live_events.append(event)

    registry = ToolRegistry()
    registry.register(_LocalImageTool())
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=sink,
        image_store=store_image,
    )

    events = _run_tool(executor, ToolCall("c-read", "read", {}))
    ended = next(e for e in events if isinstance(e, ToolExecutionEnded))

    assert len(ended.images) == 1
    assert not any(isinstance(e, BrowserViewportUpdated) for e in live_events)


def test_mutating_browser_tool_triggers_auto_screenshot(tmp_path: Path) -> None:
    store, store_image = _store_in(tmp_path / "images")
    live_events: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live_events.append(event)

    def handle(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "browser_navigate":
            return CallToolResult(
                content=[
                    TextContent(text="- Page URL: https://wisp.dev/guide\n- Page Title: Wisp Guide")
                ]
            )
        assert name == "browser_take_screenshot"
        return CallToolResult(content=[ImageContent(data=PNG_B64, mimeType="image/png")])

    client = _ScriptedMcpClient(handle)
    nav = _mcp_tool(client, remote_name="browser_navigate", safety="mutating")
    shot = _mcp_tool(client, remote_name="browser_take_screenshot", safety="read")
    registry = ToolRegistry()
    registry.register(nav)
    registry.register(shot)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=sink,
        image_store=store_image,
    )

    events = _run_tool(
        executor,
        ToolCall("c-nav", nav.name, {"url": "https://wisp.dev/guide"}),
    )
    ended = next(e for e in events if isinstance(e, ToolExecutionEnded))
    viewport = next(e for e in live_events if isinstance(e, BrowserViewportUpdated))

    assert ended.is_error is False
    # Auto-screenshot enriches the live viewport without altering the model's ToolResult.images.
    assert ended.images == ()
    assert [name for name, _ in client.calls] == [
        "browser_navigate",
        "browser_take_screenshot",
    ]
    assert viewport.image is not None
    assert store.read(viewport.image) == PNG
    assert viewport.url == "https://wisp.dev/guide"
    assert viewport.title == "Wisp Guide"
    assert viewport.source_tool == nav.name


def test_proxied_mcp_call_emits_viewport_and_auto_screenshot(tmp_path: Path) -> None:
    store, store_image = _store_in(tmp_path / "images")
    live_events: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live_events.append(event)

    def handle(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "navigate_page":
            return CallToolResult(content=[TextContent(text="Navigated to https://example.org/")])
        return CallToolResult(content=[ImageContent(data=PNG_B64, mimeType="image/png")])

    client = _ScriptedMcpClient(handle)
    nav = _mcp_tool(
        client, server_name="chrome-devtools", remote_name="navigate_page", safety="read"
    )
    shot = _mcp_tool(
        client, server_name="chrome-devtools", remote_name="take_screenshot", safety="read"
    )
    catalog = McpCatalog.from_tools((nav, shot))
    registry = ToolRegistry()
    registry.register(nav)
    registry.register(shot)
    registry.register(McpCallTool())
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=sink,
        mcp_catalog=catalog,
        image_store=store_image,
    )

    events = _run_tool(
        executor,
        ToolCall(
            "c-proxy",
            "mcp_call",
            {"name": nav.name, "arguments": {"url": "https://example.org/"}},
        ),
    )
    ended = next(e for e in events if isinstance(e, ToolExecutionEnded))
    viewport = next(e for e in live_events if isinstance(e, BrowserViewportUpdated))

    assert ended.target == nav.name
    assert viewport.source_tool == nav.name
    assert viewport.url == "https://example.org/"
    assert viewport.image is not None
    assert store.read(viewport.image) == PNG


def test_auto_screenshot_respects_policy_and_approval(tmp_path: Path) -> None:
    _, store_image = _store_in(tmp_path / "images")

    def build_and_run(
        *, policy: ToolPolicy, approval: ToolApprovalPolicy, required: list[str] | None = None
    ) -> tuple[list[str], list[WispEvent]]:
        live: list[WispEvent] = []

        async def sink(event: WispEvent) -> None:
            live.append(event)

        client = _ScriptedMcpClient(
            lambda name, args: CallToolResult(
                content=[TextContent(text="- Page Title: Protected Page")]
            )
        )
        nav = _mcp_tool(client, remote_name="browser_navigate", safety="read")
        shot = _mcp_tool(
            client,
            remote_name="browser_take_screenshot",
            safety="command",
            required=required,
        )
        registry = ToolRegistry()
        registry.register(nav)
        registry.register(shot)
        executor = ConfiguredToolExecutor(
            registry=registry,
            context=ToolContext(cwd=tmp_path, protected_paths=()),
            policy=policy,
            approval_policy=approval,
            progress_sink=sink,
            image_store=store_image,
        )
        _run_tool(executor, ToolCall("c1", nav.name, {"url": "https://secret.test/"}))
        return [name for name, _ in client.calls], live

    # 1) Denied by ToolPolicy (only read safety allowed while screenshot is command)
    calls, live = build_and_run(
        policy=ToolPolicy.allow_read_tools(),
        approval=ToolApprovalPolicy.approve_all(),
    )
    assert calls == ["browser_navigate"]
    vp = next(e for e in live if isinstance(e, BrowserViewportUpdated))
    assert vp.image is None and vp.title == "Protected Page" and vp.url == "https://secret.test/"

    # 2) Requires human approval that has not been granted
    calls2, live2 = build_and_run(
        policy=ToolPolicy.allow_all_tools(),
        approval=ToolApprovalPolicy.require_approval(),
    )
    assert calls2 == ["browser_navigate"]
    vp2 = next(e for e in live2 if isinstance(e, BrowserViewportUpdated))
    assert vp2.image is None

    # 3) Screenshot tool declares required parameters -> never invoked with {}
    calls3, _ = build_and_run(
        policy=ToolPolicy.allow_all_tools(),
        approval=ToolApprovalPolicy.approve_all(),
        required=["filename"],
    )
    assert calls3 == ["browser_navigate"]


def test_auto_screenshot_kill_switch_and_resilience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store_image = _store_in(tmp_path / "images")
    live_events: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live_events.append(event)

    def handle(name: str, args: dict[str, Any] | None) -> Any:
        if name == "browser_navigate":
            return CallToolResult(
                content=[
                    TextContent(
                        text="- Page URL: https://x.test/\x1b[31m\n- Page Title: Hi\u202eThere"
                    )
                ]
            )
        if name == "browser_click":
            return CallToolResult(content=[TextContent(text="clicked failed")], isError=True)
        return RuntimeError("screenshot transport crashed")

    client = _ScriptedMcpClient(handle)
    nav = _mcp_tool(client, remote_name="browser_navigate", safety="read")
    click = _mcp_tool(client, remote_name="browser_click", safety="read")
    shot = _mcp_tool(client, remote_name="browser_take_screenshot", safety="read")
    registry = ToolRegistry()
    for t in (nav, click, shot):
        registry.register(t)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=sink,
        image_store=store_image,
    )

    # Screenshot failure still lets primary navigate succeed and sanitizes metadata.
    events = _run_tool(executor, ToolCall("c1", nav.name, {}))
    ended = next(e for e in events if isinstance(e, ToolExecutionEnded))
    vp = next(e for e in live_events if isinstance(e, BrowserViewportUpdated))
    assert ended.is_error is False
    assert vp.image is None
    assert vp.url == "https://x.test/[31m"
    assert vp.title == "HiThere"

    # Kill-switch disables calling screenshot tool.
    client.calls.clear()
    monkeypatch.setenv("WISP_TUI_BROWSER_AUTO_SCREENSHOT", "0")
    _run_tool(executor, ToolCall("c2", nav.name, {}))
    assert [name for name, _ in client.calls] == ["browser_navigate"]

    # Primary error emits no BrowserViewportUpdated.
    live_events.clear()
    err_events = _run_tool(executor, ToolCall("c3", click.name, {}))
    err_ended = next(e for e in err_events if isinstance(e, ToolExecutionEnded))
    assert err_ended.is_error is True
    assert not any(isinstance(e, BrowserViewportUpdated) for e in live_events)


def test_codemode_and_subagent_browser_mcp_viewport_behavior(tmp_path: Path) -> None:
    store, store_image = _store_in(tmp_path / "images")
    live_events: list[WispEvent] = []

    async def sink(event: WispEvent) -> None:
        live_events.append(event)

    def handle(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "browser_navigate":
            return CallToolResult(
                content=[TextContent(text="- Page URL: https://cm.test/\n- Page Title: Codemode")]
            )
        return CallToolResult(
            content=[
                TextContent(text="screenshot captured"),
                ImageContent(data=PNG_B64, mimeType="image/png"),
            ]
        )

    client = _ScriptedMcpClient(handle)
    nav = _mcp_tool(client, remote_name="browser_navigate", safety="read")
    shot = _mcp_tool(client, remote_name="browser_take_screenshot", safety="read")
    registry = ToolRegistry()
    registry.register(nav)
    registry.register(shot)
    executor = ConfiguredToolExecutor(
        registry=registry,
        context=ToolContext(cwd=tmp_path, protected_paths=()),
        policy=ToolPolicy.allow_all_tools(),
        approval_policy=ToolApprovalPolicy.approve_all(),
        progress_sink=sink,
        image_store=store_image,
    )

    # 1) Codemode screenshot call emits BrowserViewportUpdated while keeping
    # text-only script output.
    shot_result = anyio.run(lambda: executor.run_codemode(ToolCall("cm-shot", shot.name, {})))
    assert shot_result.is_error is False
    assert "[The image could not be attached to this result.]" in shot_result.text
    vp_shot = next(e for e in live_events if isinstance(e, BrowserViewportUpdated))
    assert vp_shot.source_tool == shot.name
    assert vp_shot.call_id == "cm-shot"
    assert vp_shot.image is not None
    assert store.read(vp_shot.image) == PNG

    # 2) Codemode mutating browser call triggers safe post-action auto-screenshot.
    live_events.clear()
    client.calls.clear()
    nav_result = anyio.run(
        lambda: executor.run_codemode(ToolCall("cm-nav", nav.name, {"url": "https://cm.test/"}))
    )
    assert nav_result.is_error is False
    assert [name for name, _ in client.calls] == ["browser_navigate", "browser_take_screenshot"]
    vp_nav = next(e for e in live_events if isinstance(e, BrowserViewportUpdated))
    assert vp_nav.url == "https://cm.test/"
    assert vp_nav.title == "Codemode"
    assert vp_nav.image is not None
    assert store.read(vp_nav.image) == PNG

    # 3) Read-only subagent call never hijacks the main browser viewport.
    live_events.clear()
    sub_result = anyio.run(lambda: executor.run_nested(ToolCall("sub-shot", shot.name, {})))
    assert sub_result.is_error is False
    assert not any(isinstance(e, BrowserViewportUpdated) for e in live_events)
