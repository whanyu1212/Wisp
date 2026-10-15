"""Signing in to and out of MCP servers over RPC."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from pytest import MonkeyPatch

from tests.mcp.test_http_transport import _mock_http
from tests.mcp.test_oauth import ISSUER, MCP_URL, _AuthorizationServer
from wisp.coding.session import CodingSession
from wisp.config.mcp_servers import (
    add_user_mcp_servers,
    load_user_mcp_servers,
    remove_user_mcp_server,
)
from wisp.events import (
    RpcCommandFinished,
    RpcMcpLoginStarted,
    RpcMcpStatusReported,
    WispEvent,
)
from wisp.mcp.config import McpServerConfig
from wisp.mcp.credentials import McpTokenStore
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.commands import LoginMcpServerCommand, LogoutMcpServerCommand
from wisp.rpc.coordinator import _RpcControlEvent, _RpcSessionState
from wisp.rpc.handlers.mcp import RpcMcpServers, RpcToolSelection
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore

_ALL_TOOLS = RpcToolSelection(all_tools=True, allow_read_tools=False, allowed_tools=())


def _approve(url: str) -> bool:
    """What the user's browser does: approve, then follow the redirect home."""

    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    httpx.get(query["redirect_uri"], params={"code": "code-1", "state": query["state"]})
    return True


def _run(
    tmp_path: Path, store: McpTokenStore, *commands: object, cancel: bool = False
) -> list[WispEvent]:
    async def scenario() -> list[WispEvent]:
        servers_config = load_user_mcp_servers()
        runtime = await build_runtime(mcp_servers=servers_config, mcp_token_store=store)
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
            for command in commands:
                send, receive = anyio.create_memory_object_stream[_RpcControlEvent](1)
                async with anyio.create_task_group() as task_group:
                    running = servers.start(
                        command,  # type: ignore[arg-type]
                        running_command=None,
                        session_state=_RpcSessionState(session=None, history=(), entry_count=0),
                        task_group=task_group,
                        send=send,
                        write_event=events.append,
                    )
                    assert running is not None
                    if cancel:
                        await anyio.sleep(0.2)
                        running.cancel_scope.cancel()
                    with anyio.fail_after(10):
                        await receive.receive()
        finally:
            await runtime.aclose()
        return events

    return anyio.run(scenario)


@pytest.fixture
def store(tmp_path: Path) -> McpTokenStore:
    return McpTokenStore(tmp_path / "mcp-auth.json")


@pytest.fixture
def linear(monkeypatch: MonkeyPatch) -> _AuthorizationServer:
    server = _AuthorizationServer()
    _mock_http(monkeypatch, server.handle)
    add_user_mcp_servers((McpServerConfig(name="linear", url=MCP_URL),))
    return server


def _statuses(events: list[WispEvent]) -> list[str | None]:
    return [
        server.sign_in
        for event in events
        if isinstance(event, RpcMcpStatusReported)
        for server in event.status.servers
    ]


def test_signing_in_opens_the_page_then_connects_and_signing_out_forgets_it(
    tmp_path: Path, store: McpTokenStore, linear: _AuthorizationServer, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("wisp.rpc.handlers.mcp.webbrowser.open", _approve)

    events = _run(
        tmp_path,
        store,
        LoginMcpServerCommand(id="login", name="linear"),
        LogoutMcpServerCommand(id="logout", name="linear"),
    )

    started = [event for event in events if isinstance(event, RpcMcpLoginStarted)]
    assert len(started) == 1 and started[0].authorization_url.startswith(f"{ISSUER}/authorize?")
    assert _statuses(events) == ["signed_in", "required"]
    finished = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert [(event.command_id, event.ok) for event in finished] == [
        ("login", True),
        ("logout", True),
    ]
    assert store.load("linear", MCP_URL) is None
    for event in events:
        assert "access-1" not in event.model_dump_json()


def test_a_cancelled_sign_in_saves_nothing(
    tmp_path: Path, store: McpTokenStore, linear: _AuthorizationServer, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("wisp.rpc.handlers.mcp.webbrowser.open", lambda _url: True)

    events = _run(tmp_path, store, LoginMcpServerCommand(id="login", name="linear"), cancel=True)

    finished = [event for event in events if isinstance(event, RpcCommandFinished)]
    assert [(event.ok, event.error) for event in finished] == [(False, "Signing in was cancelled")]
    assert not store.path.exists()


def test_removing_a_server_forgets_its_sign_in(
    tmp_path: Path, store: McpTokenStore, linear: _AuthorizationServer, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("wisp.rpc.handlers.mcp.webbrowser.open", _approve)
    home_store = McpTokenStore()
    _run(tmp_path, home_store, LoginMcpServerCommand(id="login", name="linear"))
    assert home_store.load("linear", MCP_URL) is not None

    remove_user_mcp_server("linear")

    assert home_store.load("linear", MCP_URL) is None


def test_a_server_whose_sign_in_cannot_be_forgotten_is_not_removed() -> None:
    from wisp.config.mcp_servers import McpSettingsError

    add_user_mcp_servers((McpServerConfig(name="linear", url=MCP_URL),))
    unreadable = McpTokenStore().path
    unreadable.parent.mkdir(parents=True, exist_ok=True)
    unreadable.write_text("not json", encoding="utf-8")
    unreadable.chmod(0o600)

    with pytest.raises(McpSettingsError, match="was not removed"):
        remove_user_mcp_server("linear")

    assert [server.name for server in load_user_mcp_servers()] == ["linear"]
