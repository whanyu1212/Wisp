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
        "mcp_tool_exposure": "direct",
        "proxied_calls": 0,
        "proxied_errors": 0,
        "proxied_argument_errors": 0,
        "run_dir": "a/b/rep1",
        "error": None,
        "credential_source": "stored",
        "codemode": "off",
        "scripts": 0,
        "failed_scripts": 0,
        "nested_calls": 0,
    }


def _side(task: str, calls: list[int], tokens: list[int]) -> list[dict[str, object]]:
    return [
        _run(task, count, tokens=amount, rep=rep)
        for rep, (count, amount) in enumerate(zip(calls, tokens, strict=True), start=1)
    ]


def test_marks_only_changes_beyond_noise() -> None:
    before = _side("a", [10, 10, 11], [1000, 4000, 2500])
    after = _side("a", [5, 5, 6], [1500, 3500, 2000])

    pair = compare_runs(before, after).pairs[0]
    calls, errors, tokens = pair.changes

    assert calls.beyond_noise and calls.interval is not None and calls.interval[1] < 0
    assert errors.interval == (0.0, 0.0) and not errors.beyond_noise
    assert not tokens.beyond_noise and tokens.interval is not None
    assert tokens.interval[0] < 0 < tokens.interval[1]
    assert (pair.passed_before, pair.passed_after, pair.tools_differ) == ((3, 3), (3, 3), False)


def test_interval_widens_for_few_runs() -> None:
    # Welch t: difference 2, standard error sqrt(1/3 + 1/3), 4 degrees of freedom.
    change = compare_runs(_side("a", [4, 5, 6], [1] * 3), _side("a", [6, 7, 8], [1] * 3))
    low, high = change.pairs[0].changes[0].interval or (0.0, 0.0)

    margin = 2.776 * (2 / 3) ** 0.5
    assert (round(low, 3), round(high, 3)) == (round(2 - margin, 3), round(2 + margin, 3))


@pytest.mark.parametrize(("before", "after"), [([10], [5]), ([10], [5, 6, 5])])
def test_no_interval_below_two_runs(before: list[int], after: list[int]) -> None:
    comparison = compare_runs(
        _side("a", before, [1] * len(before)), _side("a", after, [1] * len(after))
    )

    calls = comparison.pairs[0].changes[0]
    assert (calls.interval, calls.beyond_noise) == (None, False)
    assert "(-" in render_comparison(comparison) and "; —)" in render_comparison(comparison)


def test_pairs_scored_runs_only() -> None:
    before = [_run("a", 4), _run("a", 40, status="errored", rep=2), _run("b", 3)]
    before.append(_run("d", 3, status="errored"))
    after = [_run("a", 4, tools="u"), _run("c", 2), _run("d", 3, status="errored")]

    comparison = compare_runs(before, after)

    (pair,) = comparison.pairs
    assert (pair.task, pair.passed_before, pair.errored) == ("a", (1, 1), (1, 0))
    assert pair.changes[0].before == 4.0 and pair.tools_differ
    assert comparison.only_before == ("b with openai-codex::m",)
    assert comparison.only_after == ("c with openai-codex::m",)
    assert comparison.never_scored == ("d with openai-codex::m",)
    table = render_comparison(comparison)
    assert "| a (tools differ) | openai-codex::m | 1/1 → 1/1 (errored 1 → 0) |" in table
    assert "Scored only before:\n- b with" in table and "Scored only after:\n- c with" in table


def test_noise_floor_and_marks_rendered() -> None:
    before = _side("a", [10, 12, 14], [1000, 1000, 1000])
    after = _side("a", [20, 22, 24], [1000, 1000, 1000])

    comparison = compare_runs(before, after)

    # Half-width 2.776 * sqrt(4/3 + 4/3) over the earlier mean of 12; tokens never varied.
    assert set(comparison.noise_floor) == {"calls"}
    assert round(comparison.noise_floor["calls"], 4) == round(2.776 * (8 / 3) ** 0.5 / 12, 4)
    table = render_comparison(comparison)
    assert "12.0 → 22.0 (+10.0; " in table and ") *" in table
    assert "Of these 3 intervals, expect about 0 to be marked by chance" in table


def test_baseline_keeps_listed_fields(tmp_path: Path) -> None:
    document = baseline_document(
        [_run("a", 4)],
        label="current-tools",
        date="2026-11-08",
        suite={"reps": 3, "credential_sources": {"openai": "environment"}},
    )
    path = tmp_path / "baselines" / "2026-11-08-current-tools.json"

    write_baseline(path, document)

    (run,) = load_rows(path)
    assert tuple(sorted(run)) == tuple(sorted(BASELINE_FIELDS))
    assert json.loads(path.read_text(encoding="utf-8"))["suite"] == {"reps": 3}
    with pytest.raises(FileExistsError):
        write_baseline(path, document)
    with pytest.raises(ValueError, match="at least one run"):
        baseline_document([], label="x", date="2026-11-08", suite={})


@pytest.mark.parametrize("text", ['{"reps": 3}', "[1, 2]"], ids=["no-runs", "list"])
def test_load_rows_rejects_other_json(tmp_path: Path, text: str) -> None:
    path = tmp_path / "suite.JSON"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="not a baseline"):
        load_rows(path)


def test_baseline_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    (results / "runs.jsonl").write_text(json.dumps(_run("a", 4)) + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "BASELINES_DIR", tmp_path / "baselines")

    assert cli.main(["baseline", str(results), "--label", "current-tools"]) == 2
    assert "has no suite.json" in capsys.readouterr().err
    (results / "suite.json").write_text(
        '{"finished_at": "2026-11-08T12:00:00+00:00", "reps": 3}', encoding="utf-8"
    )
    assert cli.main(["baseline", str(results), "--label", "Bad Label"]) == 2
    assert cli.main(["baseline", str(results), "--label", "current-tools"]) == 0
    written = tmp_path / "baselines" / "2026-11-08-current-tools.json"
    assert capsys.readouterr().out.strip() == str(written)
    document = json.loads(written.read_text(encoding="utf-8"))
    assert (document["date"], document["suite"], len(document["runs"])) == (
        "2026-11-08",
        {"finished_at": "2026-11-08T12:00:00+00:00", "reps": 3},
        1,
    )
    assert cli.main(["baseline", str(results), "--label", "current-tools"]) == 2


def test_compare_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = tmp_path / "runs.jsonl"
    runs.write_text("".join(json.dumps(row) + "\n" for row in _side("a", [4, 5], [9, 9])))

    assert cli.main(["compare", str(runs), str(runs)]) == 0
    assert "| a | openai-codex::m | 2/2 → 2/2 |" in capsys.readouterr().out
    assert cli.main(["compare", str(tmp_path / "missing"), str(runs)]) == 2
    assert capsys.readouterr().err.startswith("error: ")


def test_baseline_tolerates_older_rows() -> None:
    row = _run("a", 4)
    for newer in (
        "mcp_tool_exposure",
        "proxied_calls",
        "proxied_errors",
        "proxied_argument_errors",
    ):
        row.pop(newer, None)

    (run,) = baseline_document([row], label="old", date="2026-11-08", suite={})["runs"]  # type: ignore[misc]

    assert "proxied_calls" not in run and run["task"] == "a"
