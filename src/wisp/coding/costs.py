"""Exact list-price accounting for provider usage snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from wisp.events import (
    BillableTokenUsage,
    SessionCostSummary,
    TokenUsage,
    UsageCost,
    UsageCostRates,
)
from wisp.providers.catalog import ModelPricing, ModelRegistry

_MILLION = Decimal(1_000_000)


class CostEstimator:
    """Resolve current catalog rates and snapshot a cost for one completed request."""

    def __init__(self, models: ModelRegistry | None) -> None:
        self._models = models

    def __call__(
        self,
        provider: str,
        requested_model: str | None,
        response_model: str | None,
        usage: TokenUsage,
    ) -> UsageCost:
        selected_model = response_model or requested_model
        billable = _billable_usage(provider, usage)
        canonical = (
            self._models.canonical_model(provider, selected_model)
            if self._models is not None and selected_model is not None
            else None
        )
        if billable is None:
            return UsageCost(
                provider=provider,
                requested_model=requested_model,
                model=canonical or selected_model,
                unavailable_reason="usage_incomplete",
            )
        if self._models is None or selected_model is None:
            return UsageCost(
                provider=provider,
                requested_model=requested_model,
                model=canonical or selected_model,
                billable=billable,
                unavailable_reason="pricing_unavailable",
            )
        priced = self._models.pricing(
            provider,
            selected_model,
            input_tokens=usage.input_tokens,
        )
        if priced is None:
            return UsageCost(
                provider=provider,
                requested_model=requested_model,
                model=canonical or selected_model,
                billable=billable,
                unavailable_reason="pricing_unavailable",
            )
        canonical_model, pricing = priced
        rates = _rates_from_pricing(pricing)
        estimated = _calculate_cost(billable, rates)
        if estimated is None:
            return UsageCost(
                provider=provider,
                requested_model=requested_model,
                model=canonical_model,
                billable=billable,
                rates=rates,
                unavailable_reason="pricing_unavailable",
            )
        return UsageCost(
            provider=provider,
            requested_model=requested_model,
            model=canonical_model,
            billable=billable,
            rates=rates,
            estimated_usd=estimated,
        )


def aggregate_session_cost(costs: Sequence[UsageCost | None]) -> SessionCostSummary:
    """Aggregate persisted request snapshots without repricing historical entries."""

    priced = [
        cost.estimated_usd for cost in costs if cost is not None and cost.estimated_usd is not None
    ]
    unpriced_count = sum(cost is None or cost.estimated_usd is None for cost in costs)
    return SessionCostSummary(
        known_usd=sum(priced, Decimal()),
        complete=unpriced_count == 0,
        priced_record_count=len(priced),
        unpriced_record_count=unpriced_count,
    )


def format_cost_summary(cost: SessionCostSummary | None) -> str:
    """Format a compact human-readable cumulative estimate."""

    if cost is None or (cost.priced_record_count == 0 and cost.unpriced_record_count == 0):
        return ""
    if cost.priced_record_count == 0:
        return "cost unknown"
    amount = format_usd(cost.known_usd)
    return f"cost {amount}" if cost.complete else f"cost ≥{amount}"


def format_usage_cost(cost: UsageCost | None) -> str:
    """Format one request's estimated list price for human-facing renderers."""

    if cost is None or cost.estimated_usd is None:
        return "cost unknown"
    return f"cost {format_usd(cost.estimated_usd)}"


def format_usd(amount: Decimal) -> str:
    """Round only for presentation while preserving meaningful small estimates."""

    if amount == 0:
        return "$0.00"
    if amount < Decimal("0.0001"):
        return "<$0.0001"
    if amount < Decimal("0.01"):
        return f"${amount:.4f}"
    if amount < Decimal("1"):
        return f"${amount:.3f}"
    return f"${amount:.2f}"


def _billable_usage(provider: str, usage: TokenUsage) -> BillableTokenUsage | None:
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_write_input_tokens or 0
    if provider in {"openai", "openai-codex", "google"}:
        if cache_read > usage.input_tokens:
            return None
        input_tokens = usage.input_tokens - cache_read
    elif provider == "anthropic":
        input_tokens = usage.input_tokens
    else:
        return None
    output_tokens = usage.output_tokens
    if provider == "google":
        output_tokens += usage.reasoning_output_tokens or 0
    return BillableTokenUsage(
        input_tokens=input_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        output_tokens=output_tokens,
    )


def _rates_from_pricing(pricing: ModelPricing) -> UsageCostRates:
    return UsageCostRates(
        input_usd_per_million=pricing.input_usd_per_million,
        output_usd_per_million=pricing.output_usd_per_million,
        cache_read_usd_per_million=pricing.cache_read_usd_per_million,
        cache_write_usd_per_million=pricing.cache_write_usd_per_million,
    )


def _calculate_cost(billable: BillableTokenUsage, rates: UsageCostRates) -> Decimal | None:
    components = (
        (billable.input_tokens, rates.input_usd_per_million),
        (billable.cache_read_input_tokens, rates.cache_read_usd_per_million),
        (billable.cache_write_input_tokens, rates.cache_write_usd_per_million),
        (billable.output_tokens, rates.output_usd_per_million),
    )
    if any(tokens > 0 and rate is None for tokens, rate in components):
        return None
    return sum(
        (Decimal(tokens) * rate / _MILLION for tokens, rate in components if rate is not None),
        Decimal(),
    )


__all__ = ["CostEstimator", "aggregate_session_cost", "format_cost_summary", "format_usage_cost"]
