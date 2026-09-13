"""Real SDK coverage for HTTP MCP discovery, calls, and transport boundaries."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from pytest import MonkeyPatch

import wisp.mcp.http_transport as http_transport
import wisp.mcp.runtime as mcp_runtime
from wisp.mcp.config import McpServerConfig
from wisp.mcp.transport import MAX_MCP_FRAME_BYTES
from wisp.runtime.extensions import build_runtime
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError


class _Body(httpx2.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.consumed = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.content), 4096):
            chunk = self.content[offset : offset + 4096]
            self.consumed += len(chunk)
            yield chunk


def _reply(request: httpx2.Request) -> httpx2.Response:
    if request.method == "GET":
        return httpx2.Response(405)
    if request.method == "DELETE":
        return httpx2.Response(204)
    message = json.loads(request.content)
    if "id" not in message:
        return httpx2.Response(202)
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
    match message["method"]:
        case "server/discover":
            response["error"] = {"code": -32601, "message": "Method not found"}
        case "initialize":
            response["result"] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        case "tools/list":
            response["result"] = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        case "tools/call":
            response["result"] = {
                "content": [{"type": "text", "text": message["params"]["arguments"]["value"]}]
            }
        case _:
            raise AssertionError(message)
    return httpx2.Response(
        200,
        headers={"content-type": "application/json", "mcp-session-id": "fixture-session"},
        stream=_Body(json.dumps(response).encode()),
    )


def _mock_http(monkeypatch: MonkeyPatch, handler: Callable[..., Any]) -> None:
    client_class = http_transport._HttpClient
    monkeypatch.setattr(
        http_transport,
        "_HttpClient",
        lambda **kwargs: client_class(transport=httpx2.MockTransport(handler), **kwargs),
    )


def _server() -> McpServerConfig:
    return McpServerConfig(name="fixture", url="https://example.com/private-endpoint")


def test_http_sdk_discovery_calls_redaction_and_cross_task_cleanup(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    requests: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _reply(request)

    _mock_http(monkeypatch, handle)
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=(_server(),))
        try:
            assert runtime.mcp_runtime is not None
            assert runtime.mcp_runtime.is_connected("fixture")
            result = await runtime.tools.get("mcp__fixture__echo").run(
                {"value": "private-tool-argument"}, ToolContext(cwd=tmp_path)
            )
            assert result.text == "private-tool-argument"
        finally:
            await asyncio.create_task(runtime.aclose())
        assert not runtime.mcp_runtime.is_connected("fixture")
        logging.getLogger("httpx2").warning("unrelated HTTP diagnostics remain visible")

    anyio.run(scenario)
    assert any(request.method == "DELETE" for request in requests)
    assert "private-endpoint" not in caplog.text
    assert "private-tool-argument" not in caplog.text
    assert "unrelated HTTP diagnostics remain visible" in caplog.text


@pytest.mark.parametrize("fault", ["invalid", "oversized", "compressed", "redirect"])
def test_http_rejects_unsafe_responses_without_logging_remote_data(
    fault: str, monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[str] = []
    bodies: list[_Body] = []
    secret = "remote-frame-secret"

    async def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        headers = {"content-type": "application/json"}
        body = json.dumps({"credential": secret}).encode()
        status = 200
        if fault == "oversized":
            valid = _reply(request)
            body = await valid.aread() + b" " * (MAX_MCP_FRAME_BYTES * 2)
            status, headers = valid.status_code, dict(valid.headers)
        elif fault == "compressed":
            valid = _reply(request)
            body = gzip.compress(await valid.aread())
            status, headers = valid.status_code, dict(valid.headers)
            headers["content-encoding"] = "gzip"
        elif fault == "redirect":
            status = 307
            headers["location"] = "http://outside.example/redirect-secret"
        stream = _Body(body)
        bodies.append(stream)
        return httpx2.Response(status, headers=headers, stream=stream)

    _mock_http(monkeypatch, handle)
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        with anyio.fail_after(2):
            runtime = await build_runtime(mcp_servers=(_server(),))
            try:
                assert "mcp__fixture__echo" not in runtime.tools.names()
                assert runtime.startup_events
                assert secret not in str(runtime.startup_events)
            finally:
                await runtime.aclose()

    anyio.run(scenario)
    assert all(url == _server().url for url in requests)
    assert secret not in caplog.text
    assert "private-endpoint" not in caplog.text
    if fault == "oversized":
        assert all(body.consumed <= MAX_MCP_FRAME_BYTES + 4096 for body in bodies)
        assert all(body.consumed < len(body.content) for body in bodies)


def test_http_stalled_termination_has_a_deadline(monkeypatch: MonkeyPatch) -> None:
    deleted = anyio.Event()

    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            deleted.set()
            await anyio.sleep_forever()
        return _reply(request)

    _mock_http(monkeypatch, handle)
    monkeypatch.setattr(http_transport, "_HTTP_SHUTDOWN_SECONDS", 0.02)

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=(_server(),))
        with anyio.fail_after(1):
            await runtime.aclose()
        assert deleted.is_set()

    anyio.run(scenario)


def test_http_termination_body_is_bounded_even_when_labelled_sse(monkeypatch: MonkeyPatch) -> None:
    body = _Body(b"x" * (MAX_MCP_FRAME_BYTES * 2))

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "DELETE":
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
        return _reply(request)

    _mock_http(monkeypatch, handle)

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=(_server(),))
        await runtime.aclose()

    anyio.run(scenario)
    assert 0 < body.consumed <= MAX_MCP_FRAME_BYTES + 4096
    assert body.consumed < len(body.content)


def test_http_invalid_sse_notification_is_redacted(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"level": "private-notification", "data": "private-notification"},
            }
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_Body(f"event: message\ndata: {json.dumps(notification)}\n\n".encode()),
            )
        return _reply(request)

    _mock_http(monkeypatch, handle)
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=(_server(),))
        try:
            result = await runtime.tools.get("mcp__fixture__echo").run(
                {"value": "still connected"}, ToolContext(cwd=tmp_path)
            )
            assert result.text == "still connected"
        finally:
            await runtime.aclose()

    anyio.run(scenario)
    assert "private-notification" not in caplog.text


def test_http_startup_timeout_isolated_from_healthy_server(monkeypatch: MonkeyPatch) -> None:
    async def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "stalled.example":
            await anyio.sleep_forever()
        return _reply(request)

    _mock_http(monkeypatch, handle)
    monkeypatch.setattr(mcp_runtime, "MCP_STARTUP_TIMEOUT_SECONDS", 1.0)

    async def scenario() -> None:
        with anyio.fail_after(5):
            runtime = await build_runtime(
                mcp_servers=(
                    McpServerConfig(name="stalled", url="https://stalled.example/mcp"),
                    _server(),
                )
            )
            try:
                assert "mcp__fixture__echo" in runtime.tools.names()
                assert runtime.mcp_runtime is not None
                assert len(runtime.mcp_runtime.diagnostics) == 1
                assert runtime.mcp_runtime.diagnostics[0].server_name == "stalled"
            finally:
                await runtime.aclose()

    anyio.run(scenario)


def test_http_failure_after_startup_disconnects_without_second_startup_result(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST" and json.loads(request.content)["method"] == "tools/call":
            raise httpx2.ConnectError("private-network-detail")
        return _reply(request)

    _mock_http(monkeypatch, handle)

    async def scenario() -> None:
        runtime = await build_runtime(mcp_servers=(_server(),))
        try:
            with anyio.fail_after(2), pytest.raises(ToolError, match="MCP tool call failed"):
                await runtime.tools.get("mcp__fixture__echo").run(
                    {"value": "hello"}, ToolContext(cwd=tmp_path)
                )
            assert runtime.mcp_runtime is not None
            with anyio.fail_after(1):
                while runtime.mcp_runtime.is_connected("fixture"):
                    await anyio.sleep(0)
        finally:
            await runtime.aclose()
        assert runtime.mcp_runtime is not None
        assert not runtime.mcp_runtime.is_connected("fixture")

    anyio.run(scenario)


def test_http_cancelled_startup_releases_transport(monkeypatch: MonkeyPatch) -> None:
    entered = anyio.Event()
    finished = anyio.Event()

    async def handle(_request: httpx2.Request) -> httpx2.Response:
        entered.set()
        try:
            await anyio.sleep_forever()
        finally:
            finished.set()

    _mock_http(monkeypatch, handle)

    async def scenario() -> None:
        task = asyncio.create_task(build_runtime(mcp_servers=(_server(),)))
        with anyio.fail_after(2):
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()

    anyio.run(scenario)


@pytest.mark.process
def test_http_real_socket_discovery_and_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                first, *lines = head.split("\r\n")
                method, path, _ = first.split(" ")
                headers = dict(line.split(": ", 1) for line in lines if line)
                length = next(
                    (
                        int(value)
                        for key, value in headers.items()
                        if key.lower() == "content-length"
                    ),
                    0,
                )
                body = await reader.readexactly(length)
                response = _reply(httpx2.Request(method, f"http://localhost{path}", content=body))
                content = await response.aread()
                response_headers = dict(response.headers)
                response_headers.update(
                    {"content-length": str(len(content)), "connection": "close"}
                )
                wire_headers = "".join(
                    f"{key}: {value}\r\n" for key, value in response_headers.items()
                )
                writer.write(
                    f"HTTP/1.1 {response.status_code} OK\r\n{wire_headers}\r\n".encode() + content
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            config = McpServerConfig(name="fixture", url=f"http://127.0.0.1:{port}/mcp")
            with anyio.fail_after(5):
                runtime = await build_runtime(mcp_servers=(config,))
                try:
                    result = await runtime.tools.get("mcp__fixture__echo").run(
                        {"value": "real HTTP"}, ToolContext(cwd=tmp_path)
                    )
                    assert result.text == "real HTTP"
                finally:
                    await runtime.aclose()

    anyio.run(scenario)
