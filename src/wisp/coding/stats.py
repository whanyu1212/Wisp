"""Derived lifetime and active-context session statistics."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from pydantic import ValidationError

from wisp.agent.context import (
    build_context_budget,
    context_fingerprint,
    estimate_context,
    estimate_context_budget,
)
from wisp.agent.messages import Message
from wisp.coding.compaction import NothingToCompactError, plan_manual_compaction
from wisp.coding.costs import aggregate_session_cost
from wisp.events import (
    CompactionPolicyStatus,
    ContextBudget,
    ContextObservation,
    SessionStats,
    TokenUsage,
    UsageCost,
)
from wisp.providers.base import ToolSpec
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    SessionEntry,
)
from wisp.sessions.replay import SessionReplay


def build_session_stats(
    *,
    session_id: str | None,
    entries: Sequence[SessionEntry],
    replay: SessionReplay,
    provider_messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    context_window: int | None,
    reserve_tokens: int,
    provider: str | None = None,
    model: str | None = None,
    observed_tokens: int | None = None,
    observed_is_current: bool = False,
    observed_entry_id: str | None = None,
    observed_context_fingerprint: str | None = None,
    auto_compaction_enabled: bool = True,
) -> SessionStats:
    """Derive one consistent statistics snapshot from durable session state."""

    usage_records = tuple(_usage_records(entries))
    entries_by_id = {entry.id: entry for entry in entries}
    active_entries = tuple(entries_by_id[entry_id] for entry_id in replay.path_entry_ids)
    durable_observation, durable_entry_id, legacy_tokens, legacy_is_latest = _latest_observation(
        active_entries
    )
    if durable_observation is not None:
        context = estimate_context_budget(
            provider_messages,
            tools,
            context_window=context_window,
            reserve_tokens=reserve_tokens,
            observation=durable_observation,
            provider=provider if provider is not None else durable_observation.provider,
            model=model if provider is not None else durable_observation.model,
        )
    else:
        if observed_tokens is None:
            observed_tokens = legacy_tokens
        if observed_tokens != legacy_tokens or not legacy_is_latest:
            observed_is_current = False
        if observed_entry_id is not None and observed_entry_id != durable_entry_id:
            observed_is_current = False
        if (
            observed_context_fingerprint is not None
            and observed_context_fingerprint != context_fingerprint(provider_messages, tools)
        ):
            observed_is_current = False
        estimate = estimate_context(provider_messages, tools)
        context = build_context_budget(
            estimate,
            context_window=context_window,
            reserve_tokens=reserve_tokens,
            observed_tokens=observed_tokens,
            observed_is_current=observed_is_current,
        )
    return SessionStats(
        session_id=session_id,
        entry_count=len(entries),
        active_message_count=len(replay.messages),
        compaction_count=sum(entry.kind == "compaction" for entry in entries),
        usage_record_count=len(usage_records),
        usage=_sum_usage(usage_records),
        cost=aggregate_session_cost(_cost_records(entries)),
        context=context,
        compaction=_compaction_policy_status(
            replay,
            context=context,
            auto_compaction_enabled=auto_compaction_enabled,
        ),
    )


def _compaction_policy_status(
    replay: SessionReplay,
    *,
    context: ContextBudget,
    auto_compaction_enabled: bool,
) -> CompactionPolicyStatus:
    if not auto_compaction_enabled:
        return CompactionPolicyStatus(
            auto_compaction_enabled=False,
            threshold_eligible=False,
            threshold_ineligible_reason="automatic compaction is disabled",
            overflow_recovery_enabled=False,
        )
    if context.context_window is not None and context.reserve_tokens >= context.context_window:
        return CompactionPolicyStatus(
            threshold_ineligible_reason="reserve consumes the model window",
            overflow_recovery_enabled=False,
        )
    try:
        plan_manual_compaction(replay)
    except NothingToCompactError:
        return CompactionPolicyStatus(
            threshold_ineligible_reason=(
                "model context window is unknown"
                if context.context_window is None
                else "no compactable context prefix"
            ),
            overflow_recovery_enabled=False,
        )
    if context.context_window is None:
        return CompactionPolicyStatus(
            threshold_ineligible_reason="model context window is unknown",
        )
    return CompactionPolicyStatus(
        threshold_eligible=True,
        threshold_ineligible_reason=None,
    )


def _usage_records(entries: Sequence[SessionEntry]) -> list[TokenUsage]:
    records: list[TokenUsage] = []
    for entry in entries:
        if isinstance(entry, MessageSessionEntry) and entry.message.usage is not None:
            records.append(entry.message.usage)
        if isinstance(entry, CompactionSessionEntry) and entry.compaction.usage is not None:
            records.append(entry.compaction.usage)
        if (
            isinstance(entry, EventSessionEntry)
            and entry.event.payload.get("type") == "compaction.completed"
            and entry.event.payload.get("outcome") == "failed"
        ):
            raw_usage = entry.event.payload.get("usage")
            if isinstance(raw_usage, dict):
                try:
                    records.append(TokenUsage.model_validate(raw_usage))
                except ValidationError:
                    pass
    return records


def _cost_records(entries: Sequence[SessionEntry]) -> list[UsageCost | None]:
    records: list[UsageCost | None] = []
    for entry in entries:
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "assistant":
            records.append(entry.message.cost)
        if isinstance(entry, CompactionSessionEntry):
            records.append(entry.compaction.cost)
        if (
            isinstance(entry, EventSessionEntry)
            and entry.event.payload.get("type") == "compaction.completed"
        ):
            raw_cost = entry.event.payload.get("cost")
            if isinstance(raw_cost, dict):
                try:
                    records.append(UsageCost.model_validate(raw_cost))
                except ValidationError:
                    records.append(None)
            elif entry.event.payload.get("usage") is not None:
                records.append(None)
    return records


def _sum_usage(records: Sequence[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(record.input_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        cache_read_input_tokens=_sum_complete_optional(records, "cache_read_input_tokens"),
        cache_write_input_tokens=_sum_complete_optional(records, "cache_write_input_tokens"),
        reasoning_output_tokens=_sum_optional(records, "reasoning_output_tokens"),
    )


def _sum_optional(records: Sequence[TokenUsage], field: str) -> int | None:
    values = [cast(int | None, getattr(record, field)) for record in records]
    if not values or all(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _sum_complete_optional(records: Sequence[TokenUsage], field: str) -> int | None:
    values = [cast(int | None, getattr(record, field)) for record in records]
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _latest_observation(
    entries: Sequence[SessionEntry],
) -> tuple[ContextObservation | None, str | None, int | None, bool]:
    boundary = max(
        (index for index, entry in enumerate(entries) if entry.kind == "compaction"),
        default=-1,
    )
    context_messages = [
        (entry.id, entry.message)
        for entry in entries[boundary + 1 :]
        if isinstance(entry, MessageSessionEntry) and entry.message.role != "system"
    ]
    latest_observation: ContextObservation | None = None
    latest_usage: int | None = None
    latest_usage_index: int | None = None
    latest_entry_id: str | None = None
    for index, (entry_id, message) in enumerate(context_messages):
        if message.context_observation is not None:
            latest_observation = message.context_observation
            latest_entry_id = entry_id
        if (
            message.role == "assistant"
            and message.finish_reason not in {"error", "cancelled"}
            and message.usage is not None
            and message.usage.total_tokens > 0
        ):
            latest_usage = message.usage.total_tokens
            latest_entry_id = entry_id
            latest_usage_index = index
    return (
        latest_observation,
        latest_entry_id,
        latest_usage,
        latest_usage_index == len(context_messages) - 1,
    )


__all__ = ["build_session_stats"]
