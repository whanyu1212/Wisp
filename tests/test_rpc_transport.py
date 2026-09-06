from __future__ import annotations

import io
import json
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
    GetMessagesCommand,
    ParsedRpcCommand,
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
        *[
            json.dumps({"id": "inspection-1", "type": command_type, **invalid_fields})
            for command_type in (
                "get_state",
                "get_commands",
                "get_model_catalog",
                "get_connection_catalog",
                "get_skills",
                "get_mcp_status",
            )
            for invalid_fields in (
                {"extra": True},
                {"id": []},
                {"id": ""},
                {"id": 1},
                {"id": "x" * 257},
            )
        ],
        *[
            json.dumps(
                {
                    "type": command_type,
                    "id": "connection-1",
                    "provider": "anthropic",
                    **({"api_key": "sentinel-key"} if command_type == "store_api_key" else {}),
                    **bad_fields,
                }
            )
            for command_type in ("store_api_key", "disconnect_provider", "begin_device_code")
            for bad_fields in (
                {"id": []},
                {"id": ""},
                {"id": "x" * 257},
                {"provider": None},
                {"provider": ""},
                {"provider": 1},
                {"provider": "x" * 257},
                {"extra": True},
            )
        ],
        *[
            json.dumps({"type": "store_api_key", "provider": "anthropic", **key_fields})
            for key_fields in (
                {},
                {"api_key": None},
                {"api_key": ""},
                {"api_key": 1},
                {"api_key": "sentinel-" + "x" * 8192},
            )
        ],
        *[
            json.dumps({"type": command_type})
            for command_type in ("disconnect_provider", "begin_device_code")
        ],
        '{"type":"store_api_key","api_key":"sentinel-key"}',
        '{"id": "steer", "type": "steer"}',
        '{"id": "mode-kind", "type": "set_queue_mode", "kind": "unknown", "mode": "all"}',
        '{"id": "mode-value", "type": "set_queue_mode", "kind": "steering", "mode": "invalid"}',
        '{"id": "pop", "type": "pop_queue"}',
        '{"id": "clear", "type": "clear_queue", "kind": "unknown"}',
        '{"id": "mode-kind-container", "type": "set_queue_mode", "kind": [], "mode": "all"}',
        '{"id": "mode-value-container", "type": "set_queue_mode", "kind": "steering", "mode": {}}',
        '{"id": "pop-container", "type": "pop_queue", "kind": []}',
        '{"id": "clear-container", "type": "clear_queue", "kind": {}}',
        *[
            json.dumps({**payload, **bad_fields})
            for payload in (
                {"type": "steer", "content": "text"},
                {"type": "follow_up", "content": "text"},
                {"type": "get_queue_state"},
                {"type": "set_queue_mode", "kind": "steering", "mode": "all"},
                {"type": "pop_queue", "kind": "steering"},
                {"type": "clear_queue"},
            )
            for bad_fields in ({"id": []}, {"id": ""}, {"id": "x" * 257}, {"extra": True})
        ],
        *[
            json.dumps({"type": kind, "content": value})
            for kind in ("steer", "follow_up")
            for value in (None, 1, [], {})
        ],
        '{"type":"follow_up"}',
        '{"type":"set_queue_mode","kind":null,"mode":"all"}',
        '{"type":"set_queue_mode","kind":"steering","mode":null}',
        '{"type":"set_queue_mode","kind":"steering"}',
        '{"type":"set_queue_mode","mode":"all"}',
        '{"type":"pop_queue","kind":null}',
        *[
            json.dumps({**payload, **fields})
            for payload in (
                {"type": "cancel", "target_id": "target"},
                {"type": "approval", "call_id": "call", "approved": True},
                {"type": "trust", "request_id": "request", "trusted": True},
                {"type": "shutdown"},
            )
            for fields in ({"id": []}, {"id": ""}, {"id": "x" * 257}, {"extra": True})
        ],
        *[
            json.dumps({**payload, reference: value})
            for payload, reference in (
                ({"type": "cancel"}, "target_id"),
                ({"type": "approval", "approved": True}, "call_id"),
                ({"type": "trust", "trusted": True}, "request_id"),
            )
            for value in (None, "", 1, [], {})
        ],
        *[
            json.dumps({**payload, field: value})
            for payload, field in (
                ({"type": "approval", "call_id": "call"}, "approved"),
                ({"type": "trust", "request_id": "request"}, "trusted"),
                ({"type": "trust", "request_id": "request", "trusted": True}, "transient"),
            )
            for value in ("true", 1, [], {})
        ],
        *[
            json.dumps({"type": "approval", "call_id": "call", "approved": False, "scope": scope})
            for scope in ("once", "tool_session", "all_session")
        ],
        *[
            json.dumps({"type": "approval", "call_id": "call", "approved": True, "scope": scope})
            for scope in ("forever", [])
        ],
        *[
            json.dumps({**payload, "reason": value})
            for payload in (
                {"type": "approval", "call_id": "call", "approved": True},
                {"type": "trust", "request_id": "request", "trusted": True},
            )
            for value in (1, [], {})
        ],
        '{"type":"cancel"}',
        '{"type":"approval","approved":true}',
        '{"type":"approval","call_id":"call"}',
        '{"type":"approval","call_id":"call","approved":null}',
        '{"type":"trust","trusted":true}',
        '{"type":"trust","request_id":"request"}',
        '{"type":"trust","request_id":"request","trusted":null}',
        *[
            json.dumps({**payload, **fields})
            for payload in (
                {"type": "prompt", "prompt": "text"},
                {"type": "init"},
                {"type": "compact"},
                {"type": "get_session_stats"},
            )
            for fields in ({"id": []}, {"id": ""}, {"id": "x" * 257}, {"extra": True})
        ],
        '{"type":"prompt"}',
        *[json.dumps({"type": "prompt", "prompt": value}) for value in (None, 1, [], {})],
        *[json.dumps({"type": "compact", "instructions": value}) for value in (1, [], {})],
        '{"id":"bad","type":"configure"}',
        '{"id":"bad","type":"configure","mode":"invalid"}',
        '{"id":"bad","type":"configure","effort":5}',
        '{"id":"bad","type":"configure","auto_compaction_enabled":0}',
        '{"id":"bad","type":"configure","effort":"high","clear_effort":true}',
        '{"id":"bad","type":"get_messages","limit":true}',
        '{"id":"bad","type":"get_messages","limit":0}',
        '{"id":"bad","type":"get_messages","session_id":""}',
        '{"id":"bad","type":"get_messages","before_entry_id":"one","after_entry_id":"two"}',
        '{"id":"bad","type":"get_messages","entry_ids":["entry","entry"]}',
        '{"id":"bad","type":"get_messages","full_content":true}',
        '{"id":"bad","type":"get_messages","complete_structure":"yes"}',
        '{"id":"bad","type":"get_sessions","limit":true}',
        '{"id":"bad","type":"get_sessions","limit":-1}',
        '{"id":"bad","type":"get_sessions","limit":201}',
        '{"id":"bad","type":"get_session_tree","limit":true}',
        '{"id":"bad","type":"get_session_tree","limit":0}',
        '{"id":"bad","type":"get_session_tree","after_entry_id":""}',
        '{"id":"bad","type":"select_session"}',
        '{"id":"bad","type":"select_session","session_id":""}',
        '{"id":"bad","type":"select_session","session_id":null}',
        '{"id":"bad","type":"clone_session","extra":true}',
        '{"id":"bad","type":"new_session","extra":true}',
        '{"id":"bad","type":"fork_session"}',
        '{"id":"bad","type":"fork_session","entry_id":""}',
        '{"id":"bad","type":"fork_session","entry_id":null}',
        '{"id":"bad","type":"navigate_session_tree"}',
        '{"id":"bad","type":"navigate_session_tree","entry_id":""}',
        '{"id":"bad","type":"navigate_session_tree","entry_id":null}',
        '{"id":"bad","type":"set_session_name"}',
        '{"id":"bad","type":"set_session_name","name":5}',
        '{"id":"bad","type":"set_session_name","name":null}',
        '{"id":"bad","type":"set_session_name","name":"ok","session_id":""}',
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


def test_transport_preserves_whitespace_api_key_for_execution_validation() -> None:
    events: list[object] = []
    transport = RpcStdinTransport(
        stdin=_Input([]),
        write_event=events.append,
        input_command_factory=_RpcInputCommand,
        input_closed_factory=_RpcInputClosed,
    )
    parsed = transport.parse_command(
        json.dumps({"type": "store_api_key", "provider": "anthropic", "api_key": " \t\n"}).encode()
    )
    assert parsed is not None
    assert isinstance(parsed.known, StoreApiKeyCommand)
    assert parsed.known.api_key == " \t\n"
    assert events == []


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
        assert command.command.provided_fields == {"id", "type", "mode"}
        assert events == []

    anyio.run(scenario)


def test_transport_preserves_explicit_null_field_presence() -> None:
    events: list[object] = []
    transport = RpcStdinTransport(
        stdin=_Input([]),
        write_event=events.append,
        input_command_factory=_RpcInputCommand,
        input_closed_factory=_RpcInputClosed,
    )

    omitted = transport.parse_command(b'{"id":"omitted","type":"configure","model":"gpt-5.5-pro"}')
    explicit_null = transport.parse_command(
        b'{"id":"null","type":"configure","provider":null,"model":"gpt-5.5-pro"}'
    )

    assert omitted is not None
    assert explicit_null is not None
    assert isinstance(omitted.known, ConfigureCommand)
    assert isinstance(explicit_null.known, ConfigureCommand)
    assert omitted.known.provider is None
    assert explicit_null.known.provider is None
    assert "provider" not in omitted.provided_fields
    assert "provider" in explicit_null.provided_fields
    assert events == []


def test_sdk_style_configure_omits_none_fields_from_presence_metadata() -> None:
    parsed = ParsedRpcCommand.from_known(
        ConfigureCommand(
            id="sdk",
            provider=None,
            model="gpt-5.5-pro",
            effort=None,
            auto_compaction_enabled=None,
            mode=None,
        )
    )

    assert parsed.provided_fields == {"id", "type", "model", "clear_effort"}


def test_message_read_preserves_null_presence_and_sdk_omission() -> None:
    events: list[object] = []
    transport = RpcStdinTransport(
        stdin=_Input([]),
        write_event=events.append,
        input_command_factory=_RpcInputCommand,
        input_closed_factory=_RpcInputClosed,
    )
    explicit_null = transport.parse_command(
        b'{"id":"null","type":"get_messages","entry_ids":null,'
        b'"complete_structure":null,"full_content":null}'
    )
    sdk_style = ParsedRpcCommand.from_known(
        GetMessagesCommand(
            id="sdk",
            session_id=None,
            before_entry_id=None,
            after_entry_id=None,
            entry_ids=None,
            complete_structure=None,
            full_content=None,
            allow_during_prompt=None,
        )
    )

    assert explicit_null is not None
    assert isinstance(explicit_null.known, GetMessagesCommand)
    assert explicit_null.known.entry_ids is None
    assert explicit_null.known.complete_structure is None
    assert explicit_null.known.full_content is None
    assert {
        "entry_ids",
        "complete_structure",
        "full_content",
    } < explicit_null.provided_fields
    assert "entry_ids" not in sdk_style.provided_fields
    assert "complete_structure" not in sdk_style.provided_fields
    assert "full_content" not in sdk_style.provided_fields
    assert events == []


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
        assert "api_key" in event.command.provided_fields
        assert "_api_key" not in event.command.provided_fields
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


@pytest.mark.parametrize("value", [" \t", "x" * 1024])
@pytest.mark.parametrize(
    "payload,reference",
    [
        ({"type": "cancel"}, "target_id"),
        ({"type": "approval", "approved": True}, "call_id"),
        ({"type": "trust", "trusted": True}, "request_id"),
    ],
)
def test_transport_preserves_control_reference_values(
    value: str, payload: dict[str, object], reference: str
) -> None:
    events: list[object] = []
    transport = RpcStdinTransport(
        stdin=_Input([]),
        write_event=events.append,
        input_command_factory=_RpcInputCommand,
        input_closed_factory=_RpcInputClosed,
    )
    parsed = transport.parse_command(json.dumps({**payload, reference: value}).encode())
    assert parsed is not None and parsed.known is not None
    assert getattr(parsed.known, reference) == value
    assert events == []


@pytest.mark.parametrize(
    "payload",
    [
        *[{"type": "prompt", "prompt": value} for value in ("", " \t", "日本語")],
        *[
            {"type": "compact", **fields}
            for fields in (
                {},
                {"instructions": None},
                {"instructions": ""},
                {"instructions": " \t"},
                {"instructions": " trim later "},
            )
        ],
    ],
)
def test_transport_preserves_run_text_for_execution(payload: dict[str, object]) -> None:
    events: list[object] = []
    transport = RpcStdinTransport(
        stdin=_Input([]),
        write_event=events.append,
        input_command_factory=_RpcInputCommand,
        input_closed_factory=_RpcInputClosed,
    )
    parsed = transport.parse_command(json.dumps(payload).encode())
    assert parsed is not None and parsed.known is not None
    for key, value in payload.items():
        assert getattr(parsed.known, key) == value
    assert events == []
