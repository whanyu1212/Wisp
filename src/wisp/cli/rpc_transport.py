"""JSONL stdin transport for the RPC frontend."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Awaitable, Callable
from queue import Empty, Queue
from threading import Thread
from typing import Protocol, TextIO, cast

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from wisp.events import ErrorEvent, WispEvent

_STDIN_READ_CHUNK_SIZE = 64 * 1024
_STDIN_THREAD_POLL_INTERVAL = 0.01
_STDIN_THREAD_QUEUE_SIZE = 100
_MAX_RPC_TRANSPORT_ERROR_CHARS = 1_000

type RpcEventWriter = Callable[[WispEvent], None]
type QueueFactory = Callable[[int], Queue[str | Exception]]
type ThreadFactory = Callable[..., Thread]
type WaitReadable = Callable[[int], Awaitable[None]]
type ReadFd = Callable[[int, int], bytes]


class RpcTextInput(Protocol):
    def fileno(self) -> int: ...

    def readline(self) -> str: ...


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
                raw_line = await anyio.to_thread.run_sync(self._stdin.readline)
            except Exception as exc:  # noqa: BLE001 - source failures become RPC errors
                await self._report_failure(send, exc)
                return
            if raw_line == "":
                await self._send_closed(send)
                return
            await self.send_line(send, raw_line)

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
                    raw_line = stdin.readline()
                    lines.put(raw_line)
                    if raw_line == "":
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
            if item == "":
                await self._send_closed(send)
                return
            await self.send_line(send, item)

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
                    await self.send_line(send, decode_rpc_stdin_line(buffer))
                await self._send_closed(send)
                return
            buffer.extend(chunk)
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break
                line = decode_rpc_stdin_line(buffer[:newline_index])
                del buffer[: newline_index + 1]
                await self.send_line(send, line)

    async def send_line(
        self,
        send: MemoryObjectSendStream[TControlEvent],
        raw_line: str,
    ) -> None:
        line = raw_line.strip()
        if not line:
            return
        command = self.parse_command(line)
        if command is not None:
            await send.send(self._input_command_factory(command))

    def parse_command(self, line: str) -> dict[str, object] | None:
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            self._write_event(ErrorEvent(message=f"Invalid RPC JSON: {exc.msg}"))
            return None
        if not isinstance(command, dict):
            self._write_event(ErrorEvent(message="RPC command must be a JSON object"))
            return None
        return cast(dict[str, object], command)

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


def rpc_stdin_needs_thread_reader(stdin_mode: int) -> bool:
    return os.name != "posix" and not stat.S_ISREG(stdin_mode)


def decode_rpc_stdin_line(raw_line: bytes | bytearray) -> str:
    return bytes(raw_line).decode("utf-8", errors="replace")


def text_input(stdin: TextIO) -> RpcTextInput:
    """Narrow a standard text stream to the transport's input protocol."""

    return cast(RpcTextInput, stdin)


__all__ = ["RpcStdinTransport"]
