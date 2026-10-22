"""Derived lifetime and active-context session statistics."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from pydantic import ValidationError

from wisp.agent.context_budget import (
    build_context_budget,
    estimate_context,
    estimate_context_budget,
)
from wisp.coding.compaction import NothingToCompactError, plan_manual_compaction
from wisp.coding.context_breakdown import build_context_breakdown
from wisp.coding.costs import aggregate_session_cost
from wisp.coding.prompt import PromptPart
from wisp.events import (
    AgentCompleted,
    AgentMode,
    CompactionPolicyStatus,
    ContextBudget,
    ContextObservation,
    MessageCompleted,
    MessageStarted,
    RunOutcome,
    SessionBranchSummary,
    SessionStats,
    TokenUsage,
    ToolResultReady,
    UsageCost,
    WispEvent,
)
from wisp.messages import Message
from wisp.providers.base import ToolSpec
from wisp.sessions.entries import (
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    SessionEntry,
)
from wisp.sessions.replay import SessionReplay, resolve_session_tree


@dataclass(slots=True)
class RunStats:
    """Totals for one run, reported on the `AgentCompleted` that ends it.

    The run observes each event it publishes; the clock starts when the tracker is
    created.
    """

    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    output_tokens: int | None = None
    model_seconds: float | None = None
    _response_started: float | None = None

    def observe(self, event: WispEvent) -> None:
        """Count a published tool result, a response's output tokens, and its time.

        A response is timed from its `MessageStarted` to its `MessageCompleted`. Only
        responses that report usage count, so time spent on a failed or partial response
        does not dilute throughput; one that never completes adds nothing.

        Args:
            event (WispEvent): Event the run just published.
        """
        if isinstance(event, ToolResultReady):
            self.tool_calls += 1
        elif isinstance(event, MessageStarted):
            self._response_started = time.monotonic()
        elif isinstance(event, MessageCompleted):
            started, self._response_started = self._response_started, None
            if event.usage is not None:
                self.output_tokens = (self.output_tokens or 0) + event.usage.output_tokens
                if started is not None:
                    elapsed = time.monotonic() - started
                    self.model_seconds = (self.model_seconds or 0.0) + elapsed

    def completed(
        self,
        *,
        session_id: str,
        turns: int,
        outcome: RunOutcome,
        mode: AgentMode | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> AgentCompleted:
        """Build the run's terminal event with its totals.

        Args:
            session_id (str): Session the run belongs to.
            turns (int): Turns the run completed.
            outcome (RunOutcome): How the run ended.
            mode (AgentMode | None): Mode the run ended in.
            model (str | None): Model the run ended with.
            effort (str | None): Reasoning effort the run ended with.

        Returns:
            AgentCompleted: Terminal event carrying the run's duration, tool count,
                output tokens, model streaming time, and the mode, model, and effort
                it ended with.
        """
        return AgentCompleted(
            session_id=session_id,
            turns=turns,
            outcome=outcome,
            mode=mode,
            model=model,
            effort=effort,
            duration_ms=max(0, round((time.monotonic() - self.started) * 1000)),
            tool_calls=self.tool_calls,
            output_tokens=self.output_tokens,
            model_ms=(
                None if self.model_seconds is None else max(0, round(self.model_seconds * 1000))
            ),
        )


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
    auto_compaction_enabled: bool = True,
    prompt_parts: Sequence[Sequence[PromptPart]] | None = None,
) -> SessionStats:
    """Derive one consistent statistics snapshot from durable session state.

    Context accounting starts from the latest provider observation persisted on the
    active path after the last compaction. Without one, the budget is estimated and
    ``observed_tokens`` (the session's in-memory observation, if any) is reported as
    stale because it no longer describes the active context.

    When ``prompt_parts`` labels the leading system messages of ``provider_messages``,
    the snapshot also splits the budget's effective tokens by source.
    """

    usage_records = tuple(_usage_records(entries))
    entries_by_id = {entry.id: entry for entry in entries}
    active_entries = tuple(entries_by_id[entry_id] for entry_id in replay.path_entry_ids)
    durable_observation = _latest_observation(active_entries)
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
        context = build_context_budget(
            estimate_context(provider_messages, tools),
            context_window=context_window,
            reserve_tokens=reserve_tokens,
            observed_tokens=observed_tokens,
            observed_is_current=False,
        )
    breakdown = (
        build_context_breakdown(
            prompt_messages=provider_messages[: len(prompt_parts)],
            prompt_parts=prompt_parts,
            tools=tools,
            replay=replay,
            entries=entries,
            effective_tokens=context.effective_tokens or 0,
            scaled=context.accounting_method != "fully_estimated",
        )
        if prompt_parts is not None
        else None
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
        context_breakdown=breakdown,
        branch=session_branch_summary(entries),
        used_skill_names=_used_skill_names(active_entries),
    )


def _used_skill_names(entries: Sequence[SessionEntry]) -> tuple[str, ...]:
    """Collect successfully loaded skills on the complete active branch.

    Compaction replaces provider context, but its ancestors still carry usage
    evidence. Calls alone are insufficient: failed or interrupted loads do not
    count. Return distinct names in their first successful-use order.
    """
    pending: dict[str, str] = {}
    used: dict[str, None] = {}
    for entry in entries:
        if not isinstance(entry, MessageSessionEntry):
            continue
        message = entry.message
        if message.role == "user" and message.skill_invocation is not None:
            used.setdefault(message.skill_invocation.name, None)
        if message.role == "assistant":
            for call in message.tool_calls or ():
                # Replace an earlier binding if a provider reuses a call ID.
                pending.pop(call.call_id, None)
                argument = call.arguments.get("name")
                if call.name == "skill" and isinstance(argument, str) and argument:
                    pending[call.call_id] = argument
        if message.role == "tool" and message.tool_call_id is not None:
            name = pending.pop(message.tool_call_id, None)
            if name is not None and message.tool_name == "skill" and message.is_error is False:
                used.setdefault(name, None)
    return tuple(used)


def session_branch_summary(entries: Sequence[SessionEntry]) -> SessionBranchSummary:
    """Place the active path in the session tree for the session-tree line.

    Args:
        entries (Sequence[SessionEntry]): Every entry of the session, in append order.

    Returns:
        SessionBranchSummary: The deepest branch the active path takes, or the
        branches that split off it when it follows the original line throughout.
    """
    tree = resolve_session_tree(entries)
    children: dict[str | None, list[str]] = {}
    for node in tree.nodes:
        children.setdefault(node.parent_id, []).append(node.id)
    turns = 0
    branched: SessionBranchSummary | None = None
    branches = 0
    parent_id: str | None = None
    for node in tree.active_path:
        # `turns` counts the user turns before `node`: those shared at its parent.
        alternatives = children.get(parent_id, [])
        if len(alternatives) > 1:
            branches += len(alternatives) - 1
            if alternatives[0] != node.id:
                branched = SessionBranchSummary(
                    branched_from_turn=turns, siblings=len(alternatives) - 1
                )
        if isinstance(node, MessageSessionEntry) and node.message.role == "user":
            turns += 1
        parent_id = node.id
    return branched or SessionBranchSummary(branches=branches)


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


def _latest_observation(entries: Sequence[SessionEntry]) -> ContextObservation | None:
    """Return the newest context observation after the last compaction boundary."""

    boundary = max(
        (index for index, entry in enumerate(entries) if entry.kind == "compaction"),
        default=-1,
    )
    return next(
        (
            entry.message.context_observation
            for entry in reversed(entries[boundary + 1 :])
            if isinstance(entry, MessageSessionEntry)
            and entry.message.role != "system"
            and entry.message.context_observation is not None
        ),
        None,
    )


__all__ = ["build_session_stats"]
