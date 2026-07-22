from __future__ import annotations

from collections import deque

import anyio

from wisp.cli.rpc_coordinator import _RpcInputClosed, _RpcInputCommand
from wisp.cli.rpc_transport import RpcStdinTransport
from wisp.events import ErrorEvent


class _Input:
    def __init__(self, lines: list[str | Exception]) -> None:
        self._lines = deque(lines)

    def fileno(self) -> int:
        raise OSError("no file descriptor")

    def readline(self) -> str:
        item = self._lines.popleft()
        if isinstance(item, Exception):
            raise item
        return item


def test_text_transport_reports_bounded_source_failure_and_closes() -> None:
    async def scenario() -> None:
        events: list[object] = []
        transport = RpcStdinTransport(
            stdin=_Input([OSError("x" * 500)]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            max_error_chars=80,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await transport.read_text(send, anyio.Event())
            closed = await receive.receive()

        assert isinstance(closed, _RpcInputClosed)
        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert events[0].message.startswith("Failed to read RPC stdin: ")
        assert len(events[0].message) == 80

    anyio.run(scenario)


def test_fd_transport_reports_wait_failure_and_closes() -> None:
    async def scenario() -> None:
        events: list[object] = []

        async def fail_wait_readable(_fd: int) -> None:
            raise OSError("pipe failed")

        transport = RpcStdinTransport(
            stdin=_Input([]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            wait_readable=fail_wait_readable,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await transport.read_fd(send, anyio.Event(), 7)
            closed = await receive.receive()

        assert isinstance(closed, _RpcInputClosed)
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "Failed to read RPC stdin: pipe failed"
        ]

    anyio.run(scenario)


def test_thread_transport_reports_reader_failure_and_closes() -> None:
    async def scenario() -> None:
        events: list[object] = []
        transport = RpcStdinTransport(
            stdin=_Input([RuntimeError("thread failed")]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            with anyio.fail_after(1):
                await transport.read_thread(send, anyio.Event())
                closed = await receive.receive()

        assert isinstance(closed, _RpcInputClosed)
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "Failed to read RPC stdin: thread failed"
        ]

    anyio.run(scenario)


def test_transport_ignores_bad_lines_and_publishes_later_commands() -> None:
    async def scenario() -> None:
        events: list[object] = []
        transport = RpcStdinTransport(
            stdin=_Input([]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await transport.send_line(send, "not json")
            await transport.send_line(send, '  {"id":"ok","type":"shutdown"}  ')
            command = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert command.command == {"id": "ok", "type": "shutdown"}
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "Invalid RPC JSON: Expecting value"
        ]

    anyio.run(scenario)


def test_fd_transport_delivers_valid_command_before_later_source_failure() -> None:
    async def scenario() -> None:
        events: list[object] = []
        chunks: deque[bytes | Exception] = deque(
            [b'{"id":"ok","type":"shutdown"}\n', OSError("pipe failed")]
        )

        async def wait_readable(_fd: int) -> None:
            return None

        def read_fd(_fd: int, _size: int) -> bytes:
            item = chunks.popleft()
            if isinstance(item, Exception):
                raise item
            return item

        transport = RpcStdinTransport(
            stdin=_Input([]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            wait_readable=wait_readable,
            read_fd=read_fd,
        )
        send, receive = anyio.create_memory_object_stream(2)
        async with send, receive:
            await transport.read_fd(send, anyio.Event(), 7)
            command = await receive.receive()
            closed = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert command.command["id"] == "ok"
        assert isinstance(closed, _RpcInputClosed)
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "Failed to read RPC stdin: pipe failed"
        ]

    anyio.run(scenario)


def test_fd_transport_flushes_unterminated_final_line_at_eof() -> None:
    async def scenario() -> None:
        chunks = deque([b'{"id":"last","type":"shutdown"}', b""])

        async def wait_readable(_fd: int) -> None:
            return None

        transport = RpcStdinTransport(
            stdin=_Input([]),
            write_event=lambda _event: None,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            wait_readable=wait_readable,
            read_fd=lambda _fd, _size: chunks.popleft(),
        )
        send, receive = anyio.create_memory_object_stream(2)
        async with send, receive:
            await transport.read_fd(send, anyio.Event(), 7)
            command = await receive.receive()
            closed = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert command.command["id"] == "last"
        assert isinstance(closed, _RpcInputClosed)

    anyio.run(scenario)
