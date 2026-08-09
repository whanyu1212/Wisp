"""Secure, immutable configuration for MCP servers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Annotated, Any, Self

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
_ENVIRONMENT_NAMES_CASE_INSENSITIVE = os.name == "nt"

ServerName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
Command = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Argument = Annotated[str, StringConstraints(max_length=4096)]
EnvironmentName = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
EnvironmentValue = Annotated[SecretStr, Field(max_length=16_384)]
ToolName = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class McpServerConfig(BaseModel):
    """Configuration for one user-owned MCP stdio server.

    Environment mappings and safety overrides are stored as sorted tuples so the
    frozen model is deeply immutable and deterministic. ``repr`` deliberately omits
    literal environment values, which may contain credentials.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: ServerName
    command: Command
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
    def _validate_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command must not be blank")
        if "\x00" in value:
            raise ValueError("command must not contain NUL")
        return value

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
    def _validate_environment_sources(self) -> McpServerConfig:
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
