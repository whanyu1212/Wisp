"""Typed contracts for the live JSONL-RPC handshake."""

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
    "invalid_handshake",
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


class RpcHandshakeRequest(_ProtocolModel):
    """First bounded frame sent by an external frontend."""

    type: Literal["rpc.handshake.request"] = "rpc.handshake.request"
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
    def _validate_capabilities(self) -> Self:
        if not set(self.required_capabilities).issubset(self.supported_capabilities):
            raise ValueError("required RPC capabilities must also be supported")
        return self


class RpcHandshakeAccepted(_ProtocolModel):
    """Successful backend response selecting capabilities and frame limits."""

    type: Literal["rpc.handshake.accepted"] = "rpc.handshake.accepted"
    backend_package_version: str = Field(
        min_length=1,
        max_length=128,
        json_schema_extra={"pattern": _VERSION_PATTERN},
    )
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


type RpcHandshakeResponse = Annotated[
    RpcHandshakeAccepted | RpcHandshakeRejected,
    Field(discriminator="type"),
]
RpcHandshakeRequestAdapter: TypeAdapter[RpcHandshakeRequest] = TypeAdapter(RpcHandshakeRequest)
RpcHandshakeResponseAdapter: TypeAdapter[RpcHandshakeResponse] = TypeAdapter(RpcHandshakeResponse)


def negotiate_rpc_handshake(
    request: RpcHandshakeRequest,
    *,
    backend_package_version: str,
    supported_capabilities: tuple[str, ...],
    limits: RpcTransportLimits,
) -> RpcHandshakeResponse:
    """Select the common capabilities or return a bounded rejection."""

    backend_capabilities = _canonical_capabilities(supported_capabilities)
    if not set(request.required_capabilities).issubset(backend_capabilities):
        return RpcHandshakeRejected(
            code="unsupported_capability",
            message="A required frontend capability is unavailable.",
            backend_package_version=backend_package_version,
        )
    selected_capabilities = tuple(
        capability
        for capability in request.supported_capabilities
        if capability in backend_capabilities
    )
    return RpcHandshakeAccepted(
        backend_package_version=backend_package_version,
        capabilities=selected_capabilities,
        limits=limits,
    )


def validate_rpc_handshake_response(
    request: RpcHandshakeRequest,
    response: RpcHandshakeAccepted,
) -> None:
    """Reject a successful response that violates the frontend's offered contract."""

    if not set(response.capabilities).issubset(request.supported_capabilities):
        raise ValueError("backend selected a capability the frontend did not offer")
    if not set(request.required_capabilities).issubset(response.capabilities):
        raise ValueError("backend omitted a required frontend capability")


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
    "MAX_HANDSHAKE_FRAME_BYTES",
    "MAX_LIVE_RPC_FRAME_BYTES",
    "RpcHandshakeAccepted",
    "RpcHandshakeRequest",
    "RpcHandshakeRequestAdapter",
    "RpcHandshakeRejected",
    "RpcHandshakeResponse",
    "RpcHandshakeResponseAdapter",
    "RpcTransportLimits",
    "negotiate_rpc_handshake",
    "validate_rpc_handshake_response",
]
