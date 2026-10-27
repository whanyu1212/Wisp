from __future__ import annotations

import json
from pathlib import Path

import anyio

from tests.support.paths import FIXTURES_DIR
from tests.support.session_entries import linked_entries
from wisp.agent.context_budget import observe_context
from wisp.coding.prompt import PromptPart, assemble_custom_prompt, assemble_prompt
from wisp.coding.prompt.instructions import PLAN_MODE_SYSTEM_PROMPT, WEB_SEARCH_GUIDANCE
from wisp.coding.session import CodingSession
from wisp.coding.stats import build_session_stats
from wisp.events import (
    ContextBreakdown,
    SessionStats,
    SessionStatsReported,
    ToolCallSnapshot,
    wisp_event_from_dict,
)
from wisp.messages import CompactionRecord, Message
from wisp.providers.base import ToolSpec
from wisp.providers.fake import FakeProvider
from wisp.sessions.entries import CompactionSessionEntry, MessageSessionEntry, SessionEntry
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.sessions.replay import replay_session_entries
from wisp.tools.context import ToolContext


def _tool(name: str, description: str = "Does a thing.") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
    )


TOOLS = (
    _tool("read"),
    _tool("bash", "Run a shell command." * 10),
    _tool("mcp__github__list_prs"),
    _tool("mcp__github__get_pr"),
    _tool("mcp__linear__get_issue"),
)


def _entry(message: Message, operation: str | None) -> MessageSessionEntry:
    return MessageSessionEntry(session_id="s", operation_id=operation, message=message)


def _history() -> tuple[SessionEntry, ...]:
    call = ToolCallSnapshot(
        call_id="call-1", name="bash", arguments={"command": "gh pr checks 766 --watch"}
    )
    return linked_entries(
        (
            _entry(Message(role="user", content="why does it time out"), "run-1"),
            _entry(Message(role="assistant", content="Looking."), "run-1"),
            _entry(Message(role="user", content="also check CI"), "run-1"),
            _entry(Message(role="user", content="push the fix"), "run-2"),
            _entry(Message(role="assistant", content="", tool_calls=(call,)), "run-2"),
            _entry(
                Message(
                    role="tool",
                    content="pass\n" * 400,
                    tool_call_id="call-1",
                    tool_name="bash",
                ),
                "run-2",
            ),
            _entry(Message(role="assistant", content="Pushed."), "run-2"),
        )
    )


def _stats(
    entries: tuple[SessionEntry, ...],
    prompt_messages: tuple[Message, ...],
    prompt_parts: tuple[tuple[PromptPart, ...], ...],
    *,
    observe: bool = False,
) -> SessionStats:
    replay = replay_session_entries(entries)
    provider_messages = (*prompt_messages, *replay.messages)
    if observe:
        observation = observe_context(
            provider_messages, TOOLS, provider="test", model="m", input_tokens=50_000
        )
        last = entries[-1]
        assert isinstance(last, MessageSessionEntry)
        message = last.message.model_copy(update={"context_observation": observation})
        entries = (*entries[:-1], last.model_copy(update={"message": message}))
        replay = replay_session_entries(entries)
    return build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=provider_messages,
        tools=TOOLS,
        context_window=272_000,
        reserve_tokens=40_000,
        provider="test",
        model="m",
        prompt_parts=prompt_parts,
    )


def _breakdown(stats: SessionStats) -> ContextBreakdown:
    assert stats.context_breakdown is not None
    return stats.context_breakdown


def _rows(breakdown: ContextBreakdown) -> dict[str, int]:
    return {usage.source: usage.tokens for usage in breakdown.sources}


def _assert_sums(stats: SessionStats) -> None:
    breakdown = _breakdown(stats)
    total = stats.context.effective_tokens
    assert sum(usage.tokens for usage in breakdown.sources) == total
    assert sum(turn.tokens for turn in breakdown.turns) == total
    for usage in breakdown.sources:
        if usage.children:
            assert sum(child.tokens for child in usage.children) == usage.tokens


def _prompt(tmp_path: Path, *, mode: str = "build") -> tuple[tuple[Message, ...], tuple]:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("Root rules.\n" * 50, encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("Package rules.\n", encoding="utf-8")
    prompt = assemble_prompt(
        cwd=tmp_path / "pkg",
        tools=TOOLS,
        additional_guidance=(
            PromptPart("skills", "skill index", "[WISP SKILLS]\n- review: Review code."),
            PromptPart("project_context", "web search", WEB_SEARCH_GUIDANCE),
        ),
        mode="plan" if mode == "plan" else "build",
        trusted_context_root=tmp_path,
        repository_status="git: unavailable",
    )
    return prompt.messages, prompt.parts


def test_estimated_breakdown_sums(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)

    stats = _stats(_history(), messages, parts)

    assert stats.context.accounting_method == "fully_estimated"
    assert _breakdown(stats).scaled is False
    _assert_sums(stats)
    assert list(_rows(_breakdown(stats))) == [
        "messages",
        "built_in_tools",
        "mcp_tools",
        "system_prompt",
        "skills",
        "project_instructions",
        "project_context",
    ]


def test_observed_breakdown_scales(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)

    stats = _stats(_history(), messages, parts, observe=True)

    assert stats.context.effective_tokens == 50_000
    assert _breakdown(stats).scaled is True
    _assert_sums(stats)


def test_breakdown_children(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path, mode="plan")

    sources = {
        usage.source: usage for usage in _breakdown(_stats(_history(), messages, parts)).sources
    }

    mcp = sources["mcp_tools"]
    assert [(child.label, child.count) for child in mcp.children] == [
        ("github", 2),
        ("linear", 1),
    ]
    assert mcp.count == 3
    assert [child.label for child in sources["built_in_tools"].children] == ["read", "bash"]
    assert [child.label for child in sources["project_instructions"].children] == [
        "AGENTS.md",
        "pkg/AGENTS.md",
    ]
    assert [child.label for child in sources["messages"].children] == [
        "user",
        "assistant",
        "tool output",
    ]
    assert [child.label for child in sources["system_prompt"].children] == [
        "core instructions",
        "instruction boundary",
        "plan mode",
    ]
    assert [child.label for child in sources["project_context"].children] == [
        "cwd",
        "git status",
        "project files",
        "tool summary",
        "web search",
    ]


def test_custom_prompt_row() -> None:
    prompt = assemble_custom_prompt(
        (
            Message(role="system", content="Custom policy."),
            Message(role="system", content="More custom policy."),
        ),
        additional_guidance=(
            PromptPart("skills", "skill index", "[WISP SKILLS]"),
            PromptPart("project_context", "web search", WEB_SEARCH_GUIDANCE),
        ),
        mode="plan",
    )

    stats = _stats(_history(), prompt.messages, prompt.parts)
    sources = {usage.source: usage for usage in _breakdown(stats).sources}

    _assert_sums(stats)
    assert [child.label for child in sources["custom_prompt"].children] == ["custom prompt"]
    assert [child.label for child in sources["project_context"].children] == ["web search"]
    assert [child.label for child in sources["system_prompt"].children] == ["plan mode"]
    assert sources["project_instructions"].tokens == 0
    assert PLAN_MODE_SYSTEM_PROMPT in prompt.messages[-1].content


def test_turns_group_by_run(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)

    breakdown = _breakdown(_stats(_history(), messages, parts))

    assert [turn.turn for turn in breakdown.turns] == [1, 2]
    largest = breakdown.largest[0]
    assert largest.label == "bash gh pr checks 766 --watch output"
    assert largest.turn == 2
    assert len(breakdown.largest) == 3


def test_rust_fixture_decodes() -> None:
    fixture = json.loads((FIXTURES_DIR / "rust_tui_context.json").read_text(encoding="utf-8"))

    event = wisp_event_from_dict(fixture["session.stats"])

    assert isinstance(event, SessionStatsReported)
    breakdown = _breakdown(event.stats)
    assert sum(usage.tokens for usage in breakdown.sources) == event.stats.context.effective_tokens


def test_folded_history_keeps_tool_output(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)
    entries = _history()
    replay = replay_session_entries(entries)
    # Providers without native tool history send each exchange as one assistant message.
    folded = Message(role="assistant", content="\n".join(m.content for m in replay.messages))

    stats = build_session_stats(
        session_id="s",
        entries=entries,
        replay=replay,
        provider_messages=(*messages, folded),
        tools=TOOLS,
        context_window=272_000,
        reserve_tokens=40_000,
        prompt_parts=parts,
    )

    _assert_sums(stats)
    messages_row = _breakdown(stats).sources[0]
    assert [(child.label, child.count) for child in messages_row.children] == [
        ("user", 3),
        ("assistant", 3),
        ("tool output", 1),
    ]


def test_turns_after_compaction(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)
    history = _history()
    compaction = CompactionSessionEntry(
        session_id="s",
        compaction=CompactionRecord(
            summary="Earlier work. " * 500,
            replaced_entry_ids=tuple(
                entry.id for entry in history if entry.operation_id == "run-1"
            ),
            provider="test",
        ),
    )
    entries = linked_entries((*history, compaction))

    stats = _stats(entries, messages, parts)

    # Run 1 was summarized; its summary counts toward the first turn left, run 2.
    _assert_sums(stats)
    breakdown = _breakdown(stats)
    assert [turn.turn for turn in breakdown.turns] == [2]
    labels = {item.label: item.turn for item in breakdown.largest}
    assert labels["compaction summary"] == 2


def test_empty_session_turns(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)

    stats = _stats((), messages, parts)

    breakdown = _breakdown(stats)
    assert breakdown.turns == ()
    assert breakdown.largest == ()
    assert sum(usage.tokens for usage in breakdown.sources) == stats.context.effective_tokens


def test_turns_without_operations(tmp_path: Path) -> None:
    messages, parts = _prompt(tmp_path)
    entries = linked_entries(
        tuple(
            _entry(entry.message, None)
            for entry in _history()
            if isinstance(entry, MessageSessionEntry)
        )
    )

    breakdown = _breakdown(_stats(entries, messages, parts))

    # Without operations, every user message starts a turn, steers included.
    assert [turn.turn for turn in breakdown.turns] == [1, 2, 3]


def test_session_reports_breakdown(tmp_path: Path) -> None:
    async def scenario() -> SessionStats:
        store = JsonlSessionStore(tmp_path / "sessions")
        session = store.create()
        await session.append_message(Message(role="user", content="hello"))
        await session.append_message(Message(role="assistant", content="hi", finish_reason="stop"))
        agent = CodingSession(
            provider=FakeProvider(),
            sessions=store,
            tool_context=ToolContext(cwd=tmp_path),
            prompt_messages=(Message(role="system", content="Custom policy."),),
        )
        return await agent.get_session_stats(session)

    stats = anyio.run(scenario)

    _assert_sums(stats)
    sources = [usage.source for usage in _breakdown(stats).sources]
    assert "custom_prompt" in sources
