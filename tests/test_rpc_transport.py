from __future__ import annotations

import io
import os
from collections import deque
from pathlib import Path
from queue import Queue

import anyio
import pytest

from wisp.cli.rpc_transport import RpcStdinTransport, read_rpc_stdin_handshake
from wisp.events import EVENT_SCHEMA_VERSION, ErrorEvent
from wisp.rpc import framing as rpc_framing
from wisp.rpc.commands import (
    ConfigureCommand,
    ShutdownCommand,
    StoreApiKeyCommand,
    UnknownCommandEnvelope,
)
from wisp.rpc.coordinator import _RpcInputClosed, _RpcInputCommand
from wisp.rpc.protocol import (
    LIVE_RPC_PROTOCOL_VERSION,
    MAX_HANDSHAKE_FRAME_BYTES,
    RpcHandshakeAccepted,
    RpcHandshakeRejected,
    RpcHandshakeRequest,
    RpcTransportLimits,
)


class _Input:
    def __init__(self, lines: list[str | Exception]) -> None:
        self._lines = deque(lines)

    def fileno(self) -> int:
        raise OSError("no file descriptor")

    def readline(self, _size: int = -1) -> str:
        item = self._lines.popleft()
        if isinstance(item, Exception):
            raise item
        return item


def _handshake_line() -> bytes:
    return (
        RpcHandshakeRequest(
            frontend_name="fixture",
            frontend_version="0.1.0",
            min_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
            max_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
            min_event_schema_version=EVENT_SCHEMA_VERSION,
            max_event_schema_version=EVENT_SCHEMA_VERSION,
            supported_capabilities=(),
            required_capabilities=(),
        ).model_dump_json()
        + "\n"
    ).encode()


def _limits() -> RpcTransportLimits:
    return RpcTransportLimits(max_client_frame_bytes=1024, max_server_frame_bytes=2048)


def test_stdin_handshake_accepts_the_first_bounded_frame() -> None:
    async def scenario() -> None:
        responses: list[object] = []
        accepted = await read_rpc_stdin_handshake(
            io.BytesIO(_handshake_line()),
            backend_package_version="0.1.0",
            supported_capabilities=(),
            limits=_limits(),
            write_response=responses.append,
        )

        assert isinstance(accepted, RpcHandshakeAccepted)
        assert responses == [accepted]
        assert accepted.type == "rpc.handshake.accepted"

    anyio.run(scenario)


@pytest.mark.parametrize(
    "frame",
    [
        b'{"id":"command-1","type":"shutdown"}\n',
        _handshake_line().replace(b'"type":"rpc.handshake.request",', b""),
        b'{"type":"rpc.handshake.request","type":"rpc.handshake.request"}\n',
        b"\xff\n",
        b"{}",
        b"x" * (MAX_HANDSHAKE_FRAME_BYTES + 1) + b"\n",
    ],
)
def test_stdin_handshake_rejects_invalid_first_frames(frame: bytes) -> None:
    async def scenario() -> None:
        responses: list[object] = []
        accepted = await read_rpc_stdin_handshake(
            io.BytesIO(frame),
            backend_package_version="0.1.0",
            supported_capabilities=(),
            limits=_limits(),
            write_response=responses.append,
        )

        assert accepted is None
        assert len(responses) == 1
        assert isinstance(responses[0], RpcHandshakeRejected)
        assert responses[0].code == "invalid_handshake"

    anyio.run(scenario)


def test_stdin_handshake_clean_eof_emits_no_response() -> None:
    async def scenario() -> None:
        responses: list[object] = []
        accepted = await read_rpc_stdin_handshake(
            io.BytesIO(),
            backend_package_version="0.1.0",
            supported_capabilities=(),
            limits=_limits(),
            write_response=responses.append,
        )
        assert accepted is None
        assert responses == []

    anyio.run(scenario)


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


@pytest.mark.parametrize("reader_name", ["read_text", "read_thread"])
def test_line_transport_rejects_oversized_frame_without_executing_suffix(
    reader_name: str,
) -> None:
    async def scenario() -> None:
        events: list[object] = []
        limit = 64
        stdin = io.BytesIO(b" " * (limit + 2) + b'{"id":"bad","type":"shutdown"}\n')
        transport = RpcStdinTransport(
            stdin=stdin,
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            max_frame_bytes=limit,
        )
        send, receive = anyio.create_memory_object_stream(2)
        async with send, receive:
            with anyio.fail_after(1):
                await getattr(transport, reader_name)(send, anyio.Event())
                closed = await receive.receive()

        assert isinstance(closed, _RpcInputClosed)
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "RPC frame exceeds the 64-byte limit"
        ]

    anyio.run(scenario)


@pytest.mark.parametrize(
    "bad_frame",
    [
        "",
        "   ",
        "not json",
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"value":1e400}',
    ],
)
def test_transport_ignores_bad_lines_and_publishes_later_commands(bad_frame: str) -> None:
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
            await transport.send_line(send, bad_frame)
            await transport.send_line(send, '  {"id":"ok","type":"shutdown"}  ')
            command = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert isinstance(command.command.known, ShutdownCommand)
        assert command.command.to_legacy_dict() == {"id": "ok", "type": "shutdown"}
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "RPC frame is not valid JSON"
        ]

    anyio.run(scenario)


@pytest.mark.parametrize(
    "bad_frame",
    [
        '{"id":"bad","type":"shutdown","extra":true}',
        '{"id":"bad","type":"configure","effort":"high","clear_effort":true}',
    ],
)
def test_transport_rejects_schema_invalid_known_commands(bad_frame: str) -> None:
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
            await transport.send_line(send, bad_frame)
            await transport.send_line(send, '{"id":"ok","type":"shutdown"}')
            command = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert isinstance(command.command.known, ShutdownCommand)
        assert command.command.to_legacy_dict() == {"id": "ok", "type": "shutdown"}
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "RPC command does not match the negotiated schema"
        ]

    anyio.run(scenario)


def test_transport_forwards_unknown_command_discriminators() -> None:
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
            await transport.send_line(send, '{"id":"future","type":"future_command"}')
            command = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert isinstance(command.command.value, UnknownCommandEnvelope)
        assert command.command.known is None
        assert command.command.command_type == "future_command"
        assert command.command.command_id == "future"
        assert command.command.to_legacy_dict() == {"id": "future", "type": "future_command"}
        assert "future_command" not in repr(command)
        assert events == []

    anyio.run(scenario)


def test_transport_validates_commands_with_json_semantics() -> None:
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
            await transport.send_line(send, '{"id":"mode","type":"configure","mode":"plan"}')
            command = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert isinstance(command.command.known, ConfigureCommand)
        assert command.command.known.mode == "plan"
        assert command.command.to_legacy_dict() == {
            "id": "mode",
            "type": "configure",
            "mode": "plan",
        }
        assert command.command.payload_size == len(
            b'{"id":"mode","type":"configure","mode":"plan"}'
        )
        assert events == []

    anyio.run(scenario)


def test_transport_redacts_store_api_key_after_parsing() -> None:
    async def scenario() -> None:
        secret = "sentinel-secret"
        events: list[object] = []
        transport = RpcStdinTransport(
            stdin=_Input([]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await transport.send_line(
                send,
                f'{{"id":"store","type":"store_api_key","provider":"anthropic",'
                f'"api_key":"{secret}"}}',
            )
            event = await receive.receive()

        assert isinstance(event, _RpcInputCommand)
        assert isinstance(event.command.known, StoreApiKeyCommand)
        assert event.command.known.api_key == secret
        assert secret not in repr(event)
        assert secret not in repr(event.command)
        legacy = event.command.to_legacy_dict()
        assert secret not in repr(legacy)
        assert event.command.payload_size == len(
            f'{{"id":"store","type":"store_api_key","provider":"anthropic",'
            f'"_api_key":"{secret}"}}'.encode()
        )
        assert events == []

    anyio.run(scenario)


def test_transport_recovers_when_json_nesting_exhausts_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError

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
            with monkeypatch.context() as patch:
                patch.setattr(
                    rpc_framing.json,
                    "loads",
                    raise_recursion_error,
                )
                await transport.send_line(send, '{"value": []}')
            await transport.send_line(send, '{"id":"ok","type":"shutdown"}')
            command = await receive.receive()

        assert isinstance(command, _RpcInputCommand)
        assert isinstance(command.command.known, ShutdownCommand)
        assert command.command.to_legacy_dict() == {"id": "ok", "type": "shutdown"}
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "RPC frame is not valid JSON"
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
        assert command.command.command_id == "ok"
        assert isinstance(closed, _RpcInputClosed)
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "Failed to read RPC stdin: pipe failed"
        ]

    anyio.run(scenario)


def test_fd_transport_rejects_unterminated_final_line_at_eof() -> None:
    async def scenario() -> None:
        events: list[object] = []
        chunks = deque([b'{"id":"last","type":"shutdown"}', b""])

        async def wait_readable(_fd: int) -> None:
            return None

        transport = RpcStdinTransport(
            stdin=_Input([]),
            write_event=events.append,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            wait_readable=wait_readable,
            read_fd=lambda _fd, _size: chunks.popleft(),
        )
        send, receive = anyio.create_memory_object_stream(2)
        async with send, receive:
            await transport.read_fd(send, anyio.Event(), 7)
            closed = await receive.receive()

        assert isinstance(closed, _RpcInputClosed)
        assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
            "RPC stream ended with an incomplete frame"
        ]

    anyio.run(scenario)


def test_frame_parser_accepts_max_sized_crlf_frame_across_chunks() -> None:
    limit = 64
    buffer = bytearray(b"x" * limit + b"\r")

    assert rpc_framing.pop_rpc_frame(buffer, max_frame_bytes=limit) is None

    buffer.extend(b"\nnext\n")
    assert rpc_framing.pop_rpc_frame(buffer, max_frame_bytes=limit) == b"x" * limit
    assert rpc_framing.pop_rpc_frame(buffer, max_frame_bytes=limit) == b"next"


def test_frame_parser_rejects_oversized_crlf_frame() -> None:
    limit = 64
    buffer = bytearray(b"x" * (limit + 1) + b"\r\n")

    with pytest.raises(rpc_framing.RpcFrameError, match="64-byte limit"):
        rpc_framing.pop_rpc_frame(buffer, max_frame_bytes=limit)


def test_transport_dispatches_buffered_pipe_lines() -> None:
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")

    async def scenario() -> None:
        transport = RpcStdinTransport(
            stdin=stdin,
            write_event=lambda _event: None,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
        )
        stop_reader = anyio.Event()
        send, receive = anyio.create_memory_object_stream(10)
        async with receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(transport.read, send, stop_reader)
                os.write(
                    write_fd,
                    b'{"id":"cancel-1","type":"cancel","target_id":"cmd-1"}\n'
                    b'{"id":"shutdown-1","type":"shutdown"}\n',
                )
                with anyio.fail_after(1):
                    first = await receive.receive()
                    second = await receive.receive()
                assert isinstance(first, _RpcInputCommand)
                assert isinstance(second, _RpcInputCommand)
                assert first.command.command_id == "cancel-1"
                assert second.command.command_id == "shutdown-1"
                stop_reader.set()
                task_group.cancel_scope.cancel()

    try:
        anyio.run(scenario)
    finally:
        os.close(write_fd)
        stdin.close()


def test_thread_transport_uses_configured_bounded_queue() -> None:
    created_queue_sizes: list[int] = []

    class RecordingQueue(Queue[str | Exception]):
        def __init__(self, maxsize: int = 0) -> None:
            created_queue_sizes.append(maxsize)
            super().__init__(maxsize=maxsize)

    async def scenario() -> None:
        stop_reader = anyio.Event()
        stop_reader.set()
        transport = RpcStdinTransport(
            stdin=_Input([""]),
            write_event=lambda _event: None,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            queue_factory=RecordingQueue,
            thread_queue_size=7,
        )
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive:
            await transport.read_thread(send, stop_reader)

    anyio.run(scenario)

    assert created_queue_sizes == [7]


def test_thread_transport_defaults_to_single_frame_queue() -> None:
    created_queue_sizes: list[int] = []

    class RecordingQueue(Queue[str | bytes | Exception]):
        def __init__(self, maxsize: int = 0) -> None:
            created_queue_sizes.append(maxsize)
            super().__init__(maxsize=maxsize)

    async def scenario() -> None:
        stop_reader = anyio.Event()
        stop_reader.set()
        transport = RpcStdinTransport(
            stdin=_Input([""]),
            write_event=lambda _event: None,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            queue_factory=RecordingQueue,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive:
            await transport.read_thread(send, stop_reader)

    anyio.run(scenario)

    assert created_queue_sizes == [1]


def test_transport_uses_thread_reader_for_windows_pipe() -> None:
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")

    async def fail_wait_readable(_fd: int) -> None:
        raise AssertionError("wait_readable should not be used for Windows pipe stdin")

    async def scenario() -> None:
        transport = RpcStdinTransport(
            stdin=stdin,
            write_event=lambda _event: None,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
            needs_thread_reader=lambda _mode: True,
            wait_readable=fail_wait_readable,
        )
        stop_reader = anyio.Event()
        send, receive = anyio.create_memory_object_stream(10)
        async with receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(transport.read, send, stop_reader)
                os.write(
                    write_fd,
                    b'{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
                    b'{"id":"shutdown-1","type":"shutdown"}\n',
                )
                with anyio.fail_after(1):
                    first = await receive.receive()
                    second = await receive.receive()
                assert isinstance(first, _RpcInputCommand)
                assert isinstance(second, _RpcInputCommand)
                assert first.command.command_id == "prompt-1"
                assert second.command.command_id == "shutdown-1"
                stop_reader.set()
                task_group.cancel_scope.cancel()

    try:
        anyio.run(scenario)
    finally:
        os.close(write_fd)
        stdin.close()


def test_transport_handles_regular_file_stdin(tmp_path: Path) -> None:
    input_path = tmp_path / "commands.jsonl"
    input_path.write_text(
        '{"id":"prompt-1","type":"prompt","prompt":"hello"}\n'
        '{"id":"shutdown-1","type":"shutdown"}\n',
        encoding="utf-8",
    )
    stdin = input_path.open("r", encoding="utf-8")

    async def scenario() -> None:
        transport = RpcStdinTransport(
            stdin=stdin,
            write_event=lambda _event: None,
            input_command_factory=_RpcInputCommand,
            input_closed_factory=_RpcInputClosed,
        )
        stop_reader = anyio.Event()
        send, receive = anyio.create_memory_object_stream(10)
        async with receive:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(transport.read, send, stop_reader)
                with anyio.fail_after(1):
                    first = await receive.receive()
                    second = await receive.receive()
                    closed = await receive.receive()
                assert isinstance(first, _RpcInputCommand)
                assert isinstance(second, _RpcInputCommand)
                assert isinstance(closed, _RpcInputClosed)
                assert first.command.command_id == "prompt-1"
                assert second.command.command_id == "shutdown-1"
                stop_reader.set()
                task_group.cancel_scope.cancel()

    try:
        anyio.run(scenario)
    finally:
        stdin.close()
