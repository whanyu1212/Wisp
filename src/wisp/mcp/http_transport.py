"""Bounded, redacted HTTP connections for the MCP SDK transport."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

import anyio
import httpx2
from mcp.client._transport import TransportStreams, WriteStream
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

from wisp.mcp.transport import MAX_MCP_FRAME_BYTES, McpServerFrameError

_HTTP_SHUTDOWN_SECONDS = 1.0
_REDACT_HTTP_LOGS: ContextVar[bool] = ContextVar("wisp_mcp_http_logs", default=False)


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _REDACT_HTTP_LOGS.get()


_LOG_FILTER = _TransportLogFilter()


class _RedactedWriteStream(anyio.abc.ObjectSendStream[SessionMessage]):
    def __init__(self, stream: WriteStream[SessionMessage]) -> None:
        self._stream = stream

    async def send(self, item: SessionMessage) -> None:
        # The SDK restores the sender's context in its HTTP request tasks.
        token = _REDACT_HTTP_LOGS.set(True)
        try:
            await self._stream.send(item)
        finally:
            _REDACT_HTTP_LOGS.reset(token)

    async def aclose(self) -> None:
        await self._stream.aclose()


class _BoundedBody(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        async for chunk in self._stream:
            size += len(chunk)
            if size > MAX_MCP_FRAME_BYTES:
                raise McpServerFrameError("MCP HTTP response exceeds the frame limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


async def _bound_response(response: httpx2.Response) -> None:
    # Bound raw bytes without allowing decompression to expand them before parsing.
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise McpServerFrameError("MCP HTTP response uses unsupported content encoding")
    event_stream = response.request.method in {"GET", "POST"} and response.headers.get(
        "content-type", ""
    ).lower().startswith("text/event-stream")
    if not event_stream:
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        response.stream = _BoundedBody(response.stream)
    # SSE uses the SDK's per-event limit; a lifetime byte limit would break healthy streams.


class _HttpClient(httpx2.AsyncClient):
    async def delete(self, url: httpx2.URL | str, **kwargs: Any) -> httpx2.Response:
        # A server that stalls session termination must not stall TUI shutdown.
        async with asyncio.timeout(_HTTP_SHUTDOWN_SECONDS):
            return await super().delete(url, **kwargs)


@asynccontextmanager
async def bounded_http_client(
    url: str,
    *,
    on_disconnect: Callable[[], None],
) -> AsyncIterator[TransportStreams]:
    """Open SDK streams with bounded bodies and task-local diagnostic redaction.

    Args:
        url (str): Validated HTTP endpoint. Redirects are not followed.
        on_disconnect (Callable[[], None]): Called when the transport exits.

    Yields:
        TransportStreams: SDK-compatible incoming and outgoing message streams.
    """
    # Filters are shared, but their decision is task-local, preserving other HTTP users' logs.
    for name in (
        "mcp.client.streamable_http",
        "client",
        "mcp.shared.jsonrpc_dispatcher",
        "httpx2",
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.http2",
        "httpcore2.proxy",
        "httpcore2.socks",
    ):
        logging.getLogger(name).addFilter(_LOG_FILTER)
    token = _REDACT_HTTP_LOGS.set(True)
    try:
        async with _HttpClient(
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            timeout=httpx2.Timeout(30.0, read=300.0),
            event_hooks={"response": [_bound_response]},
        ) as client:
            async with streamable_http_client(url, http_client=client) as (reader, writer):
                yield reader, _RedactedWriteStream(writer)
    finally:
        on_disconnect()
        _REDACT_HTTP_LOGS.reset(token)
