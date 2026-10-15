"""Secure, immutable configuration for MCP servers."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from wisp.tool_types import ToolSafety
from wisp.validation import redact_validation_error_inputs

MAX_MCP_SERVERS = 16
MAX_MCP_ARGS = 64
MAX_MCP_ENV_VARS = 64
MAX_MCP_TOOL_OVERRIDES = 256
MAX_MCP_HEADERS = 16
_ENVIRONMENT_NAMES_CASE_INSENSITIVE = os.name == "nt"

SERVER_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"
ServerName = Annotated[str, StringConstraints(pattern=SERVER_NAME_PATTERN)]
Command = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
ServerUrl = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Argument = Annotated[str, StringConstraints(max_length=4096)]
EnvironmentName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
EnvironmentValue = Annotated[SecretStr, Field(max_length=16_384)]
ToolName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
# An HTTP header field name (RFC 9110 token).
HeaderName = Annotated[
    str,
    StringConstraints(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$"),
]
HeaderValue = Annotated[SecretStr, Field(max_length=8_192)]
# A header built from one environment variable, optionally after an
# authentication scheme word: "${VAR}" or "Bearer ${VAR}". Only the variable's
# value is secret; the scheme is a short word so the visible template cannot
# carry a literal credential.
HEADER_TEMPLATE_PATTERN = re.compile(
    r"^(?:(?P<scheme>[A-Za-z]{1,20}) )?\$\{(?P<variable>[A-Za-z_][A-Za-z0-9_]*)\}$"
)
HeaderTemplate = Annotated[str, StringConstraints(max_length=256)]
# Headers the transport or the MCP protocol sets itself. Overriding them could
# break framing, defeat response bounding, or hijack the session.
RESERVED_HEADER_NAMES = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-length",
        "content-type",
        "host",
        "keep-alive",
        "mcp-protocol-version",
        "mcp-session-id",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class McpServerConfig(BaseModel):
    """Configuration for one user-owned MCP stdio or HTTP server.

    Environment mappings and safety overrides are stored as sorted tuples so the
    frozen model is deeply immutable and deterministic. ``repr`` deliberately omits
    literal environment values, which may contain credentials.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: ServerName
    command: Command | None = None
    url: ServerUrl | None = None
    args: tuple[Argument, ...] = Field(default=(), max_length=MAX_MCP_ARGS)
    env: tuple[tuple[EnvironmentName, EnvironmentValue], ...] = Field(
        default=(), max_length=MAX_MCP_ENV_VARS, repr=False
    )
    env_from: tuple[EnvironmentName, ...] = Field(default=(), max_length=MAX_MCP_ENV_VARS)
    # HTTP request headers, usually for authentication. ``headers`` holds literal
    # values, kept like ``env``; ``headers_from`` builds a value from an
    # environment variable at connect time, e.g. ``{"Authorization": "Bearer
    # ${LINEAR_API_KEY}"}``.
    headers: tuple[tuple[HeaderName, HeaderValue], ...] = Field(
        default=(), max_length=MAX_MCP_HEADERS, repr=False
    )
    headers_from: tuple[tuple[HeaderName, HeaderTemplate], ...] = Field(
        default=(), max_length=MAX_MCP_HEADERS
    )
    tool_safety: tuple[tuple[ToolName, ToolSafety], ...] = Field(
        default=(), max_length=MAX_MCP_TOOL_OVERRIDES
    )
    # A disabled server keeps its configuration but is not started.
    enabled: bool = True

    @model_validator(mode="wrap")
    @classmethod
    def _redact_validation_inputs(
        cls,
        value: Any,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(value)
        except ValidationError as exc:
            raise redact_validation_error_inputs(exc) from None

    @field_validator("env", mode="before", json_schema_input_type=dict[str, str])
    @classmethod
    def _env_mapping_to_items(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return tuple(value.items())
        if isinstance(value, tuple):
            return value
        raise ValueError("value must be a JSON object")

    @field_serializer("env", when_used="json")
    def _serialize_env(self, values: tuple[tuple[str, SecretStr], ...]) -> dict[str, SecretStr]:
        return dict(values)

    @field_validator("headers", mode="before", json_schema_input_type=dict[str, str])
    @classmethod
    def _headers_mapping_to_items(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return tuple(value.items())
        if isinstance(value, tuple):
            return value
        raise ValueError("value must be a JSON object")

    @field_serializer("headers", when_used="json")
    def _serialize_headers(self, values: tuple[tuple[str, SecretStr], ...]) -> dict[str, SecretStr]:
        return dict(values)

    @field_validator("headers_from", mode="before", json_schema_input_type=dict[str, str])
    @classmethod
    def _headers_from_mapping_to_items(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return tuple(value.items())
        if isinstance(value, tuple):
            return value
        raise ValueError("value must be a JSON object")

    @field_serializer("headers_from", when_used="json")
    def _serialize_headers_from(self, values: tuple[tuple[str, str], ...]) -> dict[str, str]:
        return dict(values)

    @field_validator(
        "tool_safety",
        mode="before",
        json_schema_input_type=dict[str, ToolSafety],
    )
    @classmethod
    def _tool_safety_mapping_to_items(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return tuple(value.items())
        if isinstance(value, tuple):
            return value
        raise ValueError("value must be a JSON object")

    @field_serializer("tool_safety", when_used="json")
    def _serialize_tool_safety(
        self, values: tuple[tuple[str, ToolSafety], ...]
    ) -> dict[str, ToolSafety]:
        return dict(values)

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("command must not be blank")
        if "\x00" in value:
            raise ValueError("command must not contain NUL")
        return value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("url must not contain control characters")
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must be an absolute HTTP or HTTPS URL")
        # Accessing port also rejects malformed and out-of-range port numbers.
        if parsed.port == 0 or parsed.netloc.endswith(":"):
            raise ValueError("url must use a valid port")
        if any(character.isspace() for character in normalized):
            raise ValueError("url must not contain whitespace")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("url must not contain a query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("unencrypted HTTP is allowed only for loopback endpoints")
        return normalized

    @field_validator("args")
    @classmethod
    def _validate_args(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in value for value in values):
            raise ValueError("arguments must not contain NUL")
        return values

    @field_validator("env")
    @classmethod
    def _validate_env(
        cls, values: tuple[tuple[str, SecretStr], ...]
    ) -> tuple[tuple[str, SecretStr], ...]:
        names = [_environment_name_key(name) for name, _ in values]
        if len(names) != len(set(names)):
            raise ValueError("environment variable names must be unique")
        if any("\x00" in value.get_secret_value() for _, value in values):
            raise ValueError("environment values must not contain NUL")
        return tuple(sorted(values))

    @field_validator("env_from")
    @classmethod
    def _validate_env_from(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [_environment_name_key(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("env_from names must be unique")
        return tuple(sorted(values))

    @field_validator("headers")
    @classmethod
    def _validate_headers(
        cls, values: tuple[tuple[str, SecretStr], ...]
    ) -> tuple[tuple[str, SecretStr], ...]:
        if any(not is_valid_header_value(value.get_secret_value()) for _, value in values):
            raise ValueError("header values must not contain line breaks or control characters")
        return tuple(sorted(values))

    @field_validator("headers_from")
    @classmethod
    def _validate_headers_from(
        cls, values: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        if any(HEADER_TEMPLATE_PATTERN.fullmatch(template) is None for _, template in values):
            raise ValueError('headers_from values must look like "${VAR}" or "Bearer ${VAR}"')
        return tuple(sorted(values))

    @field_validator("tool_safety")
    @classmethod
    def _validate_tool_safety(
        cls, values: tuple[tuple[str, ToolSafety], ...]
    ) -> tuple[tuple[str, ToolSafety], ...]:
        names = [name for name, _ in values]
        if len(names) != len(set(names)):
            raise ValueError("tool safety names must be unique")
        if any(not name.strip() or "\x00" in name for name in names):
            raise ValueError("tool safety names must not be blank or contain NUL")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def _validate_transport(self) -> McpServerConfig:
        if (self.command is None) == (self.url is None):
            raise ValueError("exactly one of command or url must be configured")
        if self.url is not None and (self.args or self.env or self.env_from):
            raise ValueError("HTTP MCP servers do not accept args, env, or env_from")
        if self.command is not None and (self.headers or self.headers_from):
            raise ValueError("stdio MCP servers do not accept headers or headers_from")
        header_names = [name.casefold() for name, _ in (*self.headers, *self.headers_from)]
        if len(header_names) != len(set(header_names)):
            raise ValueError("header names must be unique across headers and headers_from")
        if RESERVED_HEADER_NAMES.intersection(header_names):
            raise ValueError("headers must not set a header the MCP transport manages")
        literal_names = {_environment_name_key(name) for name, _ in self.env}
        inherited_names = {_environment_name_key(name) for name in self.env_from}
        overlap = literal_names.intersection(inherited_names)
        if overlap:
            raise ValueError("env and env_from must not contain the same variable")
        return self


def is_valid_header_value(value: str) -> bool:
    """Return whether a value can be sent as one HTTP header without injection.

    Args:
        value (str): Literal value or resolved environment value.

    Returns:
        bool: False for line breaks, NUL, and other control characters, which
        could split the header or smuggle another one.
    """

    return not any(
        (ord(character) < 32 and character != "\t") or ord(character) == 127 for character in value
    )


def header_template_variable(template: str) -> tuple[str | None, str]:
    """Split a validated ``headers_from`` template into its scheme and variable.

    Args:
        template (str): A value matching ``HEADER_TEMPLATE_PATTERN``.

    Returns:
        tuple[str | None, str]: The scheme word, if any, and the variable name.

    Raises:
        ValueError: The template is not a valid header template.
    """

    match = HEADER_TEMPLATE_PATTERN.fullmatch(template)
    if match is None:
        raise ValueError("invalid header template")
    return match.group("scheme"), match.group("variable")


def _environment_name_key(name: str) -> str:
    if _ENVIRONMENT_NAMES_CASE_INSENSITIVE:
        return name.casefold()
    return name
