"""Summarize `runs.jsonl` into pass rates, cost, and setup-consistency warnings.

Current models pass most tasks, so the report leads with how much work a run
took (tool calls, tool errors, tokens), which is what separates two setups; pass
rates guard against a setup that breaks tasks. pass^k is the chance that k runs
of a task all pass (from tau-bench): with c passes in n scored runs,
C(c, k) / C(n, k). It shows reliability, which pass^1, the plain pass rate,
hides. Errored runs are not scored: a rate limit or a crash says nothing about
the task, so they are counted on their own.
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
        mean_calls (float | None): Mean tool calls per scored run.
        mean_tool_errors (float | None): Mean failed tool calls per scored run.
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
    mean_calls: float | None
    mean_tool_errors: float | None
    mean_tokens: float | None
    known_usd: Decimal


@dataclass(frozen=True, slots=True)
class ModelScore:
    """One model across tasks: means of the per-task scores.

    The 95% interval on pass^1 resamples tasks, so it reflects how much the
    result depends on which tasks happened to be in the suite. Mean calls and
    tokens weigh each task equally; compare them only across the same tasks.
    """

    model: str
    tasks: int
    pass_1: float | None
    low: float | None
    high: float | None
    mean_calls: float | None
    mean_tokens: float | None


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
    """Read runs from a results folder, its `runs.jsonl`, or a baseline JSON file.

    Raises:
        ValueError: When a baseline file holds no `runs` list.
    """

    if path.is_dir():
        path = path / "runs.jsonl"
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".json":
        return [json.loads(line) for line in text.splitlines() if line]
    document = json.loads(text)
    runs = document.get("runs") if isinstance(document, dict) else None
    if not isinstance(runs, list):
        raise ValueError(f"{path} is not a baseline: it has no runs list")
    return runs


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
        scored = [
            score
            for score in report.task_scores
            if score.model == model and score.pass_1 is not None
        ]
        report.model_scores.append(_score_model(model, scored, rng))
    report.warnings.extend(_setup_warnings(rows))
    return report


def render_markdown(report: Report) -> str:
    """Render a report as Markdown tables."""

    lines = [
        "| Task | Model | Mean calls | Tool errors | Mean tokens | Passed | pass^1 "
        f"| pass^{report.k} | Errored | Timed out | Known USD |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for score in report.task_scores:
        lines.append(
            f"| {score.task} | {score.model} | {_number(score.mean_calls, 1)} "
            f"| {_number(score.mean_tool_errors, 1)} | {_number(score.mean_tokens)} "
            f"| {score.passed}/{score.scored} | {_percent(score.pass_1)} "
            f"| {_percent(score.pass_k)} | {score.errored} | {score.timed_out} "
            f"| {score.known_usd} |"
        )
    lines += [
        "",
        "| Model | Tasks | Mean calls | Mean tokens | Mean pass^1 | 95% interval |",
        "|---|---|---|---|---|---|",
    ]
    for model in report.model_scores:
        interval = (
            f"{_percent(model.low)} – {_percent(model.high)}" if model.low is not None else "—"
        )
        lines.append(
            f"| {model.model} | {model.tasks} | {_number(model.mean_calls, 1)} "
            f"| {_number(model.mean_tokens)} | {_percent(model.pass_1)} | {interval} |"
        )
    if report.warnings:
        lines += ["", "Warnings:", *(f"- {warning}" for warning in report.warnings)]
    return "\n".join(lines)


def _score_task(task: str, model: str, runs: Sequence[dict[str, object]], k: int) -> TaskScore:
    scored = [run for run in runs if run["status"] != "errored"]
    passed = sum(1 for run in scored if run["status"] == "passed")
    tokens = [int(str(run["input_tokens"])) + int(str(run["output_tokens"])) for run in scored]
    calls = [count_sum(run["tool_calls"]) for run in scored]
    tool_errors = [count_sum(run["tool_errors"]) for run in scored]
    return TaskScore(
        task=task,
        model=model,
        scored=len(scored),
        passed=passed,
        errored=len(runs) - len(scored),
        timed_out=sum(1 for run in scored if run["timed_out"]),
        pass_1=passed / len(scored) if scored else None,
        pass_k=pass_hat_k(passed, len(scored), k),
        mean_calls=fmean(calls) if calls else None,
        mean_tool_errors=fmean(tool_errors) if tool_errors else None,
        mean_tokens=fmean(tokens) if tokens else None,
        known_usd=sum((Decimal(str(run["known_usd"])) for run in runs), Decimal(0)),
    )


def _score_model(model: str, scores: Sequence[TaskScore], rng: random.Random) -> ModelScore:
    """Average one model's task scores; ``scores`` holds only tasks with scored runs."""

    if not scores:
        return ModelScore(
            model=model,
            tasks=0,
            pass_1=None,
            low=None,
            high=None,
            mean_calls=None,
            mean_tokens=None,
        )
    rates = [score.pass_1 for score in scores if score.pass_1 is not None]
    means = sorted(fmean(rng.choices(rates, k=len(rates))) for _ in range(_BOOTSTRAP_SAMPLES))
    return ModelScore(
        model=model,
        tasks=len(scores),
        pass_1=fmean(rates),
        low=means[int(0.025 * len(means))],
        high=means[int(0.975 * len(means)) - 1],
        mean_calls=fmean(score.mean_calls for score in scores if score.mean_calls is not None),
        mean_tokens=fmean(score.mean_tokens for score in scores if score.mean_tokens is not None),
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


def _number(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def count_sum(counts: object) -> int:
    """Sum a row's per-name counts, such as `tool_calls`."""

    if not isinstance(counts, dict):
        raise ValueError(f"expected a mapping of counts, got {counts!r}")
    return sum(int(count) for count in counts.values())
