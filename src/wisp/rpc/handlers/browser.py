"""RPC command handler for user-initiated interactive browser takeover."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream
from typing_extensions import TypeAliasType

from wisp.coding.browser_address import resolve_browser_address
from wisp.coding.browser_control import (
    BrowserControlError,
    BrowserController,
    BrowserImageStore,
    BrowserInteraction,
    McpBrowserController,
)
from wisp.coding.browser_live import LiveBrowser
from wisp.coding.browser_recents import (
    RECENT_ADDRESSES_FILENAME,
    RecentAddressesBusyError,
    load_recent_addresses,
    record_recent_address,
)
from wisp.coding.browser_viewport import BrowserMetadata
from wisp.coding.session import CodingSession
from wisp.config.runtime import DEFAULT_BROWSER_SEARCH_ENGINE
from wisp.config.settings import BrowserSearchEngine
from wisp.events import BrowserViewportUpdated, RpcBrowserAddressesReported
from wisp.rpc.commands import BrowserInteractCommand, GetBrowserAddressesCommand
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.handlers.mcp import RpcMcpServers
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime
from wisp.sessions.errors import SessionError
from wisp.sessions.images import IMAGE_DIRECTORY_NAME, SessionImageStore
from wisp.sessions.jsonl import JsonlSessionStore

if TYPE_CHECKING:
    from wisp.events import ImagePart

RunningCommandFactory = TypeAliasType("RunningCommandFactory", Callable[..., _RpcRunningCommand])
CommandCompletedFactory = TypeAliasType(
    "CommandCompletedFactory", Callable[..., _RpcCommandCompleted]
)
# Writes a frame only if the check, made at the moment of writing, still holds, and
# says whether it did.
LiveFramePublisher = TypeAliasType(
    "LiveFramePublisher",
    Callable[[BrowserViewportUpdated, Callable[[], bool]], Awaitable[bool]],
)

_logger = logging.getLogger(__name__)


def start_rpc_browser_interact_command(
    command: BrowserInteractCommand,
    *,
    agent: CodingSession,
    runtime: WispRuntime,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    mcp_servers: RpcMcpServers | None,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
    controller: BrowserController | None = None,
    live_browser: LiveBrowser | None = None,
    search_engine: BrowserSearchEngine = DEFAULT_BROWSER_SEARCH_ENGINE,
    publish_live_frame: LiveFramePublisher | None = None,
) -> _RpcRunningCommand:
    """Start one user-initiated browser takeover interaction sequentially on ``task_group``.

    ``search_engine`` receives the text of a ``live_open`` or ``live_navigate`` that
    is not an address. ``publish_live_frame`` writes the frame a ``live_frame`` read
    returns; the host serializes it against session and project changes. Without it
    the frame goes to ``write_event`` once the same check passes.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    resolved_command = (
        command if command.id == command_id else command.model_copy(update={"id": command_id})
    )
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        _run_browser_interact,
        resolved_command,
        lifecycle,
        agent,
        runtime,
        sessions,
        session_state,
        mcp_servers,
        cancel_scope,
        send.clone(),
        command_completed_factory,
        controller or McpBrowserController(),
        live_browser,
        search_engine,
        publish_live_frame or _write_frame_if_current(lifecycle),
    )
    return running_command_factory(
        command_id=command_id,
        command_type=command.type,
        cancel_scope=cancel_scope,
    )


def _session_image_store(
    session_state: _RpcSessionState, sessions: JsonlSessionStore
) -> BrowserImageStore:
    if session_state.session is not None:
        return session_state.session.store_image
    store = SessionImageStore(sessions.root / IMAGE_DIRECTORY_NAME)

    async def store_image(data: bytes) -> ImagePart:
        return await anyio.to_thread.run_sync(store.store, data)

    return store_image


def _write_frame_if_current(lifecycle: RpcCommandLifecycle) -> LiveFramePublisher:
    async def publish(frame: BrowserViewportUpdated, is_current: Callable[[], bool]) -> bool:
        if not is_current():
            return False
        lifecycle.write_event(frame)
        return True

    return publish


def _to_browser_interaction(command: BrowserInteractCommand) -> BrowserInteraction:
    action = command.action
    match action:
        case (
            "live_open"
            | "live_window"
            | "live_frame"
            | "live_close"
            | "live_resize"
            | "live_navigate"
            | "live_forward"
        ):
            raise AssertionError("Live browser lifecycle action is not an interaction")
    return BrowserInteraction(
        action=action,
        interaction_id=command.id,
        x=command.x,
        y=command.y,
        button=command.button,
        double_click=command.double_click,
        delta_x=command.delta_x,
        delta_y=command.delta_y,
        text=command.text,
        key=command.key,
        modifiers=command.modifiers,
    )


async def _run_browser_interact(
    command: BrowserInteractCommand,
    lifecycle: RpcCommandLifecycle,
    agent: CodingSession,
    runtime: WispRuntime,
    sessions: JsonlSessionStore,
    session_state: _RpcSessionState,
    mcp_servers: RpcMcpServers | None,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    command_completed_factory: CommandCompletedFactory,
    controller: BrowserController,
    live_browser: LiveBrowser | None,
    search_engine: BrowserSearchEngine,
    publish_live_frame: LiveFramePublisher,
) -> None:
    error: str | None = None
    entry_count = session_state.entry_count
    try:
        with cancel_scope:
            if command.live or command.action.startswith("live_"):
                await _run_live_browser_command(
                    command, agent, lifecycle, live_browser, search_engine, publish_live_frame
                )
            else:
                if mcp_servers is not None:
                    await mcp_servers.wait_for_startup()
                tools = agent.tool_registry.all() if agent.tool_registry is not None else ()
                result = await controller.execute(
                    _to_browser_interaction(command),
                    tools=tools,
                    context=agent.tool_context,
                    policy=agent.tool_policy,
                    image_store=_session_image_store(session_state, sessions),
                    tracker=agent.browser_handoff,
                    connected=runtime.mcp_server_connected,
                )
                if result.viewport_event is not None:
                    lifecycle.write_event(result.viewport_event)
                agent.browser_handoff.record_user_action(
                    command.action,
                    server_name=result.server_name,
                    metadata=result.metadata,
                )
        if cancel_scope.cancelled_caught and error is None:
            error = f"RPC command cancelled: {lifecycle.command_id}"
    except BrowserControlError as exc:
        error = str(exc) or f"Browser {command.action} failed"
    except anyio.get_cancelled_exc_class():
        # Only cancellation from outside the command reaches here; ``cancel_scope``
        # absorbs its own. The terminal event is still written, then it propagates.
        error = f"RPC command cancelled: {lifecycle.command_id}"
        raise
    except Exception as exc:  # noqa: BLE001 - isolate failures without leaking command text
        error = f"Browser interaction failed: {type(exc).__name__}"
    finally:
        with anyio.CancelScope(shield=True):
            async with send:
                if error is not None:
                    lifecycle.fail(error)
                else:
                    lifecycle.finish()
                await send.send(
                    command_completed_factory(
                        command_id=lifecycle.command_id,
                        command_type=command.type,
                        ok=error is None,
                        history=None,
                        entry_count=entry_count,
                    )
                )


async def _run_live_browser_command(
    command: BrowserInteractCommand,
    agent: CodingSession,
    lifecycle: RpcCommandLifecycle,
    browser: LiveBrowser | None,
    search_engine: BrowserSearchEngine,
    publish_live_frame: LiveFramePublisher,
) -> None:
    if browser is None:
        raise BrowserControlError("This RPC host does not support the live browser")
    if command.action == "live_close":
        await browser.close()
        return
    if not agent.trusted:
        raise BrowserControlError("The live browser requires a trusted project")
    frame: BrowserViewportUpdated | None
    record_handoff = False
    if command.action == "live_open":
        if command.url is not None:
            address = resolve_browser_address(command.url, search_engine)
            frame = await browser.open(address.url)
            if not address.is_search:
                await _remember_address(browser.profile_dir, address.url)
        else:
            frame = await browser.switch_mode(window=False)
        record_handoff = True
    elif command.action == "live_window":
        if command.url is not None:
            address = resolve_browser_address(command.url, search_engine)
            frame = await browser.switch_mode(window=True, url=address.url)
            if not address.is_search:
                await _remember_address(browser.profile_dir, address.url)
        else:
            frame = await browser.switch_mode(window=True)
        record_handoff = True
    elif command.action == "live_resize":
        assert command.viewport is not None
        frame = await browser.resize(command.viewport)
    elif command.action == "live_navigate":
        assert command.url is not None
        address = resolve_browser_address(command.url, search_engine)
        await browser.navigate(address.url)
        if not address.is_search:
            await _remember_address(browser.profile_dir, address.url)
        frame = await browser.poll_frame()
        record_handoff = True
    elif command.action == "live_forward":
        await browser.move_in_history(1)
        frame = await browser.poll_frame()
        record_handoff = True
    elif command.action == "live_frame":
        # Waits for a frame the client has not seen. The coordinator runs it beside
        # input commands, and the client sends the next one only after this ends.
        # The session or project may change during the wait, so the tab and trust
        # are checked again as the frame is written.
        lifetime = browser.lifetime
        frame = await browser.next_frame()
        if frame is not None:
            await publish_live_frame(frame, lambda: agent.trusted and browser.is_current(lifetime))
        return
    elif command.action == "refresh":
        frame = await browser.poll_frame()
    else:
        await browser.interact(_to_browser_interaction(command))
        agent.browser_handoff.record_user_action(
            command.action,
            server_name="live_browser",
            metadata=BrowserMetadata(
                url=getattr(browser, "current_url", None),
                title=getattr(browser, "current_title", None),
            ),
        )
        if command.action not in {"back", "reload"}:
            return
        frame = await browser.poll_frame()
    if record_handoff:
        agent.browser_handoff.record_user_action(
            command.action,
            server_name="live_browser",
            metadata=BrowserMetadata(
                url=(frame.url if frame is not None else None)
                or getattr(browser, "current_url", None),
                title=(frame.title if frame is not None else None)
                or getattr(browser, "current_title", None),
            ),
        )
    if frame is not None:
        lifecycle.write_event(frame)


async def _remember_address(profile_dir: Path, url: str) -> None:
    # The page is already open, so a list that cannot be saved must not fail it.
    try:
        await anyio.to_thread.run_sync(record_recent_address, profile_dir, url)
    except (OSError, SessionError, RecentAddressesBusyError) as exc:
        # Only the error's type and errno: its message could carry a URL or file content.
        reason = type(exc).__name__
        if isinstance(exc, OSError) and exc.errno is not None:
            reason += f" (errno {exc.errno})"
        _logger.warning(
            "Could not save the live browser's recent addresses to %s: %s",
            profile_dir / RECENT_ADDRESSES_FILENAME,
            reason,
        )


async def handle_rpc_browser_addresses_command(
    command: GetBrowserAddressesCommand,
    *,
    agent: CodingSession,
    live_browser: LiveBrowser | None,
    write_event: RpcEventWriter,
) -> None:
    """Report whether the live browser may open and the addresses it opened recently.

    Only reads the saved list; it never starts Chromium. An untrusted project gets
    no addresses, and the list is not read for it.

    Args:
        command (GetBrowserAddressesCommand): The request being answered.
        agent (CodingSession): Session whose project trust gates the live browser.
        live_browser (LiveBrowser | None): The host's live browser, or None when the
            host cannot run one; the command then fails.
        write_event (RpcEventWriter): Receives the report and lifecycle events.
    """
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    if live_browser is None:
        lifecycle.fail("This RPC host does not support the live browser")
        return
    trusted = agent.trusted
    addresses = (
        await anyio.to_thread.run_sync(load_recent_addresses, live_browser.profile_dir)
        if trusted
        else ()
    )
    write_event(
        RpcBrowserAddressesReported(
            command_id=lifecycle.command_id, trusted=trusted, addresses=addresses
        )
    )
    lifecycle.finish()
