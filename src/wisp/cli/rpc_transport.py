"""JSONL stdin transport for the RPC frontend."""

from __future__ import annotations

import os
import stat
from collections.abc import Awaitable, Callable
from functools import partial
from queue import Empty, Queue
from threading import Thread
from typing import Protocol, TextIO, cast

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from pydantic import ValidationError

from wisp.events import EVENT_SCHEMA_VERSION, ErrorEvent, WispEvent
from wisp.rpc.framing import RpcFrameError, decode_rpc_object, pop_rpc_frame
from wisp.rpc.protocol import (
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    MAX_LIVE_RPC_PROTOCOL_VERSION,
    MIN_LIVE_RPC_PROTOCOL_VERSION,
    RpcHandshakeAccepted,
    RpcHandshakeRejected,
    RpcHandshakeRequestAdapter,
    RpcHandshakeResponse,
    RpcTransportLimits,
    negotiate_rpc_handshake,
)

_STDIN_READ_CHUNK_SIZE = 64 * 1024
_STDIN_THREAD_POLL_INTERVAL = 0.01
# Keep at most one complete raw frame ahead of async parsing. Each negotiated
# frame may be large, so a deeper queue would multiply the per-frame limit into
# an unsafe aggregate allocation while the coordinator is backpressured.
_STDIN_THREAD_QUEUE_SIZE = 1
_MAX_RPC_TRANSPORT_ERROR_CHARS = 1_000

type RpcEventWriter = Callable[[WispEvent], None]
type QueueFactory = Callable[[int], Queue[str | bytes | Exception]]
type ThreadFactory = Callable[..., Thread]
type WaitReadable = Callable[[int], Awaitable[None]]
type ReadFd = Callable[[int, int], bytes]


class RpcTextInput(Protocol):
    def fileno(self) -> int: ...

    def readline(self, size: int = -1) -> str | bytes: ...


async def read_rpc_stdin_handshake(
    stdin: RpcTextInput,
    *,
    backend_package_version: str,
    supported_capabilities: tuple[str, ...],
    limits: RpcTransportLimits,
    write_response: Callable[[RpcHandshakeResponse], None],
) -> RpcHandshakeAccepted | None:
    """Read and answer the mandatory first external RPC frame."""

    raw_line = await anyio.to_thread.run_sync(
        partial(stdin.readline, MAX_HANDSHAKE_FRAME_BYTES + 2)
    )
    if raw_line in {"", b""}:
        return None
    encoded_line = raw_line.encode("utf-8") if isinstance(raw_line, str) else raw_line
    response: RpcHandshakeResponse
    try:
        if not encoded_line.endswith(b"\n"):
            raise RpcFrameError("RPC handshake frame is incomplete")
        frame = encoded_line[:-1]
        if frame.endswith(b"\r"):
            frame = frame[:-1]
        payload = decode_rpc_object(frame, max_frame_bytes=MAX_HANDSHAKE_FRAME_BYTES)
        if "type" not in payload:
            raise RpcFrameError("RPC handshake frame is missing its type discriminator")
        request = RpcHandshakeRequestAdapter.validate_json(frame)
    except (RpcFrameError, ValidationError, ValueError):
        response = RpcHandshakeRejected(
            code="invalid_handshake",
            message="The first RPC frame is not a valid handshake request.",
            backend_package_version=backend_package_version,
            min_protocol_version=MIN_LIVE_RPC_PROTOCOL_VERSION,
            max_protocol_version=MAX_LIVE_RPC_PROTOCOL_VERSION,
            event_schema_version=EVENT_SCHEMA_VERSION,
        )
    else:
        response = negotiate_rpc_handshake(
            request,
            backend_package_version=backend_package_version,
            supported_capabilities=supported_capabilities,
            limits=limits,
        )
    write_response(response)
    return response if isinstance(response, RpcHandshakeAccepted) else None


class RpcStdinTransport[TControlEvent]:
    """Read JSONL commands without owning coordinator or agent behavior."""

    def __init__(
        self,
        *,
        stdin: RpcTextInput,
        write_event: RpcEventWriter,
        input_command_factory: Callable[[dict[str, object]], TControlEvent],
        input_closed_factory: Callable[[], TControlEvent],
        queue_factory: QueueFactory = lambda maxsize: Queue(maxsize=maxsize),
        thread_factory: ThreadFactory = Thread,
        wait_readable: WaitReadable = anyio.wait_readable,
        read_fd: ReadFd = os.read,
        needs_thread_reader: Callable[[int], bool] | None = None,
        read_chunk_size: int = _STDIN_READ_CHUNK_SIZE,
        thread_poll_interval: float = _STDIN_THREAD_POLL_INTERVAL,
        thread_queue_size: int = _STDIN_THREAD_QUEUE_SIZE,
        max_error_chars: int = _MAX_RPC_TRANSPORT_ERROR_CHARS,
        max_frame_bytes: int = MAX_LIVE_RPC_FRAME_BYTES,
    ) -> None:
        self._stdin = stdin
        self._write_event = write_event
        self._input_command_factory = input_command_factory
        self._input_closed_factory = input_closed_factory
        self._queue_factory = queue_factory
        self._thread_factory = thread_factory
        self._wait_readable = wait_readable
        self._read_fd = read_fd
        self._needs_thread_reader = needs_thread_reader or rpc_stdin_needs_thread_reader
        self._read_chunk_size = read_chunk_size
        self._thread_poll_interval = thread_poll_interval
        self._thread_queue_size = thread_queue_size
        self._max_error_chars = max_error_chars
        self._max_frame_bytes = max_frame_bytes

    async def read(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        stop_reader: anyio.Event,
    ) -> None:
        """Select a platform-appropriate stdin reader and publish control events."""

        async with send:
            try:
                fd = self._stdin.fileno()
                stdin_mode = os.fstat(fd).st_mode
            except (AttributeError, OSError, ValueError):
                await self.read_text(send, stop_reader)
                return
            if stat.S_ISREG(stdin_mode):
                await self.read_text(send, stop_reader)
                return
            if self._needs_thread_reader(stdin_mode):
                await self.read_thread(send, stop_reader)
                return
            await self.read_fd(send, stop_reader, fd)

    async def read_text(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        stop_reader: anyio.Event,
    ) -> None:
        while not stop_reader.is_set():
            try:
                raw_line = await anyio.to_thread.run_sync(self._readline)
            except Exception as exc:  # noqa: BLE001 - source failures become RPC errors
                await self._report_failure(send, exc)
                return
            if raw_line in {"", b""}:
                await self._send_closed(send)
                return
            if not await self._accept_readline_result(send, raw_line):
                return

    async def read_thread(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        stop_reader: anyio.Event,
    ) -> None:
        lines = self._queue_factory(self._thread_queue_size)
        stdin = self._stdin

        def read_lines() -> None:
            try:
                while True:
                    raw_line = stdin.readline(self._max_frame_bytes + 2)
                    lines.put(raw_line)
                    if raw_line in {"", b""}:
                        return
            except Exception as exc:  # noqa: BLE001 - forwarded to the async reader
                lines.put(exc)

        self._thread_factory(
            target=read_lines,
            name="wisp-rpc-stdin-reader",
            daemon=True,
        ).start()
        while not stop_reader.is_set():
            try:
                item = lines.get_nowait()
            except Empty:
                await anyio.sleep(self._thread_poll_interval)
                continue
            if isinstance(item, Exception):
                await self._report_failure(send, item)
                return
            if item in {"", b""}:
                await self._send_closed(send)
                return
            if not await self._accept_readline_result(send, item):
                return

    async def read_fd(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        stop_reader: anyio.Event,
        fd: int,
    ) -> None:
        buffer = bytearray()
        while not stop_reader.is_set():
            try:
                await self._wait_readable(fd)
                if stop_reader.is_set():
                    return
                chunk = self._read_fd(fd, self._read_chunk_size)
            except BlockingIOError:
                continue
            except Exception as exc:  # noqa: BLE001 - source failures become RPC errors
                await self._report_failure(send, exc)
                return
            if chunk == b"":
                if buffer:
                    self._write_event(
                        ErrorEvent(message="RPC stream ended with an incomplete frame")
                    )
                await self._send_closed(send)
                return
            buffer.extend(chunk)
            while True:
                try:
                    frame = pop_rpc_frame(buffer, max_frame_bytes=self._max_frame_bytes)
                except RpcFrameError as exc:
                    self._write_event(ErrorEvent(message=str(exc)))
                    await self._send_closed(send)
                    return
                if frame is None:
                    break
                if not await self.send_line(send, frame):
                    return

    async def send_line(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        raw_line: str | bytes,
    ) -> bool:
        frame = raw_line.encode("utf-8") if isinstance(raw_line, str) else raw_line
        if frame.endswith(b"\n"):
            frame = frame[:-1]
        if frame.endswith(b"\r"):
            frame = frame[:-1]
        if not frame.strip():
            return True
        if len(frame) > self._max_frame_bytes:
            self._write_event(
                ErrorEvent(message=f"RPC frame exceeds the {self._max_frame_bytes}-byte limit")
            )
            await self._send_closed(send)
            return False
        command = self.parse_command(frame)
        if command is not None:
            await send.send(self._input_command_factory(command))
        return True

    async def _accept_readline_result(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        raw_line: str | bytes,
    ) -> bool:
        complete = (
            raw_line.endswith("\n") if isinstance(raw_line, str) else raw_line.endswith(b"\n")
        )
        if not complete:
            frame = raw_line.encode("utf-8") if isinstance(raw_line, str) else raw_line
            message = (
                f"RPC frame exceeds the {self._max_frame_bytes}-byte limit"
                if len(frame) > self._max_frame_bytes
                else "RPC stream ended with an incomplete frame"
            )
            self._write_event(ErrorEvent(message=message))
            await self._send_closed(send)
            return False
        return await self.send_line(send, raw_line)

    def parse_command(self, frame: bytes) -> dict[str, object] | None:
        try:
            return decode_rpc_object(frame, max_frame_bytes=self._max_frame_bytes)
        except RpcFrameError as exc:
            self._write_event(ErrorEvent(message=str(exc)))
            return None

    async def _report_failure(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        exc: Exception,
    ) -> None:
        prefix = "Failed to read RPC stdin: "
        detail = str(exc) or type(exc).__name__
        max_detail_chars = max(0, self._max_error_chars - len(prefix))
        if len(detail) > max_detail_chars:
            detail = detail[:max_detail_chars]
        self._write_event(ErrorEvent(message=f"{prefix}{detail}"))
        await self._send_closed(send)

    async def _send_closed(self, send: MemoryObjectSendStream[TControlEvent]) -> None:
        await send.send(self._input_closed_factory())

    def _readline(self) -> str | bytes:
        return self._stdin.readline(self._max_frame_bytes + 2)


def rpc_stdin_needs_thread_reader(stdin_mode: int) -> bool:
    return os.name != "posix" and not stat.S_ISREG(stdin_mode)


def decode_rpc_stdin_line(raw_line: bytes | bytearray) -> str:
    return bytes(raw_line).decode("utf-8")


def text_input(stdin: TextIO) -> RpcTextInput:
    """Narrow a standard text stream to the transport's input protocol."""

    return cast(RpcTextInput, stdin)


__all__ = ["RpcStdinTransport"]
