"""Derived lifetime and active-context session statistics."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from wisp.agent.context import build_context_budget, context_fingerprint, estimate_context
from wisp.agent.messages import Message, SessionEntry
from wisp.coding.costs import aggregate_session_cost
from wisp.events import SessionStats, TokenUsage, UsageCost
from wisp.providers.base import ToolSpec
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
    observed_tokens: int | None = None,
    observed_is_current: bool = False,
    observed_entry_id: str | None = None,
    observed_context_fingerprint: str | None = None,
) -> SessionStats:
    """Derive one consistent statistics snapshot from durable session state."""

    usage_records = tuple(_usage_records(entries))
    durable_observation, durable_entry_id, durable_observation_is_latest = _latest_observation(
        entries
    )
    if observed_tokens is None:
        observed_tokens = durable_observation
    if observed_tokens != durable_observation or not durable_observation_is_latest:
        observed_is_current = False
    if observed_entry_id is not None and observed_entry_id != durable_entry_id:
        observed_is_current = False
    if (
        observed_context_fingerprint is not None
        and observed_context_fingerprint != context_fingerprint(provider_messages, tools)
    ):
        observed_is_current = False
    estimate = estimate_context(provider_messages, tools)
    return SessionStats(
        session_id=session_id,
        entry_count=len(entries),
        active_message_count=len(replay.messages),
        compaction_count=sum(entry.kind == "compaction" for entry in entries),
        usage_record_count=len(usage_records),
        usage=_sum_usage(usage_records),
        cost=aggregate_session_cost(_cost_records(entries)),
        context=build_context_budget(
            estimate,
            context_window=context_window,
            reserve_tokens=reserve_tokens,
            observed_tokens=observed_tokens,
            observed_is_current=observed_is_current,
        ),
    )


def _usage_records(entries: Sequence[SessionEntry]) -> list[TokenUsage]:
    records: list[TokenUsage] = []
    for entry in entries:
        if entry.message is not None and entry.message.usage is not None:
            records.append(entry.message.usage)
        if entry.compaction is not None and entry.compaction.usage is not None:
            records.append(entry.compaction.usage)
        if (
            entry.event is not None
            and entry.event.get("type") == "compaction.completed"
            and entry.event.get("outcome") == "failed"
        ):
            raw_usage = entry.event.get("usage")
            if isinstance(raw_usage, dict):
                try:
                    records.append(TokenUsage.model_validate(raw_usage))
                except ValidationError:
                    pass
    return records


def _cost_records(entries: Sequence[SessionEntry]) -> list[UsageCost | None]:
    records: list[UsageCost | None] = []
    for entry in entries:
        if entry.message is not None and entry.message.role == "assistant":
            records.append(entry.message.cost)
        if entry.compaction is not None:
            records.append(entry.compaction.cost)
        if entry.event is not None and entry.event.get("type") == "compaction.completed":
            raw_cost = entry.event.get("cost")
            if isinstance(raw_cost, dict):
                try:
                    records.append(UsageCost.model_validate(raw_cost))
                except ValidationError:
                    records.append(None)
            elif entry.event.get("usage") is not None:
                records.append(None)
    return records


def _sum_usage(records: Sequence[TokenUsage]) -> TokenUsage:
    return TokenUsage(
        input_tokens=sum(record.input_tokens for record in records),
        output_tokens=sum(record.output_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        cache_read_input_tokens=_sum_optional(records, "cache_read_input_tokens"),
        cache_write_input_tokens=_sum_optional(records, "cache_write_input_tokens"),
        reasoning_output_tokens=_sum_optional(records, "reasoning_output_tokens"),
    )


def _sum_optional(records: Sequence[TokenUsage], field: str) -> int | None:
    values = [getattr(record, field) for record in records]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _latest_observation(
    entries: Sequence[SessionEntry],
) -> tuple[int | None, str | None, bool]:
    boundary = max(
        (index for index, entry in enumerate(entries) if entry.kind == "compaction"),
        default=-1,
    )
    context_messages = [
        (entry.id, entry.message)
        for entry in entries[boundary + 1 :]
        if entry.kind == "message" and entry.message is not None and entry.message.role != "system"
    ]
    latest_usage: int | None = None
    latest_usage_index: int | None = None
    latest_entry_id: str | None = None
    for index, (entry_id, message) in enumerate(context_messages):
        if (
            message.role == "assistant"
            and message.finish_reason not in {"error", "cancelled"}
            and message.usage is not None
            and message.usage.total_tokens > 0
        ):
            latest_usage = message.usage.total_tokens
            latest_entry_id = entry_id
            latest_usage_index = index
    return latest_usage, latest_entry_id, latest_usage_index == len(context_messages) - 1


__all__ = ["build_session_stats"]
