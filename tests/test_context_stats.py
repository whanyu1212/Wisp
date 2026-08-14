from __future__ import annotations

import json
import math

import pytest

from wisp.agent.context import build_context_budget, context_fingerprint, estimate_context
from wisp.agent.messages import CompactionRecord, Message
from wisp.coding.compaction import should_auto_compact
from wisp.coding.stats import build_session_stats
from wisp.events import (
    CompactionPolicyStatus,
    ContextEstimated,
    SessionStatsReported,
    TokenUsage,
    wisp_event_from_json,
)
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntry,
)
from wisp.sessions.replay import replay_session_entries


def _usage(
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    *,
    cache_read: int | None = None,
    cache_write: int | None = None,
    reasoning: int | None = None,
) -> TokenUsage:
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        reasoning_output_tokens=reasoning,
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


def _serialized_tokens(payload: object) -> int:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return math.ceil(len(text.encode("utf-8")) / 4)


def test_context_estimate_preserves_ascii_heuristic() -> None:
    message = Message(role="user", content="plain ASCII")
    payload = [{"role": "user", "content": "plain ASCII"}]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    estimate = estimate_context((message,))

    assert len(serialized) == len(serialized.encode("utf-8"))
    assert estimate.message_tokens == math.ceil(len(serialized) / 4)


@pytest.mark.parametrize("content", ["漢字かな", "👩‍💻🚀", "e\u0301", "ASCII と emoji 🌍"])
def test_context_estimate_uses_utf8_size_for_unicode_messages(content: str) -> None:
    payload = [{"role": "user", "content": content}]

    first = estimate_context((Message(role="user", content=content),))
    second = estimate_context((Message(role="user", content=content),))

    assert first == second
    assert first.message_tokens == _serialized_tokens(payload)
    assert first.message_tokens >= math.ceil(
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)) / 4
    )


def test_context_estimate_uses_utf8_size_for_every_payload_category() -> None:
    messages = (
        Message(role="system", content="系统 🌐"),
        Message(role="user", content="質問 e\u0301"),
    )
    result = ToolCallResult(call_id="呼出", output="結果 ✅", is_error=False)
    tool = ToolSpec(
        name="検索",
        description="探す 🔎",
        input_schema={"type": "object", "properties": {"値": {"type": "string"}}},
    )

    estimate = estimate_context(messages, (tool,), (result,))

    assert estimate.system_tokens == _serialized_tokens([{"role": "system", "content": "系统 🌐"}])
    assert estimate.message_tokens == _serialized_tokens(
        [
            {"role": "user", "content": "質問 e\u0301"},
            {"call_id": "呼出", "output": "結果 ✅", "is_error": False},
        ]
    )
    assert estimate.tool_schema_tokens == _serialized_tokens(
        [
            {
                "name": "検索",
                "description": "探す 🔎",
                "input_schema": {
                    "type": "object",
                    "properties": {"値": {"type": "string"}},
                },
            }
        ]
    )
    assert estimate.total_tokens == (
        estimate.system_tokens + estimate.message_tokens + estimate.tool_schema_tokens
    )


def test_unicode_context_fingerprint_remains_compatible() -> None:
    messages = (
        Message(role="system", content="系统"),
        Message(role="user", content="café 👩‍💻 e\u0301"),
    )
    tools = (
        ToolSpec(
            name="検索",
            description="探す 🔎",
            input_schema={"type": "object", "properties": {"値": {"type": "string"}}},
        ),
    )

    assert context_fingerprint(messages, tools) == (
        "6b0bd768cecdf716c02766c3fdc84a68853b1b9cdc86b49355caba3980e1c2cd"
    )


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


@pytest.mark.parametrize("reserve", [100, 101, 16_384])
def test_auto_compaction_skips_when_reserve_consumes_context_window(reserve: int) -> None:
    estimate = estimate_context((Message(role="user", content="hello"),))
    budget = build_context_budget(
        estimate,
        context_window=100,
        reserve_tokens=reserve,
    )

    assert should_auto_compact(budget, enabled=True) is False


def test_session_stats_reports_threshold_policy_eligibility() -> None:
    entries: tuple[SessionEntry, ...] = (
        MessageSessionEntry(
            id="user-1", session_id="s", message=Message(role="user", content="one")
        ),
        MessageSessionEntry(
            id="assistant-1",
            session_id="s",
            message=Message(role="assistant", content="one answer", finish_reason="stop"),
        ),
        MessageSessionEntry(
            id="user-2", session_id="s", message=Message(role="user", content="two")
        ),
        MessageSessionEntry(
            id="assistant-2",
            session_id="s",
            message=Message(role="assistant", content="two answer", finish_reason="stop"),
        ),
    )
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

    assert stats.compaction == CompactionPolicyStatus(
        auto_compaction_enabled=True,
        threshold_eligible=True,
        threshold_ineligible_reason=None,
        overflow_recovery_enabled=True,
    )
    unknown_window_stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=None,
        reserve_tokens=100,
    )
    assert unknown_window_stats.compaction == CompactionPolicyStatus(
        threshold_ineligible_reason="model context window is unknown",
    )


@pytest.mark.parametrize(
    ("context_window", "reserve_tokens", "enabled", "reason", "overflow_enabled"),
    [
        (None, 100, True, "model context window is unknown", False),
        (100, 100, True, "reserve consumes the model window", False),
        (100, 10, True, "no compactable context prefix", False),
        (100, 10, False, "automatic compaction is disabled", False),
    ],
)
def test_session_stats_explains_unavailable_threshold_policy(
    context_window: int | None,
    reserve_tokens: int,
    enabled: bool,
    reason: str,
    overflow_enabled: bool,
) -> None:
    stats = build_session_stats(
        session_id=None,
        entries=(),
        replay=replay_session_entries(()),
        provider_messages=(),
        tools=(),
        context_window=context_window,
        reserve_tokens=reserve_tokens,
        auto_compaction_enabled=enabled,
    )

    assert stats.compaction.threshold_eligible is False
    assert stats.compaction.threshold_ineligible_reason == reason
    assert stats.compaction.overflow_recovery_enabled is overflow_enabled


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
    legacy_stats = reported.model_copy(update={"schema_version": 25})
    legacy_stats_payload = json.loads(legacy_stats.model_dump_json())
    assert "compaction" not in legacy_stats_payload["stats"]
    legacy_event = wisp_event_from_json(json.dumps(legacy_stats_payload))
    assert legacy_event.schema_version == 25
    assert isinstance(legacy_event, SessionStatsReported)
    assert legacy_event.stats.compaction is None
    stats_policy_payload = json.loads(reported.model_dump_json())
    stats_policy_payload["schema_version"] = 25
    with pytest.raises(ValueError, match="Session compaction policy requires schema_version 26"):
        wisp_event_from_json(json.dumps(stats_policy_payload))
    assert (
        wisp_event_from_json(
            estimated.model_copy(update={"schema_version": 9}).model_dump_json()
        ).schema_version
        == 9
    )
    with pytest.raises(ValueError, match="require schema_version 9 through 31"):
        wisp_event_from_json(estimated.model_copy(update={"schema_version": 8}).model_dump_json())


def test_session_stats_sum_authoritative_usage_and_invalidate_pre_compaction_observation() -> None:
    old_user = MessageSessionEntry(
        id="old-user", session_id="s", message=Message(role="user", content="a")
    )
    old_assistant = MessageSessionEntry(
        id="old-assistant",
        session_id="s",
        message=Message(
            role="assistant",
            content="b",
            finish_reason="stop",
            usage=_usage(40, 10, 75, cache_read=10, cache_write=5, reasoning=2),
        ),
    )
    retained_user = MessageSessionEntry(
        id="retained-user", session_id="s", message=Message(role="user", content="c")
    )
    retained_assistant = MessageSessionEntry(
        id="retained-assistant",
        session_id="s",
        message=Message(
            role="assistant",
            content="d",
            finish_reason="stop",
            usage=_usage(20, 5, 40, cache_read=4),
        ),
    )
    compaction = CompactionSessionEntry(
        id="compact",
        session_id="s",
        compaction=CompactionRecord(
            summary="Earlier work",
            replaced_entry_ids=("old-user", "old-assistant"),
            provider="test",
            usage=_usage(10, 5, 30, cache_write=3),
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
    assert stats.usage.cache_read_input_tokens is None
    assert stats.usage.cache_write_input_tokens is None
    assert stats.usage.reasoning_output_tokens == 2
    assert stats.compaction_count == 1
    assert stats.active_message_count == 3
    assert stats.context.observed_tokens is None
    assert stats.context.observed_is_current is False


def test_session_stats_sum_cache_usage_only_when_every_record_reports_it() -> None:
    first = MessageSessionEntry(
        id="assistant-1",
        session_id="s",
        message=Message(
            role="assistant",
            content="one",
            finish_reason="stop",
            usage=_usage(40, 10, 50, cache_read=10, cache_write=5),
        ),
    )
    second = MessageSessionEntry(
        id="assistant-2",
        session_id="s",
        message=Message(
            role="assistant",
            content="two",
            finish_reason="stop",
            usage=_usage(20, 5, 25, cache_read=4, cache_write=3),
        ),
    )
    entries: tuple[SessionEntry, ...] = (first, second)
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

    assert stats.usage.cache_read_input_tokens == 14
    assert stats.usage.cache_write_input_tokens == 8


def test_session_stats_use_latest_post_compaction_assistant_observation() -> None:
    user = MessageSessionEntry(id="user", session_id="s", message=Message(role="user", content="a"))
    assistant = MessageSessionEntry(
        id="assistant",
        session_id="s",
        message=Message(role="assistant", content="b", finish_reason="stop"),
    )
    compaction = CompactionSessionEntry(
        id="compact",
        session_id="s",
        compaction=CompactionRecord(
            summary="Earlier work",
            replaced_entry_ids=("user",),
            provider="test",
        ),
    )
    # The retained assistant alone is invalid replay, so retain a complete second turn.
    retained_user = MessageSessionEntry(
        id="retained-user", session_id="s", message=Message(role="user", content="c")
    )
    retained_assistant = MessageSessionEntry(
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
    post_user = MessageSessionEntry(
        id="post-user", session_id="s", message=Message(role="user", content="e")
    )
    post_assistant = MessageSessionEntry(
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


def test_session_stats_use_active_branch_observation_but_lifetime_usage() -> None:
    root = MessageSessionEntry(
        id="root",
        session_id="s",
        parent_id=None,
        message=Message(role="user", content="question"),
    )
    abandoned = MessageSessionEntry(
        id="abandoned",
        session_id="s",
        parent_id="root",
        message=Message(
            role="assistant",
            content="old answer",
            finish_reason="stop",
            usage=_usage(40, 10, 75),
        ),
    )
    selection = ActiveLeafSessionEntry(
        id="selection",
        session_id="s",
        previous_leaf_id="abandoned",
        active_leaf_id="root",
    )
    current = MessageSessionEntry(
        id="current",
        session_id="s",
        parent_id="root",
        message=Message(
            role="assistant",
            content="new answer",
            finish_reason="stop",
            usage=_usage(12, 3, 21),
        ),
    )
    entries: tuple[SessionEntry, ...] = (root, abandoned, selection, current)
    replay = replay_session_entries(entries)

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
        observed_entry_id="current",
        observed_context_fingerprint=context_fingerprint(replay.messages),
    )

    assert stats.usage.total_tokens == 96
    assert stats.context.observed_tokens == 21
    assert stats.context.observed_is_current is True
