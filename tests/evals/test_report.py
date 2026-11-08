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


def _row(
    task: str,
    status: str,
    *,
    rep: int,
    tools: str = "t",
    timed_out: bool = False,
    calls: int = 4,
    tool_errors: int = 0,
) -> dict:
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
        "tool_calls": {"read": calls - 1, "edit": 1},
        "tool_errors": {"not_found": tool_errors} if tool_errors else {},
        "tools_sha256": tools,
        "instructions_sha256": f"i{rep}",
    }


def test_errored_runs_not_scored() -> None:
    rows = [
        _row("a", "passed", rep=1),
        _row("a", "failed", rep=2, timed_out=True, calls=8, tool_errors=1),
        _row("a", "errored", rep=3, calls=40, tool_errors=9),
        _row("b", "passed", rep=1, calls=2),
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
    assert (first.mean_calls, first.mean_tool_errors) == (6.0, 0.5)
    assert (second.pass_1, second.pass_k, second.mean_calls) == (1.0, None, 2.0)
    model = report.model_scores[0]
    assert (model.pass_1, model.mean_calls, model.mean_tokens) == (0.75, 4.0, 120.0)
    assert report.warnings == []
    table = render_markdown(report)
    assert "| a | openai::m | 6.0 | 0.5 | 120 | 1/2 | 50% | 0% | 1 | 1 | 1.5 |" in table
    assert "| openai::m | 2 | 4.0 | 120 | 75% |" in table


def test_differing_tools_warned() -> None:
    rows = [_row("a", "passed", rep=1), _row("a", "passed", rep=2, tools="u")]

    assert build_report(rows).warnings == ["a with openai::m: runs offered 2 different tool sets"]


def test_reports_proxied_calls() -> None:
    rows = [_row("a", "passed", rep=1), _row("a", "passed", rep=2)]
    rows[0].update(proxied_calls=4, proxied_errors=1)

    table = render_markdown(build_report(rows))

    assert "| openai::m | 4 | 1 (25%) |" in table
    assert "Proxied" not in render_markdown(build_report([_row("a", "passed", rep=1)]))
