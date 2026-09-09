from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from wisp.agent.messages import CompactionRecord, Message
from wisp.coding.costs import CostEstimator, aggregate_session_cost, format_cost_summary, format_usd
from wisp.coding.stats import build_session_stats
from wisp.events import MessageCompleted, TokenUsage, wisp_event_from_json
from wisp.providers.catalog import (
    ModelCatalog,
    ModelCatalogProviderEntry,
    ModelPricing,
    ModelRegistry,
    builtin_catalog,
)
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntryAdapter,
)
from wisp.sessions.replay import replay_session_entries


def _models() -> ModelRegistry:
    return ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=(
                ModelCatalogProviderEntry(
                    name="openai",
                    display_name="OpenAI",
                    default_model="model",
                    docs_url="https://example.com",
                    models=("model",),
                    model_aliases={"latest": "model"},
                    pricing={
                        "model": (
                            ModelPricing(
                                input_usd_per_million=Decimal("2"),
                                cache_read_usd_per_million=Decimal("0.5"),
                                cache_write_usd_per_million=Decimal("2.5"),
                                output_usd_per_million=Decimal("8"),
                            ),
                            ModelPricing(
                                input_token_threshold=1_001,
                                input_usd_per_million=Decimal("4"),
                                cache_read_usd_per_million=Decimal("1"),
                                cache_write_usd_per_million=Decimal("5"),
                                output_usd_per_million=Decimal("16"),
                            ),
                        )
                    },
                ),
                ModelCatalogProviderEntry(
                    name="anthropic",
                    display_name="Anthropic",
                    default_model="model",
                    docs_url="https://example.com",
                    models=("model",),
                    pricing={
                        "model": (
                            ModelPricing(
                                effective_until=date(9999, 12, 30),
                                input_usd_per_million=Decimal("1"),
                                cache_read_usd_per_million=Decimal("0.1"),
                                cache_write_usd_per_million=Decimal("1.25"),
                                output_usd_per_million=Decimal("5"),
                            ),
                            ModelPricing(
                                effective_from=date.max,
                                input_usd_per_million=Decimal("2"),
                                cache_read_usd_per_million=Decimal("0.2"),
                                cache_write_usd_per_million=Decimal("2.5"),
                                output_usd_per_million=Decimal("10"),
                            ),
                        )
                    },
                ),
            ),
        )
    )


def test_estimator_prices_openai_cache_once_and_resolves_alias() -> None:
    estimate = CostEstimator(_models())(
        "openai",
        "latest",
        "latest",
        TokenUsage(
            input_tokens=1_000,
            output_tokens=500,
            total_tokens=1_500,
            cache_read_input_tokens=400,
            cache_write_input_tokens=300,
        ),
    )

    assert estimate.model == "model"
    assert estimate.billable is not None
    assert estimate.billable.input_tokens == 300
    assert estimate.billable.cache_read_input_tokens == 400
    assert estimate.billable.cache_write_input_tokens == 300
    assert estimate.estimated_usd == Decimal("0.00555")


def test_estimator_rejects_openai_cache_buckets_larger_than_input() -> None:
    estimate = CostEstimator(_models())(
        "openai",
        "model",
        "model",
        TokenUsage(
            input_tokens=100,
            output_tokens=10,
            total_tokens=110,
            cache_read_input_tokens=60,
            cache_write_input_tokens=50,
        ),
    )

    assert estimate.estimated_usd is None
    assert estimate.unavailable_reason == "usage_incomplete"


def test_estimator_normalizes_codex_cache_buckets_before_pricing_lookup() -> None:
    estimate = CostEstimator(_models())(
        "openai-codex",
        "model",
        "model",
        TokenUsage(
            input_tokens=1_000,
            output_tokens=100,
            total_tokens=1_100,
            cache_read_input_tokens=400,
            cache_write_input_tokens=300,
        ),
    )

    assert estimate.billable is not None
    assert estimate.billable.input_tokens == 300
    assert estimate.billable.cache_read_input_tokens == 400
    assert estimate.billable.cache_write_input_tokens == 300
    assert estimate.unavailable_reason == "pricing_unavailable"


def test_estimator_normalizes_deepseek_cache_hits_before_pricing_lookup() -> None:
    estimate = CostEstimator(ModelRegistry(builtin_catalog()))(
        "deepseek",
        "deepseek-v4-pro",
        "deepseek-v4-pro",
        TokenUsage(
            input_tokens=1_000,
            output_tokens=500,
            total_tokens=1_500,
            cache_read_input_tokens=400,
        ),
    )

    assert estimate.billable is not None
    assert estimate.billable.input_tokens == 600
    assert estimate.billable.cache_read_input_tokens == 400
    assert estimate.billable.cache_write_input_tokens == 0
    assert estimate.estimated_usd is None
    assert estimate.unavailable_reason == "pricing_unavailable"


def test_estimator_prices_xai_like_openai_responses_usage() -> None:
    estimate = CostEstimator(ModelRegistry(builtin_catalog()))(
        "xai",
        "grok-4.6",
        "grok-4.6",
        TokenUsage(
            input_tokens=1_000,
            output_tokens=500,
            total_tokens=1_500,
            cache_read_input_tokens=400,
        ),
    )

    assert estimate.model == "grok-4.6"
    assert estimate.billable is not None
    assert estimate.billable.input_tokens == 600
    assert estimate.billable.cache_read_input_tokens == 400
    assert estimate.billable.cache_write_input_tokens == 0
    assert estimate.estimated_usd == Decimal("0.0044")


def test_estimator_rejects_xai_cache_buckets_larger_than_input() -> None:
    estimate = CostEstimator(ModelRegistry(builtin_catalog()))(
        "xai",
        "grok-4.6",
        "grok-4.6",
        TokenUsage(
            input_tokens=100,
            output_tokens=10,
            total_tokens=110,
            cache_read_input_tokens=60,
            cache_write_input_tokens=50,
        ),
    )

    assert estimate.estimated_usd is None
    assert estimate.unavailable_reason == "usage_incomplete"


def test_estimator_uses_long_context_band_and_never_prices_unknown_models() -> None:
    estimator = CostEstimator(_models())
    long_context = estimator(
        "openai",
        "model",
        "model",
        TokenUsage(input_tokens=1_001, output_tokens=0, total_tokens=1_001),
    )
    unknown = estimator(
        "openai",
        "unknown",
        "unknown",
        TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )

    assert long_context.estimated_usd == Decimal("0.004004")
    assert unknown.estimated_usd is None
    assert unknown.unavailable_reason == "pricing_unavailable"


def test_estimator_keeps_anthropic_cache_categories_separate() -> None:
    estimate = CostEstimator(_models())(
        "anthropic",
        "model",
        "model",
        TokenUsage(
            input_tokens=1_000,
            output_tokens=100,
            total_tokens=1_600,
            cache_read_input_tokens=200,
            cache_write_input_tokens=300,
        ),
    )

    assert estimate.estimated_usd == Decimal("0.001895")


def test_estimator_prices_gemini_cache_and_thinking_tokens() -> None:
    estimate = CostEstimator(ModelRegistry(builtin_catalog()))(
        "google",
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        TokenUsage(
            input_tokens=1_000,
            output_tokens=100,
            reasoning_output_tokens=200,
            total_tokens=1_300,
            cache_read_input_tokens=400,
        ),
    )

    assert estimate.billable is not None
    assert estimate.billable.input_tokens == 600
    assert estimate.billable.cache_read_input_tokens == 400
    assert estimate.billable.output_tokens == 300
    assert estimate.rates is not None
    # Use the selected snapshot so this integration test survives the scheduled
    # price change; the date-boundary tests below verify the exact rates.
    assert estimate.rates.cache_read_usd_per_million is not None
    assert (
        estimate.estimated_usd
        == (
            600 * estimate.rates.input_usd_per_million
            + 400 * estimate.rates.cache_read_usd_per_million
            + 300 * estimate.rates.output_usd_per_million
        )
        / 1_000_000
    )


def test_registry_selects_effective_dated_anthropic_rates() -> None:
    models = _models()

    before = models.pricing("anthropic", "model", input_tokens=1, at=date(9999, 12, 30))
    after = models.pricing("anthropic", "model", input_tokens=1, at=date.max)

    assert before is not None
    assert after is not None
    assert before[1].input_usd_per_million == Decimal("1")
    assert after[1].input_usd_per_million == Decimal("2")


@pytest.mark.parametrize(
    ("model", "input_tokens", "rates"),
    [
        ("gpt-6-astra", 272_000, ("10", "1", "12.5", "50")),
        ("gpt-6-astra", 272_001, ("20", "2", "25", "75")),
        ("gpt-5.6-sol", 272_000, ("4", "0.4", "5", "20")),
        ("gpt-5.6-sol", 272_001, ("8", "0.8", "10", "30")),
        ("gpt-5.6-terra", 272_000, ("2", "0.2", "2.5", "12")),
        ("gpt-5.6-terra", 272_001, ("4", "0.4", "5", "18")),
        ("gpt-5.6-luna", 272_000, ("0.2", "0.02", "0.25", "1.2")),
        ("gpt-5.6-luna", 272_001, ("0.4", "0.04", "0.5", "1.8")),
    ],
)
def test_builtin_openai_prices_at_long_context_boundary(
    model: str, input_tokens: int, rates: tuple[str, str, str, str]
) -> None:
    registry = ModelRegistry(builtin_catalog())
    selected = registry.pricing("openai", model, input_tokens=input_tokens, at=date(2026, 9, 9))

    assert selected is not None
    band = selected[1]
    assert (
        band.input_usd_per_million,
        band.cache_read_usd_per_million,
        band.cache_write_usd_per_million,
        band.output_usd_per_million,
    ) == tuple(Decimal(rate) for rate in rates)
    assert registry.pricing("openai-codex", model, input_tokens=input_tokens) is None


@pytest.mark.parametrize("model", ["gemini-3.7-flash", "gemini-3.8-flash"])
@pytest.mark.parametrize(
    ("at", "rates"),
    [
        (date(2026, 12, 31), ("0.75", "0.075", "3.75")),
        (date(2027, 1, 1), ("1.5", "0.15", "7.5")),
    ],
)
def test_builtin_gemini_prices_change_after_introductory_period(
    model: str, at: date, rates: tuple[str, str, str]
) -> None:
    selected = ModelRegistry(builtin_catalog()).pricing(
        "google", model, input_tokens=500_000, at=at
    )

    assert selected is not None
    band = selected[1]
    assert (
        band.input_usd_per_million,
        band.cache_read_usd_per_million,
        band.output_usd_per_million,
    ) == tuple(Decimal(rate) for rate in rates)


@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("claude-fable-5-1", ("10", "0.25", "12.5", "50")),
        ("claude-opus-5", ("5", "0.5", "6.25", "25")),
        ("claude-sonnet-5", ("2", "0.2", "2.5", "10")),
    ],
)
def test_builtin_anthropic_current_prices_include_discounted_cache_reads(
    model: str, rates: tuple[str, str, str, str]
) -> None:
    selected = ModelRegistry(builtin_catalog()).pricing(
        "anthropic", model, input_tokens=500_000, at=date(2026, 9, 9)
    )

    assert selected is not None
    band = selected[1]
    assert (
        band.input_usd_per_million,
        band.cache_read_usd_per_million,
        band.cache_write_usd_per_million,
        band.output_usd_per_million,
    ) == tuple(Decimal(rate) for rate in rates)


def test_catalog_rejects_overlapping_bands_and_cost_display_preserves_tiny_amounts() -> None:
    with pytest.raises(ValidationError, match="overlapping price bands"):
        ModelCatalogProviderEntry(
            name="provider",
            display_name="Provider",
            default_model="model",
            docs_url="https://example.com",
            models=("model",),
            pricing={
                "model": (
                    ModelPricing(
                        effective_from=date(2026, 1, 1),
                        effective_until=date(2026, 12, 31),
                        input_usd_per_million=Decimal("1"),
                        output_usd_per_million=Decimal("1"),
                    ),
                    ModelPricing(
                        effective_from=date(2026, 6, 1),
                        effective_until=date(2027, 1, 1),
                        input_usd_per_million=Decimal("2"),
                        output_usd_per_million=Decimal("2"),
                    ),
                )
            },
        )

    assert format_usd(Decimal("0.0000001")) == "<$0.0001"
    assert format_usd(Decimal("0.00005")) == "<$0.0001"


def test_session_cost_summary_marks_legacy_usage_as_partial_without_repricing() -> None:
    priced = CostEstimator(_models())(
        "openai",
        "model",
        "model",
        TokenUsage(input_tokens=100, output_tokens=100, total_tokens=200),
    )
    summary = aggregate_session_cost((priced, None))

    assert summary.known_usd == Decimal("0.001")
    assert summary.complete is False
    assert summary.priced_record_count == 1
    assert summary.unpriced_record_count == 1
    assert format_cost_summary(summary) == "cost ≥$0.0010"


def test_session_stats_uses_persisted_cost_snapshots_for_messages_and_compactions() -> None:
    cost = CostEstimator(_models())(
        "openai",
        "model",
        "model",
        TokenUsage(input_tokens=100, output_tokens=100, total_tokens=200),
    )
    entries = (
        MessageSessionEntry(
            id="user",
            session_id="session",
            message=Message(role="user", content="question"),
        ),
        MessageSessionEntry(
            id="answer",
            session_id="session",
            message=Message(
                role="assistant",
                content="answer",
                usage=TokenUsage(input_tokens=100, output_tokens=100, total_tokens=200),
                cost=cost,
            ),
        ),
        MessageSessionEntry(
            id="next-user",
            session_id="session",
            message=Message(role="user", content="next question"),
        ),
        MessageSessionEntry(
            id="next-answer",
            session_id="session",
            message=Message(
                role="assistant",
                content="next answer",
                finish_reason="stop",
                cost=cost,
            ),
        ),
        CompactionSessionEntry(
            id="compact",
            session_id="session",
            compaction=CompactionRecord(
                summary="summary",
                replaced_entry_ids=("user", "answer"),
                provider="openai",
                usage=TokenUsage(input_tokens=100, output_tokens=100, total_tokens=200),
                cost=cost,
            ),
        ),
    )

    stats = build_session_stats(
        session_id="session",
        entries=entries,
        replay=replay_session_entries(entries),
        provider_messages=(),
        tools=(),
        context_window=None,
        reserve_tokens=100,
    )

    assert stats.cost.known_usd == Decimal("0.003")
    assert stats.cost.complete is True
    assert stats.cost.priced_record_count == 3
    reloaded = SessionEntryAdapter.validate_json(entries[4].model_dump_json())
    assert isinstance(reloaded, CompactionSessionEntry)
    assert reloaded.compaction.cost == cost


def test_session_stats_marks_legacy_successful_messages_unpriced() -> None:
    entries = (
        MessageSessionEntry(
            id="user",
            session_id="session",
            message=Message(role="user", content="question"),
        ),
        MessageSessionEntry(
            id="answer",
            session_id="session",
            message=Message(role="assistant", content="answer", finish_reason="stop"),
        ),
    )

    stats = build_session_stats(
        session_id="session",
        entries=entries,
        replay=replay_session_entries(entries),
        provider_messages=(),
        tools=(),
        context_window=None,
        reserve_tokens=100,
    )

    assert stats.cost.complete is False
    assert stats.cost.unpriced_record_count == 1


def test_cost_events_require_schema_v12_and_round_trip() -> None:
    cost = CostEstimator(_models())(
        "openai",
        "model",
        "model",
        TokenUsage(input_tokens=100, output_tokens=100, total_tokens=200),
    )
    event = MessageCompleted(
        turn=1,
        content="answer",
        finish_reason="stop",
        usage=TokenUsage(input_tokens=100, output_tokens=100, total_tokens=200),
        cost=cost,
    )

    assert wisp_event_from_json(event.model_dump_json()) == event
    with pytest.raises(ValidationError, match="usage cost requires schema_version 12"):
        MessageCompleted(
            schema_version=11,
            turn=1,
            content="answer",
            finish_reason="stop",
            cost=cost,
        )
