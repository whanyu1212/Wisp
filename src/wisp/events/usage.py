"""Token usage, list-price cost, context budget, and compaction events."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wisp.events._base import CompactionReason, RunOutcome, WispEvent


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
    # The prompt-cache key the request was actually sent with; None when the
    # adapter takes no key. A clone or fork gets a new key, so this identifies
    # which provider cache a later request can still reuse.
    prompt_cache_key: str | None = None


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


class SessionStatsReported(WispEvent):
    """On-demand, non-persisted session statistics returned over RPC."""

    type: Literal["session.stats"] = "session.stats"
    command_id: str
    stats: SessionStats
