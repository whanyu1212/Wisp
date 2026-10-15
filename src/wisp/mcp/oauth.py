"""OAuth sign-in for HTTP MCP servers, and the tokens it stores.

The MCP SDK's :class:`OAuthClientProvider` implements the MCP authorization
spec: resource and authorization-server discovery, dynamic client
registration, PKCE, state and issuer checks, and refresh. This module supplies
what the SDK leaves to the client:

- :class:`~wisp.mcp.credentials.McpTokenStore`, the private
  ``~/.wisp/mcp-auth.json`` file. Each credential is bound to the server URL
  it was issued for and is never used for another URL.
- :func:`login_mcp_server`, the interactive sign-in: a loopback redirect,
  the browser, and a deadline.
- :func:`stored_oauth`, the non-interactive provider a running server uses.
  It refreshes tokens on its own but never opens a browser; when the user
  must sign in again it raises :class:`McpLoginRequired`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
from mcp.client import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

from wisp.auth.storage import AuthStorageError
from wisp.mcp.config import McpServerConfig
from wisp.mcp.credentials import McpCredential, McpTokenStore, uses_header_authorization
from wisp.mcp.http_transport import bounded_http_client

LOGIN_TIMEOUT_SECONDS = 300.0
_CALLBACK_PATH = "/callback"
# A refresh this close to expiry happens before the request instead of after a 401.
_EXPIRY_MARGIN_SECONDS = 30.0
_MAX_CALLBACK_REQUEST_BYTES = 16 * 1024


class McpLoginRequired(Exception):
    """A server needs the user to sign in, or to sign in again."""


class McpLoginError(RuntimeError):
    """Signing in failed. The message holds no token or server-supplied text."""


class _ServerTokens:
    """SDK token storage for one server.

    ``persist`` saves refreshed tokens as they arrive, for a running server.
    Sign-in keeps everything in memory until it succeeds, so a failed attempt
    never mixes a new registration with old tokens.
    """

    def __init__(
        self,
        store: McpTokenStore,
        server: McpServerConfig,
        credential: McpCredential | None,
        *,
        persist: bool,
    ) -> None:
        assert server.url is not None
        self._store = store
        self._name = server.name
        self._server_url = server.url
        self._persist = persist
        self._tokens = credential.tokens if credential else None
        self._expires_at = credential.expires_at if credential else None
        self._client_info = credential.client_info if credential else None
        self.authorization_server = credential.authorization_server if credential else None

    async def get_tokens(self) -> OAuthToken | None:
        if self._tokens is None or self._expires_at is None:
            return self._tokens
        remaining = self._expires_at - time.time() - _EXPIRY_MARGIN_SECONDS
        return self._tokens.model_copy(update={"expires_in": max(0, int(remaining))})

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens
        self._expires_at = (
            time.time() + tokens.expires_in if tokens.expires_in is not None else None
        )
        credential = self.credential()
        if self._persist and credential is not None:
            await anyio.to_thread.run_sync(self._store.save, self._name, credential)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info

    def credential(self) -> McpCredential | None:
        """Return the tokens and registration held now, if both exist."""

        if self._tokens is None or self._client_info is None:
            return None
        return McpCredential(
            server_url=self._server_url,
            tokens=self._tokens,
            expires_at=self._expires_at,
            client_info=self._client_info,
            authorization_server=self.authorization_server,
        )


class _McpOAuthProvider(OAuthClientProvider):
    """The SDK provider, resuming a saved sign-in.

    The SDK treats loaded tokens as never expiring and, until it rediscovers
    the authorization server, refreshes against the MCP server's own host. A
    resumed sign-in restores both, so an expired access token is refreshed at
    the right token endpoint instead of needing a new sign-in.
    """

    def __init__(
        self,
        server_url: str,
        client_metadata: OAuthClientMetadata,
        *,
        tokens: _ServerTokens,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
    ) -> None:
        super().__init__(
            server_url,
            client_metadata,
            tokens,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        self._tokens = tokens

    async def _initialize(self) -> None:
        await super()._initialize()
        tokens = self.context.current_tokens
        if tokens is not None and tokens.expires_in is not None:
            self.context.update_token_expiry(tokens)
        metadata = self._tokens.authorization_server
        if metadata is not None and self.context.oauth_metadata is None:
            self.context.oauth_metadata = metadata
            self.context.auth_server_url = str(metadata.issuer)

    async def _handle_token_response(self, response: Any) -> None:
        # Keep the metadata of the server that issued these tokens with them.
        self._tokens.authorization_server = self.context.oauth_metadata
        await super()._handle_token_response(response)


def _client_metadata(redirect_uri: str) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name="Wisp",
        redirect_uris=[AnyUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


async def stored_oauth(server: McpServerConfig, store: McpTokenStore) -> OAuthClientProvider | None:
    """Return the provider a running server authenticates with, if signed in.

    Args:
        server (McpServerConfig): An HTTP server.
        store (McpTokenStore): Saved credentials.

    Returns:
        OAuthClientProvider | None: A provider that sends and refreshes the saved
        tokens, or None when the server has no credential for its URL or sends
        its own Authorization header. The provider raises
        :class:`McpLoginRequired` instead of opening a browser.
    """

    if server.url is None or uses_header_authorization(server):
        return None
    try:
        credential = await anyio.to_thread.run_sync(store.load, server.name, server.url)
    except AuthStorageError:
        return None
    if credential is None:
        return None

    async def sign_in_again(_url: str) -> None:
        raise McpLoginRequired

    async def no_callback() -> AuthorizationCodeResult:
        raise McpLoginRequired

    redirect_uris = credential.client_info.redirect_uris or [AnyUrl("http://127.0.0.1/")]
    return _McpOAuthProvider(
        server.url,
        _client_metadata(str(redirect_uris[0])),
        tokens=_ServerTokens(store, server, credential, persist=True),
        redirect_handler=sign_in_again,
        callback_handler=no_callback,
    )


async def login_mcp_server(
    server: McpServerConfig,
    *,
    store: McpTokenStore,
    open_url: Callable[[str], Awaitable[None]],
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> None:
    """Sign in to an HTTP server in the browser and save its tokens.

    Starts a one-shot redirect listener on the loopback interface, then
    connects to the server so its 401 starts the SDK's authorization flow. The
    client registers afresh (the redirect port differs each time), and nothing
    is saved unless the whole flow succeeds.

    Args:
        server (McpServerConfig): An HTTP server without its own Authorization header.
        store (McpTokenStore): Where the credential is saved.
        open_url (Callable[[str], Awaitable[None]]): Shows the authorization URL to
            the user and opens their browser. Only HTTPS URLs (or plain HTTP to a
            loopback address) are passed on.
        timeout (float): Seconds the whole sign-in may take.

    Raises:
        McpLoginError: The server cannot be signed in to, the user did not finish
            in time, or the authorization server refused.
    """

    if server.url is None:
        raise McpLoginError(f"MCP server {server.name} runs a command; only HTTP servers sign in")
    if uses_header_authorization(server):
        raise McpLoginError(
            f"MCP server {server.name} sends its own Authorization header; remove it to sign in"
        )

    async def show(url: str) -> None:
        _check_authorization_url(url)
        await open_url(url)

    try:
        with anyio.fail_after(timeout):
            async with _redirect_listener() as listener:
                tokens = _ServerTokens(store, server, None, persist=False)
                provider = _McpOAuthProvider(
                    server.url,
                    _client_metadata(listener.redirect_uri),
                    tokens=tokens,
                    redirect_handler=show,
                    callback_handler=listener.result,
                )
                async with AsyncExitStack() as stack:
                    transport = bounded_http_client(
                        server.url,
                        on_disconnect=lambda: None,
                        headers=_static_headers(server),
                        auth=provider,
                    )
                    await stack.enter_async_context(Client(transport, cache=None))
    except TimeoutError:
        raise McpLoginError(f"Signing in to {server.name} did not finish in time") from None
    except McpLoginError:
        raise
    except Exception as exc:  # noqa: BLE001 - server and SDK details are untrusted
        problem = _login_problem(exc)
        if problem is not None:
            raise McpLoginError(problem) from None
        raise McpLoginError(f"Signing in to {server.name} failed") from None
    credential = tokens.credential()
    if credential is None:
        raise McpLoginError(f"{server.name} did not issue a token")
    await anyio.to_thread.run_sync(store.save, server.name, credential)


def _static_headers(server: McpServerConfig) -> dict[str, str]:
    # Only literal headers: a headers_from variable may be unset, and sign-in
    # does not need it. Authorization was refused above.
    return {name: value.get_secret_value() for name, value in server.headers}


def _check_authorization_url(url: str) -> None:
    """Refuse an authorization URL that would not be a safe page to open."""

    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname and _is_loopback(parts.hostname):
        return
    raise McpLoginError("the server's sign-in page is not an HTTPS URL")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _login_problem(exc: BaseException, depth: int = 0) -> str | None:
    """Find a sign-in failure Wisp raised itself, inside SDK task groups."""

    if depth > 16:
        return None
    if isinstance(exc, McpLoginError):
        return str(exc)
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            if (problem := _login_problem(inner, depth + 1)) is not None:
                return problem
    cause = exc.__cause__ or exc.__context__
    return _login_problem(cause, depth + 1) if cause is not None else None


@dataclass
class _RedirectListener:
    redirect_uri: str
    _result: asyncio.Future[AuthorizationCodeResult]

    async def result(self) -> AuthorizationCodeResult:
        return await self._result


@asynccontextmanager
async def _redirect_listener() -> AsyncIterator[_RedirectListener]:
    """Listen on a loopback port for the authorization server's one redirect."""

    loop = asyncio.get_running_loop()
    result: asyncio.Future[AuthorizationCodeResult] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, _, rest = request_line.partition(" ")
        target = rest.rsplit(" ", 1)[0]
        parts = urlsplit(target)
        if method != "GET" or parts.path != _CALLBACK_PATH or result.done():
            await _respond(writer, 404, "Not found.")
            return
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if "code" in query:
            result.set_result(
                AuthorizationCodeResult(
                    code=query["code"], state=query.get("state"), iss=query.get("iss")
                )
            )
            await _respond(writer, 200, "Signed in. You can close this tab and return to Wisp.")
        else:
            result.set_exception(McpLoginError("the authorization server refused the sign-in"))
            await _respond(writer, 400, "Sign-in was not completed. Return to Wisp.")

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=_MAX_CALLBACK_REQUEST_BYTES)
    port = server.sockets[0].getsockname()[1]
    try:
        async with server:
            yield _RedirectListener(f"http://127.0.0.1:{port}{_CALLBACK_PATH}", result)
    finally:
        if not result.done():
            result.cancel()


async def _respond(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    body = f"<!doctype html><title>Wisp</title><p>{message}</p>".encode()
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}[status]
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    try:
        await writer.drain()
    finally:
        writer.close()


__all__ = [
    "LOGIN_TIMEOUT_SECONDS",
    "McpLoginError",
    "McpLoginRequired",
    "login_mcp_server",
    "stored_oauth",
]
