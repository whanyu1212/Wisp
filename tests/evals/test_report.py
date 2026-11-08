"""Scoring runs (`evals.tool_use.report`)."""

from __future__ import annotations

import pytest

from evals.tool_use.report import build_report, pass_hat_k, render_markdown


@pytest.mark.parametrize(
    ("passed", "scored", "k", "expected"),
    [(3, 3, 3, 1.0), (2, 3, 3, 0.0), (3, 4, 2, 0.5), (1, 2, 3, None), (0, 0, 1, None)],
)
def test_pass_hat_k(passed: int, scored: int, k: int, expected: float | None) -> None:
    assert pass_hat_k(passed, scored, k) == expected


def _row(task: str, status: str, *, rep: int, tools: str = "t", timed_out: bool = False) -> dict:
    return {
        "task": task,
        "provider": "openai",
        "model": "m",
        "rep": rep,
        "status": status,
        "timed_out": timed_out,
        "input_tokens": 100,
        "output_tokens": 20,
        "known_usd": "0.5",
        "tools_sha256": tools,
        "instructions_sha256": f"i{rep}",
    }


def test_errored_runs_not_scored() -> None:
    rows = [
        _row("a", "passed", rep=1),
        _row("a", "failed", rep=2, timed_out=True),
        _row("a", "errored", rep=3),
        _row("b", "passed", rep=1),
    ]

    report = build_report(rows, k=2)

    first, second = report.task_scores
    assert (first.task, first.scored, first.passed, first.errored, first.timed_out) == (
        "a",
        2,
        1,
        1,
        1,
    )
    assert (first.pass_1, first.pass_k, str(first.known_usd)) == (0.5, 0.0, "1.5")
    assert (second.pass_1, second.pass_k) == (1.0, None)
    assert report.model_scores[0].pass_1 == 0.75
    assert report.warnings == []
    assert "| a | openai::m | 1/2 | 50% | 0% | 1 | 1 |" in render_markdown(report)


def test_differing_tools_warned() -> None:
    rows = [_row("a", "passed", rep=1), _row("a", "passed", rep=2, tools="u")]

    assert build_report(rows).warnings == ["a with openai::m: runs offered 2 different tool sets"]
