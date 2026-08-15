"""Versioned events emitted by the Wisp agent core."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from wisp.agent.mode import AgentMode
from wisp.skills.models import (
    SkillDiagnosticCode,
    SkillDiagnosticSeverity,
    SkillInvocationEvidence,
    SkillSource,
)
from wisp.tool_types import ToolFailureCode

EVENT_SCHEMA_VERSION: Literal[33] = 33
THRESHOLD_COMPACTION_SCHEMA_VERSION = 10
OVERFLOW_COMPACTION_SCHEMA_VERSION = 11
COST_ACCOUNTING_SCHEMA_VERSION = 12
QUEUE_UPDATE_SCHEMA_VERSION = 13
QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION = 14
QUEUE_ITEMS_REMOVED_SCHEMA_VERSION = 15
RPC_STATE_SCHEMA_VERSION = 16
RPC_MESSAGES_SCHEMA_VERSION = 17
RPC_SESSIONS_SCHEMA_VERSION = 18
RPC_SESSION_DERIVATION_SCHEMA_VERSION = 19
RPC_SESSION_TREE_SCHEMA_VERSION = 20
RPC_SESSION_NAME_SCHEMA_VERSION = 21
RPC_MESSAGE_TOOL_RESULT_SCHEMA_VERSION = 22
RPC_COMMANDS_SCHEMA_VERSION = 23
RPC_SESSION_UNREVERT_SCHEMA_VERSION = 24
PROCESS_METADATA_SCHEMA_VERSION = 25
COMPACTION_POLICY_SCHEMA_VERSION = 26
AGENT_MODE_SCHEMA_VERSION = 27
SKILL_INVOCATION_SCHEMA_VERSION = 28
SKILL_CATALOG_SCHEMA_VERSION = 29
MCP_STATUS_SCHEMA_VERSION = 30
PACKAGE_SKILLS_SCHEMA_VERSION = 31
CONTEXT_ACCOUNTING_SCHEMA_VERSION = 32
TOOL_FAILURE_METADATA_SCHEMA_VERSION = 33
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
_PROCESS_METADATA_EVENT_TYPES = frozenset({"tool.result", "tool.execution.ended"})
_TOOL_RESULT_EVENT_TYPES = frozenset({"tool.result", "tool.execution.ended"})
_TOOL_FAILURE_METADATA_FIELDS = frozenset({"failure_code", "retryable", "recovery_hint"})
_PROCESS_METADATA_FIELDS = frozenset(
    {
        "process_id",
        "process_state",
        "process_error",
        "stdout",
        "stderr",
        "stdout_truncated",
        "stderr_truncated",
        "stdout_dropped_bytes",
        "stderr_dropped_bytes",
    }
)


def _strip_context_accounting_fields(value: object) -> None:
    if not isinstance(value, dict):
        return
    value.pop("trailing_estimated_tokens", None)
    value.pop("effective_tokens", None)
    value.pop("accounting_method", None)


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for versioned events consumed by every Wisp frontend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    schema_version: Literal[
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
    ] = EVENT_SCHEMA_VERSION
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _require_integer_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Wisp event schema_version must be an integer")
        return value


class ToolCallSnapshot(BaseModel):
    """Serializable tool-call state attached to a completed assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    parse_error: str | None = None


class AgentStarted(WispEvent):
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
    delay_seconds: float
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
    estimated_percent: float | None = Field(default=None, ge=0)
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


class MessageCompleted(WispEvent):
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

    @model_validator(mode="after")
    def _validate_cost_schema(self) -> Self:
        if self.cost is not None and self.schema_version < COST_ACCOUNTING_SCHEMA_VERSION:
            raise ValueError(
                f"usage cost requires schema_version {COST_ACCOUNTING_SCHEMA_VERSION} or newer"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < COST_ACCOUNTING_SCHEMA_VERSION:
            data.pop("cost", None)
        if self.schema_version < CONTEXT_ACCOUNTING_SCHEMA_VERSION:
            data.pop("context_observation", None)
        return data


class ContextPressure(WispEvent):
    """Provider-reported total usage crossed the configured warning threshold."""

    type: Literal["context.pressure"] = "context.pressure"
    turn: int
    provider: str
    model: str | None = None
    context_window: int = Field(gt=0)
    observed_tokens: int = Field(ge=0)
    remaining_tokens: int = Field(ge=0)
    pressure_ratio: float = Field(ge=0)


class ContextEstimated(WispEvent):
    """Approximate context budget immediately before a provider request."""

    type: Literal["context.estimated"] = "context.estimated"
    turn: int
    provider: str
    model: str | None = None
    budget: ContextBudget

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < CONTEXT_ACCOUNTING_SCHEMA_VERSION:
            _strip_context_accounting_fields(data.get("budget"))
        return data


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
    value (see ``_rebuild_agent_for_trusted_project`` in ``wisp.cli.rpc``) rather than
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

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < COMPACTION_POLICY_SCHEMA_VERSION:
            data.pop("auto_compaction_enabled", None)
        return data


class _ToolResultEvent(WispEvent):
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

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < PROCESS_METADATA_SCHEMA_VERSION:
            _strip_process_metadata_fields(data)
        if self.schema_version < TOOL_FAILURE_METADATA_SCHEMA_VERSION:
            for field in _TOOL_FAILURE_METADATA_FIELDS:
                data.pop(field, None)
        return data


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
            if self.schema_version < THRESHOLD_COMPACTION_SCHEMA_VERSION:
                raise ValueError(
                    "threshold compaction requires schema_version "
                    f"{THRESHOLD_COMPACTION_SCHEMA_VERSION} or newer"
                )
            if self.trigger_budget is None:
                raise ValueError("threshold compaction requires a trigger budget")
        elif self.reason == "overflow":
            if self.schema_version < OVERFLOW_COMPACTION_SCHEMA_VERSION:
                raise ValueError(
                    "overflow compaction requires schema_version "
                    f"{OVERFLOW_COMPACTION_SCHEMA_VERSION} or newer"
                )
            if self.trigger_budget is None:
                raise ValueError("overflow compaction requires a trigger budget")
        elif self.trigger_budget is not None:
            raise ValueError("manual compaction must not include a trigger budget")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version in {8, 9}:
            data.pop("trigger_budget", None)
        if self.schema_version < CONTEXT_ACCOUNTING_SCHEMA_VERSION:
            _strip_context_accounting_fields(data.get("trigger_budget"))
        return data


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
        if self.reason == "threshold" and self.schema_version < THRESHOLD_COMPACTION_SCHEMA_VERSION:
            raise ValueError(
                "threshold compaction requires schema_version "
                f"{THRESHOLD_COMPACTION_SCHEMA_VERSION} or newer"
            )
        if self.reason == "overflow":
            if self.schema_version < OVERFLOW_COMPACTION_SCHEMA_VERSION:
                raise ValueError(
                    "overflow compaction requires schema_version "
                    f"{OVERFLOW_COMPACTION_SCHEMA_VERSION} or newer"
                )
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
        if self.cost is not None and self.schema_version < COST_ACCOUNTING_SCHEMA_VERSION:
            raise ValueError(
                f"usage cost requires schema_version {COST_ACCOUNTING_SCHEMA_VERSION} or newer"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < OVERFLOW_COMPACTION_SCHEMA_VERSION:
            data.pop("will_retry", None)
        if self.schema_version < COST_ACCOUNTING_SCHEMA_VERSION:
            data.pop("cost", None)
        return data


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

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < COST_ACCOUNTING_SCHEMA_VERSION:
            stats = cast(dict[str, object], data["stats"])
            stats.pop("cost", None)
        if self.schema_version < COMPACTION_POLICY_SCHEMA_VERSION:
            stats = cast(dict[str, object], data["stats"])
            stats.pop("compaction", None)
        if self.schema_version < CONTEXT_ACCOUNTING_SCHEMA_VERSION:
            stats = cast(dict[str, object], data["stats"])
            _strip_context_accounting_fields(stats.get("context"))
        return data


class RpcStateReported(WispEvent):
    """Immediate, non-persisted in-memory state returned over RPC."""

    type: Literal["rpc.state"] = "rpc.state"
    command_id: str
    state: RpcStateSnapshot

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"RPC state reports require schema_version {RPC_STATE_SCHEMA_VERSION} or newer"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        state = cast(dict[str, object], data["state"])
        if self.schema_version < RPC_SESSION_NAME_SCHEMA_VERSION:
            state.pop("session_name", None)
        if self.schema_version < AGENT_MODE_SCHEMA_VERSION:
            state.pop("mode", None)
        return data


class RpcCommandsReported(WispEvent):
    """Immediate, non-persisted command registry snapshot returned over RPC."""

    type: Literal["rpc.commands"] = "rpc.commands"
    command_id: str
    commands: tuple[RpcCommandDescriptor, ...] = ()

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_COMMANDS_SCHEMA_VERSION:
            raise ValueError(
                f"RPC command reports require schema_version {RPC_COMMANDS_SCHEMA_VERSION} or newer"
            )
        return self


def _validate_package_skill_schema(
    catalog: RpcSkillCatalogSnapshot,
    *,
    schema_version: int,
) -> None:
    if schema_version >= PACKAGE_SKILLS_SCHEMA_VERSION:
        return
    if any(entry.source == "package:wisp" for entry in catalog.entries) or any(
        diagnostic.source == "package:wisp" for diagnostic in catalog.diagnostics
    ):
        raise ValueError(
            f"Package skill sources require schema_version {PACKAGE_SKILLS_SCHEMA_VERSION} or newer"
        )


class RpcSkillsReported(WispEvent):
    """Immediate, non-persisted skill catalog snapshot returned over RPC."""

    type: Literal["rpc.skills"] = "rpc.skills"
    command_id: str
    catalog: RpcSkillCatalogSnapshot

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < SKILL_CATALOG_SCHEMA_VERSION:
            raise ValueError(
                f"RPC skill reports require schema_version {SKILL_CATALOG_SCHEMA_VERSION} or newer"
            )
        _validate_package_skill_schema(self.catalog, schema_version=self.schema_version)
        return self


class RpcMcpStatusReported(WispEvent):
    """Immediate, non-persisted MCP runtime status returned over RPC."""

    type: Literal["rpc.mcp"] = "rpc.mcp"
    command_id: str
    status: RpcMcpStatusSnapshot

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < MCP_STATUS_SCHEMA_VERSION:
            raise ValueError(
                "RPC MCP status reports require schema_version "
                f"{MCP_STATUS_SCHEMA_VERSION} or newer"
            )
        return self


class SkillCatalogUpdated(WispEvent):
    """A trust transition replaced the catalog available to future operations."""

    type: Literal["skill.catalog.updated"] = "skill.catalog.updated"
    catalog: RpcSkillCatalogSnapshot

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < SKILL_CATALOG_SCHEMA_VERSION:
            raise ValueError(
                "skill catalog updates require schema_version "
                f"{SKILL_CATALOG_SCHEMA_VERSION} or newer"
            )
        _validate_package_skill_schema(self.catalog, schema_version=self.schema_version)
        return self


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

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_MESSAGES_SCHEMA_VERSION:
            raise ValueError(
                f"RPC message reports require schema_version {RPC_MESSAGES_SCHEMA_VERSION} or newer"
            )
        if self.schema_version < RPC_MESSAGE_TOOL_RESULT_SCHEMA_VERSION and any(
            message.tool_result is not None for message in self.messages
        ):
            raise ValueError(
                "RPC message tool-result metadata requires schema_version "
                f"{RPC_MESSAGE_TOOL_RESULT_SCHEMA_VERSION} or newer"
            )
        if self.schema_version < SKILL_INVOCATION_SCHEMA_VERSION and any(
            message.skill_invocation is not None for message in self.messages
        ):
            raise ValueError(
                "RPC message skill-invocation metadata requires schema_version "
                f"{SKILL_INVOCATION_SCHEMA_VERSION} or newer"
            )
        if self.next_before_entry_id is not None and not self.truncated:
            raise ValueError(
                "RPC message reports cannot include next_before_entry_id unless truncated"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < RPC_MESSAGE_TOOL_RESULT_SCHEMA_VERSION:
            messages = data.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        message.pop("tool_result", None)
        return data


class RpcSessionsReported(WispEvent):
    """On-demand, bounded persisted session catalog returned over RPC."""

    type: Literal["rpc.sessions"] = "rpc.sessions"
    command_id: str
    sessions: tuple[RpcSessionSummary, ...] = ()
    selected_session_id: str | None = Field(default=None, min_length=1)
    selected_session_path: Path | None = None
    selected_session_name: str | None = None

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_SESSIONS_SCHEMA_VERSION:
            raise ValueError(
                f"RPC session reports require schema_version {RPC_SESSIONS_SCHEMA_VERSION} or newer"
            )
        if (self.selected_session_id is None) != (self.selected_session_path is None):
            raise ValueError(
                "RPC session reports must include selected_session_id and "
                "selected_session_path together"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < RPC_SESSION_NAME_SCHEMA_VERSION:
            data.pop("selected_session_name", None)
            sessions = data.get("sessions")
            if isinstance(sessions, list):
                for summary in sessions:
                    if isinstance(summary, dict):
                        summary.pop("name", None)
        return data


class RpcSessionSelected(WispEvent):
    """Confirmation that an RPC session selection has become active."""

    type: Literal["rpc.session.selected"] = "rpc.session.selected"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
    entry_count: int = Field(ge=0)
    session_name: str | None = None

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_SESSIONS_SCHEMA_VERSION:
            raise ValueError(
                f"RPC session selection events require schema_version "
                f"{RPC_SESSIONS_SCHEMA_VERSION} or newer"
            )
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < RPC_SESSION_NAME_SCHEMA_VERSION:
            data.pop("session_name", None)
        return data


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
        if self.schema_version < RPC_SESSION_DERIVATION_SCHEMA_VERSION:
            raise ValueError(
                "RPC session derivation events require schema_version "
                f"{RPC_SESSION_DERIVATION_SCHEMA_VERSION} or newer"
            )
        if self.source_session_id == self.session_id:
            raise ValueError("RPC session derivation must create a new session id")
        return self

    @model_serializer(mode="wrap")
    def _serialize_versioned(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.schema_version < RPC_SESSION_NAME_SCHEMA_VERSION:
            data.pop("source_session_name", None)
            data.pop("session_name", None)
        return data


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

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_SESSION_NAME_SCHEMA_VERSION:
            raise ValueError(
                "RPC session name events require schema_version "
                f"{RPC_SESSION_NAME_SCHEMA_VERSION} or newer"
            )
        return self


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
        if self.schema_version < RPC_SESSION_TREE_SCHEMA_VERSION:
            raise ValueError(
                "RPC session tree events require schema_version "
                f"{RPC_SESSION_TREE_SCHEMA_VERSION} or newer"
            )
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
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_SESSION_TREE_SCHEMA_VERSION:
            raise ValueError(
                "RPC session tree navigation events require schema_version "
                f"{RPC_SESSION_TREE_SCHEMA_VERSION} or newer"
            )
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
        if self.schema_version < RPC_SESSION_UNREVERT_SCHEMA_VERSION:
            raise ValueError(
                "RPC session tree unrevert events require schema_version "
                f"{RPC_SESSION_UNREVERT_SCHEMA_VERSION} or newer"
            )
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

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < QUEUE_UPDATE_SCHEMA_VERSION:
            raise ValueError(
                f"queue updates require schema_version {QUEUE_UPDATE_SCHEMA_VERSION} or newer"
            )
        return self


class QueueItemsRemoved(WispEvent):
    """Queued text removed by an RPC pop or clear operation."""

    type: Literal["queue.items.removed"] = "queue.items.removed"
    command_id: str
    operation: Literal["pop", "clear"]
    kind: QueueKind | None = None
    steering: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < QUEUE_ITEMS_REMOVED_SCHEMA_VERSION:
            raise ValueError(
                "queue removal results require schema_version "
                f"{QUEUE_ITEMS_REMOVED_SCHEMA_VERSION} or newer"
            )
        if self.operation == "pop" and self.kind is None:
            raise ValueError("queue pop results require a queue kind")
        if self.kind == "steering" and self.follow_up:
            raise ValueError("steering queue removal results cannot contain follow-up items")
        if self.kind == "follow_up" and self.steering:
            raise ValueError("follow-up queue removal results cannot contain steering items")
        if self.operation == "pop" and len(self.steering) + len(self.follow_up) > 1:
            raise ValueError("queue pop results can contain at most one removed item")
        return self


class QueueMessageInjected(WispEvent):
    """A queued user message crossed into the active transcript."""

    type: Literal["queue.message.injected"] = "queue.message.injected"
    kind: QueueKind
    content: str
    skill_invocation: SkillInvocationEvidence | None = None

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION:
            raise ValueError(
                "queue message injection requires schema_version "
                f"{QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION} or newer"
            )
        if (
            self.skill_invocation is not None
            and self.schema_version < SKILL_INVOCATION_SCHEMA_VERSION
        ):
            raise ValueError(
                "queue skill-invocation metadata requires schema_version "
                f"{SKILL_INVOCATION_SCHEMA_VERSION} or newer"
            )
        return self


class SkillInvoked(WispEvent):
    """An explicit skill directive became one provider-visible user message."""

    type: Literal["skill.invoked"] = "skill.invoked"
    session_id: str
    message_entry_id: str
    invocation: SkillInvocationEvidence
    provider_content: str
    queue_kind: QueueKind | None = None

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < SKILL_INVOCATION_SCHEMA_VERSION:
            raise ValueError(
                "skill invocations require schema_version "
                f"{SKILL_INVOCATION_SCHEMA_VERSION} or newer"
            )
        return self


class ModelProviderAutoSwitched(WispEvent):
    """A model-only ``configure`` command's resolved model belonged to another provider.

    Emitted by the RPC process immediately before the ``configure`` command's
    ``RpcCommandFinished`` when the model-registry-backed auto-switch in
    ``_handle_rpc_configure_command`` changes ``agent.provider`` as a side effect
    of a model-only ``/model <id>`` request. Without this, an out-of-process
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
    | RpcCommandsReported
    | RpcSkillsReported
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


def _require_current_schema(data: JsonObject) -> None:
    version = data.get("schema_version")
    if type(version) is not int or not 5 <= version <= EVENT_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported Wisp event schema_version: "
            f"{version!r}; expected 5 through {EVENT_SCHEMA_VERSION}"
        )
    _reject_legacy_session_name_fields(data, version=version)
    _reject_legacy_process_metadata(data, version=version)
    _reject_legacy_tool_failure_metadata(data, version=version)
    if data.get("type") in {"compaction.started", "compaction.completed"}:
        if not 8 <= version <= EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"Compaction events require schema_version 8 through {EVENT_SCHEMA_VERSION}, "
                f"got {version!r}"
            )
        if data.get("reason", "manual") == "threshold" and (
            not isinstance(version, int) or version < THRESHOLD_COMPACTION_SCHEMA_VERSION
        ):
            raise ValueError(
                "Threshold compaction events require schema_version "
                f"{THRESHOLD_COMPACTION_SCHEMA_VERSION} or newer, "
                f"got {version!r}"
            )
        if data.get("reason", "manual") == "overflow" and (
            not isinstance(version, int) or version < OVERFLOW_COMPACTION_SCHEMA_VERSION
        ):
            raise ValueError(
                "Overflow compaction events require schema_version "
                f"{OVERFLOW_COMPACTION_SCHEMA_VERSION} or newer, got {version!r}"
            )
        if version in {8, 9} and data.get("trigger_budget") is not None:
            raise ValueError(
                "Compaction trigger budgets require schema_version "
                f"{THRESHOLD_COMPACTION_SCHEMA_VERSION} or newer"
            )
        if "will_retry" in data and (
            not isinstance(version, int) or version < OVERFLOW_COMPACTION_SCHEMA_VERSION
        ):
            raise ValueError(
                "Compaction retry metadata requires schema_version "
                f"{OVERFLOW_COMPACTION_SCHEMA_VERSION} or newer"
            )
        if "cost" in data and version < COST_ACCOUNTING_SCHEMA_VERSION:
            raise ValueError(
                "Compaction cost metadata requires schema_version "
                f"{COST_ACCOUNTING_SCHEMA_VERSION} or newer"
            )
    if (
        data.get("type") == "message.completed"
        and "cost" in data
        and (version < COST_ACCOUNTING_SCHEMA_VERSION)
    ):
        raise ValueError(
            "Message cost metadata requires schema_version "
            f"{COST_ACCOUNTING_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "session.stats":
        stats = data.get("stats")
        if isinstance(stats, dict) and "cost" in stats and version < COST_ACCOUNTING_SCHEMA_VERSION:
            raise ValueError(
                "Session cost metadata requires schema_version "
                f"{COST_ACCOUNTING_SCHEMA_VERSION} or newer"
            )
        if (
            isinstance(stats, dict)
            and "compaction" in stats
            and version < COMPACTION_POLICY_SCHEMA_VERSION
        ):
            raise ValueError(
                "Session compaction policy requires schema_version "
                f"{COMPACTION_POLICY_SCHEMA_VERSION} or newer"
            )
    if (
        data.get("type") == "project.config.applied"
        and "auto_compaction_enabled" in data
        and version < COMPACTION_POLICY_SCHEMA_VERSION
    ):
        raise ValueError(
            "Project compaction policy requires schema_version "
            f"{COMPACTION_POLICY_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "queue.updated" and version < QUEUE_UPDATE_SCHEMA_VERSION:
        raise ValueError(
            f"Queue update events require schema_version {QUEUE_UPDATE_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.state" and version < RPC_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"RPC state events require schema_version {RPC_STATE_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.commands" and version < RPC_COMMANDS_SCHEMA_VERSION:
        raise ValueError(
            "RPC command report events require schema_version "
            f"{RPC_COMMANDS_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.mcp" and version < MCP_STATUS_SCHEMA_VERSION:
        raise ValueError(
            f"RPC MCP status events require schema_version {MCP_STATUS_SCHEMA_VERSION} or newer"
        )
    if data.get("type") in {"rpc.skills", "skill.catalog.updated"} and (
        version < PACKAGE_SKILLS_SCHEMA_VERSION
    ):
        catalog = data.get("catalog")
        if isinstance(catalog, dict):
            entries = catalog.get("entries")
            diagnostics = catalog.get("diagnostics")
            descriptors = (
                *(entries if isinstance(entries, list) else ()),
                *(diagnostics if isinstance(diagnostics, list) else ()),
            )
            if any(
                isinstance(descriptor, dict) and descriptor.get("source") == "package:wisp"
                for descriptor in descriptors
            ):
                raise ValueError(
                    "Package skill sources require schema_version "
                    f"{PACKAGE_SKILLS_SCHEMA_VERSION} or newer"
                )
    if data.get("type") == "rpc.messages" and version < RPC_MESSAGES_SCHEMA_VERSION:
        raise ValueError(
            "RPC message report events require schema_version "
            f"{RPC_MESSAGES_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.messages" and version < RPC_MESSAGE_TOOL_RESULT_SCHEMA_VERSION:
        messages = data.get("messages")
        if isinstance(messages, list) and any(
            isinstance(message, dict) and message.get("tool_result") is not None
            for message in messages
        ):
            raise ValueError(
                "RPC message tool-result metadata requires schema_version "
                f"{RPC_MESSAGE_TOOL_RESULT_SCHEMA_VERSION} or newer"
            )
    if data.get("type") in {"rpc.sessions", "rpc.session.selected"} and (
        version < RPC_SESSIONS_SCHEMA_VERSION
    ):
        raise ValueError(
            f"RPC session events require schema_version {RPC_SESSIONS_SCHEMA_VERSION} or newer"
        )
    if data.get("type") in {"rpc.session.cloned", "rpc.session.forked"} and (
        version < RPC_SESSION_DERIVATION_SCHEMA_VERSION
    ):
        raise ValueError(
            "RPC session derivation events require schema_version "
            f"{RPC_SESSION_DERIVATION_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.session.name_changed" and (
        version < RPC_SESSION_NAME_SCHEMA_VERSION
    ):
        raise ValueError(
            "RPC session name events require schema_version "
            f"{RPC_SESSION_NAME_SCHEMA_VERSION} or newer"
        )
    if data.get("type") in {"rpc.session.tree", "rpc.session.tree.navigated"} and (
        version < RPC_SESSION_TREE_SCHEMA_VERSION
    ):
        raise ValueError(
            "RPC session tree events require schema_version "
            f"{RPC_SESSION_TREE_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "queue.items.removed" and version < QUEUE_ITEMS_REMOVED_SCHEMA_VERSION:
        raise ValueError(
            "Queue removal result events require schema_version "
            f"{QUEUE_ITEMS_REMOVED_SCHEMA_VERSION} or newer"
        )
    if (
        data.get("type") == "queue.message.injected"
        and version < QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION
    ):
        raise ValueError(
            "Queue message injection events require schema_version "
            f"{QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION} or newer"
        )
    if data.get("type") in {"context.estimated", "session.stats"} and not (
        9 <= version <= EVENT_SCHEMA_VERSION
    ):
        raise ValueError(
            f"Context statistics events require schema_version 9 through {EVENT_SCHEMA_VERSION}, "
            f"got {version!r}"
        )


def _reject_legacy_session_name_fields(data: JsonObject, *, version: int) -> None:
    if version >= RPC_SESSION_NAME_SCHEMA_VERSION:
        return
    event_type = data.get("type")
    has_name_field = False
    if event_type == "rpc.state":
        state = data.get("state")
        has_name_field = isinstance(state, dict) and "session_name" in state
    elif event_type == "rpc.sessions":
        has_name_field = "selected_session_name" in data
        sessions = data.get("sessions")
        if isinstance(sessions, list):
            has_name_field = has_name_field or any(
                isinstance(summary, dict) and "name" in summary for summary in sessions
            )
    elif event_type == "rpc.session.selected":
        has_name_field = "session_name" in data
    elif event_type in {"rpc.session.cloned", "rpc.session.forked"}:
        has_name_field = "source_session_name" in data or "session_name" in data
    if has_name_field:
        raise ValueError(
            "RPC session name fields require schema_version "
            f"{RPC_SESSION_NAME_SCHEMA_VERSION} or newer"
        )


def _reject_legacy_tool_failure_metadata(data: JsonObject, *, version: int) -> None:
    if version >= TOOL_FAILURE_METADATA_SCHEMA_VERSION:
        return
    if data.get("type") not in _TOOL_RESULT_EVENT_TYPES:
        return
    if not any(field in data for field in _TOOL_FAILURE_METADATA_FIELDS):
        return
    raise ValueError(
        "Tool failure metadata requires schema_version "
        f"{TOOL_FAILURE_METADATA_SCHEMA_VERSION} or newer"
    )


def _reject_legacy_process_metadata(data: JsonObject, *, version: int) -> None:
    if version >= PROCESS_METADATA_SCHEMA_VERSION:
        return
    if data.get("type") not in _PROCESS_METADATA_EVENT_TYPES:
        return
    if not any(field in data for field in _PROCESS_METADATA_FIELDS):
        return
    raise ValueError(
        f"Bash process metadata requires schema_version {PROCESS_METADATA_SCHEMA_VERSION} or newer"
    )


def _strip_process_metadata_fields(data: dict[str, object]) -> None:
    for field in _PROCESS_METADATA_FIELDS:
        data.pop(field, None)


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one supported-schema JSONL event line into a typed Wisp event."""

    data = JsonObjectAdapter.validate_json(line)
    _require_current_schema(data)
    return KnownWispEventAdapter.validate_python(data)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one supported-schema event dictionary into a typed Wisp event."""

    _require_current_schema(data)
    return KnownWispEventAdapter.validate_python(data)
