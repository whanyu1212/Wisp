from __future__ import annotations

import pytest
from pydantic import ValidationError

from wisp.events import EVENT_SCHEMA_VERSION
from wisp.rpc.protocol import (
    LIVE_RPC_PROTOCOL_VERSION,
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    RpcClientHello,
    RpcHandshakeRejected,
    RpcServerHandshakeAdapter,
    RpcServerHello,
    RpcTransportLimits,
    negotiate_rpc_handshake,
)


def _limits() -> RpcTransportLimits:
    return RpcTransportLimits(
        max_client_frame_bytes=8 * 1024 * 1024,
        max_server_frame_bytes=16 * 1024 * 1024,
    )


def _client_hello(
    *,
    min_protocol_version: int = 1,
    max_protocol_version: int = 1,
    min_event_schema_version: int = EVENT_SCHEMA_VERSION,
    max_event_schema_version: int = EVENT_SCHEMA_VERSION,
    supported_capabilities: tuple[str, ...] = ("streaming.text", "tools"),
    required_capabilities: tuple[str, ...] = ("streaming.text",),
) -> RpcClientHello:
    return RpcClientHello(
        frontend_name="wisp-rust-tui",
        frontend_version="0.1.0",
        min_protocol_version=min_protocol_version,
        max_protocol_version=max_protocol_version,
        min_event_schema_version=min_event_schema_version,
        max_event_schema_version=max_event_schema_version,
        supported_capabilities=supported_capabilities,
        required_capabilities=required_capabilities,
    )


def test_client_hello_canonicalizes_capabilities_and_keeps_independent_ranges() -> None:
    hello = _client_hello(
        max_protocol_version=2,
        supported_capabilities=("tools", "streaming.text"),
    )

    assert hello.type == "rpc.client.hello"
    assert hello.min_protocol_version == 1
    assert hello.max_protocol_version == 2
    assert hello.min_event_schema_version == EVENT_SCHEMA_VERSION
    assert hello.max_event_schema_version == EVENT_SCHEMA_VERSION
    assert hello.supported_capabilities == ("streaming.text", "tools")


def test_handshake_capabilities_must_be_unique_and_required_must_be_supported() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        _client_hello(supported_capabilities=("tools", "tools"), required_capabilities=())

    with pytest.raises(ValidationError, match="must also be supported"):
        _client_hello(supported_capabilities=("tools",), required_capabilities=("streaming.text",))


@pytest.mark.parametrize(
    ("protocol_range", "event_range", "message"),
    [
        ((2, 1), (EVENT_SCHEMA_VERSION, EVENT_SCHEMA_VERSION), "minimum protocol version"),
        ((1, 1), (35, 34), "minimum event schema version"),
    ],
)
def test_client_hello_rejects_inverted_ranges(
    protocol_range: tuple[int, int],
    event_range: tuple[int, int],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _client_hello(
            min_protocol_version=protocol_range[0],
            max_protocol_version=protocol_range[1],
            min_event_schema_version=event_range[0],
            max_event_schema_version=event_range[1],
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("frontend_name", "rust-tui\n"),
        ("frontend_name", "Rust TUI"),
        ("frontend_version", "0.1.0\x1b[31m"),
    ],
)
def test_client_hello_rejects_unsafe_identity_text(field: str, value: str) -> None:
    payload = {
        "frontend_name": "wisp-rust-tui",
        "frontend_version": "0.1.0",
        "min_protocol_version": 1,
        "max_protocol_version": 1,
        "min_event_schema_version": EVENT_SCHEMA_VERSION,
        "max_event_schema_version": EVENT_SCHEMA_VERSION,
        "supported_capabilities": (),
        "required_capabilities": (),
    }
    payload[field] = value

    with pytest.raises(ValidationError, match="safe"):
        RpcClientHello.model_validate(payload)


def test_transport_limits_are_directional_and_portably_bounded() -> None:
    assert MAX_HANDSHAKE_FRAME_BYTES < MAX_LIVE_RPC_FRAME_BYTES
    with pytest.raises(ValidationError):
        RpcTransportLimits(
            max_client_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES + 1,
            max_server_frame_bytes=1,
        )


def test_server_hello_requires_and_reports_the_complete_contract() -> None:
    hello = RpcServerHello(
        backend_package_version="0.1.0",
        protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        event_schema_version=EVENT_SCHEMA_VERSION,
        min_frontend_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        max_frontend_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        capabilities=("streaming.text",),
        limits=_limits(),
    )

    assert hello.protocol_version == LIVE_RPC_PROTOCOL_VERSION
    assert hello.event_schema_version == EVENT_SCHEMA_VERSION
    assert hello.capabilities == ("streaming.text",)
    assert hello.limits.max_client_frame_bytes == 8 * 1024 * 1024

    with pytest.raises(ValidationError, match="protocol_version"):
        RpcServerHandshakeAdapter.validate_json(
            '{"type":"rpc.server.hello","backend_package_version":"0.1.0",'
            '"event_schema_version":34,"min_frontend_protocol_version":1,'
            '"max_frontend_protocol_version":1,"capabilities":[],"limits":'
            '{"max_client_frame_bytes":1024,"max_server_frame_bytes":1024}}'
        )


def test_negotiation_selects_highest_common_versions_and_capability_intersection() -> None:
    result = negotiate_rpc_handshake(
        _client_hello(
            min_protocol_version=1,
            max_protocol_version=3,
            supported_capabilities=("tools", "streaming.text", "sessions"),
            required_capabilities=("streaming.text",),
        ),
        backend_package_version="0.1.0",
        supported_capabilities=("streaming.text", "sessions", "backend.only"),
        limits=_limits(),
        min_protocol_version=1,
        max_protocol_version=2,
    )

    assert isinstance(result, RpcServerHello)
    assert result.protocol_version == 2
    assert result.event_schema_version == EVENT_SCHEMA_VERSION
    assert result.capabilities == ("sessions", "streaming.text")


@pytest.mark.parametrize(
    ("client", "backend_capabilities", "expected_code"),
    [
        (
            _client_hello(min_protocol_version=2, max_protocol_version=2),
            ("streaming.text",),
            "protocol_version_mismatch",
        ),
        (
            _client_hello(min_event_schema_version=35, max_event_schema_version=35),
            ("streaming.text",),
            "event_schema_version_mismatch",
        ),
        (
            _client_hello(
                supported_capabilities=("streaming.text", "tools"),
                required_capabilities=("tools",),
            ),
            ("streaming.text",),
            "unsupported_capability",
        ),
    ],
)
def test_negotiation_returns_bounded_structured_rejections(
    client: RpcClientHello,
    backend_capabilities: tuple[str, ...],
    expected_code: str,
) -> None:
    result = negotiate_rpc_handshake(
        client,
        backend_package_version="0.1.0",
        supported_capabilities=backend_capabilities,
        limits=_limits(),
    )

    assert isinstance(result, RpcHandshakeRejected)
    assert result.code == expected_code
    assert result.backend_package_version == "0.1.0"
    assert result.event_schema_version == EVENT_SCHEMA_VERSION


def test_server_handshake_adapter_parses_complete_success_and_rejection() -> None:
    success = RpcServerHandshakeAdapter.validate_json(
        '{"type":"rpc.server.hello","backend_package_version":"0.1.0",'
        '"protocol_version":1,"event_schema_version":34,'
        '"min_frontend_protocol_version":1,"max_frontend_protocol_version":1,'
        '"capabilities":[],"limits":{"max_client_frame_bytes":1024,'
        '"max_server_frame_bytes":2048}}'
    )
    rejection = RpcServerHandshakeAdapter.validate_json(
        '{"type":"rpc.handshake.rejected","code":"protocol_version_mismatch",'
        '"message":"No compatible live RPC protocol version.",'
        '"backend_package_version":"0.1.0","min_protocol_version":1,'
        '"max_protocol_version":1,"event_schema_version":34}'
    )

    assert isinstance(success, RpcServerHello)
    assert isinstance(rejection, RpcHandshakeRejected)


@pytest.mark.parametrize("message", ["x" * 1_001, "unsafe\x1b[31m", "unsafe\u202e"])
def test_handshake_rejection_message_is_bounded_and_control_free(message: str) -> None:
    with pytest.raises(ValidationError):
        RpcHandshakeRejected(
            code="invalid_handshake",
            message=message,
            backend_package_version="0.1.0",
            min_protocol_version=1,
            max_protocol_version=1,
            event_schema_version=EVENT_SCHEMA_VERSION,
        )
