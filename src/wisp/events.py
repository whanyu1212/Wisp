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

EVENT_SCHEMA_VERSION = 20
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
JsonObject = dict[str, object]
MessageRole = Literal["system", "user", "assistant", "tool"]
RunOutcome = Literal["completed", "failed", "cancelled"]
FinishReason = Literal["stop", "tool_calls", "length", "error", "cancelled"]
RetryReason = Literal["network", "timeout", "rate_limit", "server_error", "transient_http"]
CompactionReason = Literal["manual", "threshold", "overflow"]
QueueMode = Literal["one_at_a_time", "all"]
QueueKind = Literal["steering", "follow_up"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class WispEvent(BaseModel):
    """Base class for versioned events consumed by every Wisp frontend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    schema_version: Literal[5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20] = 20
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

    method: Literal["chars_div_4_v1"] = "chars_div_4_v1"
    system_tokens: int = Field(ge=0)
    message_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ContextBudget(BaseModel):
    """Current estimate, latest observation, and model-window budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    estimate: ContextEstimate
    observed_tokens: int | None = Field(default=None, ge=0)
    observed_is_current: bool = False
    context_window: int | None = Field(default=None, gt=0)
    reserve_tokens: int = Field(ge=0)
    remaining_tokens: int | None = None
    estimated_percent: float | None = Field(default=None, ge=0)
    over_budget: bool | None = None


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
    active_command_id: str | None = None
    active_command_type: str | None = None
    cancel_requested: bool = False


class RpcMessageToolCallSnapshot(BaseModel):
    """Bounded tool-call state retained on an RPC transcript message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments: JsonObject
    arguments_original_bytes: int = Field(ge=0)
    arguments_truncated: bool = False
    parse_error: str | None = None


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


class RpcSessionSummary(BaseModel):
    """One persisted session summary returned by the RPC session catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    session_path: Path
    updated_at: datetime
    entry_count: int = Field(ge=0)
    active_leaf_id: str | None = Field(default=None, min_length=1)


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
    and ``/provider`` / ``/model`` / ``/auth`` / ``/login`` commands match the config
    the agent is actually running with.

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
    auth_path: Path


class ToolExecutionEnded(WispEvent):
    type: Literal["tool.execution.ended"] = "tool.execution.ended"
    call_id: str
    name: str
    output: str
    is_error: bool
    # Process exit status for shell-like tools, promoted from ToolResult.data by
    # the executor (which knows the tool and holds the structured result). None
    # for tools without exit-code semantics and for error paths that produced no
    # ToolResult. See ToolResultReady.exit_code for why this is a narrow scalar
    # rather than the whole data mapping.
    exit_code: int | None = None
    # Pre-write file snapshot for the diff renderer, promoted from ToolResult.data
    # for write-like tools only. None for every other tool and for error paths.
    # See ToolResultReady.before_text for the wire/bounding rationale.
    before_text: str | None = None
    # Whether a write created a new file. Disambiguates before_text=None: a create
    # renders as pure additions, an overwrite with no usable snapshot falls back to
    # the summary. False for every non-write tool. See ToolResultReady.created.
    created: bool = False
    # One-line success summary for read-type tools (read/grep/find/ls), built from
    # the tool's structured data. None for tools without one. See
    # ToolResultReady.summary.
    summary: str | None = None
    # Whether the tool capped its own output (past its max_output bytes/lines). The
    # dropped content never leaves the tool, so this bool is the only signal that an
    # expanded card is still not the whole story. See ToolResultReady.truncated.
    truncated: bool = False


class ToolResultReady(WispEvent):
    type: Literal["tool.result"] = "tool.result"
    call_id: str
    name: str
    output: str
    is_error: bool
    # The one structured fact tool-aware rendering needs today: a shell command's
    # exit status. Deliberately a bounded, JSON-safe scalar rather than the raw
    # ToolResult.data mapping — the renderer runs in the TUI process and only sees
    # events *after* they cross the RPC wire (agent subprocess → JSON → client),
    # so the signal must serialize; but shipping the whole mapping would re-emit
    # unbounded tool data (e.g. an `ls` entry list) past ToolContext's bounds and
    # risk non-JSON payloads. This field crosses the only serialized consumer of
    # these events — the same-version RPC transport (sessions store Messages, not
    # raw events) — so no schema bump is needed. Set only for tools with genuine
    # exit-code semantics, so a card is never spuriously reddened.
    exit_code: int | None = None
    # The file's contents *before* a write overwrote them, so the renderer can show
    # a before/after diff instead of the flat "Wrote N bytes" summary. Like
    # exit_code, this is a bounded, JSON-safe scalar that must survive the RPC wire:
    # the write tool captures the prior text before clobbering the file, caps it
    # (dropping the snapshot entirely rather than shipping an unbounded or partial
    # file), and the executor promotes it here for the write tool only. None means
    # no snapshot — a newly created file, a binary/oversize/unreadable prior file,
    # or any non-write tool. The renderer uses ``created`` to tell those apart.
    before_text: str | None = None
    # Whether a write created a new file (vs. overwrote one). With before_text=None
    # this is the only thing separating a create — rendered as a pure-addition diff
    # of the new content — from an overwrite whose prior text couldn't be captured,
    # which must fall back to the plain summary rather than masquerade as a create.
    # A bounded JSON-safe scalar like before_text; False for every non-write tool.
    created: bool = False
    # A concise one-line summary of a successful read-type tool (read/grep/find/ls),
    # e.g. "read 42 lines from foo.py" or "grep: 3 matches", shown on the card in
    # place of a raw output dump. Like the other promoted fields it is a bounded,
    # JSON-safe scalar that must survive the RPC wire: the tool computes it from its
    # own structured data (never by re-parsing output), the summary module bounds it
    # at the source, and the executor promotes it only for tools that have one. None
    # for diff/shell tools, unknown tools, and error paths.
    summary: str | None = None
    # Whether the tool capped its own output past its max_output bytes/lines. The
    # ``output`` on this event is already the tool-bounded string, and the dropped
    # content never crossed the wire — so even a fully expanded card can be missing
    # more. This bool lets the expanded card say so honestly ("truncated at the
    # tool's limit") instead of implying it shows everything. A bounded JSON-safe
    # scalar like the others; False for tools that returned everything and for error
    # paths that produced no ToolResult.
    truncated: bool = False


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
        if self.next_before_entry_id is not None and not self.truncated:
            raise ValueError(
                "RPC message reports cannot include next_before_entry_id unless truncated"
            )
        return self


class RpcSessionsReported(WispEvent):
    """On-demand, bounded persisted session catalog returned over RPC."""

    type: Literal["rpc.sessions"] = "rpc.sessions"
    command_id: str
    sessions: tuple[RpcSessionSummary, ...] = ()
    selected_session_id: str | None = Field(default=None, min_length=1)
    selected_session_path: Path | None = None

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


class RpcSessionSelected(WispEvent):
    """Confirmation that an RPC session selection has become active."""

    type: Literal["rpc.session.selected"] = "rpc.session.selected"
    command_id: str
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
    entry_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < RPC_SESSIONS_SCHEMA_VERSION:
            raise ValueError(
                f"RPC session selection events require schema_version "
                f"{RPC_SESSIONS_SCHEMA_VERSION} or newer"
            )
        return self


class _RpcSessionDerived(WispEvent):
    """Shared source and target identity for RPC session derivation."""

    command_id: str
    source_session_id: str = Field(min_length=1)
    source_session_path: Path
    source_active_leaf_id: str | None = Field(default=None, min_length=1)
    session_id: str = Field(min_length=1)
    session_path: Path
    active_leaf_id: str | None = Field(default=None, min_length=1)
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

    @model_validator(mode="after")
    def _validate_schema_version(self) -> Self:
        if self.schema_version < QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION:
            raise ValueError(
                "queue message injection requires schema_version "
                f"{QUEUE_MESSAGE_INJECTED_SCHEMA_VERSION} or newer"
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
    | RpcMessagesReported
    | RpcSessionsReported
    | RpcSessionSelected
    | RpcSessionCloned
    | RpcSessionForked
    | RpcSessionTreeReported
    | RpcSessionTreeNavigated
    | QueueUpdated
    | QueueItemsRemoved
    | QueueMessageInjected
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
    if data.get("type") == "queue.updated" and version < QUEUE_UPDATE_SCHEMA_VERSION:
        raise ValueError(
            f"Queue update events require schema_version {QUEUE_UPDATE_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.state" and version < RPC_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"RPC state events require schema_version {RPC_STATE_SCHEMA_VERSION} or newer"
        )
    if data.get("type") == "rpc.messages" and version < RPC_MESSAGES_SCHEMA_VERSION:
        raise ValueError(
            "RPC message report events require schema_version "
            f"{RPC_MESSAGES_SCHEMA_VERSION} or newer"
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


def wisp_event_from_json(line: str) -> KnownWispEvent:
    """Parse one supported-schema JSONL event line into a typed Wisp event."""

    data = JsonObjectAdapter.validate_json(line)
    _require_current_schema(data)
    return KnownWispEventAdapter.validate_python(data)


def wisp_event_from_dict(data: JsonObject) -> KnownWispEvent:
    """Parse one supported-schema event dictionary into a typed Wisp event."""

    _require_current_schema(data)
    return KnownWispEventAdapter.validate_python(data)
