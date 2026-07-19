from __future__ import annotations

import pytest

from wisp.agent.context import build_context_budget, context_fingerprint, estimate_context
from wisp.agent.messages import CompactionRecord, Message, SessionEntry
from wisp.coding.compaction import should_auto_compact
from wisp.coding.stats import build_session_stats
from wisp.events import (
    ContextEstimated,
    SessionStatsReported,
    TokenUsage,
    wisp_event_from_json,
)
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.sessions.replay import replay_session_entries


def _usage(input_tokens: int, output_tokens: int, total_tokens: int) -> TokenUsage:
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def test_context_estimate_accounts_for_system_messages_tools_and_results() -> None:
    messages = (
        Message(role="system", content="system"),
        Message(role="user", content="hello"),
    )
    tools = (
        ToolSpec(
            name="lookup",
            description="Look up a value",
            input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        ),
    )

    initial = estimate_context(messages, tools)
    continued = estimate_context(
        messages,
        tools,
        (ToolCallResult(call_id="call-1", output="a large tool result" * 20),),
    )

    assert initial.system_tokens > 0
    assert initial.message_tokens > 0
    assert initial.tool_schema_tokens > 0
    assert initial.total_tokens == (
        initial.system_tokens + initial.message_tokens + initial.tool_schema_tokens
    )
    assert continued.message_tokens > initial.message_tokens
    assert continued.total_tokens > initial.total_tokens


def test_context_budget_is_permissive_for_unknown_models_and_tracks_reserve() -> None:
    estimate = estimate_context((Message(role="user", content="x" * 400),))

    unknown = build_context_budget(estimate, context_window=None, reserve_tokens=16)
    constrained = build_context_budget(estimate, context_window=100, reserve_tokens=16)

    assert unknown.context_window is None
    assert unknown.remaining_tokens is None
    assert unknown.over_budget is None
    assert constrained.remaining_tokens == 100 - 16 - estimate.total_tokens
    assert constrained.over_budget is (constrained.remaining_tokens <= 0)


@pytest.mark.parametrize(
    ("enabled", "window", "observed", "current", "estimated", "expected"),
    [
        (True, 100, 79, True, 1_000, False),
        (True, 100, 80, True, 1_000, False),
        (True, 100, 81, True, 1, True),
        (False, 100, 81, True, 1_000, False),
        (True, None, None, False, 1_000_000, False),
        (True, 100, None, False, 81, True),
        (True, 100, 79, False, 81, True),
    ],
)
def test_auto_compaction_uses_strict_threshold_and_current_observation(
    enabled: bool,
    window: int | None,
    observed: int | None,
    current: bool,
    estimated: int,
    expected: bool,
) -> None:
    estimate = estimate_context((Message(role="user", content="x" * (estimated * 4)),))
    estimate = estimate.model_copy(update={"total_tokens": estimated})
    budget = build_context_budget(
        estimate,
        context_window=window,
        reserve_tokens=20,
        observed_tokens=observed,
        observed_is_current=current,
    )

    assert should_auto_compact(budget, enabled=enabled) is expected


def test_context_statistics_events_accept_schema_v9_and_current() -> None:
    estimate = estimate_context((Message(role="user", content="hello"),))
    budget = build_context_budget(estimate, context_window=1_000, reserve_tokens=100)
    estimated = ContextEstimated(turn=1, provider="test", model="model", budget=budget)
    stats = build_session_stats(
        session_id=None,
        entries=(),
        replay=replay_session_entries(()),
        provider_messages=(),
        tools=(),
        context_window=None,
        reserve_tokens=100,
    )
    reported = SessionStatsReported(command_id="stats-1", stats=stats)

    assert wisp_event_from_json(estimated.model_dump_json()) == estimated
    assert wisp_event_from_json(reported.model_dump_json()) == reported
    assert (
        wisp_event_from_json(
            estimated.model_copy(update={"schema_version": 9}).model_dump_json()
        ).schema_version
        == 9
    )
    with pytest.raises(ValueError, match="require schema_version 9 or 10"):
        wisp_event_from_json(estimated.model_copy(update={"schema_version": 8}).model_dump_json())


def test_session_stats_sum_authoritative_usage_and_invalidate_pre_compaction_observation() -> None:
    old_user = SessionEntry(
        id="old-user", session_id="s", message=Message(role="user", content="a")
    )
    old_assistant = SessionEntry(
        id="old-assistant",
        session_id="s",
        message=Message(
            role="assistant",
            content="b",
            finish_reason="stop",
            usage=_usage(40, 10, 75),
        ),
    )
    retained_user = SessionEntry(
        id="retained-user", session_id="s", message=Message(role="user", content="c")
    )
    retained_assistant = SessionEntry(
        id="retained-assistant",
        session_id="s",
        message=Message(
            role="assistant",
            content="d",
            finish_reason="stop",
            usage=_usage(20, 5, 40),
        ),
    )
    compaction = SessionEntry(
        id="compact",
        session_id="s",
        kind="compaction",
        compaction=CompactionRecord(
            summary="Earlier work",
            replaced_entry_ids=("old-user", "old-assistant"),
            provider="test",
            usage=_usage(10, 5, 30),
        ),
    )
    entries = (old_user, old_assistant, retained_user, retained_assistant, compaction)
    replay = replay_session_entries(entries)

    stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=1_000,
        reserve_tokens=100,
    )

    assert stats.usage_record_count == 3
    assert stats.usage.input_tokens == 70
    assert stats.usage.output_tokens == 20
    assert stats.usage.total_tokens == 145
    assert stats.compaction_count == 1
    assert stats.active_message_count == 3
    assert stats.context.observed_tokens is None
    assert stats.context.observed_is_current is False


def test_session_stats_use_latest_post_compaction_assistant_observation() -> None:
    user = SessionEntry(id="user", session_id="s", message=Message(role="user", content="a"))
    assistant = SessionEntry(
        id="assistant",
        session_id="s",
        message=Message(role="assistant", content="b", finish_reason="stop"),
    )
    compaction = SessionEntry(
        id="compact",
        session_id="s",
        kind="compaction",
        compaction=CompactionRecord(
            summary="Earlier work",
            replaced_entry_ids=("user",),
            provider="test",
        ),
    )
    # The retained assistant alone is invalid replay, so retain a complete second turn.
    retained_user = SessionEntry(
        id="retained-user", session_id="s", message=Message(role="user", content="c")
    )
    retained_assistant = SessionEntry(
        id="retained-assistant",
        session_id="s",
        message=Message(role="assistant", content="d", finish_reason="stop"),
    )
    compaction = compaction.model_copy(
        update={
            "compaction": compaction.compaction.model_copy(
                update={"replaced_entry_ids": ("user", "assistant")}
            )
        }
    )
    post_user = SessionEntry(
        id="post-user", session_id="s", message=Message(role="user", content="e")
    )
    post_assistant = SessionEntry(
        id="post-assistant",
        session_id="s",
        message=Message(
            role="assistant",
            content="f",
            finish_reason="stop",
            usage=_usage(12, 3, 21),
        ),
    )
    entries = (
        user,
        assistant,
        retained_user,
        retained_assistant,
        compaction,
        post_user,
        post_assistant,
    )
    replay = replay_session_entries(entries)
    fingerprint = context_fingerprint(replay.messages)

    stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=None,
        reserve_tokens=16,
        observed_tokens=21,
        observed_is_current=True,
        observed_entry_id="post-assistant",
        observed_context_fingerprint=fingerprint,
    )

    assert stats.context.observed_tokens == 21
    assert stats.context.observed_is_current is True

    changed = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=(*replay.messages, Message(role="system", content="changed")),
        tools=(),
        context_window=None,
        reserve_tokens=16,
        observed_tokens=21,
        observed_is_current=True,
        observed_entry_id="post-assistant",
        observed_context_fingerprint=fingerprint,
    )
    assert changed.context.observed_is_current is False
