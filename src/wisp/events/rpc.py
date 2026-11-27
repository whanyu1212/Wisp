"""RPC command lifecycle, backend state, catalogs, permissions, and project-file reports."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import TypeAliasType

from wisp.events._base import ImagePart, ManagedProcessState, QueueMode, WispEvent
from wisp.events.tools import PROCESS_COMMAND_SUMMARY_MAX_CHARS, TOOL_PROGRESS_TAIL_MAX_BYTES
from wisp.project_files import (
    MAX_PROJECT_FILES,
    MAX_PROJECT_PATH_CHARS,
    PROJECT_DIRECTORY_PATTERN,
    PROJECT_PATH_PATTERN,
    is_display_safe_path,
)
from wisp.skills.models import SkillDiagnosticCode, SkillDiagnosticSeverity, SkillSource

# The tool-approval posture a frontend selects; persisted per project by ``wisp.trust``.
# A ``type`` alias so the schema bundle keeps its named ``PermissionMode`` definition.
PermissionMode = TypeAliasType("PermissionMode", Literal["ask", "auto", "auto_commands", "yolo"])

# Whether the agent may change the project ("build") or only inspect it and plan ("plan").
# A ``type`` alias for the same reason as ``PermissionMode``.
AgentMode = TypeAliasType("AgentMode", Literal["build", "plan"])

DEFAULT_AGENT_MODE: AgentMode = "build"

# When a run offers the codemode tool: "automatic" when a script may call one of its MCP
# tools, or "always" or "never". Saved as the user setting ``codemode`` (unset, true or false).
CodemodeSetting = TypeAliasType("CodemodeSetting", Literal["automatic", "always", "never"])
CodemodePresentation = TypeAliasType(
    "CodemodePresentation", Literal["direct_and_codemode", "codemode_only"]
)

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
MAX_STARTUP_HISTORY_PREVIEW_LINES = 8
MAX_STARTUP_HISTORY_PREVIEW_LINE_CHARS = 4_096
# The most recent calls kept per file; the TUI jumps to the latest one.
MAX_GIT_DIFF_FILE_EDITS = 16
MAX_LIVE_BROWSER_WIDTH = 1_600
MAX_LIVE_BROWSER_HEIGHT = 1_200
# Includes base64 overhead for a full-size, incompressible RGBA PNG.
MAX_LIVE_BROWSER_FRAME_CHARS = 11_000_000
MAX_BROWSER_URL_CHARS = 2_048
MAX_RECENT_BROWSER_ADDRESSES = 20
MAX_BROWSER_TITLE_CHARS = 512
MAX_BROWSER_SOURCE_TOOL_CHARS = 128
MAX_BROWSER_VIEWPORT_SIDE = 16_384


class CodingSessionState(BaseModel):
    """Read-only in-memory configuration and queue summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str | None = None
    mode: AgentMode = DEFAULT_AGENT_MODE
    effort: str | None = None
    auto_compaction_enabled: bool
    web_search: bool = False
    steering_mode: QueueMode
    follow_up_mode: QueueMode
    pending_steering_count: int = Field(ge=0)
    pending_follow_up_count: int = Field(ge=0)


class BackgroundProcess(BaseModel):
    """A process started with ``bash operation=start`` that is still running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: str = Field(min_length=1, max_length=128)
    # The command as started, cut to a summary for display.
    command: str = Field(max_length=PROCESS_COMMAND_SUMMARY_MAX_CHARS)
    # When it started, in Unix milliseconds, like a tool result's process times.
    started_at_ms: int = Field(ge=0)
    # Whether the model asked to be told when it stops.
    notify: bool = False


class RpcProcessSnapshot(BaseModel):
    """A background process of the current session, running or ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    process_id: str = Field(min_length=1, max_length=128)
    # The command as started, cut to a summary for display.
    command: str = Field(max_length=PROCESS_COMMAND_SUMMARY_MAX_CHARS)
    state: ManagedProcessState
    # Set once the process completed; None for any other state.
    exit_code: int | None = None
    # When it started and, once it has, when it ended, in Unix milliseconds.
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int | None = Field(default=None, ge=0)
    # Whether the user stopped it (state ``cancelled``), rather than the agent.
    stopped_by_user: bool = False
    # Whether the model asked to be told when it stops.
    notify: bool = False


class RpcProcessOutputChunk(BaseModel):
    """A run of text one stream printed with nothing from the other in between."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream: Literal["stdout", "stderr"]
    # Decoded as UTF-8 with invalid bytes replaced; untrusted, so frontends make it
    # safe before drawing it.
    text: str


class RpcStateSnapshot(CodingSessionState):
    """RPC-facing state extended with session and active-command identity."""

    session_id: str | None = None
    session_path: Path | None = None
    session_name: str | None = None
    active_command_id: str | None = None
    active_command_type: str | None = None
    cancel_requested: bool = False
    # The session's background processes, oldest first. They belong to the
    # session: changing sessions stops them.
    background_processes: tuple[BackgroundProcess, ...] = ()


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


class RpcMcpHeaderFrom(BaseModel):
    """One HTTP header whose value Wisp builds from an environment variable.

    The header is sent as ``"{scheme} {value}"``, or the bare value when
    ``scheme`` is None. Showing it tells the user which variable's value goes to
    the server.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    header: str = Field(min_length=1)
    variable: str = Field(min_length=1)
    scheme: str | None = None


class RpcMcpServerLaunch(BaseModel):
    """What one MCP server runs or connects to, as a user must see it to confirm it.

    Exactly one of ``command`` or ``url`` is set. Environment variables appear only
    by name: ``env_names`` are set from values stored in the settings file, which are
    never sent, and ``env_from`` are forwarded from Wisp's own environment. An HTTP
    server's headers appear the same way: ``header_names`` carry stored values, and
    ``headers_from`` send the named variables to the server's URL.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env_names: tuple[str, ...] = ()
    env_from: tuple[str, ...] = ()
    header_names: tuple[str, ...] = ()
    headers_from: tuple[RpcMcpHeaderFrom, ...] = ()


class RpcMcpServerSnapshot(BaseModel):
    """Sanitized status for one configured MCP server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    # "connecting" is reported only between a host starting and its startup
    # connect ending; a server never returns to it.
    status: Literal["connecting", "connected", "disconnected", "unavailable", "disabled"]
    tool_names: tuple[str, ...] = ()
    # The tools among ``tool_names`` Wisp treats as read-only, in the same order:
    # those the server's `tool_safety` marked so when it last started. Only these
    # can a subagent or plan mode call.
    read_only_tool_names: tuple[str, ...] = ()
    # The tools among ``tool_names`` the server itself says are read-only
    # (`readOnlyHint`) and its `tool_safety` leaves unset, in the same order. Wisp
    # treats them as any other tool until the user accepts them into `tool_safety`
    # with ``mark_mcp_tools_read_only``.
    read_only_hint_tool_names: tuple[str, ...] = ()
    error: str | None = None
    # The configuration the server was last started (or skipped) with.
    launch: RpcMcpServerLaunch | None = None
    # OAuth sign-in for an HTTP server: "required" when it refused Wisp, or the
    # saved sign-in failed to refresh; "signed_in" when it started with a saved
    # sign-in; "available" when the user could sign in. None for servers that
    # run a command or send their own Authorization header.
    sign_in: Literal["required", "signed_in", "available"] | None = None

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


class RpcMcpServerPreview(BaseModel):
    """One server parsed from a pasted config snippet, not yet saved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    launch: RpcMcpServerLaunch


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
    default_effort: RpcModelEffort | None = None


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
    # Whether the selected model takes images, and how many image bytes one
    # request may carry, so a composer can refuse an attachment before sending.
    image_input: bool = False
    max_request_image_bytes: int = Field(default=0, ge=0)


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
    # Required: an unvalidated () default would bypass min_length on decode.
    methods: tuple[RpcConnectionMethodSnapshot, ...] = Field(min_length=1, max_length=8)


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


ReloadSettingName = TypeAliasType(
    "ReloadSettingName",
    Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")],
)


class RpcSettingsReloaded(WispEvent):
    """Reload summary with public session state, never credentials or raw settings."""

    type: Literal["rpc.settings.reloaded"] = "rpc.settings.reloaded"
    state: CodingSessionState
    command_id: str
    applied: tuple[ReloadSettingName, ...] = Field(default=(), max_length=32)
    unchanged: tuple[ReloadSettingName, ...] = Field(default=(), max_length=32)
    overridden: tuple[ReloadSettingName, ...] = Field(default=(), max_length=32)
    restart_required: tuple[ReloadSettingName, ...] = Field(default=(), max_length=32)


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
    # True in broad folders, where elevated modes are session-only. The wire
    # name is retained for existing clients; it applies to both elevated modes.
    session_only_yolo: bool = False
    # Saved per-project bash command-prefix allow rules, oldest first.
    command_allow_rules: tuple[str, ...] = ()


class RpcPermissionsReported(WispEvent):
    """Permission snapshot returned by a permission inspection or change."""

    type: Literal["rpc.permissions"] = "rpc.permissions"
    command_id: str
    permissions: PermissionState


class CodemodeState(BaseModel):
    """The session's codemode setting and whether its next run would offer the tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    setting: CodemodeSetting
    presentation: CodemodePresentation = "direct_and_codemode"
    effective_presentation: CodemodePresentation = "direct_and_codemode"
    # Whether the `codemode` extra is installed; without it no run offers the tool.
    available: bool
    # Whether a run started now would offer the tool, given its tools and MCP exposure.
    offered: bool
    # The setting `WISP_CODEMODE` gives, which outranks the saved one at startup.
    env_setting: CodemodeSetting | None = None
    # The MCP tools a run started now may use, and how many of them a script may
    # call: eligible tools, less any that direct exposure cannot call by name.
    mcp_tools: int = Field(ge=0)
    scriptable_mcp_tools: int = Field(ge=0)


class RpcCodemodeReported(WispEvent):
    """Codemode snapshot returned by a codemode inspection or change."""

    type: Literal["rpc.codemode"] = "rpc.codemode"
    command_id: str
    codemode: CodemodeState


# The ways a subagent can start; `spawn_agent` takes the same words as ``context``.
SubagentKindName = TypeAliasType("SubagentKindName", Literal["fresh", "fork"])


class SubagentKind(BaseModel):
    """One way a subagent starts, and what a subagent started that way would get now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: SubagentKindName
    # The model it would be asked on. None is the provider's default model.
    model: str | None = None
    # The reasoning effort it would be asked at. None is the model's own default.
    effort: str | None = None
    # What it is told about being a subagent. A fresh one reads this after the
    # session prompt; a forked one after the conversation, with its task.
    instructions: str


class SubagentsState(BaseModel):
    """The session's subagents setting and what its next run's subagents would use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Whether runs offer the `spawn_agent` tool. Saved as the user setting ``subagents``.
    enabled: bool
    # Whether a run started now would offer the tool, given its tools and mode.
    offered: bool
    # What `WISP_SUBAGENTS` sets, which outranks the saved setting at startup.
    env_enabled: bool | None = None
    # The user setting ``subagent_model``: the model a subagent that starts fresh
    # should use. None leaves it on the session's model.
    model: str | None = None
    # The model such a subagent would use now: the setting, unless the model catalog
    # lists the current provider without that model, when it is the session's own. A
    # forked subagent always uses the session's model.
    model_in_use: str | None = None
    # What `WISP_SUBAGENT_MODEL` names, which outranks the saved setting at startup.
    env_model: str | None = None
    # The tools a subagent of a run started now could use, and the tools that run
    # offers which a subagent is refused. Both are empty when the run would have no
    # tools; they do not depend on whether subagents are on.
    tools: tuple[str, ...] = ()
    refused_tools: tuple[str, ...] = ()
    kinds: tuple[SubagentKind, ...] = ()
    # Limits every subagent shares: tool rounds each, tasks one call may hand out,
    # subagents working at once, and the bytes of one report that are kept.
    max_tool_rounds: int = Field(default=0, ge=0)
    max_tasks: int = Field(default=0, ge=0)
    max_parallel: int = Field(default=0, ge=0)
    report_max_bytes: int = Field(default=0, ge=0)


class RpcSubagentsReported(WispEvent):
    """Subagents snapshot returned by a subagents inspection or change."""

    type: Literal["rpc.subagents"] = "rpc.subagents"
    command_id: str
    subagents: SubagentsState


class RpcWorkspaceReported(WispEvent):
    """Where the session's commands run and the Git branch checked out there.

    The host sends it at startup, as each prompt run starts, and as it finishes, so a
    frontend can show the folder and keep the branch current after a run switches
    it. It also sends one after ``change_directory`` moves the folder, and after a
    session or a branch of it becomes active, since that restores the folder the
    session last recorded. A run's tools may work outside the session folder
    (``/init`` works from the project root); the report at its start names the
    folder they use. A branch switched outside Wisp while idle shows after the next
    run.
    """

    type: Literal["rpc.workspace"] = "rpc.workspace"
    cwd: Path
    # The current branch, or the short commit for a detached HEAD; None outside a
    # Git work tree or when Git does not answer in time.
    git_branch: str | None = None
    # Shared session image store directory so frontends can resolve images stored
    # during a fresh or /new run before the session file is committed.
    session_images_dir: Path | None = None


class RpcStartupHistoryPreview(WispEvent):
    """Optional, unsolicited display text shown while the startup transcript loads."""

    type: Literal["rpc.startup.history_preview"] = "rpc.startup.history_preview"
    lines: tuple[
        Annotated[str, Field(strict=True, max_length=MAX_STARTUP_HISTORY_PREVIEW_LINE_CHARS)], ...
    ] = Field(min_length=1, max_length=MAX_STARTUP_HISTORY_PREVIEW_LINES)


class RpcStateReported(WispEvent):
    """Immediate, non-persisted in-memory state returned over RPC."""

    type: Literal["rpc.state"] = "rpc.state"
    command_id: str
    state: RpcStateSnapshot


class RpcProcessesReported(WispEvent):
    """The session's background processes, running or ended.

    They come in the order they became background processes: a foreground command
    moved there joins when it moves.
    """

    type: Literal["rpc.processes"] = "rpc.processes"
    command_id: str
    processes: tuple[RpcProcessSnapshot, ...] = ()


class RpcProcessOutputReported(WispEvent):
    """The latest output of one background process, read without draining it.

    The agent's next poll of the process returns what it would have without this
    read. ``chunks`` hold both streams in the order they printed, oldest first;
    output from before a foreground command moved to the background comes as its
    stdout, then its stderr.
    """

    type: Literal["rpc.process_output"] = "rpc.process_output"
    command_id: str
    process: RpcProcessSnapshot
    chunks: tuple[RpcProcessOutputChunk, ...] = ()
    # UTF-8 bytes printed before the first chunk that the tail no longer holds.
    omitted_bytes: int = Field(default=0, ge=0)


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
    """One bounded snapshot for a discovery request or full-scan progress update."""

    type: Literal["rpc.project_files"] = "rpc.project_files"
    command_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, le=2**53 - 1, strict=True)
    entries: tuple[RpcProjectFile, ...] = Field(max_length=MAX_PROJECT_FILES)
    truncated: bool
    directory: str | None = Field(
        default=None, max_length=MAX_PROJECT_PATH_CHARS, pattern=PROJECT_DIRECTORY_PATTERN
    )
    in_progress: bool = Field(default=False, strict=True)

    @field_validator("directory")
    @classmethod
    def _require_display_safe_directory(cls, value: str | None) -> str | None:
        if value and not is_display_safe_path(value):
            raise ValueError("Directory scope must be a display-safe relative UTF-8 path")
        return value

    @model_validator(mode="after")
    def _require_full_scan_for_progress(self) -> Self:
        if self.in_progress and self.directory is not None:
            raise ValueError("Progress updates require a full project scan")
        return self


class RpcGitDiffEdit(BaseModel):
    """One successful edit or write call in the active session path that targeted a file.

    ``entry_id`` names the session entry holding the call's result. It identifies
    the call even when a provider reuses ``call_id``, so a frontend finds its card.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    call_id: str = Field(min_length=1, max_length=256)
    entry_id: str = Field(min_length=1, max_length=256)
    tool: Literal["edit", "write"]


class RpcGitDiffFile(RpcProjectFile):
    """One authorized file pair, or an explicit unavailable-content notice.

    ``edits`` lists the session's own edit and write calls on this path, oldest
    first. An empty tuple means no call in the active session path touched the
    file, so its changes came from outside this session.
    """

    status: Literal["modified", "added", "deleted", "untracked"]
    before: str = Field(max_length=50_000)
    after: str = Field(max_length=50_000)
    notice: str = Field(max_length=256)
    # Lax so a decoded JSON array is accepted as the tuple, as for the event's files.
    edits: tuple[RpcGitDiffEdit, ...] = Field(
        default=(), max_length=MAX_GIT_DIFF_FILE_EDITS, strict=False
    )


class RpcGitDiffReported(WispEvent):
    """Bounded read-only Git snapshot; untracked content is never included."""

    type: Literal["rpc.git_diff"] = "rpc.git_diff"
    command_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, le=2**53 - 1, strict=True)
    head: str = Field(max_length=64)
    files: tuple[RpcGitDiffFile, ...] = Field(max_length=256)
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


class RpcMcpSnippetPreviewed(WispEvent):
    """Servers parsed from a pasted MCP config snippet, returned over RPC.

    Environment values from the snippet are never included: a frontend asks for
    each one again, or forwards the variable from Wisp's environment.
    """

    type: Literal["rpc.mcp.preview"] = "rpc.mcp.preview"
    command_id: str
    servers: tuple[RpcMcpServerPreview, ...]


class RpcMcpLoginStarted(WispEvent):
    """Signing in to an MCP server needs the user in their browser.

    Wisp tries to open ``authorization_url`` itself; a frontend shows it too,
    for when no browser could be opened. It is the authorization server's own
    page (HTTPS, or HTTP on a loopback address) and carries no secret.
    """

    type: Literal["rpc.mcp.login"] = "rpc.mcp.login"
    command_id: str
    name: str = Field(min_length=1)
    authorization_url: str = Field(min_length=1, max_length=8_192)


class McpStatusUpdated(WispEvent):
    """MCP servers that were still connecting when the host started have settled.

    Sent once, unprompted, when the startup connect ends, so a frontend can replace
    the "connecting" status it was given earlier.
    """

    type: Literal["mcp.status.updated"] = "mcp.status.updated"
    status: RpcMcpStatusSnapshot


class SkillCatalogUpdated(WispEvent):
    """A trust transition replaced the catalog available to future operations."""

    type: Literal["skill.catalog.updated"] = "skill.catalog.updated"
    catalog: RpcSkillCatalogSnapshot


class LiveBrowserViewport(BaseModel):
    """CSS layout size and device pixel ratio requested for an isolated Chromium tab."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    width: int = Field(ge=1, le=MAX_LIVE_BROWSER_WIDTH)
    height: int = Field(ge=1, le=MAX_LIVE_BROWSER_HEIGHT)
    device_scale_factor: int = Field(ge=1, le=2)


class BrowserViewportUpdated(WispEvent):
    """Transient live snapshot of an agent browser session's current page and frame."""

    type: Literal["browser.viewport.updated"] = "browser.viewport.updated"
    live_png: str | None = Field(
        default=None, min_length=1, max_length=MAX_LIVE_BROWSER_FRAME_CHARS, repr=False
    )
    image: ImagePart | None = None
    url: str | None = Field(default=None, min_length=1, max_length=MAX_BROWSER_URL_CHARS)
    title: str | None = Field(default=None, min_length=1, max_length=MAX_BROWSER_TITLE_CHARS)
    viewport_width: int | None = Field(
        default=None, ge=1, le=MAX_BROWSER_VIEWPORT_SIDE, strict=True
    )
    viewport_height: int | None = Field(
        default=None, ge=1, le=MAX_BROWSER_VIEWPORT_SIDE, strict=True
    )
    live_device_scale: int | None = Field(default=None, ge=1, le=2, strict=True)
    window_mode: bool = False
    source_tool: str | None = Field(
        default=None, min_length=1, max_length=MAX_BROWSER_SOURCE_TOOL_CHARS
    )
    call_id: str | None = Field(default=None, min_length=1, max_length=256)


class RpcBrowserAddressesReported(WispEvent):
    """Whether the live browser may open here, and its recently opened addresses.

    ``addresses`` are newest first and always empty when ``trusted`` is false.
    """

    type: Literal["rpc.browser_addresses"] = "rpc.browser_addresses"
    command_id: str = Field(min_length=1, max_length=256)
    trusted: bool
    addresses: tuple[Annotated[str, Field(min_length=1, max_length=MAX_BROWSER_URL_CHARS)], ...] = (
        Field(max_length=MAX_RECENT_BROWSER_ADDRESSES)
    )


class RpcShellProgress(WispEvent):
    """Transient live output snapshot for a user-initiated `!` shell command."""

    type: Literal["rpc.shell.progress"] = "rpc.shell.progress"
    command_id: str = Field(min_length=1, max_length=256)
    stdout_tail: str = Field(default="", max_length=TOOL_PROGRESS_TAIL_MAX_BYTES)
    stderr_tail: str = Field(default="", max_length=TOOL_PROGRESS_TAIL_MAX_BYTES)
    stdout_bytes: int = Field(default=0, ge=0, strict=True)
    stderr_bytes: int = Field(default=0, ge=0, strict=True)
    elapsed_ms: int = Field(default=0, ge=0, strict=True)


class RpcShellCompleted(WispEvent):
    """Terminal result of a user-initiated `!` shell command, kept outside model history."""

    type: Literal["rpc.shell.completed"] = "rpc.shell.completed"
    command_id: str = Field(min_length=1, max_length=256)
    command: str = Field(min_length=1, max_length=32_768)
    exit_code: int | None = Field(default=None, strict=True)
    output: str = Field(default="", max_length=65_536)
    stdout: str = Field(default="", max_length=65_536)
    stderr: str = Field(default="", max_length=65_536)
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    duration_ms: int = Field(default=0, ge=0, strict=True)
    error: str | None = Field(default=None, min_length=1, max_length=2_048)
