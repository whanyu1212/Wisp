"""Authentication headers for HTTP MCP servers: resolution, pinning, and redaction."""

from __future__ import annotations

import logging
from pathlib import Path

import anyio
import httpx2
import pytest
from pytest import MonkeyPatch

import wisp.mcp.http_transport as http_transport
from tests.mcp.test_http_transport import _mock_http, _reply
from wisp.mcp.config import McpServerConfig
from wisp.runtime.extensions import build_runtime
from wisp.tools.context import ToolContext

TOKEN = "lin_api_sentinel-token-value"
VARIABLE = "WISP_MCP_TEST_TOKEN"


def _protected(seen: list[httpx2.Request]) -> object:
    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx2.Response(401)
        return _reply(request)

    return handle


def _server(**headers: object) -> McpServerConfig:
    return McpServerConfig.model_validate(
        {"name": "linear", "url": "https://mcp.linear.example/mcp", **headers}
    )


def _start(server: McpServerConfig, tmp_path: Path) -> tuple[str | None, tuple[str, ...]]:
    async def scenario() -> tuple[str | None, tuple[str, ...]]:
        runtime = await build_runtime(mcp_servers=(server,))
        try:
            messages = tuple(event.message for event in runtime.startup_events)
            if "mcp__linear__echo" not in runtime.tools.names():
                return None, messages
            result = await runtime.tools.get("mcp__linear__echo").run(
                {"value": "authorized"}, ToolContext(cwd=tmp_path)
            )
            return result.text, messages
        finally:
            await runtime.aclose()

    return anyio.run(scenario)


def test_a_forwarded_bearer_token_authenticates_without_being_logged(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    seen: list[httpx2.Request] = []
    _mock_http(monkeypatch, _protected(seen))
    monkeypatch.setenv(VARIABLE, TOKEN)
    caplog.set_level(logging.DEBUG)

    text, messages = _start(
        _server(headers_from={"Authorization": f"Bearer ${{{VARIABLE}}}"}), tmp_path
    )

    assert (text, messages) == ("authorized", ())
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)
    assert TOKEN not in caplog.text


def test_a_stored_header_value_authenticates(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    _mock_http(monkeypatch, _protected([]))

    text, _ = _start(_server(headers={"Authorization": f"Bearer {TOKEN}"}), tmp_path)

    assert text == "authorized"


def test_a_missing_token_variable_skips_the_server_without_naming_it(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[httpx2.Request] = []
    _mock_http(monkeypatch, _protected(seen))
    monkeypatch.delenv(VARIABLE, raising=False)

    text, messages = _start(
        _server(headers_from={"Authorization": f"Bearer ${{{VARIABLE}}}"}), tmp_path
    )

    assert text is None
    assert seen == []
    assert messages == (
        "MCP server linear is unavailable: a configured environment variable is unavailable",
    )


@pytest.mark.parametrize(
    "value",
    [
        # A newline would end the header and inject another one.
        f"{TOKEN}\r\nX-Injected: yes",
        # HTTP clients refuse surrounding spaces; a pasted token often has one.
        f"{TOKEN} ",
        f"\t{TOKEN}",
        "",
    ],
)
def test_a_token_variable_that_cannot_be_sent_is_reported_and_never_sent(
    monkeypatch: MonkeyPatch, tmp_path: Path, value: str
) -> None:
    seen: list[httpx2.Request] = []
    _mock_http(monkeypatch, _protected(seen))
    monkeypatch.setenv(VARIABLE, value)

    text, messages = _start(_server(headers_from={"Authorization": f"${{{VARIABLE}}}"}), tmp_path)

    assert text is None
    assert seen == []
    assert len(messages) == 1
    assert "a header value is empty or has invalid characters or spacing" in messages[0]
    assert TOKEN not in messages[0]


def test_headers_are_sent_only_to_the_configured_origin() -> None:
    seen: dict[str, str | None] = {}

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen[str(request.url)] = request.headers.get("authorization")
        return httpx2.Response(204)

    async def scenario() -> None:
        async with http_transport._HttpClient(
            origin=http_transport._origin(httpx2.URL("https://mcp.linear.example/mcp")),
            auth_headers={"Authorization": f"Bearer {TOKEN}"},
            transport=httpx2.MockTransport(handle),
        ) as client:
            for url in (
                "https://mcp.linear.example/mcp",
                "https://MCP.linear.example:443/other",
                "https://evil.example/mcp",
                "http://mcp.linear.example/mcp",
                "https://mcp.linear.example:8443/mcp",
            ):
                await client.send(client.build_request("POST", url))

    anyio.run(scenario)

    assert seen == {
        "https://mcp.linear.example/mcp": f"Bearer {TOKEN}",
        "https://mcp.linear.example/other": f"Bearer {TOKEN}",
        "https://evil.example/mcp": None,
        "http://mcp.linear.example/mcp": None,
        "https://mcp.linear.example:8443/mcp": None,
    }
