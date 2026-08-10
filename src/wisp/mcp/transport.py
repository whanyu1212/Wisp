"""Bounded MCP stdio transport that redacts invalid server frames."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TextIO

import anyio
import anyio.lowlevel
import mcp_types as types
from anyio.streams.memory import MemoryObjectSendStream
from mcp.client._transport import TransportStreams
from mcp.client.stdio import (
    StdioServerParameters,
    _aclose_all,
    _create_platform_compatible_process,
    _drain_stdout,
    _get_executable_command,
    _stop_server_process,
    get_default_environment,
)
from mcp.shared.message import SessionMessage

MAX_MCP_FRAME_BYTES = 2_097_152
_WRITER_FLUSH_TIMEOUT_SECONDS = 0.5
_INVALID_SERVER_FRAME = "MCP server returned an invalid protocol frame"


class McpServerFrameError(ValueError):
    """Raised without retaining the invalid server-controlled frame."""


@asynccontextmanager
async def bounded_stdio_client(
    server: StdioServerParameters,
    *,
    errlog: TextIO,
    max_frame_bytes: int = MAX_MCP_FRAME_BYTES,
) -> AsyncGenerator[TransportStreams, None]:
    """Connect over official SDK streams while bounding frames before parsing."""

    process = await _create_platform_compatible_process(
        command=_get_executable_command(server.command),
        args=server.args,
        env=get_default_environment() | (server.env or {}),
        errlog=errlog,
        cwd=server.cwd,
    )
    read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](0)
    writer_done = anyio.Event()

    async def stdout_reader() -> None:
        assert process.stdout is not None
        buffer = bytearray()
        try:
            async with read_send:
                while True:
                    try:
                        chunk = await process.stdout.receive()
                    except anyio.EndOfStream:
                        break
                    buffer.extend(chunk)
                    while (newline := buffer.find(b"\n")) >= 0:
                        if newline > max_frame_bytes:
                            await _report_invalid_frame(read_send)
                            return
                        line = bytes(buffer[:newline])
                        del buffer[: newline + 1]
                        try:
                            message = _parse_frame(line, server)
                        except (UnicodeError, ValueError):
                            await _report_invalid_frame(read_send)
                            return
                        try:
                            await read_send.send(message)
                        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                            return
                    if len(buffer) > max_frame_bytes:
                        await _report_invalid_frame(read_send)
                        return
                if buffer:
                    await _report_invalid_frame(read_send)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, ConnectionError, OSError):
            pass
        finally:
            await _drain_stdout(process)

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_receive:
                async for message in write_receive:
                    serialized = message.message.model_dump_json(by_alias=True, exclude_unset=True)
                    data = (serialized + "\n").encode(
                        encoding=server.encoding,
                        errors=server.encoding_error_handler,
                    )
                    await process.stdin.send(data)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
            await read_send.aclose()
        finally:
            writer_done.set()

    async def shutdown() -> None:
        read_receive.close()
        write_send.close()
        with anyio.move_on_after(_WRITER_FLUSH_TIMEOUT_SECONDS):
            await writer_done.wait()
        await _stop_server_process(process)
        await _aclose_all(read_receive, write_send, read_send, write_receive)
        await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(stdout_reader)
        task_group.start_soon(stdin_writer)
        try:
            yield read_receive, write_send
        finally:
            with anyio.CancelScope(shield=True):
                await shutdown()
            task_group.cancel_scope.cancel()
    await anyio.lowlevel.cancel_shielded_checkpoint()


def _parse_frame(frame: bytes, server: StdioServerParameters) -> SessionMessage:
    text = frame.decode(encoding=server.encoding, errors=server.encoding_error_handler)
    message = types.jsonrpc_message_adapter.validate_json(text, by_name=False)
    return SessionMessage(message)


async def _report_invalid_frame(
    send: MemoryObjectSendStream[SessionMessage | Exception],
) -> None:
    await send.send(McpServerFrameError(_INVALID_SERVER_FRAME))


__all__ = ["MAX_MCP_FRAME_BYTES", "McpServerFrameError", "bounded_stdio_client"]
