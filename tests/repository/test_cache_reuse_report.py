from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from scripts.cache_reuse_report import (
    CACHE_KEYED_PROVIDERS,
    build_report,
    classify_prompt,
    classify_within_run,
    copied_entry_ids,
    estimate_tokens,
    main,
    reached,
    read_session_entries,
    split_prompts,
)
from wisp.agent.messages import CompactionRecord, Message
from wisp.events import ContextObservation, JsonObject, TokenUsage, ToolCallSnapshot
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
    MessageSessionEntry,
    PersistedEventEnvelope,
    SessionEntry,
    session_entry_to_json,
)

START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
STATIC = "You are Wisp. " * 400  # ~1.4k estimated tokens
CONTEXT = "[WISP PROJECT CONTEXT]\ncwd: /repo\ngit (snapshot): branch main, clean"


class SessionBuilder:
    """Append entries the way CodingSession does: system block, user, responses."""

    def __init__(self) -> None:
        self.entries: list[SessionEntry] = []
        self.leaf: str | None = None
        self.clock = START
        # Each run writes its system block, user message, and responses under one ID.
        self.operation_id: str | None = "prompt-0"
        self.prompt_count = 0
        self.file_count = 0
        self.fork_created_at: datetime | None = None

    def prompt(
        self,
        text: str,
        responses: list[tuple[int, int]],
        *,
        context: str = CONTEXT,
        system_sections: tuple[str, ...] | None = None,
        parent: str | None = None,
        minutes_later: float = 1,
    ) -> str:
        """Append one prompt; ``responses`` holds (input, cached) token pairs."""

        self.clock += timedelta(minutes=minutes_later)
        self.prompt_count += 1
        self.operation_id = f"prompt-{self.prompt_count}"
        if parent is not None:
            self.leaf = parent
        for section in (STATIC, context) if system_sections is None else system_sections:
            self._message(Message(role="system", content=section))
        self._message(Message(role="user", content=text))
        for input_tokens, cached_tokens in responses:
            self.response(input_tokens, cached_tokens)
        assert self.leaf is not None
        return self.leaf

    def response(
        self,
        input_tokens: int,
        cached_tokens: int | None,
        *,
        observed_input_tokens: int | None = None,
        provider: str = "anthropic",
    ) -> None:
        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=10,
            total_tokens=input_tokens + 10,
            cache_read_input_tokens=cached_tokens,
        )
        observation = ContextObservation(
            provider=provider,
            model="same-model",
            input_tokens=observed_input_tokens or input_tokens,
            message_count=1,
            context_fingerprint="f",
        )
        self._message(
            Message(role="assistant", content="ok", usage=usage, context_observation=observation)
        )

    def unmeasured_response(self) -> None:
        """Append a successful response whose provider reported no usage at all."""

        self._message(Message(role="assistant", content="ok"))

    def navigate(self, entry_id: str) -> None:
        """Select an older leaf, as tree navigation does, without appending a prompt."""

        self._append(
            ActiveLeafSessionEntry(
                session_id="s",
                previous_leaf_id=self.leaf,
                active_leaf_id=entry_id,
                reason="navigation",
                selected_entry_id=entry_id,
            ),
            leaf=entry_id,
        )

    def last_system_entry_id(self) -> str:
        return next(
            e.id
            for e in reversed(self.entries)
            if isinstance(e, MessageSessionEntry) and e.message.role == "system"
        )

    def compaction(self) -> None:
        assert self.leaf is not None
        record = CompactionRecord(summary="s", replaced_entry_ids=(self.leaf,), provider="fake")
        self._append(
            CompactionSessionEntry(
                session_id="s",
                parent_id=self.leaf,
                compaction=record,
                operation_id=self.operation_id,
            )
        )

    def _message(self, message: Message) -> None:
        self.clock += timedelta(seconds=1)
        message = message.model_copy(update={"created_at": self.clock})
        self._append(
            MessageSessionEntry(
                session_id="s",
                parent_id=self.leaf,
                message=message,
                created_at=self.clock,
                operation_id=self.operation_id,
            )
        )

    def _append(self, entry: SessionEntry, *, leaf: str | None = None) -> None:
        self.entries.append(entry)
        self.leaf = leaf or entry.id

    def tool_round(self, input_tokens: int, cached_tokens: int) -> None:
        """Append an assistant tool call and its result, as a tool turn does."""

        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=10,
            total_tokens=input_tokens + 10,
            cache_read_input_tokens=cached_tokens,
        )
        call = ToolCallSnapshot(call_id="call-1", name="read", arguments={})
        self._message(Message(role="assistant", content="", usage=usage, tool_calls=(call,)))
        self._message(Message(role="tool", content="ok", tool_call_id="call-1", tool_name="read"))

    def event(self, payload: dict[str, object]) -> None:
        """Append a persisted runtime event, e.g. ``context.pressure``."""

        self.clock += timedelta(seconds=1)
        self._append(
            EventSessionEntry(
                session_id="s",
                parent_id=self.leaf,
                event=PersistedEventEnvelope(payload=cast(JsonObject, payload)),
                created_at=self.clock,
                operation_id=self.operation_id,
            )
        )

    def steer(self, text: str) -> None:
        """Append a steering message inside the running prompt's operation."""

        self._message(Message(role="user", content=text))

    def file_name(self, created_at: datetime) -> str:
        """Return a store-style file name; each call gets a distinct id suffix."""

        self.file_count += 1
        suffix = "abcdef"[self.file_count - 1] * 8
        return f"{created_at:%Y%m%d-%H%M%S}-{suffix}.jsonl"

    def fork_file_name(self) -> str:
        assert self.fork_created_at is not None
        return self.file_name(self.fork_created_at)

    def write(self, path: Path, *, session_id: str = "s") -> Path:
        entries = [e.model_copy(update={"session_id": session_id}) for e in self.entries]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(session_entry_to_json(e) + "\n" for e in entries))
        return path


def outcomes(builder: SessionBuilder) -> list[str | None]:
    prompts = split_prompts(builder.entries, session="s")
    results = [classify_prompt(p, idle_minutes=60) for p in prompts]
    return [r.outcome if r else None for r in results]


@pytest.mark.parametrize(
    ("second_first_response", "expected"),
    [
        ((52_000, 51_700), "previous-tail"),
        ((52_000, 20_100), "previous-start"),
        ((52_000, 1_536), "instructions"),
        ((52_000, 35_000), "partial"),
        ((52_000, 0), "zero"),
    ],
)
def test_classifies_where_the_cache_stopped(
    second_first_response: tuple[int, int], expected: str
) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(20_000, 0), (35_000, 19_800), (51_800, 34_900)])
    builder.prompt("second", [second_first_response])

    assert outcomes(builder) == [None, expected]


@pytest.mark.parametrize(
    ("cached", "target", "expected"),
    [(1, 100, False), (100, 600, False), (95, 100, True), (51_700, 52_000, True)],
)
def test_reached_requires_most_of_a_short_request(cached: int, target: int, expected: bool) -> None:
    assert reached(cached, target) is expected


def test_markdown_escapes_table_delimiters_from_session_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)], system_sections=("[A|B] one",))
    builder.prompt("second", [(11_000, 0)], system_sections=("[A|B] two",))
    # Explicit PATH inputs can have any stem, including a table delimiter.
    builder.write(tmp_path / "run|1.jsonl")

    assert main([str(tmp_path), "--details"]) == 0
    output = capsys.readouterr().out

    # The label's `|` is escaped, so each row keeps its table's column count.
    assert "| system changed: [A\\|B] | 0 | 0 | 1 |" in output
    detail = next(line for line in output.splitlines() if line.startswith("| all |"))
    assert "| run\\|1 |" in detail
    assert detail.replace("\\|", "").count("|") == 12  # 11 columns


def test_reports_changed_system_section_compaction_and_idle_gap() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(20_000, 0)])
    builder.compaction()
    builder.response(8_000, 1_536)
    builder.prompt("second", [(9_000, 0)], context=CONTEXT + ", 1 changed", minutes_later=90)

    [prompt] = split_prompts(builder.entries, session="s")[1:]
    result = classify_prompt(prompt, idle_minutes=60)

    assert result is not None
    assert result.outcome == "zero"
    assert result.causes == (
        "system changed: [WISP PROJECT CONTEXT]",
        "compaction",
        "idle >= 60m",
    )


def test_unchanged_prompt_has_no_recorded_cause() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(20_000, 0)])
    builder.prompt("second", [(21_000, 0)])

    result = classify_prompt(split_prompts(builder.entries, session="s")[1], idle_minutes=60)

    assert result is not None
    assert result.causes == ()


def test_branched_prompt_is_compared_with_the_prompt_it_continues() -> None:
    builder = SessionBuilder()
    first_leaf = builder.prompt("first", [(10_000, 0)])
    builder.prompt("second", [(40_000, 9_900)])
    # Branch from the end of the first prompt: the cache should be judged against
    # the first prompt's 10k request, not the abandoned second prompt's 40k one.
    builder.prompt("branch", [(12_000, 9_900)], parent=first_leaf)

    prompts = split_prompts(builder.entries, session="s")

    assert prompts[2].previous is prompts[0]
    assert outcomes(builder) == [None, "previous-tail", "previous-tail"]


def test_edited_prompt_is_compared_with_the_prompt_before_the_edited_one() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("second", [(40_000, 9_900)])
    # Editing "second" branches from the entry just before it: its system block.
    edit_point = builder.last_system_entry_id()
    builder.prompt("second, edited", [(11_000, 9_900)], parent=edit_point)

    prompts = split_prompts(builder.entries, session="s")

    assert prompts[2].previous is prompts[0]
    assert outcomes(builder) == [None, "previous-tail", "previous-tail"]


def test_responses_without_reported_cache_reads_are_not_counted_as_misses() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, None), (12_000, None)])
    builder.prompt("second", [(13_000, None)])

    prompts = split_prompts(builder.entries, session="s")

    assert outcomes(builder) == [None, None]
    assert classify_within_run(prompts[0]) == {}


def test_unknown_cache_usage_keeps_response_positions() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    # The second prompt's real first response is unknown; the next one must not
    # stand in for it, and the pair around the unknown one must not be compared.
    # Each reported response is judged against the request right before it, even
    # when that request's own cache usage is unknown; nothing pairs across a gap.
    builder.prompt("second", [(11_000, None), (12_000, 10_900), (13_000, None), (20_000, 12_900)])

    [_, second] = split_prompts(builder.entries, session="s")

    assert outcomes(builder) == [None, None]
    assert classify_within_run(second) == {"reached-previous": 2}


def test_responses_without_any_usage_keep_their_positions() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    # A tool-call response with no usage chunk, then a measured post-tool response:
    # the measured one must not be treated as the run's first response.
    builder.prompt("second", [])
    builder.unmeasured_response()
    builder.response(12_000, 10_900)
    builder.unmeasured_response()
    builder.response(20_000, 11_900)

    [_, second] = split_prompts(builder.entries, session="s")

    assert len(second.responses) == 4
    assert outcomes(builder) == [None, None]
    assert classify_within_run(second) == {}


def test_a_reported_zero_is_counted_even_when_the_earlier_size_is_unknown() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [])
    builder.unmeasured_response()
    builder.prompt("second", [(12_000, 0)])
    builder.unmeasured_response()
    builder.response(14_000, 0)

    [_, second] = split_prompts(builder.entries, session="s")
    result = classify_prompt(second, idle_minutes=60)

    assert result is not None
    assert result.outcome == "zero"
    assert (result.previous_first_input_tokens, result.previous_last_input_tokens) == (None, None)
    assert classify_within_run(second) == {"zero": 1}


def test_navigating_back_within_a_run_then_compacting_drops_the_abandoned_responses() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    earlier = builder.leaf
    assert earlier is not None
    builder.response(40_000, 9_900)
    # Navigate back to the first response of the same prompt, compact there,
    # then prompt again: the abandoned 40k response is no longer on the branch.
    builder.navigate(earlier)
    builder.compaction()
    builder.prompt("after", [(3_000, 1_536)])

    [first, after] = split_prompts(builder.entries, session="s")
    result = classify_prompt(after, idle_minutes=60)

    assert [r.input_tokens for r in after.previous_responses] == [10_000]
    assert after.previous_compactions == 1
    assert result is not None
    assert result.previous_last_input_tokens == 10_000
    assert "compaction" in result.causes
    # The within-run pair (10k -> 40k) is still counted once for the first prompt.
    assert classify_within_run(first) == {"reached-previous": 1}


def test_entries_after_navigating_back_belong_to_the_selected_branch() -> None:
    builder = SessionBuilder()
    first_leaf = builder.prompt("first", [(10_000, 0)])
    builder.prompt("abandoned", [(40_000, 9_900)])
    # Navigate back to the end of "first", compact there, then prompt again.
    builder.navigate(first_leaf)
    builder.compaction()
    builder.prompt("after navigation", [(3_000, 1_536)])

    prompts = split_prompts(builder.entries, session="s")
    result = classify_prompt(prompts[2], idle_minutes=60)

    assert prompts[2].previous is prompts[0]
    assert (prompts[0].compactions, prompts[1].compactions) == (1, 0)
    assert result is not None
    assert "compaction" in result.causes


def test_prompt_from_a_mid_run_response_is_compared_with_that_response() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    mid_run = builder.leaf
    assert mid_run is not None
    builder.response(40_000, 9_900)
    # Navigate to the first response and prompt from there: the run's later 40k
    # request was abandoned, so a 9.9k hit reaches the branch point's tail.
    builder.navigate(mid_run)
    builder.prompt("branch", [(12_000, 9_900)])

    [first, branch] = split_prompts(builder.entries, session="s")
    result = classify_prompt(branch, idle_minutes=60)

    assert branch.previous is first
    assert [r.input_tokens for r in branch.previous_responses] == [10_000]
    assert result is not None
    assert result.outcome == "previous-tail"
    assert result.previous_last_input_tokens == 10_000


def test_fork_from_a_user_message_keeps_the_projected_and_fresh_system_blocks_apart() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("second", [(20_000, 9_900)])
    projected_block_end = builder.last_system_entry_id()
    # Fork from "second": the fork's history ends with second's system block
    # (without its user message), then the edited prompt appends a fresh block.
    fork = builder.entries[: builder.entries.index(_entry(builder, projected_block_end)) + 1]
    builder.entries = list(fork)
    builder.leaf = projected_block_end
    builder.prompt("second, edited", [(11_000, 9_900)])

    prompts = split_prompts(builder.entries, session="s")
    result = classify_prompt(prompts[1], idle_minutes=60)

    assert [p.entry_id for p in prompts] == [prompts[0].entry_id, prompts[1].entry_id]
    assert prompts[1].system_sections == (STATIC, CONTEXT)
    assert result is not None
    assert result.causes == ()


def _fork_and_edit_second_prompt(
    tmp_path: Path, *, provider: str = "anthropic"
) -> tuple[SessionBuilder, Path]:
    """Build a source session, then a fork from its second prompt's user message.

    The fork's history ends with the second prompt's copied system block (without
    its user message); the edited prompt appends a fresh block right after it.
    Returns the builder (holding the fork) and the written source file.
    """

    builder = SessionBuilder()
    builder.prompt("first", [])
    builder.response(10_000, 0, provider=provider)
    builder.prompt("second", [])
    builder.response(20_000, 9_900, provider=provider)
    source = builder.write(
        tmp_path / builder.file_name(builder.entries[0].created_at.replace(microsecond=0)),
        session_id="source",
    )
    projected_block_end = builder.last_system_entry_id()
    builder.entries = builder.entries[
        : builder.entries.index(_entry(builder, projected_block_end)) + 1
    ]
    builder.leaf = projected_block_end
    builder.fork_created_at = builder.clock + timedelta(minutes=5)
    builder.clock = builder.fork_created_at + timedelta(seconds=30)
    builder.prompt("second, edited", [], minutes_later=0)
    builder.response(11_000, 0, provider=provider)
    return builder, source


def test_fork_blocks_stay_apart_without_operation_ids(tmp_path: Path) -> None:
    builder, source = _fork_and_edit_second_prompt(tmp_path)
    # SDK runs may pass no operation ID, and legacy entries carry none.
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]
    fork = builder.write(tmp_path / builder.fork_file_name(), session_id="fork")

    [report] = build_report([source, fork], split_at=None, idle_minutes=60).values()
    edited = next(p for p in report.prompts if p.session == fork.stem)

    assert edited.estimated_instruction_tokens == estimate_tokens((STATIC, CONTEXT))
    assert edited.causes == ()


def test_fork_created_in_the_source_files_second_is_still_recognized(tmp_path: Path) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("second", [(20_000, 9_900)])
    created = builder.entries[0].created_at.replace(microsecond=0)
    # The source keeps going after the fork point; the fork copies only the path
    # up to the second prompt's system block, then appends its edited prompt.
    builder.prompt("source only", [(21_000, 19_900)])
    source = builder.write(tmp_path / builder.file_name(created), session_id="source")
    edit_point = next(
        e.id
        for e in reversed(builder.entries)
        if isinstance(e, MessageSessionEntry) and e.message.content == "second"
    )
    fork_path = _entry(builder, edit_point).parent_id
    assert fork_path is not None
    builder.entries = builder.entries[: builder.entries.index(_entry(builder, fork_path)) + 1]
    builder.leaf = fork_path
    builder.prompt("second, edited", [(11_000, 9_900)])
    # Both files were created in the same second, and no operation IDs are set.
    for_fork = [e.model_copy(update={"operation_id": None}) for e in builder.entries]
    builder.entries = for_fork
    fork = builder.write(tmp_path / builder.file_name(created), session_id="fork")

    [report] = build_report([source, fork], split_at=None, idle_minutes=60).values()
    edited = next(p for p in report.prompts if p.session == fork.stem)

    assert edited.estimated_instruction_tokens == estimate_tokens((STATIC, CONTEXT))
    assert "system changed" not in " ".join(edited.causes)


def test_same_second_clone_continued_alone_never_marks_the_source_as_copied(
    tmp_path: Path,
) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [])
    builder.response(10_000, 0, provider="openai-codex")
    builder.prompt("second", [])
    builder.response(11_000, 10_900, provider="openai-codex")
    created = builder.entries[0].created_at.replace(microsecond=0)
    source = builder.write(tmp_path / builder.file_name(created), session_id="source")
    # Clone at the current leaf in the same second; only the clone continues.
    builder.prompt("clone only", [])
    builder.response(12_000, 0, provider="openai-codex")
    clone = builder.write(tmp_path / builder.file_name(created), session_id="clone")

    sessions = [(path, read_session_entries(path)) for path in (source, clone)]

    # Ambiguous: neither file is marked as the copy. The source is never marked as
    # a copy of its own clone; the clone's `session changed` is omitted, not faked.
    assert copied_entry_ids(sessions) == [frozenset(), frozenset()]
    [report] = build_report([source, clone], split_at=None, idle_minutes=60).values()
    assert [p.causes for p in report.prompts] == [(), ()]


def test_fork_file_alone_keeps_system_blocks_apart_without_operation_ids(
    tmp_path: Path,
) -> None:
    builder, _ = _fork_and_edit_second_prompt(tmp_path)
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]
    # Only the fork file is reported: nothing marks its history as copied, so the
    # pause between the copied block and the edited prompt's block splits them.
    fork = builder.write(tmp_path / "fork-only" / builder.fork_file_name(), session_id="fork")

    [report] = build_report([fork], split_at=None, idle_minutes=60).values()
    [edited] = report.prompts

    assert edited.estimated_instruction_tokens == estimate_tokens((STATIC, CONTEXT))
    assert edited.causes == ()


def test_same_second_clone_continued_in_both_files_marks_neither(tmp_path: Path) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [])
    builder.response(10_000, 0, provider="openai-codex")
    created = builder.entries[0].created_at.replace(microsecond=0)
    shared = list(builder.entries)
    # The clone continues first, then the source is resumed.
    builder.prompt("clone only", [])
    builder.response(11_000, 0, provider="openai-codex")
    clone = builder.write(tmp_path / builder.file_name(created), session_id="clone")
    builder.entries = shared
    builder.leaf = shared[-1].id
    builder.prompt("source only", [])
    builder.response(12_000, 0, provider="openai-codex")
    source = builder.write(tmp_path / builder.file_name(created), session_id="source")

    sessions = [(path, read_session_entries(path)) for path in (source, clone)]

    assert copied_entry_ids(sessions) == [frozenset(), frozenset()]


def test_system_messages_with_a_repeated_tag_stay_in_one_block() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    # An SDK run without an operation ID may send two sections with the same tag.
    sections = ("[CUSTOM]\na", "[CUSTOM]\nb")
    builder.prompt("second", [(11_000, 0)], system_sections=sections)
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]

    [_, second] = split_prompts(builder.entries, session="s")

    assert second.system_sections == sections


def test_prompt_without_system_messages_is_recognized() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    # An SDK prompt replacement (`prompt_messages=()`) persists no system block.
    builder.prompt("second", [(11_000, 9_900)], system_sections=())

    prompts = split_prompts(builder.entries, session="s")

    assert len(prompts) == 2
    assert prompts[1].system_sections == ()
    assert prompts[1].previous is prompts[0]
    assert outcomes(builder) == [None, "previous-tail"]


def test_id_less_prompts_without_system_messages_are_recognized() -> None:
    builder = SessionBuilder()
    # `CodingSession.run` defaults `operation_id=None`, and `prompt_messages=()`
    # persists no system block: the first user entry has no parent, the next
    # follows the previous run's final answer.
    builder.prompt("first", [(10_000, 0)], system_sections=())
    builder.prompt("second", [(11_000, 9_900)], system_sections=())
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]

    prompts = split_prompts(builder.entries, session="s")

    assert len(prompts) == 2
    assert prompts[1].previous is prompts[0]
    assert outcomes(builder) == [None, "previous-tail"]


@pytest.mark.parametrize("between", ["event", "compaction"])
def test_id_less_prompt_after_an_event_or_compaction_is_recognized(between: str) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)], system_sections=())
    # A context.pressure event after the response, or a manual compaction, can be
    # the active leaf when the next ID-less, system-less run starts.
    if between == "event":
        builder.event({"type": "context.pressure"})
    else:
        builder.compaction()
    builder.prompt("second", [(11_000, 9_900)], system_sections=())
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]

    prompts = split_prompts(builder.entries, session="s")

    assert len(prompts) == 2
    assert prompts[1].previous is prompts[0]
    assert [r.input_tokens for r in prompts[0].responses] == [10_000]


def test_id_less_prompt_after_a_failed_run_starts_a_new_prompt() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)], system_sections=())
    # The provider fails before any response: the run ends with an error event
    # right after its user message.
    builder.prompt("failed", [], system_sections=())
    builder.event({"type": "error", "message": "provider failed"})
    builder.prompt("retry", [(11_000, 9_900)], system_sections=())
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]

    prompts = split_prompts(builder.entries, session="s")

    assert [len(p.responses) for p in prompts] == [1, 0, 1]
    assert prompts[2].previous is prompts[1]


def test_a_new_sessions_first_message_is_not_mistaken_for_a_copy(tmp_path: Path) -> None:
    builder = SessionBuilder()
    # The user message is prepared before the session file is created, so it can
    # predate the file name's second.
    builder.prompt("first", [], system_sections=())
    builder.response(10_000, 0, provider="openai-codex")
    builder.prompt("second", [], system_sections=())
    builder.response(11_000, 0, provider="openai-codex")
    created = builder.entries[0].created_at.replace(microsecond=0) + timedelta(seconds=1)
    path = builder.write(tmp_path / builder.file_name(created))

    [report] = build_report([path], split_at=None, idle_minutes=60).values()

    assert [p.causes for p in report.prompts] == [()]


def test_id_less_prompt_after_an_id_bearing_run_starts_a_new_prompt() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    # The next SDK run passes no operation ID and no system block.
    builder.prompt("second", [], system_sections=())
    builder.entries = [
        *builder.entries[:-1],
        builder.entries[-1].model_copy(update={"operation_id": None}),
    ]
    builder.operation_id = None
    builder.response(11_000, 9_900)

    prompts = split_prompts(builder.entries, session="s")

    assert len(prompts) == 2
    assert prompts[1].previous is prompts[0]
    assert outcomes(builder) == [None, "previous-tail"]
    assert classify_within_run(prompts[0]) == {}


def test_id_less_steering_after_tool_results_stays_in_the_run() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [], system_sections=())
    builder.tool_round(10_000, 0)
    builder.steer("also check the tests")
    builder.response(12_000, 9_900)
    builder.entries = [e.model_copy(update={"operation_id": None}) for e in builder.entries]

    [first] = split_prompts(builder.entries, session="s")

    assert [r.input_tokens for r in first.responses] == [10_000, 12_000]


def test_steering_message_inside_a_run_does_not_start_a_prompt() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.steer("also check the tests")
    builder.response(12_000, 9_900)

    [first] = split_prompts(builder.entries, session="s")

    assert [r.input_tokens for r in first.responses] == [10_000, 12_000]


@pytest.mark.parametrize(
    ("provider", "expected_causes"),
    [("openai-codex", ("session changed",)), ("anthropic", ())],
)
def test_first_prompt_of_a_fork_reports_a_session_change_for_cache_keyed_providers(
    tmp_path: Path, provider: str, expected_causes: tuple[str, ...]
) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [])
    builder.response(10_000, 0, provider=provider)
    builder.clock += timedelta(milliseconds=500)
    builder.prompt("second", [])
    builder.response(11_000, 10_900, provider=provider)
    source = builder.write(tmp_path / builder.file_name(START), session_id="source")
    # The fork file is created within the same second as the copied user message
    # (12:02:01.500 copied, file named 12:02:01): comparing against the floored
    # file time alone would call it fresh. The older source file identifies it.
    second_user = next(
        e
        for e in builder.entries
        if isinstance(e, MessageSessionEntry) and e.message.content == "second"
    )
    fork_created = second_user.created_at.replace(microsecond=0)
    builder.clock = builder.clock + timedelta(seconds=30)
    builder.prompt("fork only", [], minutes_later=0)
    builder.response(12_000, 0, provider=provider)
    fork = builder.write(tmp_path / builder.file_name(fork_created), session_id="fork")

    [report] = build_report([source, fork], split_at=None, idle_minutes=60).values()

    assert [(p.session[-8:], p.outcome, p.causes) for p in report.prompts] == [
        ("aaaaaaaa", "previous-tail", ()),
        ("bbbbbbbb", "zero", expected_causes),
    ]


def test_cache_keyed_providers_match_the_adapters() -> None:
    from wisp.providers import (
        AnthropicProvider,
        DeepSeekProvider,
        GoogleProvider,
        OpenAICodexProvider,
        OpenAICompatibleProvider,
        OpenAIProvider,
        XAIProvider,
    )

    adapters = (
        AnthropicProvider,
        DeepSeekProvider,
        GoogleProvider,
        OpenAICodexProvider,
        OpenAICompatibleProvider,
        OpenAIProvider,
        XAIProvider,
    )
    keyed = {
        cast(str, adapter.name)
        for adapter in adapters
        if getattr(adapter, "supports_prompt_cache_key", False) is True
    }

    assert keyed == CACHE_KEYED_PROVIDERS


def test_the_most_complete_copy_of_a_prompt_is_counted(tmp_path: Path) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("second", [])
    truncated = builder.write(tmp_path / "a-clone.jsonl", session_id="clone")
    builder.response(11_000, 9_900)
    builder.response(13_000, 10_900)
    complete = builder.write(tmp_path / "b-source.jsonl", session_id="source")

    for files in ([truncated, complete], [complete, truncated]):
        [report] = build_report(files, split_at=None, idle_minutes=60).values()

        assert [(p.session, p.outcome) for p in report.prompts] == [("b-source", "previous-tail")]
        assert report.within_run == {"reached-previous": 1}


def _entry(builder: SessionBuilder, entry_id: str) -> SessionEntry:
    return next(e for e in builder.entries if e.id == entry_id)


def test_reports_a_provider_change_even_when_the_model_name_matches() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [])
    builder.response(10_000, 0, provider="openai-compatible-a")
    builder.prompt("second", [])
    builder.response(11_000, 0, provider="openai-compatible-b")

    result = classify_prompt(split_prompts(builder.entries, session="s")[1], idle_minutes=60)

    assert result is not None
    assert result.causes == ("provider changed",)


def test_prompts_copied_into_a_fork_are_counted_once(tmp_path: Path) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("second", [(11_000, 9_900)])
    source = builder.write(tmp_path / "a-source.jsonl", session_id="source")
    # A fork copies the source history with the same entry IDs, then continues.
    builder.prompt("fork only", [(12_000, 10_900)])
    fork = builder.write(tmp_path / "b-fork.jsonl", session_id="fork")

    [report] = build_report([source, fork], split_at=None, idle_minutes=60).values()

    assert [(p.session, p.outcome) for p in report.prompts] == [
        ("a-source", "previous-tail"),
        ("b-fork", "previous-tail"),
    ]


def test_uses_the_observed_full_request_size_when_usage_excludes_cached_input() -> None:
    builder = SessionBuilder()
    # Anthropic-style usage: input_tokens counts only uncached input.
    builder.prompt("first", [(2_000, 0)])
    builder.response(1_000, 48_000, observed_input_tokens=50_000)
    builder.prompt("second", [])
    builder.response(30_000, 30_000, observed_input_tokens=60_000)

    [first, second] = split_prompts(builder.entries, session="s")
    result = classify_prompt(second, idle_minutes=60)

    assert [r.input_tokens for r in first.responses] == [2_000, 50_000]
    assert result is not None
    # 30k of a 50k previous request is partial, not previous-tail.
    assert result.outcome == "partial"


def test_prompt_without_usable_responses_is_not_classified() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("cancelled", [])
    builder.prompt("after cancel", [(11_000, 9_900)])

    # The prompt after the cancelled one is compared with the cancelled prompt,
    # which has no response to measure against.
    assert outcomes(builder) == [None, None, None]


def test_within_run_counts_skip_pairs_split_by_compaction() -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0), (12_000, 9_900), (14_000, 5_000), (15_000, 0)])
    builder.compaction()
    builder.response(6_000, 1_536)
    builder.response(7_000, 5_900)

    [prompt] = split_prompts(builder.entries, session="s")

    assert classify_within_run(prompt) == {"reached-previous": 2, "partial": 1, "zero": 1}


def test_reader_skips_an_incomplete_final_record_without_modifying_the_file(
    tmp_path: Path,
) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    path = builder.write(tmp_path / "session.jsonl")
    with path.open("a") as session_file:
        session_file.write('{"kind": "message", "trunc')
    original = path.read_bytes()

    entries = read_session_entries(path)

    assert [e.id for e in entries] == [e.id for e in builder.entries]
    assert path.read_bytes() == original


def test_split_at_reports_before_and_after_periods(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    builder = SessionBuilder()
    builder.prompt("first", [(10_000, 0)])
    builder.prompt("before", [(11_000, 0)])
    builder.prompt("after", [(12_000, 10_900)], minutes_later=30)
    path = builder.write(tmp_path / "session.jsonl")
    split_at = START + timedelta(minutes=10)

    periods = build_report([path], split_at=split_at, idle_minutes=60)

    assert [p.outcome for p in periods["before"].prompts] == ["zero"]
    assert [p.outcome for p in periods["after"].prompts] == ["previous-tail"]

    assert main([str(tmp_path), "--split-at", split_at.isoformat(), "--details"]) == 0
    output = capsys.readouterr().out
    assert "| Outcome | before | after |" in output
    assert "| zero | 1 | 0 |" in output
    assert "| no recorded cause | 0 | 0 | 1 |" in output
