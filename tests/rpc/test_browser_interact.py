"""Tests for browser_interact RPC command validation, scheduling, and host execution."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as McpToolDefinition
from pydantic import ValidationError

from tests.coding.session_support import CapturingProvider
from wisp.coding.session import CodingSession
from wisp.config.mcp_servers import McpServerConfig
from wisp.events import BrowserViewportUpdated, RpcCommandFinished, WispEvent
from wisp.mcp.tool import adapt_mcp_tool
from wisp.rpc.commands import BrowserInteractCommand, ParsedRpcCommand, RpcCommandAdapter
from wisp.rpc.coordinator import RpcCoordinator, _RpcInputCommand, _RpcSessionState
from wisp.rpc.handlers.browser import start_rpc_browser_interact_command
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
