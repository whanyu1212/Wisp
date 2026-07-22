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
)
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntry,
    SessionEntryAdapter,
    is_session_tree_entry,
)
from wisp.sessions.replay import replay_session_entries


def _linear_entries(entries: tuple[SessionEntry, ...]) -> tuple[SessionEntry, ...]:
    parent_id: str | None = None
    attached: list[SessionEntry] = []
    for entry in entries:
        if is_session_tree_entry(entry):
            entry = entry.model_copy(update={"parent_id": parent_id})
            parent_id = entry.id
        attached.append(entry)
    return tuple(attached)


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
                                effective_until=date(2026, 8, 31),
                                input_usd_per_million=Decimal("1"),
                                cache_read_usd_per_million=Decimal("0.1"),
                                cache_write_usd_per_million=Decimal("1.25"),
                                output_usd_per_million=Decimal("5"),
                            ),
                            ModelPricing(
                                effective_from=date(2026, 9, 1),
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
        ),
    )

    assert estimate.model == "model"
    assert estimate.billable is not None
    assert estimate.billable.input_tokens == 600
    assert estimate.billable.cache_read_input_tokens == 400
    assert estimate.estimated_usd == Decimal("0.0054")


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


def test_registry_selects_effective_dated_anthropic_rates() -> None:
    models = _models()

    before = models.pricing("anthropic", "model", input_tokens=1, at=date(2026, 8, 31))
    after = models.pricing("anthropic", "model", input_tokens=1, at=date(2026, 9, 1))

    assert before is not None
    assert after is not None
    assert before[1].input_usd_per_million == Decimal("1")
    assert after[1].input_usd_per_million == Decimal("2")


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
    entries = _linear_entries(
        (
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
    entries = _linear_entries(
        (
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
