"""Summarize `runs.jsonl` into pass rates, cost, and setup-consistency warnings.

pass^k is the chance that k runs of a task all pass (from tau-bench): with c
passes in n scored runs, C(c, k) / C(n, k). It shows reliability, which pass^1,
the plain pass rate, hides. Errored runs are not scored: a rate limit or a crash
says nothing about the task, so they are counted on their own.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from math import comb
from pathlib import Path
from statistics import fmean

_BOOTSTRAP_SAMPLES = 2000


@dataclass(frozen=True, slots=True)
class TaskScore:
    """One task with one model.

    Attributes:
        task (str): Task id.
        model (str): `provider::model`.
        scored (int): Runs that passed or failed.
        passed (int): Runs that passed.
        errored (int): Runs left out because they errored.
        timed_out (int): Scored runs stopped by their deadline.
        pass_1 (float | None): Pass rate over scored runs.
        pass_k (float | None): pass^k, or None with fewer than k scored runs.
        mean_tokens (float | None): Mean input plus output tokens per scored run.
        known_usd (Decimal): Cost of the priced runs.
    """

    task: str
    model: str
    scored: int
    passed: int
    errored: int
    timed_out: int
    pass_1: float | None
    pass_k: float | None
    mean_tokens: float | None
    known_usd: Decimal


@dataclass(frozen=True, slots=True)
class ModelScore:
    """One model across tasks: the mean of per-task pass^1, with a 95% interval.

    The interval resamples tasks, so it reflects how much the result depends on
    which tasks happened to be in the suite.
    """

    model: str
    tasks: int
    pass_1: float | None
    low: float | None
    high: float | None


@dataclass(slots=True)
class Report:
    """Everything the report prints."""

    k: int
    task_scores: list[TaskScore] = field(default_factory=list)
    model_scores: list[ModelScore] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def pass_hat_k(passed: int, scored: int, k: int) -> float | None:
    """Return C(passed, k) / C(scored, k), or None with fewer than k scored runs."""

    if scored < k or k < 1:
        return None
    return comb(passed, k) / comb(scored, k)


def load_rows(path: Path) -> list[dict[str, object]]:
    """Read the rows of a `runs.jsonl` file."""

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def build_report(rows: Sequence[dict[str, object]], *, k: int = 3, seed: int = 0) -> Report:
    """Score every task and model in ``rows``.

    Args:
        rows (Sequence[dict[str, object]]): Rows from `runs.jsonl`.
        k (int): The k in pass^k.
        seed (int): Seed for the bootstrap, so a report is reproducible.

    Returns:
        Report: Per-task and per-model scores, and warnings about the runs.
    """

    report = Report(k=k)
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["task"]), f"{row['provider']}::{row['model']}")].append(row)
    for (task, model), runs in sorted(groups.items()):
        report.task_scores.append(_score_task(task, model, runs, k))
    rng = random.Random(seed)
    for model in sorted({score.model for score in report.task_scores}):
        rates = [
            score.pass_1
            for score in report.task_scores
            if score.model == model and score.pass_1 is not None
        ]
        report.model_scores.append(_score_model(model, rates, rng))
    report.warnings.extend(_setup_warnings(rows))
    return report


def render_markdown(report: Report) -> str:
    """Render a report as Markdown tables."""

    lines = [
        f"| Task | Model | Passed | pass^1 | pass^{report.k} | Errored | Timed out "
        "| Mean tokens | Known USD |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for score in report.task_scores:
        lines.append(
            f"| {score.task} | {score.model} | {score.passed}/{score.scored} "
            f"| {_percent(score.pass_1)} | {_percent(score.pass_k)} | {score.errored} "
            f"| {score.timed_out} | {_number(score.mean_tokens)} | {score.known_usd} |"
        )
    lines += ["", "| Model | Tasks | Mean pass^1 | 95% interval |", "|---|---|---|---|"]
    for model in report.model_scores:
        interval = (
            f"{_percent(model.low)} – {_percent(model.high)}" if model.low is not None else "—"
        )
        lines.append(f"| {model.model} | {model.tasks} | {_percent(model.pass_1)} | {interval} |")
    if report.warnings:
        lines += ["", "Warnings:", *(f"- {warning}" for warning in report.warnings)]
    return "\n".join(lines)


def _score_task(task: str, model: str, runs: Sequence[dict[str, object]], k: int) -> TaskScore:
    scored = [run for run in runs if run["status"] != "errored"]
    passed = sum(1 for run in scored if run["status"] == "passed")
    tokens = [int(str(run["input_tokens"])) + int(str(run["output_tokens"])) for run in scored]
    return TaskScore(
        task=task,
        model=model,
        scored=len(scored),
        passed=passed,
        errored=len(runs) - len(scored),
        timed_out=sum(1 for run in scored if run["timed_out"]),
        pass_1=passed / len(scored) if scored else None,
        pass_k=pass_hat_k(passed, len(scored), k),
        mean_tokens=fmean(tokens) if tokens else None,
        known_usd=sum((Decimal(str(run["known_usd"])) for run in runs), Decimal(0)),
    )


def _score_model(model: str, rates: Sequence[float], rng: random.Random) -> ModelScore:
    if not rates:
        return ModelScore(model=model, tasks=0, pass_1=None, low=None, high=None)
    means = sorted(fmean(rng.choices(rates, k=len(rates))) for _ in range(_BOOTSTRAP_SAMPLES))
    return ModelScore(
        model=model,
        tasks=len(rates),
        pass_1=fmean(rates),
        low=means[int(0.025 * len(means))],
        high=means[int(0.975 * len(means)) - 1],
    )


def _setup_warnings(rows: Iterable[dict[str, object]]) -> list[str]:
    """Flag a task and model whose runs offered different tools.

    Each task picks its own tools, so the tools hash must match only within one
    task and model. The instructions hash is recorded but not compared: it
    covers project context, which names each run's own temporary folder.
    """

    tools: dict[tuple[str, str], set[object]] = defaultdict(set)
    for row in rows:
        if row["tools_sha256"] is not None:
            tools[(str(row["task"]), f"{row['provider']}::{row['model']}")].add(row["tools_sha256"])
    return [
        f"{task} with {model}: runs offered {len(hashes)} different tool sets"
        for (task, model), hashes in sorted(tools.items())
        if len(hashes) > 1
    ]


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"
