"""Typed command models for Wisp JSONL RPC clients."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from wisp.agent.mode import AgentMode
from wisp.events import (
    MAX_RPC_MODEL_EFFORT_CHARS,
    MAX_RPC_MODEL_ID_CHARS,
    MAX_RPC_PROVIDER_ID_CHARS,
    QueueKind,
    QueueMode,
)
from wisp.validation import redact_validation_error_inputs

type ApprovalScope = Literal["once", "tool_session", "all_session"]

MAX_RPC_COMMAND_ID_CHARS = 256
MAX_RPC_COMMAND_TYPE_CHARS = 64
MAX_RPC_API_KEY_CHARS = 8_192
STORE_API_KEY_SECRET_FIELD = "_api_key"


@dataclass(frozen=True)
class _RpcSecretValue:
    value: object = dataclass_field(repr=False)

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def detach_store_api_key(command: dict[str, object]) -> dict[str, object]:
    """Move ``api_key`` into a redacted value before queueing or logging."""

    if command.get("type") != "store_api_key" or "api_key" not in command:
        return command
    detached = dict(command)
    detached[STORE_API_KEY_SECRET_FIELD] = _RpcSecretValue(detached.pop("api_key"))
    return detached


def take_store_api_key(command: dict[str, object]) -> object | None:
    """Remove and reveal a detached API key only at the storage boundary."""

    value = command.pop("api_key", None)
    if value is None:
        value = command.pop(STORE_API_KEY_SECRET_FIELD, None)
    return value.value if isinstance(value, _RpcSecretValue) else value


def rpc_command_payload_size(command: Mapping[str, object]) -> int:
    """Return the wire-size equivalent without making secrets printable."""

    sized = dict(command)
    secret = sized.get(STORE_API_KEY_SECRET_FIELD)
    if isinstance(secret, _RpcSecretValue):
        sized[STORE_API_KEY_SECRET_FIELD] = secret.value
    return len(
        json.dumps(
            sized,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


QUEUE_RPC_COMMAND_TYPES = frozenset(
    {
        "clear_queue",
        "follow_up",
        "get_queue_state",
        "pop_queue",
        "set_queue_mode",
        "steer",
    }
)


class RpcCommandModel(BaseModel):
    """Base class for RPC commands sent to `wisp --mode rpc`."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RPC_COMMAND_ID_CHARS,
    )

    def to_json_line(self) -> str:
        """Serialize this command as one JSONL command line."""

        return f"{self.model_dump_json(exclude_none=True)}\n"


class PromptCommand(RpcCommandModel):
    """Run one agent turn."""

    type: Literal["prompt"] = "prompt"
    prompt: str


class InitCommand(RpcCommandModel):
    """Inspect the active project and create its root AGENTS.md."""

    type: Literal["init"] = "init"


class CompactCommand(RpcCommandModel):
    """Compact the active persisted session context."""

    type: Literal["compact"] = "compact"
    instructions: str | None = None


class GetSessionStatsCommand(RpcCommandModel):
    """Return lifetime usage and current context-budget statistics."""

    type: Literal["get_session_stats"] = "get_session_stats"


class GetStateCommand(RpcCommandModel):
    """Return an immediate in-memory RPC state snapshot."""

    type: Literal["get_state"] = "get_state"


class GetCommandsCommand(RpcCommandModel):
    """Return an immediate in-memory command registry snapshot."""

    type: Literal["get_commands"] = "get_commands"


class GetModelCatalogCommand(RpcCommandModel):
    """Return the effective provider/model/effort catalog."""

    type: Literal["get_model_catalog"] = "get_model_catalog"


class GetConnectionCatalogCommand(RpcCommandModel):
    """Return the sanitized provider connection catalog."""

    type: Literal["get_connection_catalog"] = "get_connection_catalog"


class StoreApiKeyCommand(RpcCommandModel):
    """Persist one provider API key through the secret-bearing command path."""

    type: Literal["store_api_key"] = "store_api_key"
    provider: str = Field(min_length=1, max_length=MAX_RPC_PROVIDER_ID_CHARS)
    api_key: str = Field(min_length=1, max_length=MAX_RPC_API_KEY_CHARS, repr=False)

    @model_validator(mode="wrap")
    @classmethod
    def _redact_api_key_validation_errors(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(value)
        except ValidationError as exc:
            raise redact_validation_error_inputs(exc, field="api_key") from None

    def __repr__(self) -> str:
        return (
            f"StoreApiKeyCommand(id={self.id!r}, provider={self.provider!r}, api_key='<redacted>')"
        )


class DisconnectProviderCommand(RpcCommandModel):
    """Remove stored credentials for one provider."""

    type: Literal["disconnect_provider"] = "disconnect_provider"
    provider: str = Field(min_length=1, max_length=MAX_RPC_PROVIDER_ID_CHARS)


class BeginDeviceCodeCommand(RpcCommandModel):
    """Start a backend-owned device-code connection flow."""

    type: Literal["begin_device_code"] = "begin_device_code"
    provider: str = Field(min_length=1, max_length=MAX_RPC_PROVIDER_ID_CHARS)


class GetSkillsCommand(RpcCommandModel):
    """Return the active immutable skill catalog snapshot."""

    type: Literal["get_skills"] = "get_skills"


class GetMcpStatusCommand(RpcCommandModel):
    """Return sanitized MCP server and registered-tool status."""

    type: Literal["get_mcp_status"] = "get_mcp_status"


class GetMessagesCommand(RpcCommandModel):
    """Return a bounded persisted transcript page."""

    type: Literal["get_messages"] = "get_messages"
    session_id: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=200, ge=1, le=500, strict=True)
    before_entry_id: str | None = Field(default=None, min_length=1)
    after_entry_id: str | None = Field(default=None, min_length=1)
    entry_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=16)
    complete_structure: bool | None = Field(default=None, strict=True)
    full_content: bool | None = Field(default=None, strict=True)
    allow_during_prompt: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def _validate_cursor_direction(self) -> GetMessagesCommand:
        if self.before_entry_id is not None and self.after_entry_id is not None:
            raise ValueError("message page cursors are mutually exclusive")
        entry_ids = self.entry_ids or ()
        if self.entry_ids is not None and (
            self.before_entry_id is not None or self.after_entry_id is not None
        ):
            raise ValueError("exact message entry IDs cannot be combined with page cursors")
        if any(not entry_id for entry_id in entry_ids):
            raise ValueError("message entry IDs must be non-empty")
        if len(set(entry_ids)) != len(entry_ids):
            raise ValueError("message entry IDs must be unique")
        if self.full_content and not entry_ids:
            raise ValueError("full message content requires exact entry IDs")
        if self.full_content and len(entry_ids) != 1:
            raise ValueError("full message content requires exactly one entry ID")
        return self


class GetSessionsCommand(RpcCommandModel):
    """Return a bounded catalog of persisted sessions."""

    type: Literal["get_sessions"] = "get_sessions"
    limit: int = Field(default=50, ge=0, le=200, strict=True)


class NewSessionCommand(RpcCommandModel):
    """Deselect the active session so the next prompt starts a fresh one."""

    type: Literal["new_session"] = "new_session"


class SelectSessionCommand(RpcCommandModel):
    """Select a persisted session as the active RPC session."""

    type: Literal["select_session"] = "select_session"
    session_id: str = Field(min_length=1)


class CloneSessionCommand(RpcCommandModel):
    """Clone the selected session's active path and select the clone."""

    type: Literal["clone_session"] = "clone_session"


class ForkSessionCommand(RpcCommandModel):
    """Fork before a persisted user message and select the fork."""

    type: Literal["fork_session"] = "fork_session"
    entry_id: str = Field(min_length=1)


class GetSessionTreeCommand(RpcCommandModel):
    """Return a bounded page of the selected session's tree."""

    type: Literal["get_session_tree"] = "get_session_tree"
    limit: int = Field(default=200, ge=1, le=500, strict=True)
    after_entry_id: str | None = Field(default=None, min_length=1)


class NavigateSessionTreeCommand(RpcCommandModel):
    """Navigate the selected session to one persisted tree entry."""

    type: Literal["navigate_session_tree"] = "navigate_session_tree"
    entry_id: str = Field(min_length=1)


class UnrevertSessionTreeCommand(RpcCommandModel):
    """Reverse the selected session's latest explicit tree navigation."""

    type: Literal["unrevert_session_tree"] = "unrevert_session_tree"


class SetSessionNameCommand(RpcCommandModel):
    """Set or clear one session's display name."""

    type: Literal["set_session_name"] = "set_session_name"
    name: str
    session_id: str | None = Field(default=None, min_length=1)


class SteerCommand(RpcCommandModel):
    """Queue text after the active run's current assistant/tool batch."""

    type: Literal["steer"] = "steer"
    content: str


class FollowUpCommand(RpcCommandModel):
    """Queue text for when the active run would otherwise stop."""

    type: Literal["follow_up"] = "follow_up"
    content: str


class GetQueueStateCommand(RpcCommandModel):
    """Return the active or retained queue state."""

    type: Literal["get_queue_state"] = "get_queue_state"


class SetQueueModeCommand(RpcCommandModel):
    """Set one active queue's drain mode."""

    type: Literal["set_queue_mode"] = "set_queue_mode"
    kind: QueueKind
    mode: QueueMode


class PopQueueCommand(RpcCommandModel):
    """Remove the latest item from one active queue."""

    type: Literal["pop_queue"] = "pop_queue"
    kind: QueueKind


class ClearQueueCommand(RpcCommandModel):
    """Clear one or both active queues."""

    type: Literal["clear_queue"] = "clear_queue"
    kind: QueueKind | None = None


class CancelCommand(RpcCommandModel):
    """Cancel a running prompt or compact command."""

    type: Literal["cancel"] = "cancel"
    target_id: str = Field(min_length=1)


class ApprovalCommand(RpcCommandModel):
    """Resolve a pending tool approval request."""

    type: Literal["approval"] = "approval"
    call_id: str = Field(min_length=1)
    approved: bool
    reason: str | None = None
    scope: ApprovalScope | None = None

    @model_validator(mode="after")
    def _validate_scope(self) -> ApprovalCommand:
        if not self.approved and self.scope is not None:
            raise ValueError("denied approvals must not include an approval scope")
        return self


class TrustCommand(RpcCommandModel):
    """Resolve a pending project-trust request."""

    type: Literal["trust"] = "trust"
    request_id: str = Field(min_length=1)
    trusted: bool
    reason: str | None = None
    transient: bool | None = None


class ShutdownCommand(RpcCommandModel):
    """Ask the RPC process to exit cleanly."""

    type: Literal["shutdown"] = "shutdown"


class ConfigureCommand(RpcCommandModel):
    """Update configuration in sequence, rejecting unresolved model-provider ambiguity."""

    type: Literal["configure"] = "configure"
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RPC_PROVIDER_ID_CHARS,
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RPC_MODEL_ID_CHARS,
    )
    effort: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RPC_MODEL_EFFORT_CHARS,
    )
    auto_compaction_enabled: bool | None = None
    mode: AgentMode | None = None
    # `to_json_line()` uses `exclude_none=True`, so a bare `effort=None` is
    # indistinguishable on the wire from never having set `effort` at all --
    # the RPC server keys off "effort" in command (dict key presence), not
    # the value, to decide whether to touch agent.effort. This field is the
    # only way a client can explicitly request clearing effort back to the
    # provider's own default: it has no default value that `exclude_none`
    # would ever drop, since `False` is not `None`.
    clear_effort: bool = False

    @model_validator(mode="after")
    def _validate_mutation(self) -> ConfigureCommand:
        if not self.clear_effort and all(
            value is None
            for value in (
                self.provider,
                self.model,
                self.effort,
                self.auto_compaction_enabled,
                self.mode,
            )
        ):
            raise ValueError("configure commands require an effective mutation")
        if self.clear_effort and self.effort is not None:
            raise ValueError("configure commands cannot set and clear effort together")
        return self


type RpcCommand = Annotated[
    PromptCommand
    | InitCommand
    | CompactCommand
    | GetSessionStatsCommand
    | GetStateCommand
    | GetCommandsCommand
    | GetModelCatalogCommand
    | GetConnectionCatalogCommand
    | StoreApiKeyCommand
    | DisconnectProviderCommand
    | BeginDeviceCodeCommand
    | GetSkillsCommand
    | GetMcpStatusCommand
    | GetMessagesCommand
    | GetSessionsCommand
    | NewSessionCommand
    | SelectSessionCommand
    | CloneSessionCommand
    | ForkSessionCommand
    | GetSessionTreeCommand
    | NavigateSessionTreeCommand
    | UnrevertSessionTreeCommand
    | SetSessionNameCommand
    | SteerCommand
    | FollowUpCommand
    | GetQueueStateCommand
    | SetQueueModeCommand
    | PopQueueCommand
    | ClearQueueCommand
    | CancelCommand
    | ApprovalCommand
    | TrustCommand
    | ShutdownCommand
    | ConfigureCommand,
    Field(discriminator="type"),
]
RpcCommandAdapter: TypeAdapter[RpcCommand] = TypeAdapter(RpcCommand)


def rpc_command_type(command: Mapping[str, object]) -> str:
    """Return a bounded command discriminator for lifecycle reporting."""

    command_type = command.get("type")
    return (
        command_type
        if isinstance(command_type, str) and 0 < len(command_type) <= MAX_RPC_COMMAND_TYPE_CHARS
        else "unknown"
    )


@dataclass(frozen=True)
class UnknownCommandEnvelope:
    """Metadata for a forward-compatible command unknown to this Wisp version."""

    command_type: str


@dataclass(frozen=True)
class ParsedRpcCommand:
    """Validated known command or bounded unknown command plus its legacy payload."""

    value: RpcCommand | UnknownCommandEnvelope = dataclass_field(repr=False)
    _payload: Mapping[str, object] = dataclass_field(repr=False)
    payload_size: int

    @classmethod
    def from_known(
        cls,
        command: RpcCommand,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> ParsedRpcCommand:
        legacy = dict(command.model_dump(exclude_none=True) if payload is None else payload)
        return cls._from_parts(command, legacy)

    @classmethod
    def from_unknown(cls, payload: Mapping[str, object]) -> ParsedRpcCommand:
        legacy = dict(payload)
        return cls._from_parts(
            UnknownCommandEnvelope(command_type=rpc_command_type(legacy)),
            legacy,
        )

    @classmethod
    def _from_parts(
        cls,
        value: RpcCommand | UnknownCommandEnvelope,
        payload: dict[str, object],
    ) -> ParsedRpcCommand:
        detached = detach_store_api_key(payload)
        return cls(
            value=value,
            _payload=MappingProxyType(detached),
            payload_size=rpc_command_payload_size(detached),
        )

    @property
    def command_type(self) -> str:
        if isinstance(self.value, UnknownCommandEnvelope):
            return self.value.command_type
        return self.value.type

    @property
    def command_id(self) -> str | None:
        command_id = self._payload.get("id")
        return command_id if isinstance(command_id, str) and command_id else None

    @property
    def known(self) -> RpcCommand | None:
        return None if isinstance(self.value, UnknownCommandEnvelope) else self.value

    @property
    def allows_prompt_read(self) -> bool:
        return isinstance(self.value, GetMessagesCommand) and self.value.allow_during_prompt is True

    def without_id(self) -> ParsedRpcCommand:
        payload = dict(self._payload)
        payload.pop("id", None)
        value = self.value
        if not isinstance(value, UnknownCommandEnvelope):
            value = value.model_copy(update={"id": None})
        return self._from_parts(value, payload)

    def to_legacy_dict(self) -> dict[str, object]:
        """Return a fresh secret-detached payload for the legacy executor."""

        return dict(self._payload)


def rpc_command_from_json(line: str) -> RpcCommand:
    """Parse one JSONL command line into a typed command model."""

    return RpcCommandAdapter.validate_json(line)
