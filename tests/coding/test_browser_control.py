"""Tests for MCP-native browser takeover controller, handoff tracking, and prompt guidance."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpToolDefinition

from tests.coding.compaction_support import VALID_COMPACTION_SUMMARY, build_model_registry
from tests.coding.session_support import CapturingProvider, ToolLoopProvider
from wisp.coding.browser_control import (
    BrowserControlError,
    BrowserHandoffTracker,
    BrowserInteraction,
    McpBrowserController,
    browser_prompt_guidance,
)
from wisp.coding.browser_viewport import BrowserMetadata
from wisp.coding.session import CodingSession
from wisp.events import ImagePart
from wisp.mcp.config import McpServerConfig
from wisp.mcp.tool import adapt_mcp_tool
from wisp.messages import Message
from wisp.providers.base import ContextOverflowError
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ToolCall,
)
from wisp.providers.fake import ScriptedProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.images import SessionImageStore
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.base import Tool
from wisp.tools.context import ToolContext
from wisp.tools.policy import ToolPolicy

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
        if isinstance(result, CallToolResult):
            return result
        return await result


def _make_mcp_tool(
    client: _ScriptedMcpClient,
    *,
    server_name: str,
    remote_name: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    safety: str = "mutating",
) -> Any:
    server = McpServerConfig(
        name=server_name,
        command=f"{server_name}-mcp",
        tool_safety={remote_name: safety},  # type: ignore[arg-type]
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
    }
    if required is not None:
        schema["required"] = required
    definition = McpToolDefinition(
        name=remote_name,
        description=f"Browser tool {remote_name}",
        inputSchema=schema,
    )
    return adapt_mcp_tool(server=server, tool=definition, client=client)


def _image_store(tmp_path: Path):
    store = SessionImageStore(tmp_path / "images")

    async def _store(data: bytes) -> ImagePart:
        return store.store(data)

    return _store


def _playwright_tools(client: _ScriptedMcpClient, *, vision: bool = False) -> list[Tool]:
    tools: list[Tool] = [
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_snapshot",
            safety="read",
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_evaluate",
            properties={"function": {"type": "string"}},
            required=["function"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_press_key",
            properties={"key": {"type": "string"}},
            required=["key"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_navigate_back",
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_take_screenshot",
            properties={"scale": {"type": "string", "enum": ["css", "device"]}},
            required=["scale"],
            safety="read",
        ),
    ]
    if vision:
        tools.extend(
            [
                _make_mcp_tool(
                    client,
                    server_name="playwright",
                    remote_name="browser_mouse_click_xy",
                    properties={
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "button": {"type": "string"},
                        "clickCount": {"type": "integer"},
                    },
                    required=["x", "y"],
                ),
                _make_mcp_tool(
                    client,
                    server_name="playwright",
                    remote_name="browser_mouse_wheel",
                    properties={
                        "deltaX": {"type": "number"},
                        "deltaY": {"type": "number"},
                    },
                    required=["deltaX", "deltaY"],
                ),
            ]
        )
    return tools


def _devtools_tools(client: _ScriptedMcpClient) -> list[Tool]:
    return [
        _make_mcp_tool(
            client, server_name="chrome-devtools", remote_name="list_pages", safety="read"
        ),
        _make_mcp_tool(
            client,
            server_name="chrome-devtools",
            remote_name="evaluate_script",
            properties={
                "pageId": {"type": "number"},
                "function": {"type": "string"},
                "format": {"type": "string"},
            },
            required=["pageId"],
        ),
        _make_mcp_tool(
            client,
            server_name="chrome-devtools",
            remote_name="type_text",
            properties={"pageId": {"type": "number"}, "text": {"type": "string"}},
            required=["pageId", "text"],
        ),
        _make_mcp_tool(
            client,
            server_name="chrome-devtools",
            remote_name="press_key",
            properties={"pageId": {"type": "number"}, "key": {"type": "string"}},
            required=["pageId", "key"],
        ),
        _make_mcp_tool(
            client,
            server_name="chrome-devtools",
            remote_name="navigate_page",
            properties={
                "pageId": {"type": "number"},
                "type": {"type": "string"},
                "url": {"type": "string"},
            },
            required=["pageId"],
        ),
        _make_mcp_tool(
            client,
            server_name="chrome-devtools",
            remote_name="take_screenshot",
            properties={"pageId": {"type": "number"}, "format": {"type": "string"}},
            required=["pageId"],
            safety="read",
        ),
    ]


def test_playwright_standard_actions_and_safe_type_template(tmp_path: Path) -> None:
    def handler(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "browser_take_screenshot":
            assert args == {"scale": "css"}
            return CallToolResult(
                content=[
                    TextContent(text="- Page URL: https://app.example/form\n- Page Title: Form"),
                    ImageContent(data=PNG_B64, mimeType="image/png"),
                ]
            )
        return CallToolResult(
            content=[TextContent(text="- Page URL: https://app.example/form\n- Page Title: Form")]
        )

    client = _ScriptedMcpClient(handler)
    tools = _playwright_tools(client)
    tracker = BrowserHandoffTracker()
    controller = McpBrowserController()
    store = _image_store(tmp_path)
    context = ToolContext(cwd=tmp_path, protected_paths=())
    secret_payload = "p@ss'; alert({1}); //"

    async def scenario() -> None:
        for cmd in (
            BrowserInteraction(
                interaction_id="c1",
                action="click",
                x=42,
                y=84,
                button="right",
                double_click=True,
            ),
            BrowserInteraction(
                interaction_id="c2", action="scroll", x=42, y=84, delta_x=0, delta_y=-240
            ),
            BrowserInteraction(interaction_id="c3", action="type", text=secret_payload),
            BrowserInteraction(
                interaction_id="c4", action="press_key", key="r", modifiers=("Control",)
            ),
            BrowserInteraction(interaction_id="c5", action="back"),
            BrowserInteraction(interaction_id="c6", action="reload"),
            BrowserInteraction(interaction_id="c7", action="refresh"),
        ):
            res = await controller.execute(
                cmd,
                tools=tools,
                context=context,
                policy=ToolPolicy.allow_all_tools(),
                image_store=store,
                tracker=tracker,
            )
            tracker.record_user_action(
                cmd.action, server_name=res.server_name, metadata=res.metadata
            )
            assert res.viewport_event is not None
            assert res.viewport_event.image is not None
            assert res.viewport_event.source_tool == f"browser_interact:{cmd.action}"

    anyio.run(scenario)

    called_names = [name for name, _ in client.calls]
    assert called_names == [
        "browser_evaluate",
        "browser_take_screenshot",
        "browser_evaluate",
        "browser_take_screenshot",
        "browser_evaluate",
        "browser_take_screenshot",
        "browser_press_key",
        "browser_take_screenshot",
        "browser_navigate_back",
        "browser_take_screenshot",
        "browser_evaluate",
        "browser_take_screenshot",
        "browser_take_screenshot",
    ]
    type_args = client.calls[4][1]
    assert type_args is not None
    assert json.dumps(secret_payload) in type_args["function"]
    press_args = client.calls[6][1]
    assert press_args == {"key": "Control+r"}

    snap = tracker.pending_snapshot()
    assert snap is not None
    assert (snap.clicks, snap.scrolls, snap.text_entries, snap.key_presses, snap.navigations) == (
        1,
        1,
        1,
        1,
        2,
    )
    note = tracker.format_note(snap)
    assert note is not None
    assert secret_payload not in note
    assert "text entry" in note
    assert "https://app.example/form" in note


def test_playwright_vision_prefers_coordinate_tools(tmp_path: Path) -> None:
    client = _ScriptedMcpClient(
        lambda name, args: CallToolResult(
            content=[ImageContent(data=PNG_B64, mimeType="image/png")]
            if name == "browser_take_screenshot"
            else [TextContent(text="ok")]
        )
    )
    tools = _playwright_tools(client, vision=True)
    tracker = BrowserHandoffTracker()
    controller = McpBrowserController()

    async def scenario() -> None:
        await controller.execute(
            BrowserInteraction(
                interaction_id="c1",
                action="click",
                x=10,
                y=20,
                button="middle",
                double_click=True,
            ),
            tools=tools,
            context=ToolContext(cwd=tmp_path, protected_paths=()),
            policy=ToolPolicy.allow_all_tools(),
            image_store=_image_store(tmp_path),
            tracker=tracker,
        )
        await controller.execute(
            BrowserInteraction(
                interaction_id="c2", action="scroll", x=10, y=20, delta_x=15, delta_y=-120
            ),
            tools=tools,
            context=ToolContext(cwd=tmp_path, protected_paths=()),
            policy=ToolPolicy.allow_all_tools(),
            image_store=_image_store(tmp_path),
            tracker=tracker,
        )

    anyio.run(scenario)

    assert client.calls[0] == (
        "browser_mouse_click_xy",
        {"x": 10, "y": 20, "button": "middle", "clickCount": 2},
    )
    assert client.calls[2] == ("browser_mouse_wheel", {"deltaX": 15, "deltaY": -120})


def test_devtools_resolves_page_id_once_and_uses_native_tools(tmp_path: Path) -> None:
    def handler(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "list_pages":
            return CallToolResult(
                content=[TextContent(text="0: https://a.test\n1: https://b.test [selected]")]
            )
        if name == "take_screenshot":
            return CallToolResult(content=[ImageContent(data=PNG_B64, mimeType="image/png")])
        return CallToolResult(content=[TextContent(text="ok")])

    client = _ScriptedMcpClient(handler)
    tools = _devtools_tools(client)
    tracker = BrowserHandoffTracker()
    controller = McpBrowserController()

    async def scenario() -> None:
        for cmd in (
            BrowserInteraction(interaction_id="d1", action="click", x=12, y=34),
            BrowserInteraction(interaction_id="d2", action="type", text="hello"),
            BrowserInteraction(interaction_id="d3", action="press_key", key=" "),
            BrowserInteraction(interaction_id="d4", action="reload"),
        ):
            await controller.execute(
                cmd,
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_all_tools(),
                image_store=_image_store(tmp_path),
                tracker=tracker,
            )

    anyio.run(scenario)

    assert tracker.last_page_id == 1
    assert client.calls[0] == ("list_pages", {})
    assert client.calls[1][0] == "evaluate_script"
    assert client.calls[1][1]["pageId"] == 1
    assert client.calls[2] == ("take_screenshot", {"pageId": 1})
    assert client.calls[3] == ("type_text", {"pageId": 1, "text": "hello"})
    assert client.calls[5] == ("press_key", {"pageId": 1, "key": "Space"})
    assert client.calls[7] == ("navigate_page", {"pageId": 1, "type": "reload"})


def test_controller_policy_server_selection_and_error_redaction(tmp_path: Path) -> None:
    client_a = _ScriptedMcpClient(lambda name, args: RuntimeError("secret-typed-boom"))
    client_b = _ScriptedMcpClient(
        lambda name, args: CallToolResult(
            content=[ImageContent(data=PNG_B64, mimeType="image/png")]
        )
    )
    tools = [*_playwright_tools(client_a), *_devtools_tools(client_b)]
    controller = McpBrowserController()
    tracker = BrowserHandoffTracker()

    async def scenario() -> None:
        # Disconnected servers yield missing server error.
        with pytest.raises(BrowserControlError, match="No connected browser MCP server"):
            await controller.execute(
                BrowserInteraction(action="refresh"),
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_all_tools(),
                image_store=_image_store(tmp_path),
                tracker=tracker,
                connected=lambda _server: False,
            )

        # Multiple connected browser servers without an active server fails clearly.
        with pytest.raises(BrowserControlError, match="Multiple browser MCP servers"):
            await controller.execute(
                BrowserInteraction(action="refresh"),
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_all_tools(),
                image_store=_image_store(tmp_path),
                tracker=tracker,
            )

        # ToolPolicy must deny disallowed tools.
        tracker.observe_agent_tool("playwright", {}, "", None)
        with pytest.raises(BrowserControlError, match="Tool policy denies"):
            await controller.execute(
                BrowserInteraction(action="type", text="super-secret-value"),
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_tool_names(set()),
                image_store=_image_store(tmp_path),
                tracker=tracker,
            )

        # Unexpected exceptions must not echo secret text.
        with pytest.raises(BrowserControlError) as exc_info:
            await controller.execute(
                BrowserInteraction(action="type", text="super-secret-value"),
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_all_tools(),
                image_store=_image_store(tmp_path),
                tracker=tracker,
            )
        assert "super-secret-value" not in str(exc_info.value)

    anyio.run(scenario)


def test_controller_timeout_raises_browser_control_error(tmp_path: Path) -> None:
    async def slow(name: str, args: dict[str, Any] | None) -> CallToolResult:
        await anyio.sleep(0.2)
        return CallToolResult(content=[TextContent(text="late")])

    client = _ScriptedMcpClient(slow)
    tools = _playwright_tools(client)
    controller = McpBrowserController(timeout_seconds=0.01)

    async def scenario() -> None:
        with pytest.raises(BrowserControlError, match="timed out"):
            await controller.execute(
                BrowserInteraction(action="refresh"),
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_all_tools(),
                image_store=_image_store(tmp_path),
                tracker=BrowserHandoffTracker(),
            )

    anyio.run(scenario)


@pytest.mark.parametrize(
    ("exposure", "codemode"),
    [
        ("direct", "never"),
        ("on_demand", "never"),
        ("on_demand", "always"),
    ],
)
def test_session_injects_browser_guidance_across_modes_and_omits_without_browser(
    tmp_path: Path,
    exposure: str,
    codemode: str,
) -> None:
    client = _ScriptedMcpClient(lambda name, args: CallToolResult(content=[TextContent(text="ok")]))
    registry = ToolRegistry()
    for tool in _playwright_tools(client):
        registry.register(tool)

    provider = CapturingProvider()
    with_browser = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        mcp_tool_exposure=exposure,  # type: ignore[arg-type]
        codemode=codemode,  # type: ignore[arg-type]
    )
    disconnected = CodingSession(
        provider=CapturingProvider(),
        sessions=JsonlSessionStore(tmp_path / "sessions-disc"),
        tool_registry=registry,
        mcp_tool_exposure=exposure,  # type: ignore[arg-type]
        codemode=codemode,  # type: ignore[arg-type]
        mcp_server_connected=lambda _name: False,
    )
    without_browser = CodingSession(
        provider=CapturingProvider(),
        sessions=JsonlSessionStore(tmp_path / "sessions-plain"),
        mcp_tool_exposure=exposure,  # type: ignore[arg-type]
        codemode=codemode,  # type: ignore[arg-type]
    )

    assert browser_prompt_guidance(registry.all())
    assert browser_prompt_guidance(registry.all(), connected=lambda _s: False) == ""

    anyio.run(lambda: _drain(with_browser.run("check page")))
    assert provider.seen_messages is not None
    browser_msgs = [m for m in provider.seen_messages if "[BROWSER MCP WORKFLOW]" in m.content]
    assert len(browser_msgs) == 1
    assert "/browser" in browser_msgs[0].content

    disc_provider = disconnected.provider
    assert isinstance(disc_provider, CapturingProvider)
    anyio.run(lambda: _drain(disconnected.run("check page")))
    assert disc_provider.seen_messages is not None
    assert not any("[BROWSER MCP WORKFLOW]" in m.content for m in disc_provider.seen_messages)

    plain_provider = without_browser.provider
    assert isinstance(plain_provider, CapturingProvider)
    anyio.run(lambda: _drain(without_browser.run("plain task")))
    assert plain_provider.seen_messages is not None
    assert not any("[BROWSER MCP WORKFLOW]" in m.content for m in plain_provider.seen_messages)


def test_browser_handoff_note_attaches_once_and_restores_on_rollback(tmp_path: Path) -> None:
    provider = CapturingProvider()
    sessions = JsonlSessionStore(tmp_path / "sessions")
    session = sessions.create()
    agent = CodingSession(provider=provider, sessions=sessions)
    agent.browser_handoff.record_user_action(
        "click",
        server_name="playwright",
        metadata=BrowserMetadata(url="https://example.test/auth", title="Login"),
    )
    agent.browser_handoff.record_user_action(
        "type",
        server_name="playwright",
        metadata=BrowserMetadata(url="https://example.test/auth", title="Login"),
    )

    anyio.run(lambda: _drain(agent.run("I signed in", session=session, operation_id="op-1")))
    assert provider.seen_messages is not None
    last_user = provider.seen_messages[-1]
    assert last_user.role == "user"
    assert "1 click, text entry" in last_user.content
    assert "https://example.test/auth" in last_user.content
    assert "I signed in" in last_user.content

    # Roll back op-1 as cancelled run would: note must return for next run.
    agent.restore_browser_handoff("op-1")
    # Calling again is a no-op (single restore).
    agent.restore_browser_handoff("op-1")
    assert agent.browser_handoff.pending_snapshot() is not None

    anyio.run(
        lambda: _drain(agent.run("retry turn", session=sessions.create(), operation_id="op-2"))
    )
    assert provider.seen_messages is not None
    assert "1 click, text entry" in provider.seen_messages[-1].content
    assert agent.browser_handoff.pending_snapshot() is None

    # Next turn carries no handoff note unless new interactions happened.
    anyio.run(
        lambda: _drain(agent.run("follow up", session=sessions.create(), operation_id="op-3"))
    )
    assert provider.seen_messages is not None
    assert provider.seen_messages[-1].content == "follow up"

    # Reset clears pending interactions.
    agent.browser_handoff.record_user_action(
        "click", server_name="playwright", metadata=BrowserMetadata()
    )
    agent.reset_session_state()
    assert agent.browser_handoff.pending_snapshot() is None


def test_executor_observes_browser_tool_even_without_progress_sink(tmp_path: Path) -> None:
    provider = ToolLoopProvider(
        turns=[
            [
                ToolCall(
                    "c1",
                    "mcp__chrome-devtools__evaluate_script",
                    {"pageId": 3, "function": "() => 1"},
                )
            ],
            ["done"],
        ]
    )
    client = _ScriptedMcpClient(lambda name, args: CallToolResult(content=[TextContent(text="1")]))
    registry = ToolRegistry()
    for tool in _devtools_tools(client):
        registry.register(tool)
    agent = CodingSession(
        provider=provider,
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=registry,
        context_reserve_tokens=0,
    )
    agent.tool_approval_policy = agent.tool_approval_policy.approve_all()

    anyio.run(lambda: _drain(agent.run("inspect")))
    assert agent.browser_handoff.active_server_name == "chrome-devtools"
    assert agent.browser_handoff.last_page_id == 3


def test_post_mutation_screenshot_failures_and_cancellation_preserve_handoff(
    tmp_path: Path,
) -> None:
    # 1) Screenshot times out after click mutated page -> still succeeds without raising.
    async def timeout_shot(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "browser_take_screenshot":
            await anyio.sleep(0.1)
        return CallToolResult(content=[TextContent(text="- Page URL: https://after-click.test")])

    client_timeout = _ScriptedMcpClient(timeout_shot)
    tools_timeout = _playwright_tools(client_timeout)
    tracker = BrowserHandoffTracker()
    controller = McpBrowserController(timeout_seconds=0.02)

    result = anyio.run(
        lambda: controller.execute(
            BrowserInteraction(action="click", x=10, y=20),
            tools=tools_timeout,
            context=ToolContext(cwd=tmp_path, protected_paths=()),
            policy=ToolPolicy.allow_all_tools(),
            image_store=_image_store(tmp_path),
            tracker=tracker,
        )
    )
    assert result.viewport_event is not None
    assert result.viewport_event.image is None

    # 2) Post-mutation screenshot fails on list_pages / pageId -> still succeeds cleanly.
    def failing_page_id(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "list_pages":
            return CallToolResult(content=[TextContent(text="no pages")])
        return CallToolResult(content=[TextContent(text="ok")])

    client_page_err = _ScriptedMcpClient(failing_page_id)
    devtools_partial = (
        _make_mcp_tool(
            client_page_err,
            server_name="devtools",
            remote_name="type_text",
            safety="mutating",
            properties={"text": {"type": "string"}},
            required=["text"],
        ),
        _make_mcp_tool(
            client_page_err,
            server_name="devtools",
            remote_name="take_screenshot",
            safety="read",
            properties={"pageId": {"type": "integer"}},
            required=["pageId"],
        ),
        _make_mcp_tool(
            client_page_err,
            server_name="devtools",
            remote_name="list_pages",
            safety="read",
            properties={},
        ),
    )
    result2 = anyio.run(
        lambda: McpBrowserController().execute(
            BrowserInteraction(action="type", text="abc"),
            tools=devtools_partial,
            context=ToolContext(cwd=tmp_path, protected_paths=()),
            policy=ToolPolicy.allow_all_tools(),
            image_store=_image_store(tmp_path),
            tracker=BrowserHandoffTracker(),
        )
    )
    assert result2.source_tool.endswith("type_text")

    # 3) External cancellation during post-mutation screenshot records handoff before propagating.
    tracker_cancel = BrowserHandoffTracker()
    shot_started = anyio.Event()

    async def cancel_shot(name: str, args: dict[str, Any] | None) -> CallToolResult:
        if name == "browser_take_screenshot":
            shot_started.set()
            await anyio.sleep(5)
        return CallToolResult(content=[TextContent(text="- Page URL: https://cancel.test/")])

    cancel_client = _ScriptedMcpClient(cancel_shot)
    cancel_tools = _playwright_tools(cancel_client)

    async def cancel_scenario() -> None:
        cancelled_raised = False
        async with anyio.create_task_group() as tg:
            scope = anyio.CancelScope()

            async def worker() -> None:
                nonlocal cancelled_raised
                with scope:
                    try:
                        await McpBrowserController().execute(
                            BrowserInteraction(action="click", x=5, y=6),
                            tools=cancel_tools,
                            context=ToolContext(cwd=tmp_path, protected_paths=()),
                            policy=ToolPolicy.allow_all_tools(),
                            image_store=_image_store(tmp_path),
                            tracker=tracker_cancel,
                        )
                    except anyio.get_cancelled_exc_class():
                        cancelled_raised = True
                        raise

            tg.start_soon(worker)
            await shot_started.wait()
            scope.cancel()
        assert cancelled_raised

    anyio.run(cancel_scenario)
    snap = tracker_cancel.pending_snapshot()
    assert snap is not None
    assert snap.clicks == 1
    assert snap.last_url == "https://cancel.test/"


def test_native_tools_with_unsupported_required_params_fall_back_to_evaluate(
    tmp_path: Path,
) -> None:
    client = _ScriptedMcpClient(
        lambda name, args: CallToolResult(
            content=[ImageContent(data=PNG_B64, mimeType="image/png")]
        )
    )
    tools = (
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_mouse_click_xy",
            safety="mutating",
            properties={
                "element": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
            required=["element", "x", "y"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_mouse_wheel",
            safety="mutating",
            properties={
                "target": {"type": "string"},
                "deltaX": {"type": "number"},
                "deltaY": {"type": "number"},
            },
            required=["target", "deltaY"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="type_text",
            safety="mutating",
            properties={"selector": {"type": "string"}, "text": {"type": "string"}},
            required=["selector", "text"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_press_key",
            safety="mutating",
            properties={"selector": {"type": "string"}, "key": {"type": "string"}},
            required=["selector", "key"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="navigate_page",
            safety="mutating",
            properties={"url": {"type": "string"}},
            required=["url"],
        ),
        _make_mcp_tool(
            client,
            server_name="playwright",
            remote_name="browser_evaluate",
            safety="mutating",
            properties={"function": {"type": "string"}},
            required=["function"],
        ),
    )

    async def scenario() -> None:
        controller = McpBrowserController()
        for req in (
            BrowserInteraction(action="click", x=10, y=20),
            BrowserInteraction(action="scroll", x=10, y=20, delta_y=120),
            BrowserInteraction(action="type", text="abc"),
            BrowserInteraction(action="press_key", key="Enter"),
            BrowserInteraction(action="back"),
            BrowserInteraction(action="reload"),
        ):
            res = await controller.execute(
                req,
                tools=tools,
                context=ToolContext(cwd=tmp_path, protected_paths=()),
                policy=ToolPolicy.allow_all_tools(),
                image_store=_image_store(tmp_path),
                tracker=BrowserHandoffTracker(),
            )
            assert res.source_tool == "mcp__playwright__browser_evaluate"

    anyio.run(scenario)
    assert [name for name, _ in client.calls] == ["browser_evaluate"] * 6


@pytest.mark.parametrize("mode", ["preflight", "overflow"])
def test_browser_handoff_survives_compaction_and_overflow_retry(tmp_path: Path, mode: str) -> None:
    first_user = Message(role="user", content="turn one " + "a" * 3_500)
    first_assistant = Message(
        role="assistant", content="answer one " + "b" * 3_500, finish_reason="stop"
    )
    second_user = Message(role="user", content="turn two")
    second_assistant = Message(role="assistant", content="answer two", finish_reason="stop")
    if mode == "preflight":
        history = (first_user, first_assistant)
        provider = ScriptedProvider(
            [
                [
                    ProviderResponseStarted(model="model"),
                    ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
                ],
                [
                    ProviderResponseStarted(model="model"),
                    ProviderResponseCompleted(content="answer two"),
                ],
            ],
            default_model="model",
        )
        models = build_model_registry(context_window=4_000, auto_compact_token_limit=1_600)
    else:
        history = (first_user, first_assistant, second_user, second_assistant)
        provider = ScriptedProvider(
            [
                [ContextOverflowError("context_length_exceeded")],
                [
                    ProviderResponseStarted(model="model"),
                    ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY),
                ],
                [
                    ProviderResponseStarted(model="model"),
                    ProviderResponseCompleted(content="answer after overflow"),
                ],
            ],
            default_model="model",
        )
        models = build_model_registry(context_window=4_000, auto_compact_token_limit=None)

    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()

    async def scenario() -> None:
        for prior in history:
            await session.append_message(prior)
        agent = CodingSession(
            provider=provider,
            sessions=store,
            model="model",
            models=models,
            prompt_messages=(Message(role="system", content="system"),),
            context_reserve_tokens=100,
        )
        agent.browser_handoff.record_user_action(
            "click",
            server_name="playwright",
            metadata=BrowserMetadata(url="https://compact.test", title="Compact"),
        )
        await _drain(agent.run("continue now", session=session, history=history))
        assert agent.browser_handoff.pending_snapshot() is None

    anyio.run(scenario)
    final_call_messages = provider.calls[-1].messages
    assert final_call_messages[-1].role == "user"
    assert "1 click" in final_call_messages[-1].content
    assert "https://compact.test" in final_call_messages[-1].content
    assert "continue now" in final_call_messages[-1].content


def test_move_into_new_project_clears_browser_state(tmp_path: Path) -> None:
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"
    project_a.mkdir()
    project_b.mkdir()
    agent = CodingSession(
        provider=CapturingProvider(),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_context=ToolContext(cwd=project_a, protected_paths=()),
        project_context_root=project_a,
    )
    agent.browser_handoff.observe_agent_tool("devtools", {"pageId": 7}, "", None)
    agent.browser_handoff.record_user_action(
        "click", server_name="devtools", metadata=BrowserMetadata(url="https://a.test")
    )

    # Moving within the same project root keeps browser state.
    sub_a = project_a / "sub"
    sub_a.mkdir()
    agent.move_into(sub_a, project_root=project_a)
    assert agent.browser_handoff.last_page_id == 7
    assert agent.browser_handoff.pending_snapshot() is not None

    # Crossing into another project root clears server/page tracking and handoff actions.
    agent.move_into(project_b, project_root=project_b)
    assert agent.browser_handoff.active_server_name is None
    assert agent.browser_handoff.last_page_id is None
    assert agent.browser_handoff.pending_snapshot() is None


async def _drain(gen: Any) -> list[object]:
    return [item async for item in gen]
