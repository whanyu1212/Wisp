"""Versioned events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from wisp.agent.mode import AgentMode
from wisp.permissions import PermissionMode
from wisp.project_files import (
    MAX_PROJECT_FILES,
    MAX_PROJECT_PATH_CHARS,
    PROJECT_PATH_PATTERN,
    is_display_safe_path,
)
from wisp.skills.models import (
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillInvocationEvidence,
    SkillSource,
)
from wisp.tool_types import ToolFailureCode

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
JsonObject = dict[str, object]
MessageRole = Literal["system", "user", "assistant", "tool"]
RunOutcome = Literal["completed", "failed", "cancelled"]
FinishReason = Literal["stop", "tool_calls", "length", "error", "cancelled"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]
CompactionReason = Literal["manual", "threshold", "overflow"]
QueueMode = Literal["one_at_a_time", "all"]
QueueKind = Literal["steering", "follow_up"]
ToolPresentationStatus = Literal["done", "error", "denied", "cancelled"]
ManagedProcessState = Literal["running", "completed", "failed", "timed_out", "cancelled"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for the typed events consumed by every Wisp frontend.

    Events carry no per-event version. The live RPC protocol bundle under
    ``schemas/live-rpc/`` is the single compatibility contract: additive changes
    regenerate the current bundle and breaking changes bump the protocol version.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    timestamp: datetime = Field(default_factory=utc_now)


class ToolCallSnapshot(BaseModel):
    """Serializable tool-call state attached to a completed assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    # Runtime-only provider metadata used to reconstruct a fresh active tool
    # exchange. It is intentionally absent from persisted/public events.
    provider_call_id: str | None = Field(default=None, exclude=True, repr=False)
    parse_error: str | None = None


class _MessageOriginEvent(WispEvent):
    """Associate live presentation with an assigned session message identity.

    The session assigns the identity before publishing the event. Persistence can
    still be buffered or fail; consumers must verify the entry in durable history
    before treating it as recoverable.
    """

    message_entry_id: str | None = Field(default=None, min_length=1, max_length=4096)


class AgentStarted(_MessageOriginEvent):
    type: Literal["agent.started"] = "agent.started"
    session_id: str


class TurnStarted(WispEvent):
    type: Literal["turn.started"] = "turn.started"
    turn: int


class ProviderRetrying(WispEvent):
    """A provider request failed before streaming and will be retried."""

    type: Literal["provider.retrying"] = "provider.retrying"
    turn: int
    provider: str
    attempt: int
    max_attempts: int
    delay_seconds: float = Field(ge=0, allow_inf_nan=False)
    reason: RetryReason
    status_code: int | None = None


class MessageStarted(WispEvent):
    type: Literal["message.started"] = "message.started"
    turn: int
    role: MessageRole = "assistant"


class MessageDelta(WispEvent):
    type: Literal["message.delta"] = "message.delta"
    turn: int
    delta: str
    role: MessageRole = "assistant"
    content_index: int = 0
    content_kind: Literal["text", "thinking"] = "text"


class TokenUsage(BaseModel):
    """Provider-reported token usage for one successful model request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)


class BillableTokenUsage(BaseModel):
    """Provider-normalized token buckets used only for list-price estimates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    cache_write_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class UsageCostRates(BaseModel):
    """Exact USD-per-million rates selected when one request completed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    cache_read_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_usd_per_million: Decimal | None = Field(default=None, ge=0)


CostUnavailableReason = Literal["pricing_unavailable", "usage_incomplete", "estimation_failed"]


class UsageCost(BaseModel):
    """Immutable list-price estimate captured with one successful response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    currency: Literal["USD"] = "USD"
    provider: str
    requested_model: str | None = None
    model: str | None = None
    billable: BillableTokenUsage | None = None
    rates: UsageCostRates | None = None
    estimated_usd: Decimal | None = Field(default=None, ge=0)
    unavailable_reason: CostUnavailableReason | None = None

    @model_validator(mode="after")
    def _validate_estimate(self) -> Self:
        if self.estimated_usd is None:
            if self.unavailable_reason is None:
                raise ValueError("unpriced usage cost requires an unavailable_reason")
            return self
        if self.unavailable_reason is not None:
            raise ValueError("priced usage cost cannot include an unavailable_reason")
        if self.billable is None or self.rates is None:
            raise ValueError("priced usage cost requires billable usage and rates")
        return self


class SessionCostSummary(BaseModel):
    """Cumulative persisted list-price accounting for a session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: Literal["USD"] = "USD"
    known_usd: Decimal = Field(default=Decimal(), ge=0)
    complete: bool = True
    priced_record_count: int = Field(default=0, ge=0)
    unpriced_record_count: int = Field(default=0, ge=0)


class ContextEstimate(BaseModel):
    """Deterministic approximation of one provider-facing request context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: Literal["chars_div_4_v1", "utf8_bytes_div_4_v2"] = "chars_div_4_v1"
    system_tokens: int = Field(ge=0)
    message_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ContextObservation(BaseModel):
    """Provider-reported input usage for one exact request-context prefix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str | None = None
    input_tokens: int = Field(ge=0)
    message_count: int = Field(ge=0)
    context_fingerprint: str = Field(min_length=1)


ContextAccountingMethod = Literal[
    "fully_estimated",
    "provider_observed",
    "provider_observed_plus_estimate",
]


class ContextBudget(BaseModel):
    """Current estimate, latest observation, and model-window budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimate: ContextEstimate
    observed_tokens: int | None = Field(default=None, ge=0)
    observed_is_current: bool = False
    trailing_estimated_tokens: int | None = Field(default=None, ge=0)
    effective_tokens: int | None = Field(default=None, ge=0)
    accounting_method: ContextAccountingMethod = "fully_estimated"
    context_window: int | None = Field(default=None, gt=0)
    reserve_tokens: int = Field(ge=0)
    remaining_tokens: int | None = None
    estimated_percent: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    over_budget: bool | None = None

    @model_validator(mode="after")
    def _default_effective_tokens(self) -> Self:
        if self.effective_tokens is None:
            tokens = (
                self.observed_tokens
                if self.observed_is_current and self.observed_tokens is not None
                else self.estimate.total_tokens
            )
            object.__setattr__(self, "effective_tokens", tokens)
        if (
            self.accounting_method == "fully_estimated"
            and self.observed_is_current
            and self.observed_tokens is not None
        ):
            object.__setattr__(self, "accounting_method", "provider_observed")
        return self


class CompactionPolicyStatus(BaseModel):
    """Current automatic-compaction policy and threshold eligibility."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auto_compaction_enabled: bool = True
    threshold_eligible: bool = False
    threshold_ineligible_reason: str | None = "status unavailable"
    overflow_recovery_enabled: bool = True


class SessionStats(BaseModel):
    """Derived lifetime usage and active-context statistics for one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str | None = None
    entry_count: int = Field(ge=0)
    active_message_count: int = Field(ge=0)
    compaction_count: int = Field(ge=0)
    usage_record_count: int = Field(ge=0)
    usage: TokenUsage
    context: ContextBudget
    compaction: CompactionPolicyStatus | None = None
    cost: SessionCostSummary = Field(default_factory=SessionCostSummary)

    @model_validator(mode="before")
    @classmethod
    def _mark_legacy_usage_unpriced(cls, data: object) -> object:
        if isinstance(data, dict) and "cost" not in data:
            normalized = dict(data)
            usage_record_count = normalized.get("usage_record_count", 0)
            if isinstance(usage_record_count, int) and usage_record_count > 0:
                normalized["cost"] = {
                    "complete": False,
                    "unpriced_record_count": usage_record_count,
                }
            return normalized
        return data


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


class RpcMessageToolCallSnapshot(BaseModel):
    """Bounded tool-call state retained on an RPC transcript message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    arguments_original_bytes: int = Field(ge=0)
    arguments_truncated: bool = False
    parse_error: str | None = None


class RpcMessageToolResultSnapshot(BaseModel):
    """Bounded presentation metadata for a persisted tool-result message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ToolPresentationStatus | None = None
    exit_code: int | None = None
    output_has_exit_status: bool = False
    before_text: str | None = None
    created: bool = False
    summary: str | None = None
    truncated: bool = False


class RpcSkillInvocationSnapshot(BaseModel):
    """Bounded explicit-skill evidence attached to an RPC transcript message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    original_content: str
    original_content_bytes: int = Field(ge=0)
    original_content_truncated: bool = False
    request: str
    request_bytes: int = Field(ge=0)
    request_truncated: bool = False
    content_sha256: str
    instructions_truncated: bool = False


class RpcMessageSnapshot(BaseModel):
    """One bounded, frontend-oriented persisted message snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str
    parent_id: str | None = None
    operation_id: str | None = None
    created_at: datetime
    role: MessageRole
    content: str
    content_original_bytes: int = Field(ge=0)
    content_truncated: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: tuple[RpcMessageToolCallSnapshot, ...] = ()
    tool_calls_original_count: int = Field(default=0, ge=0)
    tool_calls_truncated: bool = False
    response_id: str | None = None
    finish_reason: FinishReason | None = None
    is_error: bool | None = None
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    tool_result: RpcMessageToolResultSnapshot | None = None
    skill_invocation: RpcSkillInvocationSnapshot | None = None

    @model_validator(mode="after")
    def _validate_tool_result_role(self) -> Self:
        if self.tool_result is not None and self.role != "tool":
            raise ValueError("RPC tool-result metadata is valid only on tool messages")
        return self


class RpcSessionSummary(BaseModel):
    """One persisted session summary returned by the RPC session catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    session_path: Path
    updated_at: datetime
    entry_count: int = Field(ge=0)
    active_leaf_id: str | None = Field(default=None, min_length=1)
    name: str | None = None


class RpcSessionTreeNode(BaseModel):
    """One bounded node summary from a persisted session tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(min_length=1)
    parent_id: str | None = Field(default=None, min_length=1)
    operation_id: str | None = None
    created_at: datetime
    kind: Literal["message", "event", "compaction"]
    role: MessageRole | None = None
    preview: str
    preview_truncated: bool = False

    @model_validator(mode="after")
    def _validate_role(self) -> Self:
        if (self.kind == "message") != (self.role is not None):
            raise ValueError("RPC session tree message nodes must include role only for messages")
        return self


class MessageCompleted(_MessageOriginEvent):
    type: Literal["message.completed"] = "message.completed"
    turn: int
    content: str
    finish_reason: FinishReason
    role: MessageRole = "assistant"
    response_id: str | None = None
    tool_calls: tuple[ToolCallSnapshot, ...] = ()
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    context_observation: ContextObservation | None = None


class ContextPressure(WispEvent):
    """Provider-reported total usage crossed the configured warning threshold."""

    type: Literal["context.pressure"] = "context.pressure"
    turn: int
    provider: str
    model: str | None = None
    context_window: int = Field(gt=0)
    observed_tokens: int = Field(ge=0)
    remaining_tokens: int = Field(ge=0)
    pressure_ratio: float = Field(ge=0, allow_inf_nan=False)


class ContextEstimated(WispEvent):
    """Approximate context budget immediately before a provider request."""

    type: Literal["context.estimated"] = "context.estimated"
    turn: int
    provider: str
    model: str | None = None
    budget: ContextBudget


class ContextOverflow(WispEvent):
    """A provider rejected a request because its context window was exceeded."""

    type: Literal["context.overflow"] = "context.overflow"
    turn: int
    provider: str
    model: str | None = None
    context_window: int | None = Field(default=None, gt=0)
    message: str


class ToolCallRequested(WispEvent):
    type: Literal["tool.call"] = "tool.call"
    call_id: str
    name: str
    arguments: JsonObject


class ToolExecutionStarted(WispEvent):
    type: Literal["tool.execution.started"] = "tool.execution.started"
    call_id: str
    name: str
    arguments: JsonObject


class ToolApprovalRequested(WispEvent):
    type: Literal["tool.approval.requested"] = "tool.approval.requested"
    call_id: str
    name: str
    arguments: JsonObject
    safety: Literal["read", "mutating", "command"]


class ToolApprovalResolved(WispEvent):
    type: Literal["tool.approval.resolved"] = "tool.approval.resolved"
    call_id: str
    name: str
    approved: bool
    reason: str | None = None


class TrustRequested(WispEvent):
    type: Literal["trust.requested"] = "trust.requested"
    request_id: str
    project_path: Path


class TrustResolved(WispEvent):
    type: Literal["trust.resolved"] = "trust.resolved"
    request_id: str
    project_path: Path
    trusted: bool
    reason: str | None = None


class ProjectConfigApplied(WispEvent):
    """A trusted project's local config was applied mid-session.

    Emitted by the RPC process after a first-run trust approval rebuilds the runtime
    from the project's ``.wisp/settings.json``. It lets an out-of-process front-end
    (the TUI) refresh the provider/model/auth it displays and mutates, so its header
    and ``/provider`` / ``/model`` / ``/auth`` / ``/connect`` commands match the config
    the agent is actually running with. It also carries the effective automatic-
    compaction setting so frontends do not infer policy from their own startup state.

    ``effort`` carries the RPC agent's already-filtered, authoritative post-rebuild
    value (see the trusted-project rebuild in ``wisp.rpc.host``) rather than
    leaving the TUI to re-derive it from its own locally-tracked, already-once-filtered
    value -- the TUI's own copy was filtered against the untrusted-startup
    provider/model, so a tier invalid there but valid for the trusted project's
    provider/model would already be gone and unrecoverable from it. A single
    authoritative value avoids the two sides' filtering logic silently diverging.
    """

    type: Literal["project.config.applied"] = "project.config.applied"
    provider: str
    model: str | None = None
    effort: str | None = None
    auto_compaction_enabled: bool | None = None
    auth_path: Path


class _ToolResultEvent(_MessageOriginEvent):
    """Shared bounded result contract for distinct tool lifecycle events."""

    call_id: str
    name: str
    output: str
    is_error: bool
    # Typed recovery metadata for ordinary tool failures. These fields stay separate
    # from output so RPC consumers can classify failures without parsing prose.
    failure_code: ToolFailureCode | None = None
    retryable: bool = False
    recovery_hint: str | None = Field(default=None, max_length=500)
    # Process exit status for shell-like tools, promoted from ToolResult.data by
    # the executor. None for tools without exit-code semantics and error paths
    # that produced no ToolResult. This stays a narrow JSON-safe scalar rather
    # than exposing an extension-owned result mapping across the RPC boundary.
    exit_code: int | None = None
    # True only when output begins with Wisp's synthetic completion envelope.
    # Explicit provenance avoids parsing genuine legacy stdout that resembles it.
    output_has_exit_status: bool = False
    # A bounded pre-write snapshot for write-like tools. None for other tools,
    # creates, and overwrites whose previous contents could not be represented.
    before_text: str | None = None
    # Distinguishes a new file from an overwrite with no usable snapshot.
    created: bool = False
    # A bounded one-line summary for successful read-type tools.
    summary: str | None = None
    # Whether the tool itself capped output before constructing this event.
    truncated: bool = False
    # Resumable Bash metadata promoted from ToolResult.data for live JSON/RPC
    # consumers. These are bounded scalars/chunks, not the raw result mapping.
    process_id: str | None = None
    process_state: ManagedProcessState | None = None
    process_error: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_dropped_bytes: int = Field(default=0, ge=0)
    stderr_dropped_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_failure_metadata(self) -> Self:
        has_failure_metadata = (
            self.failure_code is not None or self.retryable or self.recovery_hint is not None
        )
        if has_failure_metadata and not self.is_error:
            raise ValueError("Tool failure metadata requires is_error=true")
        if (self.retryable or self.recovery_hint is not None) and self.failure_code is None:
            raise ValueError("Retry metadata requires a tool failure code")
        return self

    def _result_payload(self) -> JsonObject:
        """Return only fields declared by the shared result contract."""

        envelope_fields = WispEvent.model_fields
        return {
            name: getattr(self, name)
            for name in _ToolResultEvent.model_fields
            if name not in envelope_fields
        }


class ToolExecutionEnded(_ToolResultEvent):
    """Durable boundary reached after one tool execution finishes."""

    type: Literal["tool.execution.ended"] = "tool.execution.ended"


class ToolResultReady(_ToolResultEvent):
    """Presentation/provider projection of a completed tool execution."""

    type: Literal["tool.result"] = "tool.result"

    @classmethod
    def from_execution_ended(cls, event: ToolExecutionEnded) -> Self:
        """Project a terminal execution without duplicating its payload schema."""

        return cls.model_validate(event._result_payload())


class TurnCompleted(WispEvent):
    type: Literal["turn.completed"] = "turn.completed"
    turn: int
    outcome: RunOutcome
    finish_reason: FinishReason


class SessionSaved(WispEvent):
    type: Literal["session.saved"] = "session.saved"
    session_id: str
    path: Path


class CompactionStarted(WispEvent):
    type: Literal["compaction.started"] = "compaction.started"
    session_id: str
    reason: CompactionReason = "manual"
    source_entry_count: int = Field(ge=0)
    trigger_budget: ContextBudget | None = None

    @model_validator(mode="after")
    def _validate_trigger(self) -> CompactionStarted:
        if self.reason == "threshold":
            if self.trigger_budget is None:
                raise ValueError("threshold compaction requires a trigger budget")
        elif self.reason == "overflow":
            if self.trigger_budget is None:
                raise ValueError("overflow compaction requires a trigger budget")
        elif self.trigger_budget is not None:
            raise ValueError("manual compaction must not include a trigger budget")
        return self


class CompactionCompleted(WispEvent):
    type: Literal["compaction.completed"] = "compaction.completed"
    session_id: str
    reason: CompactionReason = "manual"
    outcome: RunOutcome
    compaction_id: str | None = None
    replaced_entry_count: int = Field(ge=0)
    retained_entry_count: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    cost: UsageCost | None = None
    error: str | None = None
    will_retry: bool = False

    @model_validator(mode="after")
    def _validate_reason(self) -> CompactionCompleted:
        if self.reason == "overflow":
            if self.outcome != "completed" and self.will_retry:
                raise ValueError("failed overflow compaction must not retry")
            if (
                self.outcome == "completed"
                and not self.will_retry
                and not (self.error or "").strip()
            ):
                raise ValueError("completed overflow compaction without retry must explain why")
        elif self.will_retry:
            raise ValueError("only overflow compaction may retry")
        return self


class AgentCompleted(WispEvent):
    type: Literal["agent.completed"] = "agent.completed"
    session_id: str
    turns: int
    outcome: RunOutcome


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


class SessionStatsReported(WispEvent):
    """On-demand, non-persisted session statistics returned over RPC."""

    type: Literal["session.stats"] = "session.stats"
    command_id: str
    stats: SessionStats


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


class RpcMessagesReported(WispEvent):
    """On-demand, bounded persisted transcript page returned over RPC."""

    type: Literal["rpc.messages"] = "rpc.messages"
    command_id: str
    session_id: str | None = None
    session_path: Path | None = None
    active_leaf_id: str | None = None
    messages: tuple[RpcMessageSnapshot, ...] = ()
    truncated: bool = False
    next_before_entry_id: str | None = None
    next_after_entry_id: str | None = None

    @model_validator(mode="after")
    def _validate_cursors(self) -> Self:
        if (
            self.next_before_entry_id is not None or self.next_after_entry_id is not None
        ) and not self.truncated:
            raise ValueError("RPC message reports cannot include a cursor unless truncated")
        if self.next_before_entry_id is not None and self.next_after_entry_id is not None:
            raise ValueError("RPC message report cursors are mutually exclusive")
        return self


class RpcSessionsReported(WispEvent):
    """On-demand, bounded persisted session catalog returned over RPC."""

    type: Literal["rpc.sessions"] = "rpc.sessions"
    command_id: str
    sessions: tuple[RpcSessionSummary, ...] = ()
    selected_session_id: str | None = Field(default=None, min_length=1)
    selected_session_path: Path | None = None
    selected_session_name: str | None = None

    @model_validator(mode="after")
    def _validate_selection(self) -> Self:
        if (self.selected_session_id is None) != (self.selected_session_path is None):
            raise ValueError(
                "RPC session reports must include selected_session_id and "
                "selected_session_path together"
            )
        return self


class RpcSessionSelected(WispEvent):
    """Confirmation that an RPC session selection has become active."""

    type: Literal["rpc.session.selected"] = "rpc.session.selected"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
    entry_count: int = Field(ge=0)
    session_name: str | None = None


class _RpcSessionDerived(WispEvent):
    """Shared source and target identity for RPC session derivation."""

    command_id: str
    source_session_id: str = Field(min_length=1)
    source_session_path: Path
    source_active_leaf_id: str | None = Field(default=None, min_length=1)
    source_session_name: str | None = None
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
    session_name: str | None = None
    entry_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_derivation(self) -> Self:
        if self.source_session_id == self.session_id:
            raise ValueError("RPC session derivation must create a new session id")
        return self


class RpcSessionCloned(_RpcSessionDerived):
    """Confirmation that the active path was cloned and selected."""

    type: Literal["rpc.session.cloned"] = "rpc.session.cloned"

    @model_validator(mode="after")
    def _validate_clone(self) -> Self:
        if self.active_leaf_id is None:
            raise ValueError("RPC cloned sessions require an active leaf")
        if self.entry_count == 0:
            raise ValueError("RPC cloned sessions require at least one entry")
        return self


class RpcSessionForked(_RpcSessionDerived):
    """Confirmation that a session was forked before a user message and selected."""

    type: Literal["rpc.session.forked"] = "rpc.session.forked"
    selected_entry_id: str = Field(min_length=1)
    selected_prompt: str


class RpcSessionNameChanged(WispEvent):
    """Confirmation that a session display name metadata record was appended."""

    type: Literal["rpc.session.name_changed"] = "rpc.session.name_changed"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    previous_name: str | None = None
    name: str | None = None
    entry_count: int = Field(ge=0)


class RpcSessionTreeReported(WispEvent):
    """Bounded append-order page of the selected persisted session tree."""

    type: Literal["rpc.session.tree"] = "rpc.session.tree"
    command_id: str
    session_id: str | None = Field(default=None, min_length=1)
    session_path: Path | None = None
    active_leaf_id: str | None = Field(default=None, min_length=1)
    total_node_count: int = Field(ge=0)
    nodes: tuple[RpcSessionTreeNode, ...] = ()
    truncated: bool = False
    next_after_entry_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_tree_report(self) -> Self:
        if (self.session_id is None) != (self.session_path is None):
            raise ValueError(
                "RPC session tree reports must include session_id and session_path together"
            )
        if len(self.nodes) > self.total_node_count:
            raise ValueError("RPC session tree page cannot exceed total_node_count")
        if (self.next_after_entry_id is not None) != self.truncated:
            raise ValueError(
                "RPC session tree reports require next_after_entry_id exactly when truncated"
            )
        if self.truncated and (
            not self.nodes or self.next_after_entry_id != self.nodes[-1].entry_id
        ):
            raise ValueError(
                "RPC session tree next_after_entry_id must identify the final returned node"
            )
        if len({node.entry_id for node in self.nodes}) != len(self.nodes):
            raise ValueError("RPC session tree pages cannot contain duplicate entry ids")
        if self.session_id is None and (
            self.active_leaf_id is not None or self.total_node_count or self.nodes or self.truncated
        ):
            raise ValueError("RPC session tree reports without a session must be empty")
        return self


class RpcSessionTreeNavigated(WispEvent):
    """Confirmation that a selected session's active path changed."""

    type: Literal["rpc.session.tree.navigated"] = "rpc.session.tree.navigated"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    selected_entry_id: str = Field(min_length=1)
    previous_active_leaf_id: str | None = Field(default=None, min_length=1)
    active_leaf_id: str | None = Field(default=None, min_length=1)
    editor_text: str | None = None
    changed: bool
    entry_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_transition(self) -> Self:
        if self.changed == (self.previous_active_leaf_id == self.active_leaf_id):
            raise ValueError(
                "RPC session tree navigation changed must match the active-leaf transition"
            )
        return self


class RpcSessionTreeUnreverted(WispEvent):
    """Confirmation that the latest explicit tree navigation was reversed."""

    type: Literal["rpc.session.tree.unreverted"] = "rpc.session.tree.unreverted"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    source_transition_id: str = Field(min_length=1)
    previous_active_leaf_id: str | None = Field(default=None, min_length=1)
    active_leaf_id: str | None = Field(default=None, min_length=1)
    entry_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_unrevert(self) -> Self:
        if self.previous_active_leaf_id == self.active_leaf_id:
            raise ValueError("RPC session tree unrevert must change the active leaf")
        return self


class QueueUpdated(WispEvent):
    """Current harness-owned steering and follow-up queue state."""

    type: Literal["queue.updated"] = "queue.updated"
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()
    steering_mode: QueueMode = "one_at_a_time"
    follow_up_mode: QueueMode = "one_at_a_time"


class QueueItemsRemoved(WispEvent):
    """Queued text removed by an RPC pop or clear operation."""

    type: Literal["queue.items.removed"] = "queue.items.removed"
    command_id: str
    operation: Literal["pop", "clear"]
    kind: QueueKind | None = None
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_removal(self) -> Self:
        if self.operation == "pop" and self.kind is None:
            raise ValueError("queue pop results require a queue kind")
        if self.kind == "steering" and self.follow_up:
            raise ValueError("steering queue removal results cannot contain follow-up items")
        if self.kind == "follow_up" and self.steering:
            raise ValueError("follow-up queue removal results cannot contain steering items")
        if self.operation == "pop" and len(self.steering) + len(self.follow_up) > 1:
            raise ValueError("queue pop results can contain at most one removed item")
        return self


class QueueMessageInjected(_MessageOriginEvent):
    """A queued user message crossed into the active transcript."""

    type: Literal["queue.message.injected"] = "queue.message.injected"
    kind: QueueKind
    content: str
    skill_invocation: SkillInvocationEvidence | None = None


class SkillInvoked(WispEvent):
    """An explicit skill directive became one provider-visible user message."""

    type: Literal["skill.invoked"] = "skill.invoked"
    session_id: str
    message_entry_id: str
    invocation: SkillInvocationEvidence
    provider_content: str
    queue_kind: QueueKind | None = None


class ModelProviderAutoSwitched(WispEvent):
    """A model-only ``configure`` command's resolved model belonged to another provider.

    Emitted by the RPC process after a successful model-registry-backed
    auto-switch for a model-only ``/model <id>`` request. Without this, an out-of-process
    front-end (the TUI) that only tracks provider changes it explicitly
    requested would keep displaying and using its old provider while the RPC
    agent has actually moved to a different one.
    """

    type: Literal["model.provider_auto_switched"] = "model.provider_auto_switched"
    command_id: str
    provider: str
    model: str


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str


type KnownWispEvent = Annotated[
    AgentStarted
    | TurnStarted
    | ProviderRetrying
    | MessageStarted
    | MessageDelta
    | MessageCompleted
    | ContextEstimated
    | ContextPressure
    | ContextOverflow
    | ToolCallRequested
    | ToolExecutionStarted
    | ToolApprovalRequested
    | ToolApprovalResolved
    | TrustRequested
    | TrustResolved
    | ProjectConfigApplied
    | ToolExecutionEnded
    | ToolResultReady
    | TurnCompleted
    | SessionSaved
    | CompactionStarted
    | CompactionCompleted
    | AgentCompleted
    | RpcCommandStarted
    | RpcCommandFinished
    | SessionStatsReported
    | RpcStateReported
    | RpcPermissionsReported
    | RpcCommandsReported
    | RpcModelCatalogReported
    | RpcConnectionCatalogReported
    | RpcDeviceCodeReported
    | RpcDeviceCodeProgressReported
    | RpcSkillsReported
    | RpcProjectFilesReported
    | ProjectFilesInvalidated
    | RpcMcpStatusReported
    | SkillCatalogUpdated
    | RpcMessagesReported
    | RpcSessionsReported
    | RpcSessionSelected
    | RpcSessionCloned
    | RpcSessionForked
    | RpcSessionNameChanged
    | RpcSessionTreeReported
    | RpcSessionTreeNavigated
    | RpcSessionTreeUnreverted
    | QueueUpdated
    | QueueItemsRemoved
    | QueueMessageInjected
    | SkillInvoked
    | ModelProviderAutoSwitched
    | ErrorEvent,
    Field(discriminator="type"),
]
KnownWispEventAdapter: TypeAdapter[KnownWispEvent] = TypeAdapter(KnownWispEvent)
JsonObjectAdapter: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one JSONL event line into a typed Wisp event."""

    return KnownWispEventAdapter.validate_json(line)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one event dictionary into a typed Wisp event."""

    return KnownWispEventAdapter.validate_python(data)
