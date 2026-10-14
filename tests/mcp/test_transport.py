from __future__ import annotations

from types import SimpleNamespace

import anyio
import mcp_types as types
from mcp.client.stdio import StdioServerParameters
from mcp.shared.message import SessionMessage
from pytest import MonkeyPatch

import wisp.mcp.transport as transport_module
from wisp.mcp.transport import bounded_stdio_client


def test_write_failure_reports_disconnect_once(monkeypatch: MonkeyPatch) -> None:
    class BrokenStdin:
        async def send(self, _data: bytes) -> None:
            raise anyio.BrokenResourceError

    class HangingStdout:
        async def receive(self) -> bytes:
            await anyio.sleep_forever()

    process = SimpleNamespace(stdin=BrokenStdin(), stdout=HangingStdout())

    async def create_process(**_kwargs: object) -> object:
        return process

    async def no_op(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(transport_module, "_create_platform_compatible_process", create_process)
    monkeypatch.setattr(transport_module, "_drain_stdout", no_op)
    monkeypatch.setattr(transport_module, "_stop_server_process", no_op)
    disconnected = anyio.Event()
    notifications = 0

    def on_disconnect() -> None:
        nonlocal notifications
        notifications += 1
        disconnected.set()

    async def scenario() -> None:
        parameters = StdioServerParameters(command="fixture")
        async with bounded_stdio_client(
            parameters,
            errlog=SimpleNamespace(),  # type: ignore[arg-type]
            on_disconnect=on_disconnect,
        ) as (_read, write):
            await write.send(
                SessionMessage(
                    message=types.JSONRPCNotification(
                        jsonrpc="2.0",
                        method="notifications/test",
                    )
                )
            )
            with anyio.fail_after(1):
                await disconnected.wait()

    anyio.run(scenario)

    assert notifications == 1


def test_platform_command_passes_posix_commands_through(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module.sys, "platform", "linux")

    assert anyio.run(transport_module._platform_command, "npx") == "npx"


def test_platform_command_resolves_windows_commands_with_the_public_helper(
    monkeypatch: MonkeyPatch,
) -> None:
    """Windows resolution uses the SDK's public helper, never its private one.

    The private ``_get_executable_command`` turned async in mcp 2.3.0; calling it
    synchronously passed a coroutine as the command and broke every stdio server.
    """

    resolved: list[str] = []

    def resolve(command: str) -> str:
        resolved.append(command)
        return f"{command}.cmd"

    monkeypatch.setattr(transport_module.sys, "platform", "win32")
    monkeypatch.setattr(transport_module, "get_windows_executable_command", resolve)

    assert anyio.run(transport_module._platform_command, "npx") == "npx.cmd"
    assert resolved == ["npx"]
    assert not hasattr(transport_module, "_get_executable_command")
