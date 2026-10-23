from __future__ import annotations

import json

import pytest

from tests.support.session_entries import linked_entries
from wisp.agent.context_budget import (
    build_context_budget,
    estimate_context,
    observe_context,
)
from wisp.coding.compaction import should_auto_compact
from wisp.coding.stats import RunStats, build_session_stats
from wisp.events import (
    CompactionPolicyStatus,
    ContextEstimate,
    ContextEstimated,
    MessageCompleted,
    MessageStarted,
    SessionStatsReported,
    TokenUsage,
    ToolCallSnapshot,
    ToolResultReady,
    wisp_event_from_json,
)
from wisp.messages import CompactionRecord, Message
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntry,
)
from wisp.sessions.replay import replay_session_entries
from wisp.skills.models import SkillInvocationEvidence


def test_used_skills_follow_successful_loads_and_the_active_branch_through_compaction() -> None:
    def entry(identifier: str, message: Message) -> MessageSessionEntry:
        return MessageSessionEntry(id=identifier, session_id="s", message=message)

    def invocation(name: str) -> Message:
        return Message(
            role="user",
            content="expanded instructions",
            skill_invocation=SkillInvocationEvidence(
                name=name,
                original_content=f"/skill:{name} help",
                request="help",
                content_sha256="a" * 64,
            ),
        )

    def call(identifier: str, name: str) -> ToolCallSnapshot:
        return ToolCallSnapshot(call_id=identifier, name="skill", arguments={"name": name})

    entries: tuple[SessionEntry, ...] = linked_entries(
        (
            entry("explicit", invocation("review")),
            entry(
                "calls",
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(
                        call("ok", "build"),
                        call("failed", "broken"),
                        call("pending", "waiting"),
                        call("duplicate", "review"),
                        call("cancelled", "cancelled"),
                    ),
                ),
            ),
            entry(
                "success",
                Message(
                    role="tool",
                    content="instructions",
                    tool_name="skill",
                    tool_call_id="ok",
                    is_error=False,
                ),
            ),
            entry(
                "error",
                Message(
                    role="tool",
                    content="missing",
                    tool_name="skill",
                    tool_call_id="failed",
                    is_error=True,
                ),
            ),
            entry(
                "duplicate",
                Message(
                    role="tool",
                    content="instructions",
                    tool_name="skill",
                    tool_call_id="duplicate",
                    is_error=False,
                ),
            ),
            entry(
                "cancelled",
                Message(
                    role="tool", content="cancelled", tool_name="skill", tool_call_id="cancelled"
                ),
            ),
            entry("later", Message(role="user", content="continue")),
            entry("answer", Message(role="assistant", content="done", finish_reason="stop")),
            CompactionSessionEntry(
                id="compact",
                session_id="s",
                compaction=CompactionRecord(
                    summary="Previous work",
                    replaced_entry_ids=(
                        "explicit",
                        "calls",
                        "success",
                        "error",
                        "duplicate",
                        "cancelled",
                    ),
                    provider="test",
                ),
            ),
            entry("sibling", invocation("sibling-only")),
        )
    )
    entries += (
        ActiveLeafSessionEntry(
            id="switch", session_id="s", previous_leaf_id="sibling", active_leaf_id="compact"
        ),
    )
    replay = replay_session_entries(entries)
    assert all(message.skill_invocation is None for message in replay.messages)
    stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=10000,
        reserve_tokens=100,
    )
    assert stats.used_skill_names == ("review", "build")
    # Reading again after serialization models resume, including branch selection.
    restored = tuple(type(item).model_validate_json(item.model_dump_json()) for item in entries)
    replay = replay_session_entries(restored)
    assert build_session_stats(
        session_id="s",
        entries=restored,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=10000,
        reserve_tokens=100,
    ).used_skill_names == ("review", "build")


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


def test_estimate_accepts_old_method_values() -> None:
    legacy = {
        "method": "chars_div_4_v1",
        "system_tokens": 1,
        "message_tokens": 2,
        "tool_schema_tokens": 3,
        "total_tokens": 6,
    }

    estimate = ContextEstimate.model_validate(legacy)

    assert estimate.method == "chars_div_4_v1"


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
def test_auto_compaction_uses_strict_threshold_and_latest_count(
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
def test_auto_compaction_skips_when_reserve_fills_window(reserve: int) -> None:
    estimate = estimate_context((Message(role="user", content="hello"),))
    budget = build_context_budget(
        estimate,
        context_window=100,
        reserve_tokens=reserve,
    )

    assert should_auto_compact(budget, enabled=True) is False


def test_stats_ignore_saved_count_after_provider_change() -> None:
    prefix = (Message(role="user", content="hello"),)
    observation = observe_context(prefix, provider="old", model="model", input_tokens=12)
    entries: tuple[SessionEntry, ...] = (
        MessageSessionEntry(id="user", session_id="s", message=prefix[0]),
        MessageSessionEntry(
            id="assistant",
            session_id="s",
            message=Message(
                role="assistant",
                content="answer",
                finish_reason="stop",
                context_observation=observation,
            ),
        ),
    )
    entries = linked_entries(entries)
    replay = replay_session_entries(entries)

    stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=100,
        reserve_tokens=10,
        provider="new",
        model="model",
    )

    assert stats.context.observed_is_current is False
    assert stats.context.accounting_method == "fully_estimated"


def test_stats_report_threshold_policy_eligibility() -> None:
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
    entries = linked_entries(entries)
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
def test_stats_explain_unavailable_threshold_policy(
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


def test_stats_events_round_trip_without_policy() -> None:
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
    # Older writers omitted the compaction policy block; absence reads as "unknown".
    without_policy = json.loads(reported.model_dump_json())
    del without_policy["stats"]["compaction"]
    restored = wisp_event_from_json(json.dumps(without_policy))
    assert isinstance(restored, SessionStatsReported)
    assert restored.stats.compaction is None


def test_stats_sum_reported_usage_and_drop_pre_compaction_count() -> None:
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
    entries = linked_entries(entries)
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


def test_stats_sum_cache_usage_only_when_always_reported() -> None:
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
    entries = linked_entries(entries)
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


def test_stats_use_latest_count_after_compaction() -> None:
    def assistant(entry_id: str, content: str, observed: int | None = None) -> MessageSessionEntry:
        observation = (
            observe_context((), provider="test", model="model", input_tokens=observed)
            if observed is not None
            else None
        )
        return MessageSessionEntry(
            id=entry_id,
            session_id="s",
            message=Message(
                role="assistant",
                content=content,
                finish_reason="stop",
                context_observation=observation,
            ),
        )

    def user(entry_id: str, content: str) -> MessageSessionEntry:
        return MessageSessionEntry(
            id=entry_id, session_id="s", message=Message(role="user", content=content)
        )

    compaction = CompactionSessionEntry(
        id="compact",
        session_id="s",
        compaction=CompactionRecord(
            summary="Earlier work",
            replaced_entry_ids=("user", "assistant"),
            provider="test",
        ),
    )
    # The retained assistant alone is invalid replay, so retain a complete second turn.
    # Its observation predates the compaction and must not describe the active context.
    entries = linked_entries(
        (
            user("user", "a"),
            assistant("assistant", "b"),
            user("retained-user", "c"),
            assistant("retained-assistant", "d", observed=99),
            compaction,
            user("post-user", "e"),
            assistant("post-assistant", "f"),
        )
    )
    request = replay_session_entries(entries).messages[:-1]
    observed = observe_context(request, provider="test", model="model", input_tokens=21)
    post_assistant = entries[-1]
    assert isinstance(post_assistant, MessageSessionEntry)
    entries = (
        *entries[:-1],
        post_assistant.model_copy(
            update={
                "message": post_assistant.message.model_copy(
                    update={"context_observation": observed}
                )
            }
        ),
    )
    replay = replay_session_entries(entries)

    stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=replay.messages,
        tools=(),
        context_window=None,
        reserve_tokens=16,
    )

    assert stats.context.observed_tokens == 21
    assert stats.context.observed_is_current is True


def test_stats_use_branch_count_but_lifetime_usage() -> None:
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
            context_observation=observe_context(
                (Message(role="user", content="question"),),
                provider="test",
                model="model",
                input_tokens=40,
            ),
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
            context_observation=observe_context(
                (Message(role="user", content="question"),),
                provider="test",
                model="model",
                input_tokens=21,
            ),
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
    )

    assert stats.usage.total_tokens == 96
    assert stats.context.observed_tokens == 21
    assert stats.context.observed_is_current is True


def test_run_stats_totals() -> None:
    stats = RunStats()
    usage = TokenUsage(input_tokens=5, output_tokens=7, total_tokens=12)
    stats.observe(MessageStarted(turn=1))
    stats.observe(MessageCompleted(turn=1, content="a", finish_reason="tool_calls", usage=usage))
    stats.observe(ToolResultReady(call_id="c", name="read", output="x", is_error=False))
    stats.observe(MessageCompleted(turn=2, content="b", finish_reason="stop"))
    stats.observe(
        MessageCompleted(
            turn=2,
            content="c",
            finish_reason="stop",
            usage=usage.model_copy(update={"output_tokens": 3}),
        )
    )

    completed = stats.completed(session_id="s", turns=2, outcome="completed")

    assert (completed.tool_calls, completed.output_tokens) == (1, 10)
    assert completed.duration_ms is not None
    assert completed.duration_ms >= 0
    # Only the response that started was timed.
    assert completed.model_ms is not None
    assert 0 <= completed.model_ms <= completed.duration_ms


def test_run_stats_times_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([10.0, 12.5, 20.0, 21.0, 22.0, 30.0])
    monkeypatch.setattr("wisp.coding.stats.time.monotonic", lambda: next(clock))
    usage = TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    stats = RunStats(started=0.0)
    stats.observe(MessageStarted(turn=1))
    stats.observe(MessageCompleted(turn=1, content="a", finish_reason="tool_calls", usage=usage))
    # Tool time between responses is left out.
    stats.observe(MessageStarted(turn=2))
    stats.observe(MessageCompleted(turn=2, content="b", finish_reason="stop", usage=usage))
    # A failed response reports no usage, so its time is left out too.
    stats.observe(MessageStarted(turn=3))
    stats.observe(MessageCompleted(turn=3, content="", finish_reason="error"))

    completed = stats.completed(session_id="s", turns=3, outcome="completed")

    assert (completed.model_ms, completed.duration_ms) == (3500, 30000)


def test_run_stats_without_usage() -> None:
    completed = RunStats().completed(session_id="s", turns=0, outcome="failed")

    assert (completed.tool_calls, completed.output_tokens, completed.model_ms) == (0, None, None)


def test_branch_summary_places_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import anyio

    from wisp.coding.stats import session_branch_summary
    from wisp.events import SessionBranchSummary
    from wisp.sessions.jsonl import JsonlSessionStore

    session = JsonlSessionStore(tmp_path).create()

    async def turn(prompt: str) -> tuple[str, str]:
        user = await session.append_message(Message(role="user", content=prompt))
        answer = await session.append_message(Message(role="assistant", content="ok"))
        return user.id, answer.id

    async def navigate(entry_id: str) -> None:
        await session.navigate_tree(entry_id, expected_active_leaf_id=session.read_active_leaf_id())

    def summary() -> SessionBranchSummary:
        return session_branch_summary(session.read_entries())

    async def scenario() -> None:
        await turn("one")
        await turn("two")
        third, original_leaf = await turn("three")
        assert summary() == SessionBranchSummary()
        # Re-asking turn three branches after turn two.
        await navigate(third)
        await turn("three again")
        assert summary() == SessionBranchSummary(branched_from_turn=2, siblings=1)
        await navigate(third)
        await turn("three once more")
        assert summary() == SessionBranchSummary(branched_from_turn=2, siblings=2)
        # Back on the original line, the branches split off it.
        await navigate(original_leaf)
        assert summary() == SessionBranchSummary(branches=2)

    anyio.run(scenario)
