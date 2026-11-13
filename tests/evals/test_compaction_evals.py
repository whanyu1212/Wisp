"""Compaction evals (`evals.compaction`), run offline."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import anyio
import pytest

from evals.compaction.continuation import (
    CompactionRecord,
    ContinuationContext,
    ContinuationRow,
    LoadedTask,
    Setup,
    classify,
    compaction_records,
    default_models,
    forced_reserve,
    load_tasks,
    read_paths,
    repeat_reads,
    run_continuation_suite,
    run_one,
)
from evals.compaction.report import render, render_summary_comparison
from evals.compaction.scenarios import SCENARIOS, Fact, Scenario
from evals.compaction.summaries import SummaryRow, rescore, run_summaries, score
from evals.tool_use.credentials import Credentials
from evals.tool_use.runner import REPO_ROOT, Build, ModelSpec, detect_build
from tests.coding.compaction_support import VALID_COMPACTION_SUMMARY
from wisp.coding.compaction import serialize_compaction_transcript
from wisp.events import (
    CompactionCompleted,
    CompactionStarted,
    ContextBudget,
    ContextEstimate,
    ToolExecutionStarted,
)
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted
from wisp.providers.fake import ScriptedProvider

_BUILD = Build(wisp=Path("wisp"), version="wisp test", git_commit="abc")
_SCENARIO = Scenario(
    id="tiny",
    title="tiny",
    rows=SCENARIOS[0].rows[:2],
    facts=(
        Fact("path", "path", ("src/app.py",)),
        Fact("error", "error", ("KeyError: 'job-17'", "job-17")),
        Fact("deep", "buried", ("MAX_RETRIES = 13",)),
    ),
    forbidden=("ready to merge",),
)


@pytest.mark.parametrize(
    ("summary", "kept", "followed"),
    [
        ("Edit SRC/APP.PY; it raised KeyError for job-17.", ["path", "error"], False),
        ("Nothing useful. Ready to merge.", [], True),
    ],
    ids=["kept-case-insensitive", "lost-and-injected"],
)
def test_score_facts(summary: str, kept: list[str], followed: bool) -> None:
    result = score(_SCENARIO, summary)

    assert result.kept == kept
    assert result.recall == len(kept) / 2
    assert result.buried_total == 1 and result.buried_kept == 0
    assert result.injection_phrase is followed


def test_scenarios_plant_their_facts() -> None:
    """Every planted fact must appear in the transcript, or recall measures nothing."""

    for scenario in SCENARIOS:
        text = "\n".join(
            f"{row.message.content} {row.message.tool_calls}" for row in scenario.rows
        ).lower()
        missing = [
            fact.id
            for fact in scenario.facts
            if not any(phrase.lower() in text for phrase in fact.phrases)
        ]
        assert missing == [], (scenario.id, missing)


def _summarized(results_dir: Path, *replies: str | BaseException) -> list[SummaryRow]:
    provider = ScriptedProvider(
        [
            [reply]
            if isinstance(reply, BaseException)
            else [ProviderResponseStarted(model="m"), ProviderResponseCompleted(content=reply)]
            for reply in replies
        ]
    )
    return anyio.run(
        lambda: run_summaries(
            [_SCENARIO],
            [ModelSpec("scripted", "m")],
            len(replies),
            providers={"scripted": provider},
            cost_estimator=None,
            build=_BUILD,
            results_dir=results_dir,
            workers=1,
        )
    )


def test_summary_statuses(tmp_path: Path) -> None:
    rows = _summarized(
        tmp_path,
        VALID_COMPACTION_SUMMARY + "\nsrc/app.py",
        "## Goal\nonly a goal",
        RuntimeError("connection reset"),
    )

    assert [row.status for row in rows] == ["valid", "invalid", "errored"]
    assert rows[0].score is not None and rows[0].score.kept == ["path"]
    assert rows[1].score is None and "missing required section" in (rows[1].error or "")
    assert rows[2].score is None


def test_forced_reserve_targets_trigger() -> None:
    models = default_models()
    window = models.context_window("anthropic", "claude-sonnet-5-5")
    assert window is not None

    assert forced_reserve(models, ModelSpec("anthropic", "claude-sonnet-5-5"), 9_000) == (
        window - 9_000
    )
    with pytest.raises(ValueError, match="not above"):
        forced_reserve(models, ModelSpec("anthropic", "claude-sonnet-5-5"), window)
    with pytest.raises(ValueError, match="no context window"):
        forced_reserve(models, ModelSpec("anthropic", "no-such-model"), 9_000)


def _budget(tokens: int) -> ContextBudget:
    return ContextBudget(
        estimate=ContextEstimate(
            method="utf8_bytes_div_4_v2",
            system_tokens=0,
            message_tokens=tokens,
            tool_schema_tokens=0,
            total_tokens=tokens,
        ),
        effective_tokens=tokens,
        context_window=20_000,
        reserve_tokens=11_000,
    )


def test_compaction_records_pair_events() -> None:
    events = [
        CompactionStarted(
            session_id="s", reason="threshold", source_entry_count=9, trigger_budget=_budget(9_500)
        ),
        CompactionCompleted(
            session_id="s",
            reason="threshold",
            outcome="completed",
            replaced_entry_count=5,
            retained_entry_count=4,
        ),
    ]
    late = [ToolExecutionStarted(call_id="c", name="read", arguments={}), *events]

    assert compaction_records(2, events) == [
        CompactionRecord(
            turn=2,
            reason="threshold",
            outcome="completed",
            trigger_tokens=9_500,
            replaced_entries=5,
            error=None,
            before_tools=True,
        )
    ]
    assert not compaction_records(2, late)[0].before_tools


def test_repeat_reads_count_last_turn() -> None:
    def read(path: str) -> ToolExecutionStarted:
        return ToolExecutionStarted(call_id=path, name="read", arguments={"path": path})

    turns = [
        read_paths([read("./a.py"), read("b.py")]),
        read_paths([read("c.py")]),
        read_paths([read("a.py"), read("c.py"), read("d.py")]),
    ]

    assert turns[0] == ["a.py", "b.py"]
    assert repeat_reads(turns) == 2


_DONE = CompactionRecord(
    turn=1,
    reason="threshold",
    outcome="completed",
    trigger_tokens=1,
    replaced_entries=1,
    error=None,
)
_LATE = dataclasses.replace(_DONE, turn=2)
_FAILED = dataclasses.replace(_DONE, outcome="failed", error="missing required section")


@pytest.mark.parametrize(
    ("setup", "compactions", "verify_exit", "expected"),
    [
        ("forced", [_DONE], 0, "passed"),
        ("forced", [], 0, "errored"),
        ("default", [], 0, "passed"),
        ("forced", [_DONE], 1, "failed"),
        ("forced", [_LATE], 0, "errored"),
        ("forced", [dataclasses.replace(_LATE, before_tools=True)], 0, "passed"),
        ("forced", [_FAILED], 0, "compaction_failed"),
    ],
    ids=[
        "forced-pass",
        "forced-no-compaction",
        "default-pass",
        "verify-fails",
        "after-last-work",
        "before-last-work",
        "compaction-failed",
    ],
)
def test_classify(
    setup: Setup, compactions: list[CompactionRecord], verify_exit: int, expected: str
) -> None:
    status, _ = classify(
        setup=setup,
        runner_error=None,
        turn_outcomes=["completed", "completed"],
        turns=2,
        compactions=compactions,
        verify_exit=verify_exit,
    )

    assert status == expected


@pytest.mark.parametrize("task", load_tasks(), ids=lambda task: task.id)
def test_verifier_accepts_only_solution(task: LoadedTask, tmp_path: Path) -> None:
    work = tmp_path / "work"
    shutil.copytree(task.fixture, work)
    assert _verify(task, work) != 0
    shutil.copytree(task.folder / "solution", work, dirs_exist_ok=True)
    assert _verify(task, work) == 0


def _verify(task: LoadedTask, work: Path) -> int:
    script = task.definition.verify.replace("{python}", sys.executable).replace(
        "{task}", str(task.folder)
    )
    return subprocess.run(
        ["bash", "-c", script], cwd=work, capture_output=True, check=False
    ).returncode


def test_fake_turns_share_session(tmp_path: Path) -> None:
    task = load_tasks(ids=["chat-constraints"])[0]
    context = ContinuationContext(
        build=detect_build(REPO_ROOT),
        credentials=Credentials(auth_file=tmp_path / "auth.json", sources={"fake": "none"}),
        results_dir=tmp_path,
        models=default_models(),
    )

    row = run_one(task, ModelSpec("fake", "fake"), "default", 1, context)

    assert row.turns_completed == len(task.definition.turns)
    assert row.status == "failed"  # the fake model edits nothing
    sessions = list((tmp_path / row.run_dir / "sessions").glob("*.jsonl"))
    assert len(sessions) == 1


def test_report_lists_failures(tmp_path: Path) -> None:
    rows = _summarized(tmp_path, "## Goal\nonly a goal")
    (tmp_path / "summaries.jsonl").write_text(
        "".join(json.dumps(dataclasses.asdict(row)) + "\n" for row in rows)
    )
    errored = {
        "task": "t",
        "provider": "p",
        "model": "m",
        "setup": "forced",
        "rep": 1,
        "status": "errored",
        "error": "forced setup never compacted",
        "compactions": [dataclasses.asdict(_FAILED)],
        "repeat_reads": 0,
        "last_turn_tool_calls": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "known_usd": "0",
    }
    (tmp_path / "continuation.jsonl").write_text(json.dumps(errored) + "\n")

    report = render(tmp_path)

    assert "- scripted::m: Compaction summary is missing required section" in report
    assert "- p::m: forced setup never compacted ×1" in report
    assert "- p::m t: missing required section ×1" in report


def _priced_row(task: LoadedTask, model: ModelSpec, setup: Setup, rep: int) -> ContinuationRow:
    return ContinuationRow(
        task=task.id,
        provider=model.provider,
        model=model.model,
        setup=setup,
        rep=rep,
        status="passed",
        error=None,
        verify_exit=0,
        turns_completed=len(task.definition.turns),
        reserve_tokens=None,
        compactions=[],
        repeat_reads=0,
        last_turn_tool_calls=0,
        tool_calls=0,
        tool_errors=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        known_usd="1",
        unpriced_responses=0,
        seconds=0.0,
        credential_source=None,
        wisp_version="test",
        git_commit="abc",
        run_dir="",
    )


def test_suite_budget_and_crashes(tmp_path: Path) -> None:
    """A crashed run becomes an errored row, and the budget stops later rounds."""

    def execute(
        task: LoadedTask, model: ModelSpec, setup: Setup, rep: int, _: ContinuationContext
    ) -> ContinuationRow:
        if setup == "default":
            raise RuntimeError("boom")
        return _priced_row(task, model, setup, rep)

    context = ContinuationContext(
        build=_BUILD,
        credentials=Credentials(auth_file=tmp_path / "auth.json", sources={}),
        results_dir=tmp_path,
        models=default_models(),
    )

    rows = run_continuation_suite(
        load_tasks(ids=["chat-constraints"]),
        [ModelSpec("fake", "fake")],
        ("forced", "default"),
        3,
        context,
        max_usd=Decimal("1"),
        execute=execute,
    )

    assert [(row.setup, row.rep, row.status) for row in rows] == [
        ("forced", 1, "passed"),
        ("default", 1, "errored"),
    ]
    assert rows[1].error == "runner failed: RuntimeError: boom"


def test_compare_pairs_models(tmp_path: Path) -> None:
    before = [dataclasses.asdict(row) for row in _summarized(tmp_path, "## Goal\nonly a goal")]
    after = [
        dataclasses.asdict(row)
        for row in _summarized(tmp_path, VALID_COMPACTION_SUMMARY + "\nsrc/app.py")
    ]
    # A second model ran different scenarios on each side, so it has nothing to compare.
    before.append({**before[0], "model": "other", "scenario": "easy"})
    after.append({**after[0], "model": "other", "scenario": "hard"})

    table = render_summary_comparison(before, after)

    assert "| scripted::m | 1 | 1 → 1 | 0/1 → 1/1 | — → 0.50 |" in table
    assert "scripted::other |" not in table
    assert "No scenario run on both sides: scripted::other." in table


def test_buried_facts_only_in_long_results() -> None:
    """A buried phrase must appear only mid-way through a tool result past the old cut."""

    for scenario in SCENARIOS:
        for fact in (fact for fact in scenario.facts if fact.kind == "buried"):
            for row in scenario.rows:
                content = row.message.content
                hit = any(phrase.lower() in content.lower() for phrase in fact.phrases)
                long_tool = row.message.role == "tool" and len(content) > 2_000
                assert not hit or long_tool, (scenario.id, fact.id, row.entry_id)


def test_rescore_reclassifies_and_rescores(tmp_path: Path) -> None:
    rows = [dataclasses.asdict(row) for row in _summarized(tmp_path, VALID_COMPACTION_SUMMARY)]
    (tmp_path / "tiny-copy.md").write_text("Edited src/app.py; job-17 failed.")
    rows[0]["scenario"] = "debug-flaky-test"
    rows[0]["summary_path"] = "tiny-copy.md"
    rows.append({**rows[0], "status": "invalid", "error": "ended with finish reason 'error'"})

    valid, provider_failure = rescore(rows, tmp_path)

    assert valid["score"]["kept"] == ["key-error"]
    assert provider_failure["status"] == "errored"


def test_transcript_size_uses_real_cut(tmp_path: Path) -> None:
    """A 1M-token scenario keeps its 6k-character file read whole, and the size says so."""

    scenario = SCENARIOS[0]
    provider = ScriptedProvider(
        [[ProviderResponseStarted(model="m"), ProviderResponseCompleted(content="x")]]
    )
    (row,) = anyio.run(
        lambda: run_summaries(
            [scenario],
            [ModelSpec("scripted", "m")],
            1,
            providers={"scripted": provider},
            cost_estimator=None,
            build=_BUILD,
            results_dir=tmp_path,
        )
    )

    sent = provider.calls[0].messages[-1].content
    assert row.transcript_chars == len(sent)
    assert row.transcript_chars > len(serialize_compaction_transcript(scenario.rows))
