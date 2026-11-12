"""Tests for restarting MCP servers between agent runs over RPC."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import anyio
import pytest
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from tests.support.paths import FIXTURES_DIR
from wisp.coding.session import CodingSession
from wisp.config.mcp_servers import validated_mcp_server
from wisp.events import RpcCommandFinished, RpcMcpServerSnapshot, WispEvent
from wisp.mcp.config import McpServerConfig
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.commands import (
    AddMcpServerCommand,
    ParsedRpcCommand,
    PreviewMcpSnippetCommand,
    ReconnectMcpCommand,
)
from wisp.rpc.coordinator import _RpcControlEvent, _RpcRunningCommand, _RpcSessionState
from wisp.rpc.handlers.mcp import (
    RpcMcpServers,
    RpcToolSelection,
    rpc_mcp_server_launch,
    rpc_mcp_status_snapshot,
)
from wisp.rpc.host import RpcToolApprovalPolicy
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.extensions import build_runtime, restart_mcp_servers
from wisp.runtime.registry import ToolRegistry, UnknownToolError
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy

SECRET = "sentinel-mcp-secret"
_ALL_TOOLS = RpcToolSelection(all_tools=True, allow_read_tools=False, allowed_tools=())


def _fixture_server(tmp_path: Path, name: str = "fixture") -> McpServerConfig:
    return validated_mcp_server(
        name,
        {
            "command": sys.executable,
            "args": ["-u", str(FIXTURES_DIR / "mcp_stdio_server.py")],
            "env": {"WISP_MCP_TEST_CLOSED_FILE": str(tmp_path / f"{name}-closed")},
        },
    )


@pytest.mark.process
def test_restart_swaps_session_tools_and_revokes_mcp_session_grants(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await build_runtime()
        try:
            builtin_names = runtime.tools.names()
            agent = CodingSession(
                provider=ScriptedProvider(()),
                sessions=JsonlSessionStore(tmp_path / "sessions"),
                tool_registry=_ALL_TOOLS.select(runtime),
            )
            approvals = RpcToolApprovalPolicy(ToolApprovalPolicy.require_approval())
            approvals.approved_tools = frozenset({"bash", "mcp__fixture__echo"})
            servers = RpcMcpServers(
                runtime=runtime,
                agent=agent,
                tool_selection=_ALL_TOOLS,
                revoke_session_tool_grants=approvals.revoke_session_tool_grants,
            )

            assert await servers._restart((_fixture_server(tmp_path),)) is None
            assert approvals.approved_tools == {"bash"}
            fixture_tools = ("mcp__fixture__echo", "mcp__fixture__runtime_environment")
            assert runtime.tools.names() == (*builtin_names, *fixture_tools)
            assert [tool.name for tool in agent.tools][-2:] == list(fixture_tools)

            disabled = _fixture_server(tmp_path).model_copy(update={"enabled": False})
            assert await servers._restart((disabled,)) is None
            assert runtime.tools.names() == builtin_names
            assert all(not tool.name.startswith("mcp__") for tool in agent.tools)
            assert runtime.disabled_mcp_servers == ("fixture",)
            assert runtime.unavailable_tool_prefixes == ("mcp__fixture__",)
            assert (tmp_path / "fixture-closed").read_text(encoding="utf-8") == "closed"
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_selection_skips_missing_mcp_tools_but_not_missing_builtins() -> None:
    async def scenario() -> None:
        runtime = await build_runtime()
        try:
            gone = RpcToolSelection(
                all_tools=False, allow_read_tools=False, allowed_tools=("read", "mcp__gone__x")
            )
            assert gone.select(runtime).names() == ("read",)
            typo = RpcToolSelection(
                all_tools=False, allow_read_tools=False, allowed_tools=("raed",)
            )
            with pytest.raises(UnknownToolError):
                typo.select(runtime)
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_revoking_keeps_startup_grants() -> None:
    fallback = ToolApprovalPolicy(approved_tools=frozenset({"mcp__docs__search"}))
    approvals = RpcToolApprovalPolicy(fallback)
    approvals.approved_tools = frozenset({"mcp__docs__search", "mcp__github__push", "edit"})

    approvals.revoke_session_tool_grants("mcp__")

    assert approvals.approved_tools == {"mcp__docs__search", "edit"}


def test_command_is_refused_while_another_operation_runs(tmp_path: Path) -> None:
    async def scenario() -> list[WispEvent]:
        runtime = await build_runtime()
        events: list[WispEvent] = []
        try:
            servers = RpcMcpServers(
                runtime=runtime,
                agent=CodingSession(
                    provider=ScriptedProvider(()), sessions=JsonlSessionStore(tmp_path)
                ),
                tool_selection=_ALL_TOOLS,
                revoke_session_tool_grants=lambda _prefix: None,
            )
            active = _RpcRunningCommand(
                command_id="prompt-1", command_type="prompt", cancel_scope=anyio.CancelScope()
            )
            send, _receive = anyio.create_memory_object_stream[_RpcControlEvent](1)
            result = servers.start(
                ReconnectMcpCommand(id="mcp-1"),
                running_command=active,
                session_state=_RpcSessionState(session=None, history=(), entry_count=0),
                task_group=cast(TaskGroup, None),
                send=cast(MemoryObjectSendStream[_RpcControlEvent], send),
                write_event=events.append,
            )
            assert result is active
        finally:
            await runtime.aclose()
        return events

    events = anyio.run(scenario)

    finished = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert [(event.ok, event.error) for event in finished] == [
        (False, "Change MCP servers after the current operation finishes")
    ]


def test_session_refuses_a_tool_swap_mid_run(tmp_path: Path) -> None:
    agent = CodingSession(provider=ScriptedProvider(()), sessions=JsonlSessionStore(tmp_path))
    agent._operation_active = True

    with pytest.raises(RuntimeError, match="busy"):
        agent.replace_tool_registry(ToolRegistry())


def test_add_command_never_shows_env_values() -> None:
    command = AddMcpServerCommand(id="add", name="github", command="npx", env={"TOKEN": SECRET})

    assert SECRET not in repr(command)
    assert SECRET not in str(command)
    parsed = ParsedRpcCommand.from_known(command)
    assert SECRET not in repr(parsed._payload)
    assert "env" in parsed.provided_fields
    assert parsed.payload_size > len(SECRET)


@pytest.mark.parametrize("outcome", ["fails", "is cancelled"])
def test_interrupted_start_removes_the_tools_it_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from wisp.mcp.runtime import McpRuntime
    from wisp.tools.builtin import ReadTool

    class _PartialTool(ReadTool):
        name = "mcp__fixture__echo"

    async def partial_start(
        cls: type[McpRuntime],
        servers: object,
        *,
        api: ExtensionAPI,
        existing_tool_names: object,
        token_store: object = None,
    ) -> McpRuntime:
        # Registration happens before `start` returns, as in the real runtime.
        api.register_tool(_PartialTool(), replace=False)
        if outcome == "fails":
            raise RuntimeError("Failed to initialize MCP runtime")
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    monkeypatch.setattr(McpRuntime, "start", classmethod(partial_start))

    async def scenario() -> None:
        runtime = await build_runtime()
        try:
            builtin_names = runtime.tools.names()
            with anyio.move_on_after(0.2):
                try:
                    await restart_mcp_servers(runtime, (_fixture_server(tmp_path),))
                except RuntimeError:
                    pass
            assert runtime.tools.names() == builtin_names
            assert runtime.mcp_runtime is None
            assert runtime.failed_mcp_servers == ("fixture",)
            assert rpc_mcp_status_snapshot(runtime).servers == (
                RpcMcpServerSnapshot(
                    name="fixture",
                    status="unavailable",
                    error="MCP server fixture is unavailable: the MCP runtime could not start",
                    launch=rpc_mcp_server_launch(_fixture_server(tmp_path)),
                ),
            )
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_cancel_request_does_not_interrupt_a_started_command(tmp_path: Path) -> None:
    async def scenario() -> list[WispEvent]:
        runtime = await build_runtime()
        events: list[WispEvent] = []
        try:
            servers = RpcMcpServers(
                runtime=runtime,
                agent=CodingSession(
                    provider=ScriptedProvider(()), sessions=JsonlSessionStore(tmp_path)
                ),
                tool_selection=_ALL_TOOLS,
                revoke_session_tool_grants=lambda _prefix: None,
            )
            send, receive = anyio.create_memory_object_stream[_RpcControlEvent](1)
            async with anyio.create_task_group() as task_group:
                running = servers.start(
                    ReconnectMcpCommand(id="mcp-1"),
                    running_command=None,
                    session_state=_RpcSessionState(session=None, history=(), entry_count=0),
                    task_group=task_group,
                    send=send,
                    write_event=events.append,
                )
                assert running is not None
                running.cancel_scope.cancel()
                await receive.receive()
        finally:
            await runtime.aclose()
        return events

    events = anyio.run(scenario)

    finished = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert [(event.ok, event.error) for event in finished] == [(True, None)]


def test_preview_command_never_shows_the_snippet() -> None:
    snippet = f'{{"mcpServers": {{"x": {{"command": "npx", "env": {{"T": "{SECRET}"}}}}}}}}'
    command = PreviewMcpSnippetCommand(id="preview", snippet=snippet)

    assert SECRET not in repr(command)
    assert SECRET not in str(command)
    parsed = ParsedRpcCommand.from_known(command)
    assert SECRET not in repr(parsed._payload)
    assert "snippet" in parsed.provided_fields


def test_a_restart_after_the_run_waits_for_an_idle_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = await build_runtime()
        try:
            agent = CodingSession(
                provider=ScriptedProvider(()), sessions=JsonlSessionStore(tmp_path)
            )
            servers = RpcMcpServers(
                runtime=runtime,
                agent=agent,
                tool_selection=_ALL_TOOLS,
                revoke_session_tool_grants=lambda _prefix: None,
            )
            assert (
                servers.confirm_proposal(
                    "propose_mcp_server", {"name": "docs", "url": "http://127.0.0.1:9/mcp"}
                )
                is None
            )
            agent._operation_active = True
            assert "after the next reply" in (await servers.restart_after_run() or "")
            agent._operation_active = False
            # The restart ran, and the server that could not start is reported.
            assert await servers.restart_after_run() == (
                "MCP server docs is unavailable: the server could not be started or initialized"
            )
            assert runtime.mcp_server_configs[0].name == "docs"
            assert await servers.restart_after_run() is None
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_add_command_never_shows_header_values() -> None:
    command = AddMcpServerCommand(
        id="add",
        name="linear",
        url="https://mcp.linear.app/mcp",
        headers={"Authorization": f"Bearer {SECRET}"},
        headers_from={"X-Workspace": "${LINEAR_WORKSPACE}"},
    )

    assert SECRET not in repr(command)
    parsed = ParsedRpcCommand.from_known(command)
    assert SECRET not in repr(parsed._payload)
    assert {"headers", "headers_from"} <= parsed.provided_fields


def test_launch_names_header_values_and_the_variables_headers_send() -> None:
    server = validated_mcp_server(
        "linear",
        {
            "url": "https://mcp.linear.app/mcp",
            "headers": {"X-Workspace": SECRET},
            "headers_from": {"Authorization": "Bearer ${LINEAR_API_KEY}"},
        },
    )

    launch = rpc_mcp_server_launch(server)

    assert launch.header_names == ("X-Workspace",)
    assert [header.model_dump() for header in launch.headers_from] == [
        {"header": "Authorization", "variable": "LINEAR_API_KEY", "scheme": "Bearer"}
    ]
    assert SECRET not in launch.model_dump_json()


def _mcp_servers(runtime: WispRuntime, agent: CodingSession) -> RpcMcpServers:
    return RpcMcpServers(
        runtime=runtime,
        agent=agent,
        tool_selection=_ALL_TOOLS,
        revoke_session_tool_grants=lambda _prefix: None,
    )


@pytest.mark.process
def test_startup_connect_matches_an_eager_build(tmp_path: Path) -> None:
    """Connecting after the build offers the same tools, in the same order."""

    async def scenario() -> None:
        server = _fixture_server(tmp_path)
        eager = await build_runtime(mcp_servers=(server,))
        deferred = await build_runtime(mcp_servers=(server,), connect_mcp=False)
        try:
            assert deferred.mcp_connecting
            assert all(not name.startswith("mcp__") for name in deferred.tools.names())
            agent = CodingSession(
                provider=ScriptedProvider(()),
                sessions=JsonlSessionStore(tmp_path / "sessions"),
                tool_registry=_ALL_TOOLS.select(deferred),
            )
            servers = _mcp_servers(deferred, agent)

            assert await servers.connect_at_startup() == ()
            await servers.wait_for_startup()

            assert not deferred.mcp_connecting
            assert deferred.tools.names() == eager.tools.names()
            assert [tool.name for tool in agent.tools] == list(eager.tools.names())
            assert rpc_mcp_status_snapshot(deferred) == rpc_mcp_status_snapshot(eager)
        finally:
            await deferred.aclose()
            await eager.aclose()

    anyio.run(scenario)


@pytest.mark.process
def test_startup_connect_waits_for_a_busy_session(tmp_path: Path) -> None:
    """A folder change being saved when servers connect must not cost the session its tools."""

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=(_fixture_server(tmp_path),), connect_mcp=False)
        try:
            agent = CodingSession(
                provider=ScriptedProvider(()),
                sessions=JsonlSessionStore(tmp_path / "sessions"),
                tool_registry=_ALL_TOOLS.select(runtime),
            )
            servers = _mcp_servers(runtime, agent)
            problems: list[tuple[str, ...]] = []

            async def connect() -> None:
                problems.append(await servers.connect_at_startup())

            async with anyio.create_task_group() as task_group:
                async with agent._exclusive_operation():  # noqa: SLF001
                    task_group.start_soon(connect)
                    while runtime.mcp_connecting:
                        await anyio.sleep(0.01)
                    # The servers are up, but the busy session has not been given
                    # their tools, so runs must keep waiting.
                    await anyio.sleep(0.05)
                    assert not servers._startup_settled.is_set()  # noqa: SLF001
            assert problems == [()]
            assert [tool.name for tool in agent.tools][-2:] == [
                "mcp__fixture__echo",
                "mcp__fixture__runtime_environment",
            ]
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_startup_connect_and_restart_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MCP command sent while servers connect waits instead of starting a second set."""

    from wisp.mcp.runtime import McpRuntime

    starts_in_progress = 0
    most_starts_at_once = 0
    first_start_entered = anyio.Event()
    release_first_start = anyio.Event()

    async def tracked_start(cls: type[McpRuntime], *_args: object, **_kwargs: object) -> McpRuntime:
        nonlocal starts_in_progress, most_starts_at_once
        starts_in_progress += 1
        most_starts_at_once = max(most_starts_at_once, starts_in_progress)
        try:
            first_start_entered.set()
            await release_first_start.wait()
            raise RuntimeError("Failed to initialize MCP runtime")
        finally:
            starts_in_progress -= 1

    monkeypatch.setattr(McpRuntime, "start", classmethod(tracked_start))

    async def scenario() -> None:
        server = _fixture_server(tmp_path)
        runtime = await build_runtime(mcp_servers=(server,), connect_mcp=False)
        try:
            agent = CodingSession(
                provider=ScriptedProvider(()),
                sessions=JsonlSessionStore(tmp_path / "sessions"),
                tool_registry=_ALL_TOOLS.select(runtime),
            )
            servers = _mcp_servers(runtime, agent)
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(servers.connect_at_startup)
                await first_start_entered.wait()
                task_group.start_soon(servers._restart, (server,))
                await anyio.wait_all_tasks_blocked()
                release_first_start.set()
            assert most_starts_at_once == 1
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_startup_connect_is_skipped_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart that ran first already connected every server."""

    from wisp.mcp.runtime import McpRuntime

    starts = 0

    async def failing_start(cls: type[McpRuntime], *_args: object, **_kwargs: object) -> McpRuntime:
        nonlocal starts
        starts += 1
        raise RuntimeError("Failed to initialize MCP runtime")

    monkeypatch.setattr(McpRuntime, "start", classmethod(failing_start))

    async def scenario() -> None:
        server = _fixture_server(tmp_path)
        runtime = await build_runtime(mcp_servers=(server,), connect_mcp=False)
        try:
            agent = CodingSession(
                provider=ScriptedProvider(()),
                sessions=JsonlSessionStore(tmp_path / "sessions"),
                tool_registry=_ALL_TOOLS.select(runtime),
            )
            servers = _mcp_servers(runtime, agent)

            await servers._restart((server,))
            assert await servers.connect_at_startup() == ()
            await servers.wait_for_startup()

            assert starts == 1
        finally:
            await runtime.aclose()

    anyio.run(scenario)
