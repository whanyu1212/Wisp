"""Comparing runs and saving baselines (`evals.tool_use.compare`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.tool_use import __main__ as cli
from evals.tool_use.compare import (
    BASELINE_FIELDS,
    baseline_document,
    compare_runs,
    render_comparison,
    write_baseline,
)
from evals.tool_use.report import load_rows


def _run(
    task: str,
    calls: int,
    *,
    tokens: int = 1000,
    status: str = "passed",
    rep: int = 1,
    tools: str = "t",
) -> dict[str, object]:
    return {
        "task": task,
        "provider": "openai-codex",
        "model": "m",
        "rep": rep,
        "status": status,
        "timed_out": False,
        "seconds": 10.0,
        "turns": 3,
        "tool_calls": {"read": calls},
        "tool_errors": {},
        "reruns": 0,
        "used_read_output": False,
        "input_tokens": tokens,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "known_usd": "0",
        "tools_sha256": tools,
        "wisp_version": "wisp 0.6.1",
        "git_commit": "abc",
        "run_dir": "a/b/rep1",
        "error": None,
        "credential_source": "stored",
    }


def _side(task: str, calls: list[int], tokens: list[int]) -> list[dict[str, object]]:
    return [
        _run(task, count, tokens=amount, rep=rep)
        for rep, (count, amount) in enumerate(zip(calls, tokens, strict=True), start=1)
    ]


def test_marks_only_changes_beyond_noise() -> None:
    before = _side("a", [10, 10, 10], [1000, 4000, 2500])
    after = _side("a", [5, 5, 6], [1500, 3500, 2000])

    pair = compare_runs(before, after).pairs[0]
    calls, errors, tokens = pair.changes

    assert (calls.before, round(calls.after, 2), calls.beyond_noise) == (10.0, 5.33, True)
    assert calls.high < 0
    assert not errors.beyond_noise and errors.low == errors.high == 0
    assert not tokens.beyond_noise and tokens.low < 0 < tokens.high
    assert (pair.passed_before, pair.passed_after, pair.tools_differ) == ((3, 3), (3, 3), False)


def test_pairs_scored_runs_only() -> None:
    before = [_run("a", 4), _run("a", 40, status="errored", rep=2), _run("b", 3)]
    after = [_run("a", 4, tools="u"), _run("c", 2)]

    comparison = compare_runs(before, after)

    (pair,) = comparison.pairs
    assert (pair.task, pair.passed_before, pair.changes[0].before) == ("a", (1, 1), 4.0)
    assert pair.tools_differ
    assert comparison.unpaired == ("b with openai-codex::m", "c with openai-codex::m")


def test_noise_floor_skips_unvaried_metrics() -> None:
    before = _side("a", [10, 12, 14], [1000, 1000, 1000])
    after = _side("a", [10, 12, 14], [1000, 1000, 1000])

    comparison = compare_runs(before, after)

    assert set(comparison.noise_floor) == {"calls"}
    table = render_comparison(comparison)
    assert "| a | openai-codex::m | 3/3 → 3/3 | 12.0 → 12.0 (+0.0;" in table
    assert "calls ±" in table and "tokens ±" not in table


def test_baseline_keeps_listed_fields(tmp_path: Path) -> None:
    document = baseline_document(
        [_run("a", 4)], label="current-tools", date="2026-11-08", suite={"reps": 3}
    )
    path = tmp_path / "baselines" / "2026-11-08-current-tools.json"

    write_baseline(path, document)

    (run,) = load_rows(path)
    assert tuple(sorted(run)) == tuple(sorted(BASELINE_FIELDS))
    assert json.loads(path.read_text(encoding="utf-8"))["suite"] == {"reps": 3}
    with pytest.raises(FileExistsError):
        write_baseline(path, document)
    with pytest.raises(ValueError, match="at least one run"):
        baseline_document([], label="x", date="2026-11-08", suite=None)


def test_load_rows_rejects_other_json(tmp_path: Path) -> None:
    path = tmp_path / "suite.json"
    path.write_text('{"reps": 3}', encoding="utf-8")

    with pytest.raises(ValueError, match="not a baseline"):
        load_rows(path)


def test_baseline_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "runs.jsonl").write_text(json.dumps(_run("a", 4)) + "\n", encoding="utf-8")
    (results / "suite.json").write_text(
        '{"finished_at": "2026-11-08T12:00:00+00:00"}', encoding="utf-8"
    )
    monkeypatch.setattr(cli, "BASELINES_DIR", tmp_path / "baselines")

    assert cli.main(["baseline", str(results), "--label", "Bad Label"]) == 2
    assert cli.main(["baseline", str(results), "--label", "current-tools"]) == 0
    written = tmp_path / "baselines" / "2026-11-08-current-tools.json"
    assert capsys.readouterr().out.strip() == str(written)
    assert cli.main(["baseline", str(results), "--label", "current-tools"]) == 2
