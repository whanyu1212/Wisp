"""Tests for HY-559: agent live-browser tools, guidance, window mode, handoff, and boundaries."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import anyio
import pytest

from wisp.coding.browser_control import BrowserControlError, browser_prompt_guidance
from wisp.coding.browser_live import (
    LiveBrowser,
    LiveBrowserElement,
    LiveBrowserPageSnapshot,
)
from wisp.coding.browser_tools import (
    LIVE_BROWSER_TOOL_NAMES,
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserReadTool,
    BrowserScreenshotTool,
    BrowserTypeTool,
    build_live_browser_tools,
)
from wisp.coding.session import CodingSession
from wisp.coding.tool_execution import ConfiguredToolExecutor
from wisp.config.runtime import WispConfig
from wisp.config.settings import BrowserDefault, resolve_settings
from wisp.events import BrowserViewportUpdated, WispEvent
from wisp.mcp.discovery import McpCatalogTool
from wisp.providers.base import Provider
from wisp.rpc.commands import BrowserInteractCommand
from wisp.rpc.handlers.browser import start_rpc_browser_interact_command
from wisp.runtime.registry import ToolRegistry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalDecision, ToolApprovalPolicy
from wisp.tools.context import ToolContext
from wisp.tools.files.operations import ReadTool, WriteTool
from wisp.tools.policy import ToolPolicy
from wisp.tools.result import ToolArgumentError, ToolError

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_TINY_PNG_B64 = base64.b64encode(_TINY_PNG).decode("ascii")


class _FakeLiveBrowser:
    """In-memory fake of ``LiveBrowser`` for agent tool, window-mode, and handoff tests."""

    def __init__(self, profile_dir: Path, *, available: bool = True) -> None:
        self.profile_dir = profile_dir
        self._available = available
        self.is_open = False
        self.window_mode = False
        self.current_url: str | None = None
        self.current_title: str | None = None
        self.cookies: dict[str, str] = {}
        self.page_text = "Sign in required"
        self.elements: tuple[LiveBrowserElement, ...] = (
            LiveBrowserElement(ref="e1", role="button", name="Continue", tag="button", x=100, y=50),
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def is_available(self) -> bool:
        return self._available

    async def open(self, url: str, *, window: bool = False) -> BrowserViewportUpdated:
        self.calls.append(("open", {"url": url, "window": window}))
        self.is_open = True
        self.window_mode = window
        self.current_url = url
        self.current_title = "Example App"
        return self._event()

    async def switch_mode(self, *, window: bool, url: str | None = None) -> BrowserViewportUpdated:
        target = url or self.current_url
        if target is None:
            raise BrowserControlError("Specify a URL to open the live browser")
        self.calls.append(("switch_mode", {"window": window, "url": target}))
        self.is_open = True
        self.window_mode = window
        self.current_url = target
        self.current_title = "Example App"
        return self._event()

    async def read_page(
        self, *, max_chars: int = 12_000, max_elements: int = 60
    ) -> LiveBrowserPageSnapshot:
        del max_chars, max_elements
        if not self.is_open:
            raise BrowserControlError("The live browser is closed")
        self.calls.append(("read_page", {}))
        return LiveBrowserPageSnapshot(
            url=self.current_url,
            title=self.current_title,
            text=self.page_text,
            elements=self.elements,
            window_mode=self.window_mode,
        )

    async def click_element(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        double_click: bool = False,
    ) -> BrowserViewportUpdated | None:
        self.calls.append(
            (
                "click_element",
                {
                    "ref": ref,
                    "selector": selector,
                    "x": x,
                    "y": y,
                    "button": button,
                    "double_click": double_click,
                },
            )
        )
        return self._event()

    async def type_text(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        clear: bool = False,
        press_enter: bool = False,
    ) -> BrowserViewportUpdated | None:
        self.calls.append(
            (
                "type_text",
                {
                    "text": text,
                    "ref": ref,
                    "selector": selector,
                    "clear": clear,
                    "press_enter": press_enter,
                },
            )
        )
        return self._event()

    async def capture_screenshot(self) -> tuple[bytes, str | None, str | None]:
        self.calls.append(("capture_screenshot", {}))
        return _TINY_PNG, self.current_url, self.current_title

    async def interact(self, request: Any) -> None:
        self.calls.append(("interact", {"action": request.action}))
        if request.action == "type":
            self.cookies["session"] = "signed-in"
            self.page_text = "Dashboard: Welcome back"

    async def poll_frame(self) -> BrowserViewportUpdated | None:
        return self._event()

    def _event(self) -> BrowserViewportUpdated:
        if self.window_mode:
            return BrowserViewportUpdated(
                url=self.current_url,
                title=self.current_title,
                window_mode=True,
            )
        return BrowserViewportUpdated(
            live_png=_TINY_PNG_B64,
            url=self.current_url,
            title=self.current_title,
            viewport_width=900,
            viewport_height=600,
            live_device_scale=1,
        )


class _FakeBrowserMcpTool:
    name = "mcp__chrome__browser_navigate"
    server_name = "chrome"
    remote_name = "browser_navigate"
    description = "Navigate the user's Chrome browser."
    input_schema: dict[str, object] = {"type": "object", "properties": {"url": {"type": "string"}}}
    safety = "mutating"

    async def run(self, arguments: Any, context: ToolContext) -> Any:
        del arguments, context
        raise AssertionError("not called")


def _fake_browser_mcp_tool() -> McpCatalogTool:
    return _FakeBrowserMcpTool()  # type: ignore[return-value]


class _DummyProvider(Provider):
    name = "dummy"
    default_model = "dummy-1"

    async def stream(self, request: Any) -> Any:
        del request
        if False:
            yield


def test_guidance_omitted_when_no_browser_available() -> None:
    assert browser_prompt_guidance((), live_browser_available=False) == ""


def test_guidance_omits_preference_when_only_live_browser_available() -> None:
    guidance = browser_prompt_guidance((), live_browser_available=True, browser_default="live")
    assert "[LIVE BROWSER WORKFLOW]" in guidance
    assert "Default browser:" not in guidance
    assert "[BROWSER MCP WORKFLOW]" not in guidance


def test_guidance_omits_preference_when_only_mcp_browser_available() -> None:
    guidance = browser_prompt_guidance((_fake_browser_mcp_tool(),), live_browser_available=False)
    assert "[BROWSER MCP WORKFLOW]" in guidance
    assert "[LIVE BROWSER WORKFLOW]" not in guidance
    assert "Default browser:" not in guidance


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        ("live", "Default browser: Wisp's live browser"),
        ("mcp", "Default browser: the user's own Chrome"),
        ("none", None),
    ],
)
def test_guidance_preference_when_both_browsers_available(
    default: BrowserDefault, expected: str | None
) -> None:
    guidance = browser_prompt_guidance(
        (_fake_browser_mcp_tool(),),
        live_browser_available=True,
        browser_default=default,
    )
    assert "[LIVE BROWSER WORKFLOW]" in guidance
    assert "[BROWSER MCP WORKFLOW]" in guidance
    if expected is None:
        assert "Default browser:" not in guidance
    else:
        assert expected in guidance


def test_tool_descriptions_identify_wisp_live_browser(tmp_path: Path) -> None:
    fake: Any = _FakeLiveBrowser(tmp_path)
    tools = build_live_browser_tools(fake, is_trusted=lambda: True)
    assert {tool.name for tool in tools} == LIVE_BROWSER_TOOL_NAMES
    for tool in tools:
        assert "Wisp's built-in live browser" in tool.description


def test_tool_safety_classes_and_approval_modes(tmp_path: Path) -> None:
    fake: Any = _FakeLiveBrowser(tmp_path)
    tools = {tool.name: tool for tool in build_live_browser_tools(fake, is_trusted=lambda: True)}

    assert tools["browser_read"].safety == "read"
    assert tools["browser_screenshot"].safety == "read"
    assert tools["browser_navigate"].safety == "mutating"
    assert tools["browser_click"].safety == "mutating"
    assert tools["browser_type"].safety == "mutating"

    for policy in (
        ToolApprovalPolicy.require_approval(),
        ToolApprovalPolicy.auto_edits(),
        ToolApprovalPolicy.auto_commands(),
    ):
        assert policy.approves(tools["browser_read"])
        assert policy.approves(tools["browser_screenshot"])
        assert not policy.approves(tools["browser_navigate"])
        assert not policy.approves(tools["browser_click"])
        assert not policy.approves(tools["browser_type"])

    yolo = ToolApprovalPolicy.approve_all()
    for tool in tools.values():
        assert yolo.approves(tool)


def test_auto_edits_does_not_auto_approve_browser_mutations(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake: Any = _FakeLiveBrowser(tmp_path)
        nav = BrowserNavigateTool(fake, is_trusted=lambda: True)
        policy = ToolApprovalPolicy.auto_edits()
        decision: ToolApprovalDecision | None = await policy.evaluate_call(
            nav,
            arguments={"url": "https://example.com"},
            context=ToolContext(cwd=tmp_path),
        )
        assert decision is None

    anyio.run(scenario)


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "chrome://settings",
        "https://user:secret@example.com/",
        "https://example.com/\nfoo",
    ],
)
def test_navigate_rejects_unsafe_urls_before_approval(tmp_path: Path, bad_url: str) -> None:
    fake: Any = _FakeLiveBrowser(tmp_path)
    nav = BrowserNavigateTool(fake, is_trusted=lambda: True)
    assert nav.check_arguments({"url": bad_url}) is not None


def test_untrusted_project_blocks_live_browser_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake: Any = _FakeLiveBrowser(tmp_path)
        nav = BrowserNavigateTool(fake, is_trusted=lambda: False)
        with pytest.raises(ToolError) as exc_info:
            await nav.run({"url": "https://example.com"}, ToolContext(cwd=tmp_path))
        assert exc_info.value.failure_code == "policy_denied"
        assert fake.calls == []

    anyio.run(scenario)


def test_browser_profile_protected_even_when_protected_paths_empty(tmp_path: Path) -> None:
    config = WispConfig(protected_paths=()).model_copy(update={"protected_paths": ()})
    context = ToolContext.from_config(config, cwd=tmp_path)
    assert ".wisp/browser-profile" in context.protected_paths
    assert ".wisp/browser-profile/**" in context.protected_paths


def test_browser_default_setting_is_user_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".wisp").mkdir(parents=True)
    (project / ".wisp").mkdir(parents=True)
    (home / ".wisp" / "settings.json").write_text('{"browser_default": "live"}', encoding="utf-8")
    (project / ".wisp" / "settings.json").write_text('{"browser_default": "mcp"}', encoding="utf-8")

    resolved = resolve_settings(project_dir=project, home_dir=home, trust_project=True)
    assert resolved.browser_default == "live"


def test_window_mode_rejected_over_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1234 5.6.7.8 22")

    async def scenario() -> None:
        async with anyio.create_task_group() as tg:
            browser = LiveBrowser(tg, profile_dir=tmp_path / "profile")
            with pytest.raises(BrowserControlError, match="SSH"):
                await browser.open("https://example.com", window=True)

    anyio.run(scenario)


def test_agent_workflow_window_switch_and_user_sign_in_handoff(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake: Any = _FakeLiveBrowser(tmp_path / "profile")
        published: list[WispEvent] = []

        async def publish(event: WispEvent) -> None:
            published.append(event)

        nav = BrowserNavigateTool(fake, is_trusted=lambda: True, publish_viewport=publish)
        read = BrowserReadTool(fake, is_trusted=lambda: True)
        click = BrowserClickTool(fake, is_trusted=lambda: True, publish_viewport=publish)
        type_tool = BrowserTypeTool(fake, is_trusted=lambda: True, publish_viewport=publish)
        shot = BrowserScreenshotTool(fake, is_trusted=lambda: True)
        ctx = ToolContext(cwd=tmp_path)

        # 1. Agent opens page in terminal mode -> publishes BrowserViewportUpdated with source_tool.
        res = await nav.run({"url": "https://app.example.com"}, ctx)
        assert "terminal" in res.text
        assert isinstance(published[-1], BrowserViewportUpdated)
        assert published[-1].source_tool == "browser_navigate"
        assert published[-1].window_mode is False

        # 2. Agent reads page and switches to window mode for passkey/sign-in; cookies survive.
        snap = await read.run({}, ctx)
        assert "[e1] button 'Continue'" in snap.text
        await nav.run({"window": True}, ctx)
        assert fake.window_mode is True
        assert isinstance(published[-1], BrowserViewportUpdated)
        assert published[-1].window_mode is True

        # 3. User signs in via live browser takeover -> handoff note recorded without leaking text.
        session = CodingSession(
            provider=_DummyProvider(),
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            trusted=True,
            live_browser=fake,
        )
        send, receive = anyio.create_memory_object_stream[Any](4)
        async with anyio.create_task_group() as tg:
            start_rpc_browser_interact_command(
                BrowserInteractCommand(action="type", live=True, text="super-secret-password"),
                agent=session,
                runtime=None,  # type: ignore[arg-type]
                sessions=session.sessions,
                session_state=type("_State", (), {"session": None, "entry_count": 0})(),  # type: ignore[arg-type]
                mcp_servers=None,
                task_group=tg,
                send=send,
                write_event=lambda _ev: None,
                live_browser=fake,
            )
            completed = await receive.receive()
            assert completed.ok
        note = session.browser_handoff.format_note(session.browser_handoff.pending_snapshot())
        assert note is not None
        assert "text entry" in note
        assert "super-secret-password" not in note

        # 4. Dock back to terminal mode; sign-in cookie & updated page state survive.
        await nav.run({"window": False}, ctx)
        assert fake.window_mode is False
        assert fake.cookies["session"] == "signed-in"
        after = await read.run({}, ctx)
        assert "Dashboard: Welcome back" in after.text

        # 5. Click, type, and opt-in screenshot all work.
        await click.run({"ref": "e1"}, ctx)
        await type_tool.run({"text": "hello", "ref": "e1", "press_enter": True}, ctx)
        shot_res = await shot.run({}, ctx)
        assert shot_res.images == (_TINY_PNG,)

    anyio.run(scenario)


def _registry_with_builtins() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    return registry


def test_session_omits_live_browser_tools_when_untrusted_or_unavailable(tmp_path: Path) -> None:
    fake_available: Any = _FakeLiveBrowser(tmp_path, available=True)
    fake_unavailable: Any = _FakeLiveBrowser(tmp_path, available=False)

    untrusted = CodingSession(
        provider=_DummyProvider(),
        sessions=JsonlSessionStore(tmp_path / "s1"),
        tool_registry=_registry_with_builtins(),
        trusted=False,
        live_browser=fake_available,
    )
    reg_untrusted = untrusted._operation_tool_registry()
    assert reg_untrusted is not None
    assert not ({t.name for t in reg_untrusted.all()} & LIVE_BROWSER_TOOL_NAMES)

    unavailable = CodingSession(
        provider=_DummyProvider(),
        sessions=JsonlSessionStore(tmp_path / "s2"),
        tool_registry=_registry_with_builtins(),
        trusted=True,
        live_browser=fake_unavailable,
    )
    reg_unavailable = unavailable._operation_tool_registry()
    assert reg_unavailable is not None
    assert not ({t.name for t in reg_unavailable.all()} & LIVE_BROWSER_TOOL_NAMES)

    # An empty tool registry (no tools enabled) stays tool-less.
    toolless = CodingSession(
        provider=_DummyProvider(),
        sessions=JsonlSessionStore(tmp_path / "s-empty"),
        tool_registry=ToolRegistry(),
        trusted=True,
        live_browser=fake_available,
    )
    reg_toolless = toolless._operation_tool_registry()
    assert reg_toolless is not None
    assert reg_toolless.all() == ()

    trusted = CodingSession(
        provider=_DummyProvider(),
        sessions=JsonlSessionStore(tmp_path / "s3"),
        tool_registry=_registry_with_builtins(),
        trusted=True,
        live_browser=fake_available,
    )
    reg_trusted = trusted._operation_tool_registry()
    assert reg_trusted is not None
    assert LIVE_BROWSER_TOOL_NAMES.issubset({t.name for t in reg_trusted.all()})


def test_executor_denies_unapproved_browser_click_in_ask_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        fake: Any = _FakeLiveBrowser(tmp_path)
        registry = ToolRegistry()
        for tool in build_live_browser_tools(fake, is_trusted=lambda: True):
            registry.register(tool)
        approval_policy = ToolApprovalPolicy.require_approval()
        ConfiguredToolExecutor(
            registry=registry,
            context=ToolContext(cwd=tmp_path),
            policy=ToolPolicy.allow_all_tools(),
            approval_policy=approval_policy,
        )
        assert not approval_policy.approves(registry.get("browser_click"))
        assert approval_policy.approves(registry.get("browser_read"))
        with pytest.raises(ToolArgumentError):
            await registry.get("browser_click").run({}, ToolContext(cwd=tmp_path))

    anyio.run(scenario)


def test_session_exposes_live_browser_specs_and_guidance_after_set_live_browser(
    tmp_path: Path,
) -> None:
    fake: Any = _FakeLiveBrowser(tmp_path, available=True)
    session = CodingSession(
        provider=_DummyProvider(),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_registry=_registry_with_builtins(),
        trusted=True,
    )
    # Mirrors WispRpcHost.run_with_streams attaching LiveBrowser after construction.
    session.set_live_browser(fake)

    op_registry = session._operation_tool_registry()
    offered, _executor = session._run_tools(
        op_registry,
        None,
        context=ToolContext(cwd=tmp_path),
    )
    offered_names = {spec.name for spec in offered}
    assert LIVE_BROWSER_TOOL_NAMES.issubset(offered_names)

    prompt = session._prompt_messages(offered, registry=op_registry)
    assert any("[LIVE BROWSER WORKFLOW]" in msg.content for msg in prompt.messages)

    # Plan mode exposes only read-only live browser tools.
    session.mode = "plan"
    plan_offered, _ = session._run_tools(
        op_registry,
        None,
        context=ToolContext(cwd=tmp_path),
    )
    plan_names = {spec.name for spec in plan_offered}
    assert {"browser_read", "browser_screenshot"}.issubset(plan_names)
    assert not ({"browser_navigate", "browser_click", "browser_type"} & plan_names)

    # Explicit tools= restriction on CodingSession is preserved.
    restricted = CodingSession(
        provider=_DummyProvider(),
        sessions=JsonlSessionStore(tmp_path / "sessions-restricted"),
        tools=(),
        tool_registry=ToolRegistry(),
        trusted=True,
        live_browser=fake,
    )
    restricted_reg = restricted._operation_tool_registry()
    restricted_offered, _ = restricted._run_tools(
        restricted_reg,
        None,
        context=ToolContext(cwd=tmp_path),
    )
    assert restricted_offered == ()
