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
    PermissionMode,
    QueueKind,
    QueueMode,
    RpcMcpServerLaunch,
)
from wisp.validation import redact_validation_error_inputs

type ApprovalScope = Literal["once", "tool_session", "all_session", "all_project"]

MAX_RPC_COMMAND_ID_CHARS = 256
MAX_RPC_MCP_SERVER_NAME_CHARS = 64
MAX_RPC_MCP_SNIPPET_CHARS = 65_536
MAX_RPC_COMMAND_TYPE_CHARS = 64
MAX_RPC_API_KEY_CHARS = 8_192
STORE_API_KEY_SECRET_FIELD = "_api_key"
# Secret-bearing fields by command type. Before a command is queued, each one is
# moved under its underscore-prefixed name and wrapped so the retained payload
# never prints it; `provided_fields` reports it under its submitted name.
_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "store_api_key": ("api_key",),
    "add_mcp_server": ("env", "headers"),
    "preview_mcp_snippet": ("snippet",),
}


@dataclass(frozen=True)
class _RpcSecretValue:
    value: object = dataclass_field(repr=False)

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def detach_secret_fields(command: dict[str, object]) -> dict[str, object]:
    """Move a command's secret fields into redacted values before queueing or logging."""

    fields = [
        field for field in _SECRET_FIELDS.get(rpc_command_type(command), ()) if field in command
    ]
    if not fields:
        return command
    detached = dict(command)
    for field in fields:
        detached[f"_{field}"] = _RpcSecretValue(detached.pop(field))
    return detached


def take_store_api_key(command: dict[str, object]) -> object | None:
    """Remove and reveal a detached API key only at the storage boundary."""

    value = command.pop("api_key", None)
    if value is None:
        value = command.pop(STORE_API_KEY_SECRET_FIELD, None)
    return value.value if isinstance(value, _RpcSecretValue) else value


def rpc_command_payload_size(command: Mapping[str, object]) -> int:
    """Return the wire-size equivalent without making secrets printable."""

    sized = {
        key: value.value if isinstance(value, _RpcSecretValue) else value
        for key, value in command.items()
    }
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


class GetPermissionsCommand(RpcCommandModel):
    """Read the active and saved project permission modes."""

    type: Literal["get_permissions"] = "get_permissions"


class SetPermissionsCommand(RpcCommandModel):
    """Save the project permission mode and reset temporary tool grants."""

    type: Literal["set_permissions"] = "set_permissions"
    mode: PermissionMode


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


class GetProjectFilesCommand(RpcCommandModel):
    """Discover bounded metadata using only backend-owned root and policy."""

    type: Literal["get_project_files"] = "get_project_files"


class GetSkillsCommand(RpcCommandModel):
    """Return the active immutable skill catalog snapshot."""

    type: Literal["get_skills"] = "get_skills"


class GetMcpStatusCommand(RpcCommandModel):
    """Return sanitized MCP server and registered-tool status."""

    type: Literal["get_mcp_status"] = "get_mcp_status"


class AddMcpServerCommand(RpcCommandModel):
    """Save an MCP server to the user settings, then restart MCP servers.

    The entry has the shape of one ``mcp_servers`` settings entry. ``env`` and
    ``headers`` hold literal values the user entered; they are written to the
    settings file and never echoed in events, errors, or this model's repr. Adding a
    server is the user's decision: a frontend must show the exact command and
    arguments or URL, the ``env_from`` names, and the variables ``headers_from``
    sends to the URL, and have the user confirm before sending it.
    """

    type: Literal["add_mcp_server"] = "add_mcp_server"
    name: str = Field(min_length=1, max_length=MAX_RPC_MCP_SERVER_NAME_CHARS)
    command: str | None = Field(default=None, min_length=1)
    # The wrap validator below sees JSON arrays as lists, which strict tuples reject.
    args: tuple[str, ...] = Field(default=(), strict=False)
    url: str | None = Field(default=None, min_length=1)
    env: dict[str, str] = Field(default_factory=dict, repr=False)
    env_from: tuple[str, ...] = Field(default=(), strict=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    headers_from: dict[str, str] = Field(default_factory=dict)
    replace: bool = False

    @model_validator(mode="wrap")
    @classmethod
    def _redact_validation_errors(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        # Errors outside `env` can still carry the whole payload as their input.
        try:
            return handler(value)
        except ValidationError as exc:
            raise redact_validation_error_inputs(exc) from None

    def __repr__(self) -> str:
        return (
            f"AddMcpServerCommand(id={self.id!r}, name={self.name!r}, command={self.command!r}, "
            f"args={self.args!r}, url={self.url!r}, env=<{len(self.env)} hidden>, "
            f"env_from={self.env_from!r}, headers=<{len(self.headers)} hidden>, "
            f"headers_from={self.headers_from!r}, replace={self.replace!r})"
        )

    __str__ = __repr__


class RemoveMcpServerCommand(RpcCommandModel):
    """Remove an MCP server from the user settings, then restart MCP servers."""

    type: Literal["remove_mcp_server"] = "remove_mcp_server"
    name: str = Field(min_length=1, max_length=MAX_RPC_MCP_SERVER_NAME_CHARS)


class SetMcpServerEnabledCommand(RpcCommandModel):
    """Enable or disable an MCP server in the user settings, then restart MCP servers.

    Enabling a server is the user's decision, as adding one is. ``expected`` is
    required when enabling: it is the ``launch`` from the ``rpc.mcp`` status that the
    user was shown and confirmed, and the server is enabled only if its saved
    settings still match it.
    """

    type: Literal["set_mcp_server_enabled"] = "set_mcp_server_enabled"
    name: str = Field(min_length=1, max_length=MAX_RPC_MCP_SERVER_NAME_CHARS)
    enabled: bool
    expected: RpcMcpServerLaunch | None = None

    @model_validator(mode="after")
    def _require_expected_when_enabling(self) -> Self:
        if self.enabled and self.expected is None:
            raise ValueError("enabling an MCP server requires the confirmed launch settings")
        return self


class PreviewMcpSnippetCommand(RpcCommandModel):
    """Parse a pasted ``{"mcpServers": {...}}`` snippet without saving anything.

    The reply lists each server's launch settings with environment variables named
    only. A snippet can hold real secret values, so it is treated like an API key: it
    is never echoed in events, errors, or this model's repr.
    """

    type: Literal["preview_mcp_snippet"] = "preview_mcp_snippet"
    snippet: str = Field(min_length=1, max_length=MAX_RPC_MCP_SNIPPET_CHARS, repr=False)

    @model_validator(mode="wrap")
    @classmethod
    def _redact_validation_errors(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        try:
            return handler(value)
        except ValidationError as exc:
            raise redact_validation_error_inputs(exc) from None

    def __repr__(self) -> str:
        return f"PreviewMcpSnippetCommand(id={self.id!r}, snippet=<{len(self.snippet)} chars>)"

    __str__ = __repr__


class ReconnectMcpCommand(RpcCommandModel):
    """Reload MCP servers from the user settings and restart them."""

    type: Literal["reconnect_mcp"] = "reconnect_mcp"


class LoginMcpServerCommand(RpcCommandModel):
    """Sign in to an HTTP MCP server with OAuth, then restart MCP servers.

    The host opens the authorization page in the user's browser and reports it
    as ``rpc.mcp.login``; the command finishes when the browser returns, the
    sign-in times out, or the command is cancelled.
    """

    type: Literal["login_mcp_server"] = "login_mcp_server"
    name: str = Field(min_length=1, max_length=MAX_RPC_MCP_SERVER_NAME_CHARS)


class LogoutMcpServerCommand(RpcCommandModel):
    """Forget an MCP server's OAuth sign-in, then restart MCP servers."""

    type: Literal["logout_mcp_server"] = "logout_mcp_server"
    name: str = Field(min_length=1, max_length=MAX_RPC_MCP_SERVER_NAME_CHARS)


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
    query: str | None = Field(default=None, max_length=1024)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _validate_catalog_query(self) -> Self:
        if self.query is not None and len(self.query.encode("utf-8")) > 1024:
            raise ValueError("Session query exceeds 1024 UTF-8 bytes")
        return self


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
    expected_token: str | None = Field(default=None, min_length=1, max_length=128)


class PopQueueCommand(RpcCommandModel):
    """Remove the latest item from one active queue."""

    type: Literal["pop_queue"] = "pop_queue"
    kind: QueueKind
    expected_token: str | None = Field(default=None, min_length=1, max_length=128)


class ClearQueueCommand(RpcCommandModel):
    """Clear one or both active queues."""

    type: Literal["clear_queue"] = "clear_queue"
    kind: QueueKind | None = None
    expected_token: str | None = Field(default=None, min_length=1, max_length=128)


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
    web_search: bool | None = None
    mode: AgentMode | None = None
    # `to_json_line()` uses `exclude_none=True`, so a bare `effort=None` is
    # indistinguishable on the wire from never having set `effort` at all --
    # the RPC server keys off "effort" in command (dict key presence), not
    # the value, to decide whether to touch agent.effort. This field is the
    # only way a client can explicitly request clearing effort back to the
    # provider's own default: it has no default value that `exclude_none`
    # would ever drop, since `False` is not `None`.
    clear_effort: bool = False
    persist_model_selection: bool = False

    @model_validator(mode="after")
    def _validate_mutation(self) -> ConfigureCommand:
        if not self.clear_effort and all(
            value is None
            for value in (
                self.provider,
                self.model,
                self.effort,
                self.auto_compaction_enabled,
                self.web_search,
                self.mode,
            )
        ):
            raise ValueError("configure commands require an effective mutation")
        if self.clear_effort and self.effort is not None:
            raise ValueError("configure commands cannot set and clear effort together")
        if self.persist_model_selection and not (
            self.provider is not None
            or self.model is not None
            or self.effort is not None
            or self.clear_effort
        ):
            raise ValueError("persist_model_selection requires a model selection mutation")
        return self


type RpcCommand = Annotated[
    PromptCommand
    | InitCommand
    | CompactCommand
    | GetSessionStatsCommand
    | GetStateCommand
    | GetPermissionsCommand
    | SetPermissionsCommand
    | GetCommandsCommand
    | GetModelCatalogCommand
    | GetConnectionCatalogCommand
    | StoreApiKeyCommand
    | DisconnectProviderCommand
    | BeginDeviceCodeCommand
    | GetSkillsCommand
    | GetProjectFilesCommand
    | GetMcpStatusCommand
    | AddMcpServerCommand
    | RemoveMcpServerCommand
    | SetMcpServerEnabledCommand
    | ReconnectMcpCommand
    | LoginMcpServerCommand
    | LogoutMcpServerCommand
    | PreviewMcpSnippetCommand
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
    """Validated command and retained ingress metadata for scheduling and rejection."""

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
        retained = dict(command.model_dump(exclude_none=True) if payload is None else payload)
        return cls._from_parts(command, retained)

    @classmethod
    def from_unknown(cls, payload: Mapping[str, object]) -> ParsedRpcCommand:
        retained = dict(payload)
        return cls._from_parts(
            UnknownCommandEnvelope(command_type=rpc_command_type(retained)),
            retained,
        )

    @classmethod
    def _from_parts(
        cls,
        value: RpcCommand | UnknownCommandEnvelope,
        payload: dict[str, object],
    ) -> ParsedRpcCommand:
        detached = detach_secret_fields(payload)
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
    def command_id_error(self) -> str | None:
        """Validate the submitted ID without changing coordinator identity semantics."""
        command_id = self._payload.get("id")
        if command_id is None:
            return None
        if isinstance(command_id, str) and command_id:
            if len(command_id) <= MAX_RPC_COMMAND_ID_CHARS:
                return None
            return f"RPC command id must contain at most {MAX_RPC_COMMAND_ID_CHARS} characters"
        return "RPC command id must be a non-empty string"

    @property
    def known(self) -> RpcCommand | None:
        return None if isinstance(self.value, UnknownCommandEnvelope) else self.value

    @property
    def allows_prompt_read(self) -> bool:
        return isinstance(self.value, GetMessagesCommand) and self.value.allow_during_prompt is True

    @property
    def provided_fields(self) -> frozenset[str]:
        """Return submitted payload field names without exposing their values."""

        secret_fields = {
            f"_{field}": field for fields in _SECRET_FIELDS.values() for field in fields
        }
        return frozenset(secret_fields.get(field, field) for field in self._payload)

    def without_id(self) -> ParsedRpcCommand:
        payload = dict(self._payload)
        payload.pop("id", None)
        value = self.value
        if not isinstance(value, UnknownCommandEnvelope):
            value = value.model_copy(update={"id": None})
        return self._from_parts(value, payload)


def rpc_command_from_json(line: str) -> RpcCommand:
    """Parse one JSONL command line into a typed command model."""

    return RpcCommandAdapter.validate_json(line)
