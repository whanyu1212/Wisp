"""Typed contracts for the negotiated live JSONL-RPC protocol."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from wisp.events import EVENT_SCHEMA_VERSION

LIVE_RPC_PROTOCOL_VERSION = 1
MIN_LIVE_RPC_PROTOCOL_VERSION = LIVE_RPC_PROTOCOL_VERSION
MAX_LIVE_RPC_PROTOCOL_VERSION = LIVE_RPC_PROTOCOL_VERSION
MAX_WIRE_VERSION = 2**32 - 1
MAX_HANDSHAKE_FRAME_BYTES = 64 * 1024
MAX_LIVE_RPC_FRAME_BYTES = 64 * 1024 * 1024
MAX_HANDSHAKE_MESSAGE_CHARS = 1_000
MAX_HANDSHAKE_CAPABILITIES = 128

_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*(?![\s\S])"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}(?![\s\S])"
_SAFE_MESSAGE_PATTERN = (
    r"^[^\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]{1,1000}"
    r"(?![\s\S])"
)
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")
_BIDI_CONTROL_CLASSES = frozenset({"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"})

type RpcCapability = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
]
type RpcHandshakeRejectionCode = Literal[
    "event_schema_version_mismatch",
    "invalid_handshake",
    "protocol_version_mismatch",
    "unsupported_capability",
]


class _ProtocolModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        strict=True,
    )


class RpcTransportLimits(_ProtocolModel):
    """Directional application-frame limits selected by the backend."""

    max_client_frame_bytes: int = Field(ge=1, le=MAX_LIVE_RPC_FRAME_BYTES, strict=True)
    max_server_frame_bytes: int = Field(ge=1, le=MAX_LIVE_RPC_FRAME_BYTES, strict=True)


class RpcClientHello(_ProtocolModel):
    """First bounded frame sent by an external frontend."""

    type: Literal["rpc.client.hello"] = "rpc.client.hello"
    frontend_name: str = Field(
        min_length=1,
        max_length=64,
        json_schema_extra={"pattern": _IDENTIFIER_PATTERN},
    )
    frontend_version: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"pattern": _VERSION_PATTERN},
    )
    min_protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    max_protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    min_event_schema_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    max_event_schema_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    supported_capabilities: tuple[RpcCapability, ...] = Field(
        max_length=MAX_HANDSHAKE_CAPABILITIES,
        json_schema_extra={"uniqueItems": True},
    )
    required_capabilities: tuple[RpcCapability, ...] = Field(
        max_length=MAX_HANDSHAKE_CAPABILITIES,
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("frontend_name")
    @classmethod
    def _validate_frontend_name(cls, value: str) -> str:
        return _require_identifier(value, field="frontend name")

    @field_validator("frontend_version")
    @classmethod
    def _validate_frontend_version(cls, value: str) -> str:
        return _require_version(value, field="frontend version")

    @field_validator("supported_capabilities", "required_capabilities")
    @classmethod
    def _canonicalize_capabilities(cls, capabilities: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_capabilities(capabilities)

    @model_validator(mode="after")
    def _validate_ranges_and_capabilities(self) -> Self:
        if self.min_protocol_version > self.max_protocol_version:
            raise ValueError("minimum protocol version cannot exceed maximum protocol version")
        if self.min_event_schema_version > self.max_event_schema_version:
            raise ValueError(
                "minimum event schema version cannot exceed maximum event schema version"
            )
        if not set(self.required_capabilities).issubset(self.supported_capabilities):
            raise ValueError("required RPC capabilities must also be supported")
        return self


class RpcServerHello(_ProtocolModel):
    """Successful backend response selecting one compatible live contract."""

    type: Literal["rpc.server.hello"] = "rpc.server.hello"
    backend_package_version: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"pattern": _VERSION_PATTERN},
    )
    protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    event_schema_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    min_frontend_protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    max_frontend_protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    capabilities: tuple[RpcCapability, ...] = Field(
        max_length=MAX_HANDSHAKE_CAPABILITIES,
        json_schema_extra={"uniqueItems": True},
    )
    limits: RpcTransportLimits

    @field_validator("backend_package_version")
    @classmethod
    def _validate_backend_version(cls, value: str) -> str:
        return _require_version(value, field="backend package version")

    @field_validator("capabilities")
    @classmethod
    def _canonicalize_capabilities(cls, capabilities: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_capabilities(capabilities)

    @model_validator(mode="after")
    def _validate_protocol_contract(self) -> Self:
        if self.min_frontend_protocol_version > self.max_frontend_protocol_version:
            raise ValueError(
                "minimum frontend protocol version cannot exceed maximum frontend protocol version"
            )
        if not (
            self.min_frontend_protocol_version
            <= self.protocol_version
            <= self.max_frontend_protocol_version
        ):
            raise ValueError(
                "selected protocol version is outside the frontend compatibility range"
            )
        return self


class RpcHandshakeRejected(_ProtocolModel):
    """Bounded pre-protocol response explaining why negotiation failed."""

    type: Literal["rpc.handshake.rejected"] = "rpc.handshake.rejected"
    code: RpcHandshakeRejectionCode
    message: str = Field(
        min_length=1,
        max_length=MAX_HANDSHAKE_MESSAGE_CHARS,
        json_schema_extra={"pattern": _SAFE_MESSAGE_PATTERN},
    )
    backend_package_version: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"pattern": _VERSION_PATTERN},
    )
    min_protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    max_protocol_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)
    event_schema_version: int = Field(ge=1, le=MAX_WIRE_VERSION, strict=True)

    @field_validator("backend_package_version")
    @classmethod
    def _validate_backend_version(cls, value: str) -> str:
        return _require_version(value, field="backend package version")

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        if any(
            unicodedata.category(character) == "Cc"
            or unicodedata.bidirectional(character) in _BIDI_CONTROL_CLASSES
            for character in value
        ):
            raise ValueError("handshake messages must not contain control characters")
        return value

    @model_validator(mode="after")
    def _validate_protocol_range(self) -> Self:
        if self.min_protocol_version > self.max_protocol_version:
            raise ValueError("minimum protocol version cannot exceed maximum protocol version")
        return self


type RpcServerHandshake = Annotated[
    RpcServerHello | RpcHandshakeRejected,
    Field(discriminator="type"),
]
RpcServerHandshakeAdapter: TypeAdapter[RpcServerHandshake] = TypeAdapter(RpcServerHandshake)


def negotiate_rpc_handshake(
    client: RpcClientHello,
    *,
    backend_package_version: str,
    supported_capabilities: tuple[str, ...],
    limits: RpcTransportLimits,
    min_protocol_version: int = MIN_LIVE_RPC_PROTOCOL_VERSION,
    max_protocol_version: int = MAX_LIVE_RPC_PROTOCOL_VERSION,
    event_schema_version: int = EVENT_SCHEMA_VERSION,
) -> RpcServerHandshake:
    """Select a deterministic common contract or return a bounded rejection."""

    backend_capabilities = _canonical_capabilities(supported_capabilities)
    common_minimum = max(client.min_protocol_version, min_protocol_version)
    common_maximum = min(client.max_protocol_version, max_protocol_version)

    def reject(code: RpcHandshakeRejectionCode, message: str) -> RpcHandshakeRejected:
        return RpcHandshakeRejected(
            code=code,
            message=message,
            backend_package_version=backend_package_version,
            min_protocol_version=min_protocol_version,
            max_protocol_version=max_protocol_version,
            event_schema_version=event_schema_version,
        )

    if common_minimum > common_maximum:
        return reject(
            "protocol_version_mismatch",
            "No compatible live RPC protocol version.",
        )
    if not (
        client.min_event_schema_version <= event_schema_version <= client.max_event_schema_version
    ):
        return reject(
            "event_schema_version_mismatch",
            "No compatible live event schema version.",
        )
    if not set(client.required_capabilities).issubset(backend_capabilities):
        return reject(
            "unsupported_capability",
            "A required frontend capability is unavailable.",
        )
    selected_capabilities = tuple(
        capability
        for capability in client.supported_capabilities
        if capability in backend_capabilities
    )
    return RpcServerHello(
        backend_package_version=backend_package_version,
        protocol_version=common_maximum,
        event_schema_version=event_schema_version,
        min_frontend_protocol_version=min_protocol_version,
        max_frontend_protocol_version=max_protocol_version,
        capabilities=selected_capabilities,
        limits=limits,
    )


def _canonical_capabilities(capabilities: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("RPC capabilities must be unique")
    return tuple(
        sorted(_require_identifier(value, field="RPC capability") for value in capabilities)
    )


def _require_identifier(value: str, *, field: str) -> str:
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe lowercase identifier")
    return value


def _require_version(value: str, *, field: str) -> str:
    if _VERSION_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must contain only safe version characters")
    return value


__all__ = [
    "LIVE_RPC_PROTOCOL_VERSION",
    "MAX_HANDSHAKE_FRAME_BYTES",
    "MAX_LIVE_RPC_FRAME_BYTES",
    "MAX_LIVE_RPC_PROTOCOL_VERSION",
    "MIN_LIVE_RPC_PROTOCOL_VERSION",
    "RpcClientHello",
    "RpcHandshakeRejected",
    "RpcServerHandshake",
    "RpcServerHandshakeAdapter",
    "RpcServerHello",
    "RpcTransportLimits",
    "negotiate_rpc_handshake",
]
