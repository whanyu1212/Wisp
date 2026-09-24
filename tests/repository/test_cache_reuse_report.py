from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.cache_reuse_report import (
    build_report,
    classify_prompt,
    classify_within_run,
    main,
    read_session_entries,
    split_prompts,
)
from wisp.agent.messages import CompactionRecord, Message
from wisp.events import ContextObservation, TokenUsage
from wisp.sessions.entries import (
    CompactionSessionEntry,
    MessageSessionEntry,
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

    def prompt(
        self,
        text: str,
        responses: list[tuple[int, int]],
        *,
        context: str = CONTEXT,
        parent: str | None = None,
        minutes_later: float = 1,
    ) -> str:
        """Append one prompt; ``responses`` holds (input, cached) token pairs."""

        self.clock += timedelta(minutes=minutes_later)
        if parent is not None:
            self.leaf = parent
        for section in (STATIC, context):
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

    def last_system_entry_id(self) -> str:
        return next(
            e.id
            for e in reversed(self.entries)
            if isinstance(e, MessageSessionEntry) and e.message.role == "system"
        )

    def compaction(self) -> None:
        assert self.leaf is not None
        record = CompactionRecord(summary="s", replaced_entry_ids=(self.leaf,), provider="fake")
        self._append(CompactionSessionEntry(session_id="s", parent_id=self.leaf, compaction=record))

    def _message(self, message: Message) -> None:
        self.clock += timedelta(seconds=1)
        message = message.model_copy(update={"created_at": self.clock})
        self._append(
            MessageSessionEntry(
                session_id="s",
                parent_id=self.leaf,
                message=message,
                created_at=self.clock,
            )
        )

    def _append(self, entry: SessionEntry) -> None:
        self.entries.append(entry)
        self.leaf = entry.id

    def write(self, path: Path, *, session_id: str = "s") -> Path:
        entries = [e.model_copy(update={"session_id": session_id}) for e in self.entries]
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
