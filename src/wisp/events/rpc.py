"""RPC command lifecycle, backend state, catalogs, permissions, and project-file reports."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wisp.agent.mode import AgentMode
from wisp.events._base import QueueMode, WispEvent
from wisp.permissions import PermissionMode
from wisp.project_files import (
    MAX_PROJECT_FILES,
    MAX_PROJECT_PATH_CHARS,
    PROJECT_PATH_PATTERN,
    is_display_safe_path,
)
from wisp.skills.models import SkillDiagnosticCode, SkillDiagnosticSeverity, SkillSource

MAX_RPC_MODEL_CATALOG_PROVIDERS = 128
MAX_RPC_CONNECTION_PROVIDERS = 32
MAX_RPC_CONNECTION_METHODS = 64
MAX_RPC_CONNECTION_LABEL_CHARS = 256
MAX_RPC_DEVICE_CODE_CHARS = 128
MAX_RPC_DEVICE_CODE_URI_CHARS = 512
MAX_RPC_DEVICE_CODE_ATTEMPTS = 10_000
MAX_RPC_OAUTH_EXPIRY_CHARS = 64
MAX_RPC_MODEL_CATALOG_MODELS_PER_PROVIDER = 512
MAX_RPC_MODEL_CATALOG_MODELS = 4_096
MAX_RPC_MODEL_CATALOG_EFFORT_LEVELS = 16
MAX_RPC_PROVIDER_ID_CHARS = 128
MAX_RPC_MODEL_ID_CHARS = 256
MAX_RPC_MODEL_DISPLAY_CHARS = 256
MAX_RPC_MODEL_EFFORT_CHARS = 64


class CodingSessionState(BaseModel):
    """Read-only in-memory configuration and queue summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str | None = None
    mode: AgentMode = "build"
    effort: str | None = None
    auto_compaction_enabled: bool
    steering_mode: QueueMode
    follow_up_mode: QueueMode
    pending_steering_count: int = Field(ge=0)
    pending_follow_up_count: int = Field(ge=0)


class RpcStateSnapshot(CodingSessionState):
    """RPC-facing state extended with session and active-command identity."""

    session_id: str | None = None
    session_path: Path | None = None
    session_name: str | None = None
    active_command_id: str | None = None
    active_command_type: str | None = None
    cancel_requested: bool = False


class RpcCommandArgument(BaseModel):
    """Frontend-neutral argument metadata for a discoverable RPC command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    required: bool = False


class RpcCommandDescriptor(BaseModel):
    """Frontend-neutral command metadata returned by RPC discovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    category: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    slash_command: str = Field(min_length=2)
    slash_aliases: tuple[str, ...] = ()
    arguments: tuple[RpcCommandArgument, ...] = ()
    accepts_arguments: bool = False
    prefill_on_partial_enter: bool = False
    order: int

    @model_validator(mode="after")
    def _validate_slash_spelling(self) -> Self:
        if self.slash_command != f"/{self.name}":
            raise ValueError("RPC command descriptor slash_command must match name")
        for alias in self.slash_aliases:
            if not (alias.startswith("/") or alias.startswith(":")):
                raise ValueError("RPC command descriptor slash_aliases must be command tokens")
        return self


class RpcSkillCatalogEntry(BaseModel):
    """One model-free skill descriptor returned to RPC frontends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    source: SkillSource


class RpcSkillDiagnostic(BaseModel):
    """One isolated skill discovery diagnostic returned to RPC frontends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: SkillDiagnosticCode
    severity: SkillDiagnosticSeverity
    message: str
    source: SkillSource
    path: Path | None = None


class RpcSkillCatalogSnapshot(BaseModel):
    """Current immutable skill catalog and project-trust state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[RpcSkillCatalogEntry, ...] = ()
    diagnostics: tuple[RpcSkillDiagnostic, ...] = ()
    project_trusted: bool = False


class RpcMcpServerSnapshot(BaseModel):
    """Sanitized status for one configured MCP server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    status: Literal["connected", "disconnected", "unavailable"]
    tool_names: tuple[str, ...] = ()
    error: str | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        if self.status != "unavailable" and self.error is not None:
            raise ValueError("available MCP server states cannot include an error")
        if self.status == "unavailable" and self.error is None:
            raise ValueError("unavailable MCP servers require an error")
        return self


class RpcMcpStatusSnapshot(BaseModel):
    """Current sanitized MCP server and registered-tool status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: tuple[RpcMcpServerSnapshot, ...] = ()


RpcProviderId = Annotated[
    str,
    Field(min_length=1, max_length=MAX_RPC_PROVIDER_ID_CHARS),
]
RpcModelId = Annotated[
    str,
    Field(min_length=1, max_length=MAX_RPC_MODEL_ID_CHARS),
]
RpcModelEffort = Annotated[
    str,
    Field(min_length=1, max_length=MAX_RPC_MODEL_EFFORT_CHARS),
]


class RpcModelCatalogEntry(BaseModel):
    """Picker-relevant metadata for one canonical provider model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: RpcModelId
    lifecycle: Literal["stable", "preview", "legacy"] | None = None
    effort_levels: tuple[RpcModelEffort, ...] = Field(
        default=(),
        max_length=MAX_RPC_MODEL_CATALOG_EFFORT_LEVELS,
    )


class RpcModelProviderSnapshot(BaseModel):
    """Bounded model metadata and runtime availability for one provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: RpcProviderId
    display_name: Annotated[
        str,
        Field(min_length=1, max_length=MAX_RPC_MODEL_DISPLAY_CHARS),
    ]
    default_model: RpcModelId
    available: bool
    models: tuple[RpcModelCatalogEntry, ...] = Field(
        default=(),
        max_length=MAX_RPC_MODEL_CATALOG_MODELS_PER_PROVIDER,
    )


class RpcModelSelectionSnapshot(BaseModel):
    """Backend-authoritative current provider, model, and effort selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: RpcProviderId
    model: RpcModelId | None = None
    effective_model: RpcModelId | None = None
    catalog_model: RpcModelId | None = None
    effort: RpcModelEffort | None = None


class RpcModelCatalogSnapshot(BaseModel):
    """Bounded effective model catalog plus the current backend selection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selection: RpcModelSelectionSnapshot
    providers: tuple[RpcModelProviderSnapshot, ...] = Field(
        default=(),
        max_length=MAX_RPC_MODEL_CATALOG_PROVIDERS,
    )

    @model_validator(mode="after")
    def _validate_total_models(self) -> Self:
        if sum(len(provider.models) for provider in self.providers) > MAX_RPC_MODEL_CATALOG_MODELS:
            raise ValueError(
                f"RPC model catalogs may contain at most {MAX_RPC_MODEL_CATALOG_MODELS} models"
            )
        return self


class RpcConnectionMethodSnapshot(BaseModel):
    """Sanitized authentication method for one provider connection picker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: RpcProviderId
    label: Annotated[str, Field(min_length=1, max_length=MAX_RPC_CONNECTION_LABEL_CHARS)]
    kind: Literal["api_key", "device_code"]
    source: Literal["environment", "stored", "missing"]
    environment_variable: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=MAX_RPC_CONNECTION_LABEL_CHARS),
    ] = None
    oauth_expires_at: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=MAX_RPC_OAUTH_EXPIRY_CHARS),
    ] = None
    has_stored_credential: bool = False


class RpcConnectionProviderSnapshot(BaseModel):
    """One provider family and its sanitized authentication methods."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: RpcProviderId
    label: Annotated[str, Field(min_length=1, max_length=MAX_RPC_CONNECTION_LABEL_CHARS)]
    methods: tuple[RpcConnectionMethodSnapshot, ...] = Field(
        default=(),
        min_length=1,
        max_length=8,
    )


class RpcConnectionCatalogSnapshot(BaseModel):
    """Bounded sanitized provider connection catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    providers: tuple[RpcConnectionProviderSnapshot, ...] = Field(
        default=(),
        max_length=MAX_RPC_CONNECTION_PROVIDERS,
    )

    @model_validator(mode="after")
    def _validate_total_methods(self) -> Self:
        if sum(len(provider.methods) for provider in self.providers) > MAX_RPC_CONNECTION_METHODS:
            raise ValueError(
                f"RPC connection catalogs may contain at most {MAX_RPC_CONNECTION_METHODS} methods"
            )
        return self


class RpcCommandStarted(WispEvent):
    type: Literal["rpc.command.started"] = "rpc.command.started"
    command_id: str
    command_type: str


class RpcCommandFinished(WispEvent):
    type: Literal["rpc.command.finished"] = "rpc.command.finished"
    command_id: str
    command_type: str
    ok: bool
    error: str | None = None


class PermissionState(BaseModel):
    """Effective approval mode and user-owned default for the active project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: PermissionMode
    saved_mode: PermissionMode | None = None
    project_path: Path | None = None


class RpcPermissionsReported(WispEvent):
    """Permission snapshot returned by a permission inspection or change."""

    type: Literal["rpc.permissions"] = "rpc.permissions"
    command_id: str
    permissions: PermissionState


class RpcStateReported(WispEvent):
    """Immediate, non-persisted in-memory state returned over RPC."""

    type: Literal["rpc.state"] = "rpc.state"
    command_id: str
    state: RpcStateSnapshot


class RpcCommandsReported(WispEvent):
    """Immediate, non-persisted command registry snapshot returned over RPC."""

    type: Literal["rpc.commands"] = "rpc.commands"
    command_id: str
    commands: tuple[RpcCommandDescriptor, ...] = ()


class RpcModelCatalogReported(WispEvent):
    """Immediate, non-persisted effective model catalog returned over RPC."""

    type: Literal["rpc.model_catalog"] = "rpc.model_catalog"
    command_id: str
    catalog: RpcModelCatalogSnapshot


class RpcConnectionCatalogReported(WispEvent):
    """Immediate, non-persisted provider connection catalog returned over RPC."""

    type: Literal["rpc.connection_catalog"] = "rpc.connection_catalog"
    command_id: str
    catalog: RpcConnectionCatalogSnapshot


class RpcDeviceCodeReported(WispEvent):
    """User-visible device-code challenge for one connection command."""

    type: Literal["rpc.device_code"] = "rpc.device_code"
    command_id: str
    provider: RpcProviderId
    verification_uri: Annotated[
        str,
        Field(min_length=1, max_length=MAX_RPC_DEVICE_CODE_URI_CHARS),
    ]
    user_code: Annotated[str, Field(min_length=1, max_length=MAX_RPC_DEVICE_CODE_CHARS)]


class RpcDeviceCodeProgressReported(WispEvent):
    """Sanitized progress from one backend-owned device-code poll loop."""

    type: Literal["rpc.device_code.progress"] = "rpc.device_code.progress"
    command_id: str
    provider: RpcProviderId
    attempt: int = Field(ge=1, le=MAX_RPC_DEVICE_CODE_ATTEMPTS)


class RpcProjectFile(BaseModel):
    """Display-safe relative metadata; never authorization for a file operation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, regex_engine="python-re")

    path: str = Field(min_length=1, max_length=MAX_PROJECT_PATH_CHARS, pattern=PROJECT_PATH_PATTERN)
    kind: Literal["file", "directory"]

    @field_validator("path")
    @classmethod
    def _require_display_safe_path(cls, value: str) -> str:
        if not is_display_safe_path(value):
            raise ValueError("Project paths must be display-safe relative UTF-8")
        return value


class RpcProjectFilesReported(WispEvent):
    """One bounded, non-persisted snapshot for a discovery request."""

    type: Literal["rpc.project_files"] = "rpc.project_files"
    command_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, le=2**53 - 1, strict=True)
    entries: tuple[RpcProjectFile, ...] = Field(max_length=MAX_PROJECT_FILES)
    truncated: bool


class ProjectFilesInvalidated(WispEvent):
    """Previously returned file metadata is obsolete after a policy transition."""

    type: Literal["project_files.invalidated"] = "project_files.invalidated"
    generation: int = Field(ge=1, le=2**53 - 1, strict=True)


class RpcSkillsReported(WispEvent):
    """Immediate, non-persisted skill catalog snapshot returned over RPC."""

    type: Literal["rpc.skills"] = "rpc.skills"
    command_id: str
    catalog: RpcSkillCatalogSnapshot


class RpcMcpStatusReported(WispEvent):
    """Immediate, non-persisted MCP runtime status returned over RPC."""

    type: Literal["rpc.mcp"] = "rpc.mcp"
    command_id: str
    status: RpcMcpStatusSnapshot


class SkillCatalogUpdated(WispEvent):
    """A trust transition replaced the catalog available to future operations."""

    type: Literal["skill.catalog.updated"] = "skill.catalog.updated"
    catalog: RpcSkillCatalogSnapshot
