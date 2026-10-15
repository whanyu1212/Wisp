"""OAuth sign-in for HTTP MCP servers against a fixture authorization server."""

from __future__ import annotations

import json
import logging
import stat
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import httpx2
import pytest
from pytest import MonkeyPatch

from tests.mcp.test_http_transport import _mock_http, _reply
from wisp.mcp.config import McpServerConfig
from wisp.mcp.credentials import McpTokenStore
from wisp.mcp.oauth import (
    McpLoginError,
    login_mcp_server,
)
from wisp.runtime.extensions import build_runtime
from wisp.tools.context import ToolContext

MCP_URL = "https://mcp.example.test/mcp"
ISSUER = "https://auth.example.test"


class _AuthorizationServer:
    """An MCP server protected by an OAuth authorization server, in one handler."""

    def __init__(self) -> None:
        self.valid_access: set[str] = set()
        self.refresh_tokens: dict[str, str] = {}
        self.refuse_refresh = False
        self.issued = 0
        self.registrations = 0
        self.requests: list[httpx2.Request] = []

    def issue(self) -> dict[str, object]:
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.valid_access.add(access)
        self.refresh_tokens[refresh] = access
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh,
        }

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(MCP_URL):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token in self.valid_access:
                return _reply(request)
            return httpx2.Response(
                401,
                headers={
                    "www-authenticate": 'Bearer resource_metadata="'
                    'https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
                },
            )
        if url == "https://mcp.example.test/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(
                200, json={"resource": MCP_URL, "authorization_servers": [ISSUER]}
            )
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return httpx2.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "registration_endpoint": f"{ISSUER}/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if url == f"{ISSUER}/register":
            self.registrations += 1
            metadata = json.loads(request.content)
            return httpx2.Response(201, json={**metadata, "client_id": "client-1"})
        if url == f"{ISSUER}/token":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code" and form["code"] == "code-1":
                return httpx2.Response(200, json=self.issue())
            if form["grant_type"] == "refresh_token" and not self.refuse_refresh:
                if self.refresh_tokens.pop(form["refresh_token"], None) is not None:
                    return httpx2.Response(200, json=self.issue())
            return httpx2.Response(400, json={"error": "invalid_grant"})
        return httpx2.Response(404)


def _server(**extra: object) -> McpServerConfig:
    return McpServerConfig.model_validate({"name": "linear", "url": MCP_URL, **extra})


async def _approve_in_browser(url: str) -> None:
    """What the user's browser does: approve, then follow the redirect home."""

    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"{ISSUER}/authorize?")
    assert query["code_challenge_method"] == "S256"
    async with httpx.AsyncClient() as browser:
        response = await browser.get(
            query["redirect_uri"], params={"code": "code-1", "state": query["state"]}
        )
    assert response.status_code == 200


def _login(server: McpServerConfig, store: McpTokenStore, open_url: object = None) -> None:
    async def scenario() -> None:
        await login_mcp_server(
            server,
            store=store,
            open_url=open_url or _approve_in_browser,  # type: ignore[arg-type]
            timeout=10,
        )

    anyio.run(scenario)


def _connect(
    server: McpServerConfig, store: McpTokenStore, tmp_path: Path
) -> tuple[str | None, tuple[str, ...]]:
    async def scenario() -> tuple[str | None, tuple[str, ...]]:
        runtime = await build_runtime(mcp_servers=(server,), mcp_token_store=store)
        try:
            messages = tuple(event.message for event in runtime.startup_events)
            if "mcp__linear__echo" not in runtime.tools.names():
                return None, messages
            result = await runtime.tools.get("mcp__linear__echo").run(
                {"value": "signed in"}, ToolContext(cwd=tmp_path)
            )
            return result.text, messages
        finally:
            await runtime.aclose()

    return anyio.run(scenario)


@pytest.fixture
def authorization_server(monkeypatch: MonkeyPatch) -> _AuthorizationServer:
    server = _AuthorizationServer()
    _mock_http(monkeypatch, server.handle)
    return server


@pytest.fixture
def store(tmp_path: Path) -> McpTokenStore:
    return McpTokenStore(tmp_path / "mcp-auth.json")


def test_signing_in_saves_tokens_bound_to_the_url_in_a_private_file(
    authorization_server: _AuthorizationServer, store: McpTokenStore
) -> None:
    _login(_server(), store)

    credential = store.load("linear", MCP_URL)
    assert credential is not None
    assert credential.tokens.access_token == "access-1"
    assert credential.client_info.client_id == "client-1"
    assert store.load("linear", "https://other.example.test/mcp") is None
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_a_signed_in_server_connects_and_its_token_stays_on_its_origin(
    authorization_server: _AuthorizationServer,
    store: McpTokenStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _login(_server(), store)
    authorization_server.requests.clear()
    caplog.set_level(logging.DEBUG)

    text, messages = _connect(_server(), store, tmp_path)

    assert (text, messages) == ("signed in", ())
    for request in authorization_server.requests:
        bearer = request.headers.get("authorization")
        assert bearer is None or str(request.url).startswith(MCP_URL)
    assert "access-1" not in caplog.text and "refresh-1" not in caplog.text


def test_an_expired_token_is_refreshed_and_saved_without_a_browser(
    authorization_server: _AuthorizationServer, store: McpTokenStore, tmp_path: Path
) -> None:
    _login(_server(), store)
    credential = store.load("linear", MCP_URL)
    assert credential is not None
    store.save("linear", replace(credential, expires_at=0.0))
    authorization_server.valid_access.clear()

    text, messages = _connect(_server(), store, tmp_path)

    assert (text, messages) == ("signed in", ())
    refreshed = store.load("linear", MCP_URL)
    assert refreshed is not None and refreshed.tokens.access_token == "access-2"


def test_a_refused_refresh_asks_the_user_to_sign_in_again(
    authorization_server: _AuthorizationServer, store: McpTokenStore, tmp_path: Path
) -> None:
    _login(_server(), store)
    authorization_server.valid_access.clear()
    authorization_server.refuse_refresh = True

    text, messages = _connect(_server(), store, tmp_path)

    assert text is None
    assert messages == (
        "MCP server linear is unavailable: sign in with `wisp mcp login linear` or /mcp",
    )


def test_a_server_that_needs_a_sign_in_says_so(
    authorization_server: _AuthorizationServer, store: McpTokenStore, tmp_path: Path
) -> None:
    text, messages = _connect(_server(), store, tmp_path)

    assert text is None
    assert messages == (
        "MCP server linear is unavailable: sign in with `wisp mcp login linear` or /mcp",
    )
    assert authorization_server.registrations == 0


def test_a_changed_url_never_receives_the_old_token(
    authorization_server: _AuthorizationServer, store: McpTokenStore, tmp_path: Path
) -> None:
    _login(_server(), store)
    authorization_server.requests.clear()
    moved = McpServerConfig(name="linear", url="https://mcp.example.test/other")

    _connect(moved, store, tmp_path)

    assert all("authorization" not in request.headers for request in authorization_server.requests)


def test_a_server_with_its_own_authorization_header_cannot_sign_in(
    authorization_server: _AuthorizationServer, store: McpTokenStore
) -> None:
    with pytest.raises(McpLoginError, match="sends its own Authorization header"):
        _login(_server(headers_from={"Authorization": "Bearer ${TOKEN}"}), store)


def test_a_sign_in_page_that_is_not_https_is_never_opened(
    monkeypatch: MonkeyPatch, store: McpTokenStore
) -> None:
    server = _AuthorizationServer()

    def downgraded(request: httpx2.Request) -> httpx2.Response:
        response = server.handle(request)
        if str(request.url) == f"{ISSUER}/.well-known/oauth-authorization-server":
            metadata = json.loads(response.content)
            metadata["authorization_endpoint"] = "http://evil.example.test/authorize"
            return httpx2.Response(200, json=metadata)
        return response

    _mock_http(monkeypatch, downgraded)
    opened: list[str] = []

    async def record(url: str) -> None:
        opened.append(url)

    with pytest.raises(McpLoginError, match="not an HTTPS URL"):
        _login(_server(), store, record)
    assert opened == []
    assert store.load("linear", MCP_URL) is None


def test_a_sign_in_the_user_never_finishes_saves_nothing(
    authorization_server: _AuthorizationServer, store: McpTokenStore
) -> None:
    async def never(url: str) -> None:
        del url

    async def scenario() -> None:
        await login_mcp_server(_server(), store=store, open_url=never, timeout=0.5)

    with pytest.raises(McpLoginError, match="did not finish in time"):
        anyio.run(scenario)
    assert not store.path.exists()
