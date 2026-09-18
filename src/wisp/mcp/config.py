"""Secure, immutable configuration for MCP servers."""

from __future__ import annotations

import os
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

from wisp.config.validation import redact_validation_error_inputs
from wisp.tool_types import ToolSafety

MAX_MCP_SERVERS = 16
MAX_MCP_ARGS = 64
MAX_MCP_ENV_VARS = 64
MAX_MCP_TOOL_OVERRIDES = 256
_ENVIRONMENT_NAMES_CASE_INSENSITIVE = os.name == "nt"

ServerName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
Command = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
ServerUrl = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Argument = Annotated[str, StringConstraints(max_length=4096)]
EnvironmentName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
EnvironmentValue = Annotated[SecretStr, Field(max_length=16_384)]
ToolName = Annotated[str, StringConstraints(min_length=1, max_length=128)]


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
    tool_safety: tuple[tuple[ToolName, ToolSafety], ...] = Field(
        default=(), max_length=MAX_MCP_TOOL_OVERRIDES
    )

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
        literal_names = {_environment_name_key(name) for name, _ in self.env}
        inherited_names = {_environment_name_key(name) for name in self.env_from}
        overlap = literal_names.intersection(inherited_names)
        if overlap:
            raise ValueError("env and env_from must not contain the same variable")
        return self


def _environment_name_key(name: str) -> str:
    if _ENVIRONMENT_NAMES_CASE_INSENSITIVE:
        return name.casefold()
    return name
