"""Tests for browser_interact RPC command validation, scheduling, and host execution."""

from __future__ import annotations

import base64
import errno
import fcntl
import json
import logging
import os
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any, Literal

import anyio
import pytest
from anyio.abc import TaskGroup
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpToolDefinition
from pydantic import ValidationError

from tests.coding.session_support import CapturingProvider
from tests.rpc.execution_support import dispatch_parsed
from tests.support.rpc import RecordingEventWriter, build_rpc_executor_fixture
from wisp.coding.browser_control import BrowserInteractionResult
from wisp.coding.browser_live import LiveBrowser
from wisp.coding.browser_profile import browser_profile
from wisp.coding.browser_recents import (
    RECENT_ADDRESSES_FILENAME,
    RecentAddressesBusyError,
    load_recent_addresses,
    record_recent_address,
)
from wisp.coding.browser_viewport import BrowserMetadata
from wisp.coding.session import CodingSession
from wisp.config.mcp_servers import McpServerConfig
from wisp.config.runtime import WispConfig
from wisp.config.settings import BrowserSearchEngine
from wisp.events import (
    BrowserViewportUpdated,
    ErrorEvent,
    LiveBrowserViewport,
    RpcBrowserAddressesReported,
    RpcCommandFinished,
    SessionDirectoryChanged,
    WispEvent,
)
from wisp.mcp.tool import adapt_mcp_tool
from wisp.messages import Message
from wisp.rpc import host as rpc_host
from wisp.rpc.commands import (
    BrowserInteractCommand,
    GetBrowserAddressesCommand,
    ParsedRpcCommand,
    ProcessWakeCommand,
    RpcCommandAdapter,
)
from wisp.rpc.coordinator import (
    RpcCoordinator,
    _bypasses_active_command,
    _RpcCommandCompleted,
    _RpcDispatchResult,
    _RpcInputClosed,
    _RpcInputCommand,
    _RpcProcessWake,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.handlers.browser import (
    handle_rpc_browser_addresses_command,
    start_rpc_browser_interact_command,
)
from wisp.rpc.host import InProcessOptions, RpcHost, build_session_runtime
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sessions.images import IMAGE_DIRECTORY_NAME, SessionImageStore
from wisp.sessions.jsonl import JsonlSessionStore

PNG = (Path(__file__).parents[1] / "fixtures" / "images" / "pixel.png").read_bytes()
PNG_B64 = base64.b64encode(PNG).decode("ascii")


def test_browser_interact_command_validation_and_secret_redaction() -> None:
    secret = "hunter2-secret-pass"
    parsed = RpcCommandAdapter.validate_json(
        json.dumps({"type": "browser_interact", "id": "b1", "action": "type", "text": secret})
    )
    assert isinstance(parsed, BrowserInteractCommand)
    assert secret not in repr(parsed)
    assert secret not in str(parsed)

    envelope = ParsedRpcCommand.from_known(parsed)
    assert "text" in envelope.provided_fields
    assert secret not in repr(envelope._payload)

    # Validation failures (both cross-field and field-level) must never leak typed text.
    with pytest.raises(ValidationError) as exc_info:
        RpcCommandAdapter.validate_json(
            json.dumps(
                {"type": "browser_interact", "action": "click", "x": 1, "y": 2, "text": secret}
            )
        )
    assert secret not in str(exc_info.value)

    oversized_secret = f"{secret}-" + ("x" * 1_100)
    with pytest.raises(ValidationError) as oversized_exc:
        RpcCommandAdapter.validate_json(
            json.dumps({"type": "browser_interact", "action": "type", "text": oversized_secret})
        )
    assert secret not in str(oversized_exc.value)

    # Reject zero-delta scroll, unsupported keys, control characters, and missing coords.
    for bad in (
        {
            "type": "browser_interact",
            "action": "scroll",
            "x": 1,
            "y": 2,
            "delta_x": 0,
            "delta_y": 0,
        },
        {"type": "browser_interact", "action": "press_key", "key": "NotAKey"},
        {
            "type": "browser_interact",
            "action": "press_key",
            "key": "a",
            "modifiers": ["Control", "Control"],
        },
        {"type": "browser_interact", "action": "type", "text": "ab\x00cd"},
        {"type": "browser_interact", "action": "click", "x": -1, "y": 5},
    ):
        with pytest.raises(ValidationError):
            RpcCommandAdapter.validate_python(bad)


def test_coordinator_rejects_browser_interact_while_busy_and_serializes_when_idle() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(session=None, history=(), entry_count=0))
        send, receive = anyio.create_memory_object_stream(16)
        dispatched: list[str] = []
        rejected: list[tuple[str | None, str]] = []
        cancel_scope = anyio.CancelScope()

        async def dispatch(command: ParsedRpcCommand, running: Any) -> Any:
            dispatched.append(command.command_id or "")
            from wisp.rpc.coordinator import _RpcDispatchResult, _RpcRunningCommand

            return _RpcDispatchResult(
                running_command=_RpcRunningCommand(
                    command_id=command.command_id or "",
                    command_type=command.command_type,  # type: ignore[arg-type]
                    cancel_scope=cancel_scope,
                )
            )

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            rejected.append((command.command_id, message))

        # Start an active prompt.
        prompt_cmd = ParsedRpcCommand.from_known(
            RpcCommandAdapter.validate_python({"type": "prompt", "id": "p1", "prompt": "hi"})
        )
        await coordinator.handle_event(
            _RpcInputCommand(prompt_cmd), dispatch=dispatch, reject=reject
        )
        assert dispatched == ["p1"]

        # Browser interact arriving while prompt is active is rejected immediately, never queued.
        click_busy = ParsedRpcCommand.from_known(
            BrowserInteractCommand(id="b-busy", action="click", x=10, y=20)
        )
        await coordinator.handle_event(
            _RpcInputCommand(click_busy), dispatch=dispatch, reject=reject
        )
        assert rejected == [
            ("b-busy", "Finish or cancel the active operation before interacting with the browser")
        ]
        assert not coordinator.queued_commands

        # Finish the prompt, then send two browser interactions back to back.
        from wisp.rpc.coordinator import _RpcCommandCompleted

        await coordinator.handle_event(
            _RpcCommandCompleted(
                command_id="p1",
                command_type="prompt",
                ok=True,
                history=(),
                entry_count=0,
            ),
            dispatch=dispatch,
            reject=reject,
        )
        b1 = ParsedRpcCommand.from_known(
            BrowserInteractCommand(id="b1", action="click", x=10, y=20)
        )
        b2 = ParsedRpcCommand.from_known(BrowserInteractCommand(id="b2", action="type", text="abc"))
        await coordinator.handle_event(_RpcInputCommand(b1), dispatch=dispatch, reject=reject)
        await coordinator.handle_event(_RpcInputCommand(b2), dispatch=dispatch, reject=reject)
        assert dispatched == ["p1", "b1"]
        assert [c.command_id for c in coordinator.queued_commands] == ["b2"]

        await send.aclose()
        await receive.aclose()

    anyio.run(scenario)


def test_handler_runs_without_selected_session_and_records_handoff(tmp_path: Path) -> None:
    class _FakeClient:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> CallToolResult:
            if name == "browser_take_screenshot":
                return CallToolResult(
                    content=[
                        TextContent(text="- Page URL: https://fresh.test/"),
                        ImageContent(data=PNG_B64, mimeType="image/png"),
                    ]
                )
            return CallToolResult(content=[TextContent(text="- Page URL: https://fresh.test/")])

    class _FakeMcpRuntime:
        def is_connected(self, server_name: str) -> bool:
            return server_name == "playwright"

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        server = McpServerConfig(
            name="playwright",
            command="playwright-mcp",
            tool_safety={"browser_take_screenshot": "read", "browser_evaluate": "mutating"},
        )
        client = _FakeClient()
        runtime: WispRuntime = await build_runtime()
        object.__setattr__(runtime, "mcp_runtime", _FakeMcpRuntime())
        runtime.tools.register(
            adapt_mcp_tool(
                server=server,
                tool=McpToolDefinition(
                    name="browser_take_screenshot",
                    description="screenshot",
                    inputSchema={
                        "type": "object",
                        "properties": {"scale": {"type": "string"}},
                        "required": ["scale"],
                    },
                ),
                client=client,
            )
        )
        runtime.tools.register(
            adapt_mcp_tool(
                server=server,
                tool=McpToolDefinition(
                    name="browser_evaluate",
                    description="eval",
                    inputSchema={
                        "type": "object",
                        "properties": {"function": {"type": "string"}},
                        "required": ["function"],
                    },
                ),
                client=client,
            )
        )
        try:
            sessions = JsonlSessionStore(tmp_path / "sessions")
            agent = CodingSession(
                provider=CapturingProvider(),
                sessions=sessions,
                tool_registry=runtime.tools,
            )
            session_state = _RpcSessionState(session=None, history=(), entry_count=0)
            events: list[WispEvent] = []
            send, receive = anyio.create_memory_object_stream(4)

            async with anyio.create_task_group() as tg:
                running = start_rpc_browser_interact_command(
                    BrowserInteractCommand(id="bi-fresh", action="click", x=25, y=35),
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=session_state,
                    mcp_servers=None,
                    task_group=tg,
                    send=send,
                    write_event=events.append,
                )
                assert running.command_id == "bi-fresh"
                completed = await receive.receive()
                assert completed.ok is True
            await send.aclose()
            await receive.aclose()

            viewport = next(e for e in events if isinstance(e, BrowserViewportUpdated))
            finished = next(e for e in events if isinstance(e, RpcCommandFinished))
            assert finished.ok is True
            assert viewport.image is not None
            assert (sessions.root / IMAGE_DIRECTORY_NAME).is_dir()
            assert (
                SessionImageStore(sessions.root / IMAGE_DIRECTORY_NAME).read(viewport.image) == PNG
            )

            snap = agent.browser_handoff.pending_snapshot()
            assert snap is not None
            assert snap.clicks == 1
            assert snap.last_url == "https://fresh.test/"
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_browser_interact_cancellation_reports_failed_completion(tmp_path: Path) -> None:
    started = anyio.Event()

    class _SlowController:
        async def execute(self, *args: object, **kwargs: object) -> BrowserInteractionResult:
            started.set()
            await anyio.sleep(5)
            return BrowserInteractionResult(
                viewport_event=None,
                server_name="playwright",
                source_tool="browser_evaluate",
                metadata=BrowserMetadata(),
            )

    async def scenario() -> None:
        runtime = await build_runtime()
        try:
            sessions = JsonlSessionStore(tmp_path / "sessions")
            agent = CodingSession(provider=CapturingProvider(), sessions=sessions)
            session_state = _RpcSessionState(session=None, history=(), entry_count=0)
            events: list[WispEvent] = []
            send, receive = anyio.create_memory_object_stream(4)

            async with anyio.create_task_group() as tg:
                running = start_rpc_browser_interact_command(
                    BrowserInteractCommand(id="bi-cancel", action="refresh"),
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=session_state,
                    mcp_servers=None,
                    task_group=tg,
                    send=send,
                    write_event=events.append,
                    controller=_SlowController(),  # type: ignore[arg-type]
                )
                await started.wait()
                running.cancel_scope.cancel()
                completed = await receive.receive()
                assert completed.ok is False
            await send.aclose()
            await receive.aclose()

            error_event = next(e for e in events if isinstance(e, ErrorEvent))
            finished = next(e for e in events if isinstance(e, RpcCommandFinished))
            assert "RPC command cancelled: bi-cancel" in error_event.message
            assert finished.ok is False
            assert finished.error == "RPC command cancelled: bi-cancel"
        finally:
            await runtime.aclose()

    anyio.run(scenario)


@pytest.mark.parametrize("trusted", [True, False])
def test_resize_respects_trust(tmp_path: Path, trusted: bool) -> None:
    viewport = LiveBrowserViewport(width=640, height=480, device_scale_factor=2)
    frame = BrowserViewportUpdated(
        live_png=PNG_B64, viewport_width=640, viewport_height=480, live_device_scale=2
    )
    resized: list[LiveBrowserViewport] = []

    class _Browser:
        async def resize(self, requested: LiveBrowserViewport) -> BrowserViewportUpdated:
            resized.append(requested)
            return frame

    async def scenario() -> None:
        runtime = await build_runtime()
        try:
            sessions = JsonlSessionStore(tmp_path / "sessions")
            agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=trusted)
            session_state = _RpcSessionState(session=None, history=(), entry_count=0)
            events: list[WispEvent] = []
            send, receive = anyio.create_memory_object_stream(4)
            async with send, receive, anyio.create_task_group() as tasks:
                start_rpc_browser_interact_command(
                    BrowserInteractCommand(action="live_resize", viewport=viewport),
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=session_state,
                    mcp_servers=None,
                    task_group=tasks,
                    send=send,
                    write_event=events.append,
                    live_browser=_Browser(),  # type: ignore[arg-type]
                )
                assert (await receive.receive()).ok == trusted
            assert isinstance(events[-1], RpcCommandFinished)
            assert events[-1].ok == trusted
            if trusted:
                assert events[-2] == frame
                assert resized == [viewport]
            else:
                assert "trusted project" in (events[-1].error or "")
                assert not resized
        finally:
            await runtime.aclose()

    anyio.run(scenario)


class _RecordingLiveBrowser:
    """Stands in for LiveBrowser without starting Chromium."""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.opened: list[str] = []

    async def open(self, url: str) -> BrowserViewportUpdated:
        self.opened.append(url)
        return BrowserViewportUpdated(url=url)

    async def navigate(self, url: str) -> None:
        self.opened.append(url)

    async def poll_frame(self) -> None:
        return None


@pytest.fixture
def browser_profile_dir(tmp_path: Path) -> Path:
    directory = tmp_path / ".wisp" / "browser-profile"
    with browser_profile(directory):
        pass
    return directory


def _run_live_command(
    tmp_path: Path,
    command: BrowserInteractCommand,
    browser: _RecordingLiveBrowser,
    *,
    search_engine: BrowserSearchEngine = "google",
    events: list[WispEvent] | None = None,
) -> RpcCommandFinished:
    events = [] if events is None else events

    async def scenario() -> RpcCommandFinished:
        runtime = await build_runtime()
        try:
            sessions = JsonlSessionStore(tmp_path / "sessions")
            agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=True)
            send, receive = anyio.create_memory_object_stream(4)
            async with send, receive, anyio.create_task_group() as tasks:
                start_rpc_browser_interact_command(
                    command,
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=_RpcSessionState(session=None, history=(), entry_count=0),
                    mcp_servers=None,
                    task_group=tasks,
                    send=send,
                    write_event=events.append,
                    live_browser=browser,  # type: ignore[arg-type]
                    search_engine=search_engine,
                )
                await receive.receive()
            finished = events[-1]
            assert isinstance(finished, RpcCommandFinished)
            return finished
        finally:
            await runtime.aclose()

    return anyio.run(scenario)


@pytest.mark.parametrize("action", ["live_open", "live_navigate"])
@pytest.mark.parametrize(
    ("engine", "url"),
    [
        ("google", "https://www.google.com/search?q=rust+traits"),
        ("duckduckgo", "https://duckduckgo.com/?q=rust+traits"),
        ("bing", "https://www.bing.com/search?q=rust+traits"),
    ],
)
def test_search_text_opens_engine(
    tmp_path: Path,
    browser_profile_dir: Path,
    action: Literal["live_open", "live_navigate"],
    engine: BrowserSearchEngine,
    url: str,
) -> None:
    browser = _RecordingLiveBrowser(browser_profile_dir)
    command = BrowserInteractCommand(action=action, url="rust traits")

    finished = _run_live_command(tmp_path, command, browser, search_engine=engine)

    assert finished.ok
    assert browser.opened == [url]
    assert load_recent_addresses(browser_profile_dir) == ()


@pytest.mark.parametrize("action", ["live_open", "live_navigate"])
def test_address_is_recorded(
    tmp_path: Path, browser_profile_dir: Path, action: Literal["live_open", "live_navigate"]
) -> None:
    browser = _RecordingLiveBrowser(browser_profile_dir)
    for text in ("example.com/docs", "localhost:3000"):
        command = BrowserInteractCommand(action=action, url=text)
        assert _run_live_command(tmp_path, command, browser).ok

    assert browser.opened == ["https://example.com/docs", "http://localhost:3000"]
    assert load_recent_addresses(browser_profile_dir) == (
        "http://localhost:3000",
        "https://example.com/docs",
    )


@pytest.mark.parametrize("action", ["live_open", "live_navigate"])
@pytest.mark.parametrize(
    "text",
    [
        "user:secret@example.com",
        "user:secret@host/path with spaces",
        "//user:secret@host/path",
        "localhost:65536",
        "https://host:abc",
    ],
)
def test_unsafe_address_is_rejected(
    tmp_path: Path,
    browser_profile_dir: Path,
    action: Literal["live_open", "live_navigate"],
    text: str,
) -> None:
    """Credentials and bad ports fail before the browser runs or a search is made."""
    browser = _RecordingLiveBrowser(browser_profile_dir)
    command = BrowserInteractCommand(action=action, url=text)

    finished = _run_live_command(tmp_path, command, browser)

    assert not finished.ok
    assert "HTTP or HTTPS URL" in (finished.error or "")
    assert "secret" not in (finished.error or "")
    assert browser.opened == []
    assert load_recent_addresses(browser_profile_dir) == ()


def test_unsaved_address_still_navigates(
    tmp_path: Path, browser_profile_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (browser_profile_dir / RECENT_ADDRESSES_FILENAME).mkdir()
    browser = _RecordingLiveBrowser(browser_profile_dir)
    command = BrowserInteractCommand(action="live_navigate", url="example.com/private-path")

    with caplog.at_level(logging.WARNING, logger="wisp.rpc.handlers.browser"):
        finished = _run_live_command(tmp_path, command, browser)

    assert finished.ok
    assert browser.opened == ["https://example.com/private-path"]
    assert str(browser_profile_dir / RECENT_ADDRESSES_FILENAME) in caplog.text
    assert "private-path" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EACCES, "sentinel-7f3a https://example.com/leaked"),
        RecentAddressesBusyError("sentinel-7f3a https://example.com/leaked"),
    ],
    ids=["os-error", "busy"],
)
def test_save_failure_log_is_redacted(
    tmp_path: Path,
    browser_profile_dir: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail(profile_dir: Path, url: str) -> None:
        raise error

    monkeypatch.setattr("wisp.rpc.handlers.browser.record_recent_address", fail)
    browser = _RecordingLiveBrowser(browser_profile_dir)
    command = BrowserInteractCommand(action="live_navigate", url="example.com/private-path")

    with caplog.at_level(logging.WARNING, logger="wisp.rpc.handlers.browser"):
        finished = _run_live_command(tmp_path, command, browser)

    assert finished.ok
    assert browser.opened == ["https://example.com/private-path"]
    assert str(browser_profile_dir / RECENT_ADDRESSES_FILENAME) in caplog.text
    assert type(error).__name__ in caplog.text
    assert "sentinel-7f3a" not in caplog.text
    assert "example.com" not in caplog.text


@pytest.mark.timeout(10)
def test_contended_save_still_opens(
    tmp_path: Path, browser_profile_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recents lock held by another process does not hold up the page."""
    monkeypatch.setattr("wisp.coding.browser_recents._LOCK_TIMEOUT_SECONDS", 0.1)
    lock = os.open(
        browser_profile_dir / f"{RECENT_ADDRESSES_FILENAME}.lock", os.O_CREAT | os.O_RDWR, 0o600
    )
    events: list[WispEvent] = []
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        browser = _RecordingLiveBrowser(browser_profile_dir)
        command = BrowserInteractCommand(action="live_open", url="example.com")
        finished = _run_live_command(tmp_path, command, browser, events=events)
    finally:
        os.close(lock)

    assert finished.ok
    assert [event.url for event in events if isinstance(event, BrowserViewportUpdated)] == [
        "https://example.com"
    ]
    assert load_recent_addresses(browser_profile_dir) == ()


def test_addresses_report_skips_bad_entries(tmp_path: Path, browser_profile_dir: Path) -> None:
    """An entry that cannot be encoded is dropped instead of failing the report."""
    (browser_profile_dir / RECENT_ADDRESSES_FILENAME).write_text(
        '["https://a.test/\\ud800", "https://b.test/"]', encoding="utf-8"
    )

    async def scenario() -> list[WispEvent]:
        sessions = JsonlSessionStore(tmp_path / "sessions")
        agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=True)
        events: list[WispEvent] = []
        await handle_rpc_browser_addresses_command(
            GetBrowserAddressesCommand(id="addresses-1"),
            agent=agent,
            live_browser=_RecordingLiveBrowser(browser_profile_dir),  # type: ignore[arg-type]
            write_event=events.append,
        )
        return events

    events = anyio.run(scenario)

    report = next(event for event in events if isinstance(event, RpcBrowserAddressesReported))
    assert report.addresses == ("https://b.test/",)
    assert isinstance(events[-1], RpcCommandFinished)
    assert events[-1].ok


@pytest.mark.parametrize("trusted", [True, False])
def test_addresses_report_respects_trust(
    tmp_path: Path, browser_profile_dir: Path, trusted: bool
) -> None:
    record_recent_address(browser_profile_dir, "https://example.com/")

    class _UnstartableBrowser:
        @property
        def profile_dir(self) -> Path:
            assert trusted, "an untrusted project read the saved addresses"
            return browser_profile_dir

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"report used the browser: {name}")

    async def scenario() -> list[WispEvent]:
        sessions = JsonlSessionStore(tmp_path / "sessions")
        agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=trusted)
        events: list[WispEvent] = []
        await handle_rpc_browser_addresses_command(
            GetBrowserAddressesCommand(id="addresses-1"),
            agent=agent,
            live_browser=_UnstartableBrowser(),  # type: ignore[arg-type]
            write_event=events.append,
        )
        return events

    events = anyio.run(scenario)

    report = next(event for event in events if isinstance(event, RpcBrowserAddressesReported))
    assert report.command_id == "addresses-1"
    assert report.trusted is trusted
    assert report.addresses == (("https://example.com/",) if trusted else ())
    assert isinstance(events[-1], RpcCommandFinished)
    assert events[-1].ok


def test_addresses_report_needs_live_browser(tmp_path: Path) -> None:
    async def scenario() -> list[WispEvent]:
        sessions = JsonlSessionStore(tmp_path / "sessions")
        agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=True)
        events: list[WispEvent] = []
        await handle_rpc_browser_addresses_command(
            GetBrowserAddressesCommand(id="addresses-1"),
            agent=agent,
            live_browser=None,
            write_event=events.append,
        )
        return events

    events = anyio.run(scenario)

    assert not any(isinstance(event, RpcBrowserAddressesReported) for event in events)
    assert isinstance(events[-1], RpcCommandFinished)
    assert not events[-1].ok


def test_executor_reports_addresses_during_a_run(tmp_path: Path, browser_profile_dir: Path) -> None:
    """The report bypasses an active prompt and leaves it running."""
    record_recent_address(browser_profile_dir, "https://example.com/")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.agent.trusted = True
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        try:
            send, receive = anyio.create_memory_object_stream(1)
            async with send, receive, anyio.create_task_group() as task_group:
                executor = fixture.executor(task_group=task_group, send=send)
                executor.live_browser = _RecordingLiveBrowser(browser_profile_dir)  # type: ignore[assignment]
                result = await dispatch_parsed(
                    executor, {"id": "addresses-1", "type": "get_browser_addresses"}, running
                )
        finally:
            await fixture.runtime.aclose()

        assert result.running_command is running
        report = fixture.events[1]
        assert isinstance(report, RpcBrowserAddressesReported)
        assert report.addresses == ("https://example.com/",)
        assert _bypasses_active_command("get_browser_addresses")

    anyio.run(scenario)


def test_executor_uses_configured_engine(tmp_path: Path, browser_profile_dir: Path) -> None:
    browser = _RecordingLiveBrowser(browser_profile_dir)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.agent.trusted = True
        try:
            send, receive = anyio.create_memory_object_stream(1)
            async with send, receive, anyio.create_task_group() as task_group:
                executor = fixture.executor(task_group=task_group, send=send)
                executor.live_browser = browser  # type: ignore[assignment]
                executor.browser_search_engine = "duckduckgo"
                await dispatch_parsed(
                    executor,
                    {"type": "browser_interact", "action": "live_open", "url": "wisp agent"},
                )
                assert (await receive.receive()).ok
        finally:
            await fixture.runtime.aclose()

    anyio.run(scenario)

    assert browser.opened == ["https://duckduckgo.com/?q=wisp+agent"]


def _parsed(payload: dict[str, object]) -> ParsedRpcCommand:
    return ParsedRpcCommand.from_known(RpcCommandAdapter.validate_python(payload))


class _CoordinatorDriver:
    """Feeds a running coordinator and records what it dispatched and rejected.

    Each dispatched command becomes a running command that stays outstanding until
    the test completes it.
    """

    def __init__(self, coordinator: RpcCoordinator) -> None:
        self.coordinator = coordinator
        self.dispatched: list[tuple[str, str | None]] = []
        self.scopes: dict[str, anyio.CancelScope] = {}
        self.rejected: list[tuple[str | None, str]] = []
        self._send, self._receive = anyio.create_memory_object_stream[Any](16)

    async def run(self) -> None:
        async with self._send, self._receive:
            await self.coordinator.run(self._receive, dispatch=self._dispatch, reject=self._reject)

    async def _dispatch(self, command: ParsedRpcCommand, running: Any) -> _RpcDispatchResult:
        command_id = command.command_id or ""
        self.dispatched.append((command_id, running.command_id if running else None))
        self.scopes[command_id] = anyio.CancelScope()
        return _RpcDispatchResult(
            running_command=_RpcRunningCommand(
                command_id,
                command.command_type,  # type: ignore[arg-type]
                self.scopes[command_id],
            ),
            should_shutdown=command.command_type == "shutdown",
        )

    async def _reject(self, command: ParsedRpcCommand, message: str) -> None:
        self.rejected.append((command.command_id, message))

    async def accept(self, payload: dict[str, object]) -> None:
        await self._send.send(_RpcInputCommand(_parsed(payload)))
        await anyio.wait_all_tasks_blocked()

    async def complete(self, command_id: str, command_type: str) -> None:
        await self._send.send(
            _RpcCommandCompleted(
                command_id=command_id,
                command_type=command_type,  # type: ignore[arg-type]
                ok=True,
                history=None,
                entry_count=0,
            )
        )
        await anyio.wait_all_tasks_blocked()


def _click(command_id: str) -> dict[str, object]:
    return {"type": "browser_interact", "id": command_id, "action": "click", "x": 1, "y": 1}


def _frame_read(command_id: str) -> dict[str, object]:
    return {"type": "browser_interact", "id": command_id, "action": "live_frame", "live": True}


def test_frame_read_runs_beside_other_work() -> None:
    """A frame wait never queues behind, blocks, or reorders other commands."""

    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(session=None, history=(), entry_count=0))
        driver = _CoordinatorDriver(coordinator)
        dispatched, accept, complete = driver.dispatched, driver.accept, driver.complete
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(driver.run)
            # The starvation regression: a frame read while a wheel command runs.
            await accept(_click("wheel-1"))
            await accept(_frame_read("frame-1"))
            await accept(_click("wheel-2"))
            assert dispatched == [("wheel-1", None), ("frame-1", "wheel-1")]
            assert coordinator.running_command is not None
            assert coordinator.running_command.command_id == "wheel-1"
            assert [queued.command_id for queued in coordinator.queued_commands] == ["wheel-2"]

            # Input still runs in order, and the pending frame read does not hold it back.
            await complete("wheel-1", "browser_interact")
            assert dispatched[-1] == ("wheel-2", None)
            await complete("wheel-2", "browser_interact")
            assert coordinator.running_command is None
            assert set(coordinator.auxiliary_commands) == {"frame-1"}

            # Nor does it hold back a settings reload, and it reads during a prompt.
            await accept({"type": "reload", "id": "reload-1"})
            assert dispatched[-1] == ("reload-1", None)
            await complete("reload-1", "reload")
            await accept({"type": "prompt", "id": "prompt-1", "prompt": "hi"})
            await complete("frame-1", "browser_interact")
            await accept(_frame_read("frame-2"))
            assert dispatched[-1] == ("frame-2", "prompt-1")
            assert coordinator.running_command.command_id == "prompt-1"
            assert driver.rejected == []

            # Shutdown cancels the wait and refuses new ones.
            await accept({"type": "shutdown", "id": "shutdown-1"})
            assert driver.scopes["frame-2"].cancel_called
            assert not driver.scopes["prompt-1"].cancel_called
            await accept(_frame_read("frame-3"))
            assert driver.rejected == [
                ("frame-3", "Live browser frames are unavailable during shutdown")
            ]
            await complete("frame-2", "browser_interact")
            await complete("prompt-1", "prompt")
            assert dispatched[-1] == ("shutdown-1", None)

    anyio.run(scenario)


def test_one_frame_read_at_a_time() -> None:
    """A second waiting read is turned away, so frame reads never fill the command queue."""

    async def scenario() -> None:
        coordinator = RpcCoordinator(
            _RpcSessionState(session=None, history=(), entry_count=0), max_queued_commands=2
        )
        driver = _CoordinatorDriver(coordinator)
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(driver.run)
            await driver.accept(_click("click-1"))
            await driver.accept(_frame_read("frame-1"))
            # The page is static, so the first read is still waiting.
            await driver.accept(_frame_read("frame-2"))
            await driver.accept(_click("click-2"))
            assert driver.rejected == [("frame-2", "A live browser frame read is already waiting")]
            assert set(coordinator.auxiliary_commands) == {"frame-1"}
            assert [queued.command_id for queued in coordinator.queued_commands] == ["click-2"]

            # Once the first read ends, the next one is accepted.
            await driver.complete("frame-1", "browser_interact")
            await driver.accept(_frame_read("frame-3"))
            assert driver.dispatched[-1] == ("frame-3", "click-1")
            assert len(driver.rejected) == 1
            tasks.cancel_scope.cancel()

    anyio.run(scenario)


def test_frame_read_ends_when_input_closes() -> None:
    async def scenario() -> None:
        coordinator = RpcCoordinator(_RpcSessionState(session=None, history=(), entry_count=0))
        scope = anyio.CancelScope()

        async def dispatch(command: ParsedRpcCommand, running: Any) -> _RpcDispatchResult:
            return _RpcDispatchResult(
                running_command=_RpcRunningCommand("frame-1", "browser_interact", scope)
            )

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            raise AssertionError(message)

        await coordinator.handle_event(
            _RpcInputCommand(
                _parsed({"type": "browser_interact", "id": "frame-1", "action": "live_frame"})
            ),
            dispatch=dispatch,
            reject=reject,
        )
        await coordinator.handle_event(_RpcInputClosed(), dispatch=dispatch, reject=reject)
        assert scope.cancel_called

    anyio.run(scenario)


class _WaitingLiveBrowser:
    """Stands in for LiveBrowser; ``next_frame`` returns what the test releases."""

    def __init__(self) -> None:
        self.waiting = anyio.Event()
        self.release = anyio.Event()
        self.frame: BrowserViewportUpdated | None = None
        self.reads = 0
        self.closed = False
        self.lifetime = object()

    async def next_frame(self) -> BrowserViewportUpdated | None:
        self.reads += 1
        self.waiting.set()
        await self.release.wait()
        return self.frame

    def is_current(self, lifetime: object) -> bool:
        return lifetime is self.lifetime and not self.closed


_LIVE_FRAME = BrowserViewportUpdated(
    live_png=PNG_B64, viewport_width=1, viewport_height=1, live_device_scale=1
)


class _OutcomeTaskGroup:
    """Runs tasks on a real task group and records whether each returned or was cancelled."""

    def __init__(self, tasks: TaskGroup) -> None:
        self._tasks = tasks
        self.outcomes: list[str] = []

    def start_soon(self, func: Callable[..., Awaitable[None]], *args: object) -> None:
        async def run() -> None:
            try:
                await func(*args)
            except anyio.get_cancelled_exc_class():
                self.outcomes.append("cancelled")
                raise
            self.outcomes.append("returned")

        self._tasks.start_soon(run)


def _start_frame_read(
    tmp_path: Path,
    browser: _WaitingLiveBrowser,
    events: list[WispEvent],
    *,
    trusted: bool = True,
    act: Callable[[_RpcRunningCommand, CodingSession], Awaitable[None]] | None = None,
) -> bool:
    """Run one frame read to its end, writing its events to ``events`` as they happen."""

    async def scenario() -> bool:
        runtime = await build_runtime()
        try:
            sessions = JsonlSessionStore(tmp_path / "sessions")
            agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=trusted)
            send, receive = anyio.create_memory_object_stream[Any](4)
            async with send, receive, anyio.create_task_group() as tasks:
                running = start_rpc_browser_interact_command(
                    BrowserInteractCommand(id="frame-1", action="live_frame", live=True),
                    agent=agent,
                    runtime=runtime,
                    sessions=sessions,
                    session_state=_RpcSessionState(session=None, history=(), entry_count=0),
                    mcp_servers=None,
                    task_group=tasks,
                    send=send,
                    write_event=events.append,
                    live_browser=browser,  # type: ignore[arg-type]
                )
                if act is not None:
                    await act(running, agent)
                completed = await receive.receive()
                return bool(completed.ok)
        finally:
            await runtime.aclose()

    return anyio.run(scenario)


def _finishes(events: list[WispEvent]) -> list[RpcCommandFinished]:
    return [event for event in events if isinstance(event, RpcCommandFinished)]


@pytest.mark.parametrize("closed", [False, True])
def test_frame_read_waits_for_frame(tmp_path: Path, closed: bool) -> None:
    """The read answers once a frame exists, or ends quietly if the browser was closed."""
    browser = _WaitingLiveBrowser()
    events: list[WispEvent] = []

    async def act(running: _RpcRunningCommand, agent: CodingSession) -> None:
        await browser.waiting.wait()
        await anyio.wait_all_tasks_blocked()
        assert _finishes(events) == []
        browser.frame = None if closed else _LIVE_FRAME
        browser.release.set()

    assert _start_frame_read(tmp_path, browser, events, act=act)
    answers = [
        type(event)
        for event in events
        if isinstance(event, BrowserViewportUpdated | RpcCommandFinished)
    ]
    assert answers == (
        [RpcCommandFinished] if closed else [BrowserViewportUpdated, RpcCommandFinished]
    )
    assert [finish.ok for finish in _finishes(events)] == [True]


@pytest.mark.parametrize("change", ["trust", "browser"])
def test_frame_rechecked_before_publishing(tmp_path: Path, change: str) -> None:
    """A frame is dropped if trust or the tab it came from ended while the read waited."""
    browser = _WaitingLiveBrowser()
    events: list[WispEvent] = []

    async def act(running: _RpcRunningCommand, agent: CodingSession) -> None:
        await browser.waiting.wait()
        if change == "trust":
            agent.trusted = False
        else:
            browser.closed = True
        browser.frame = _LIVE_FRAME
        browser.release.set()

    assert _start_frame_read(tmp_path, browser, events, act=act)
    assert not any(isinstance(event, BrowserViewportUpdated) for event in events)
    assert [finish.ok for finish in _finishes(events)] == [True]


def test_frame_read_needs_trust(tmp_path: Path) -> None:
    browser = _WaitingLiveBrowser()
    events: list[WispEvent] = []
    assert not _start_frame_read(tmp_path, browser, events, trusted=False)
    assert browser.reads == 0
    assert not any(isinstance(event, BrowserViewportUpdated) for event in events)
    assert isinstance(events[-1], RpcCommandFinished)
    assert "trusted project" in (events[-1].error or "")


def test_frame_read_cancels(tmp_path: Path) -> None:
    browser = _WaitingLiveBrowser()
    events: list[WispEvent] = []

    async def act(running: _RpcRunningCommand, agent: CodingSession) -> None:
        await browser.waiting.wait()
        running.cancel_scope.cancel()

    assert not _start_frame_read(tmp_path, browser, events, act=act)
    assert isinstance(events[-1], RpcCommandFinished)
    assert events[-1].error == "RPC command cancelled: frame-1"
    assert not any(isinstance(event, BrowserViewportUpdated) for event in events)


def test_host_cancellation_propagates(tmp_path: Path) -> None:
    """Cancelling the task that owns a read ends it with its terminal event, then propagates."""
    browser = _WaitingLiveBrowser()
    events: list[WispEvent] = []

    async def scenario() -> _OutcomeTaskGroup:
        runtime = await build_runtime()
        try:
            sessions = JsonlSessionStore(tmp_path / "sessions")
            agent = CodingSession(provider=CapturingProvider(), sessions=sessions, trusted=True)
            send, receive = anyio.create_memory_object_stream[Any](4)
            async with send, receive:
                async with anyio.create_task_group() as tasks:
                    owner = _OutcomeTaskGroup(tasks)
                    start_rpc_browser_interact_command(
                        BrowserInteractCommand(id="frame-1", action="live_frame", live=True),
                        agent=agent,
                        runtime=runtime,
                        sessions=sessions,
                        session_state=_RpcSessionState(session=None, history=(), entry_count=0),
                        mcp_servers=None,
                        task_group=owner,  # type: ignore[arg-type]
                        send=send,
                        write_event=events.append,
                        live_browser=browser,  # type: ignore[arg-type]
                    )
                    await browser.waiting.wait()
                    tasks.cancel_scope.cancel()
                assert not receive.receive_nowait().ok
            return owner
        finally:
            await runtime.aclose()

    owner = anyio.run(scenario)
    assert owner.outcomes == ["cancelled"]
    assert [finish.error for finish in _finishes(events)] == ["RPC command cancelled: frame-1"]


async def _wait_for(condition: Callable[[], bool]) -> None:
    with anyio.fail_after(5):
        while not condition():
            await anyio.sleep(0.001)


@pytest.mark.parametrize("untrusted_project", [False, True])
def test_session_switch_ends_frame_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, untrusted_project: bool
) -> None:
    """A read pending in a trusted session publishes nothing once another session is selected.

    The other session is in the same trusted project, or in a project never trusted.
    """
    launch, elsewhere = tmp_path / "launch", tmp_path / "elsewhere"
    launch.mkdir()
    elsewhere.mkdir()
    config = WispConfig(provider="fake", session_dir=tmp_path / "sessions")
    browsers: list[LiveBrowser] = []

    def open_browser(tasks: TaskGroup) -> LiveBrowser:
        # A tab whose capture is running and whose current frame was delivered.
        browser = LiveBrowser(tasks)
        browser._socket = object()  # type: ignore[assignment]
        browser._scope = anyio.CancelScope()
        browser._viewport = LiveBrowserViewport(width=1, height=1, device_scale_factor=1)
        browser._loader = browser._capture_loader = "page"
        browsers.append(browser)
        return browser

    monkeypatch.setattr(rpc_host, "LiveBrowser", open_browser)

    async def scenario() -> list[WispEvent]:
        other = JsonlSessionStore(config.session_dir).create()
        await other.append_message(Message(role="user", content="other work"))
        if untrusted_project:
            await other.append_event(
                SessionDirectoryChanged(
                    session_id=other.session_id, previous_cwd=launch, cwd=elsewhere
                )
            )
        writer = RecordingEventWriter()
        runtime = await build_session_runtime(config, connect_mcp=False)
        try:
            host = await RpcHost.create(
                config,
                runtime,
                options=InProcessOptions(
                    cwd=launch, project_context_root=launch, startup_trusted=True
                ),
                write_event=writer,
                render_events=writer.render_events,
                wake_by_default=False,
            )
            send, receive = anyio.create_memory_object_stream[Any](16)

            def finished(command_id: str) -> bool:
                return any(finish.command_id == command_id for finish in _finishes(writer.events))

            async with send, receive, anyio.create_task_group() as tasks:
                tasks.start_soon(
                    partial(host.run_with_streams, receive, send=send, task_group=tasks)
                )
                await send.send(_RpcInputCommand(_parsed(_frame_read("frame-1"))))
                await _wait_for(lambda: "frame-1" in host.coordinator.auxiliary_commands)
                await anyio.wait_all_tasks_blocked()
                select = {
                    "type": "select_session",
                    "id": "select-1",
                    "session_id": other.session_id,
                }
                await send.send(_RpcInputCommand(_parsed(select)))
                await _wait_for(lambda: finished("select-1"))
                assert host.agent.trusted is not untrusted_project
                # Chromium paints the old page again after the switch.
                browsers[0]._frame = PNG_B64
                browsers[0]._signal_change()
                await _wait_for(lambda: finished("frame-1"))
                await send.send(_RpcInputClosed())
            return writer.events
        finally:
            await runtime.aclose()

    events = anyio.run(scenario)
    assert not any(isinstance(event, BrowserViewportUpdated) for event in events)
    assert [finish.ok for finish in _finishes(events) if finish.command_id == "frame-1"] == [True]


def test_executor_reads_frames_during_input(tmp_path: Path) -> None:
    """A frame read starts its own command beside a running browser command."""

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        fixture.agent.trusted = True
        browser = _WaitingLiveBrowser()
        running = _RpcRunningCommand("click-1", "browser_interact", anyio.CancelScope())
        try:
            send, receive = anyio.create_memory_object_stream[Any](1)
            async with send, receive, anyio.create_task_group() as task_group:
                executor = fixture.executor(task_group=task_group, send=send)
                executor.live_browser = browser  # type: ignore[assignment]
                result = await dispatch_parsed(
                    executor,
                    {"id": "frame-1", "type": "browser_interact", "action": "live_frame"},
                    running,
                )
                assert result.running_command is not None
                assert result.running_command.command_id == "frame-1"
                await browser.waiting.wait()
                browser.release.set()
                assert (await receive.receive()).ok
        finally:
            await fixture.runtime.aclose()

    anyio.run(scenario)


@pytest.mark.parametrize(("pending", "wakes"), [("live_frame", True), ("files", False)])
def test_frame_read_lets_agent_wake(pending: str, wakes: bool) -> None:
    """A background process may wake the agent while a frame read waits, unlike other reads."""

    async def scenario() -> list[str]:
        dispatched: list[str] = []
        due = False
        send, receive = anyio.create_memory_object_stream[Any](8)

        def wake() -> ParsedRpcCommand:
            return ParsedRpcCommand.from_known(ProcessWakeCommand(id="wake-1"))

        async def dispatch(command: ParsedRpcCommand, running: Any) -> _RpcDispatchResult:
            command_id = command.command_id or ""
            dispatched.append(command_id)
            return _RpcDispatchResult(
                running_command=_RpcRunningCommand(
                    command_id,
                    command.command_type,  # type: ignore[arg-type]
                    anyio.CancelScope(),
                )
            )

        async def reject(command: ParsedRpcCommand, message: str) -> None:
            raise AssertionError(message)

        coordinator = RpcCoordinator(
            _RpcSessionState(session=None, history=(), entry_count=0),
            wake=wake,
            wake_due=lambda: due,
        )
        read = (
            {"type": "browser_interact", "id": "read-1", "action": "live_frame"}
            if pending == "live_frame"
            else {"type": "get_project_files", "id": "read-1"}
        )
        async with send, receive, anyio.create_task_group() as tasks:
            tasks.start_soon(partial(coordinator.run, receive, dispatch=dispatch, reject=reject))
            await send.send(_RpcInputCommand(_parsed(read)))
            await anyio.wait_all_tasks_blocked()
            due = True
            await send.send(_RpcProcessWake())
            await anyio.wait_all_tasks_blocked()
            tasks.cancel_scope.cancel()
        return dispatched

    assert anyio.run(scenario) == (["read-1", "wake-1"] if wakes else ["read-1"])
