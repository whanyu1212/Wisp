from __future__ import annotations

import pytest
from pydantic import ValidationError

from wisp.rpc.protocol import (
    MAX_HANDSHAKE_FRAME_BYTES,
    MAX_LIVE_RPC_FRAME_BYTES,
    RpcHandshakeAccepted,
    RpcHandshakeRejected,
    RpcHandshakeRequest,
    RpcHandshakeResponseAdapter,
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
    supported_capabilities: tuple[str, ...] = ("streaming.text", "tools"),
    required_capabilities: tuple[str, ...] = ("streaming.text",),
) -> RpcHandshakeRequest:
    return RpcHandshakeRequest(
        frontend_name="wisp-rust-tui",
        frontend_version="0.1.0",
        supported_capabilities=supported_capabilities,
        required_capabilities=required_capabilities,
    )


def test_client_hello_canonicalizes_capabilities() -> None:
    hello = _client_hello(supported_capabilities=("tools", "streaming.text"))

    assert hello.type == "rpc.handshake.request"
    assert hello.supported_capabilities == ("streaming.text", "tools")


@pytest.mark.parametrize(
    "removed",
    [
        {"min_protocol_version": 9, "max_protocol_version": 9},
        {"min_event_schema_version": 39, "max_event_schema_version": 39},
    ],
)
def test_client_hello_rejects_removed_version_fields(removed: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="version"):
        RpcHandshakeRequest.model_validate(
            {
                "frontend_name": "wisp-rust-tui",
                "frontend_version": "0.1.0",
                "supported_capabilities": (),
                "required_capabilities": (),
                **removed,
            }
        )


def test_handshake_capabilities_must_be_unique_and_required_must_be_supported() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        _client_hello(supported_capabilities=("tools", "tools"), required_capabilities=())

    with pytest.raises(ValidationError, match="must also be supported"):
        _client_hello(supported_capabilities=("tools",), required_capabilities=("streaming.text",))


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
        "supported_capabilities": (),
        "required_capabilities": (),
    }
    payload[field] = value

    with pytest.raises(ValidationError, match="safe"):
        RpcHandshakeRequest.model_validate(payload)


def test_transport_limits_are_directional_and_portably_bounded() -> None:
    assert MAX_HANDSHAKE_FRAME_BYTES < MAX_LIVE_RPC_FRAME_BYTES
    with pytest.raises(ValidationError):
        RpcTransportLimits(
            max_client_frame_bytes=MAX_LIVE_RPC_FRAME_BYTES + 1,
            max_server_frame_bytes=1,
        )


def test_server_hello_reports_capabilities_and_limits() -> None:
    hello = RpcHandshakeAccepted(
        backend_package_version="0.1.0",
        capabilities=("streaming.text",),
        limits=_limits(),
    )

    assert hello.capabilities == ("streaming.text",)
    assert hello.limits.max_client_frame_bytes == 8 * 1024 * 1024

    with pytest.raises(ValidationError, match="protocol_version"):
        RpcHandshakeResponseAdapter.validate_json(
            '{"type":"rpc.handshake.accepted","backend_package_version":"0.1.0",'
            '"protocol_version":9,"capabilities":[],"limits":'
            '{"max_client_frame_bytes":1024,"max_server_frame_bytes":1024}}'
        )


def test_negotiation_selects_the_capability_intersection() -> None:
    result = negotiate_rpc_handshake(
        _client_hello(
            supported_capabilities=("tools", "streaming.text", "sessions"),
            required_capabilities=("streaming.text",),
        ),
        backend_package_version="0.1.0",
        supported_capabilities=("streaming.text", "sessions", "backend.only"),
        limits=_limits(),
    )

    assert isinstance(result, RpcHandshakeAccepted)
    assert result.capabilities == ("sessions", "streaming.text")


def test_negotiation_rejects_a_missing_required_capability() -> None:
    result = negotiate_rpc_handshake(
        _client_hello(
            supported_capabilities=("streaming.text", "tools"),
            required_capabilities=("tools",),
        ),
        backend_package_version="0.1.0",
        supported_capabilities=("streaming.text",),
        limits=_limits(),
    )

    assert isinstance(result, RpcHandshakeRejected)
    assert result.code == "unsupported_capability"
    assert result.backend_package_version == "0.1.0"


def test_server_handshake_adapter_parses_complete_success_and_rejection() -> None:
    success = RpcHandshakeResponseAdapter.validate_json(
        '{"type":"rpc.handshake.accepted","backend_package_version":"0.1.0",'
        '"capabilities":[],"limits":{"max_client_frame_bytes":1024,'
        '"max_server_frame_bytes":2048}}'
    )
    rejection = RpcHandshakeResponseAdapter.validate_json(
        '{"type":"rpc.handshake.rejected","code":"unsupported_capability",'
        '"message":"A required frontend capability is unavailable.",'
        '"backend_package_version":"0.1.0"}'
    )

    assert isinstance(success, RpcHandshakeAccepted)
    assert isinstance(rejection, RpcHandshakeRejected)


@pytest.mark.parametrize("message", ["x" * 1_001, "unsafe\x1b[31m", "unsafe‮"])
def test_handshake_rejection_message_is_bounded_and_control_free(message: str) -> None:
    with pytest.raises(ValidationError):
        RpcHandshakeRejected(
            code="invalid_handshake",
            message=message,
            backend_package_version="0.1.0",
        )
