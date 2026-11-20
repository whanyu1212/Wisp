"""RPC command handler for user-initiated interactive browser takeover."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream
from typing_extensions import TypeAliasType

from wisp.coding.browser_control import (
    BrowserControlError,
    BrowserController,
    BrowserImageStore,
    BrowserInteraction,
    McpBrowserController,
)
from wisp.coding.browser_live import LiveBrowser
from wisp.coding.session import CodingSession
from wisp.events import BrowserViewportUpdated
from wisp.rpc.commands import BrowserInteractCommand
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.handlers.mcp import RpcMcpServers
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime
from wisp.sessions.images import IMAGE_DIRECTORY_NAME, SessionImageStore
from wisp.sessions.jsonl import JsonlSessionStore

if TYPE_CHECKING:
    from wisp.events import ImagePart

RunningCommandFactory = TypeAliasType("RunningCommandFactory", Callable[..., _RpcRunningCommand])
CommandCompletedFactory = TypeAliasType(
    "CommandCompletedFactory", Callable[..., _RpcCommandCompleted]
)


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
) -> _RpcRunningCommand:
    """Start one user-initiated browser takeover interaction sequentially on ``task_group``."""

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


def _to_browser_interaction(command: BrowserInteractCommand) -> BrowserInteraction:
    action = command.action
    match action:
        case "live_open" | "live_frame" | "live_close":
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
) -> None:
    error: str | None = None
    entry_count = session_state.entry_count
    try:
        with cancel_scope:
            if command.live or command.action in {"live_open", "live_frame", "live_close"}:
                await _run_live_browser_command(command, agent, lifecycle, live_browser)
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
        error = f"RPC command cancelled: {lifecycle.command_id}"
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
) -> None:
    if browser is None:
        raise BrowserControlError("This RPC host does not support the live browser")
    if command.action == "live_close":
        await browser.close()
        return
    if not agent.trusted:
        raise BrowserControlError("The live browser requires a trusted project")
    frame: BrowserViewportUpdated | None
    if command.action == "live_open":
        assert command.url is not None
        frame = await browser.open(command.url)
    elif command.action in {"live_frame", "refresh"}:
        frame = browser.take_frame()
    else:
        await browser.interact(_to_browser_interaction(command))
        return
    if frame is not None:
        lifecycle.write_event(frame)
