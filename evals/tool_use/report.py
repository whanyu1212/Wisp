"""Summarize `runs.jsonl` into pass rates, cost, and setup-consistency warnings.

Current models pass most tasks, so the report leads with how much work a run
took (tool calls, tool errors, tokens), which is what separates two setups; pass
rates guard against a setup that breaks tasks. pass^k is the chance that k runs
of a task all pass (from tau-bench): with c passes in n scored runs,
C(c, k) / C(n, k). It shows reliability, which pass^1, the plain pass rate,
hides. Errored runs are not scored: a rate limit or a crash says nothing about
the task, so they are counted on their own.

Tokens are counted the same way for every provider (`token_split`), and a run that
had no price when it ran gets an estimate at the API list price (`estimated_usd`).
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from math import comb
from pathlib import Path
from statistics import fmean

from wisp.coding.costs import CostEstimator
from wisp.events import BillableTokenUsage, TokenUsage
from wisp.providers.catalog import ModelRegistry, builtin_catalog

_BOOTSTRAP_SAMPLES = 2000
_MILLION = Decimal(1_000_000)
# Subscription providers have no list price, so their runs are estimated at the
# API price of the same model.
LIST_PRICE_PROVIDERS: Mapping[str, str] = {"openai-codex": "openai"}
# Without a catalog the estimator prices nothing: it only sorts a provider's usage
# into the parts it bills, which is what counting tokens alike needs.
_billable_parts = CostEstimator(None)


@dataclass(frozen=True, slots=True)
class TokenSplit:
    """One run's tokens, counted the same way for every provider.

    Attributes:
        input (int): Every input token: uncached, read from the provider's cache,
            and written to it.
        cached (int): The part of ``input`` read from the cache.
        output (int): Output tokens.
    """

    input: int
    cached: int
    output: int

    @property
    def total(self) -> int:
        return self.input + self.output


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
        mean_input_tokens (float | None): Mean input tokens per scored run, cached
            ones included; see `TokenSplit`.
        mean_cached_tokens (float | None): Mean input tokens read from the cache.
        mean_output_tokens (float | None): Mean output tokens per scored run.
        known_usd (Decimal): Cost of the runs that had a price when they ran.
        estimated_usd (Decimal | None): `estimated_usd` summed over the runs that
            had none, or None when no run has an estimate.
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
    mean_input_tokens: float | None
    mean_cached_tokens: float | None
    mean_output_tokens: float | None
    known_usd: Decimal
    estimated_usd: Decimal | None

    @property
    def mean_tokens(self) -> float | None:
        """Mean input plus output tokens per scored run."""

        if self.mean_input_tokens is None or self.mean_output_tokens is None:
            return None
        return self.mean_input_tokens + self.mean_output_tokens


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
class ScriptUse:
    """One model's `codemode` scripts across its runs.

    Attributes:
        scripts (int): Scripts run.
        failed (int): Scripts that failed.
        reasons (Counter[str]): The failures by reason, from each run's
            `script_failures`. Runs recorded before that field add nothing here,
            so these can add up to less than ``failed``.
    """

    scripts: int = 0
    failed: int = 0
    reasons: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class Report:
    """Everything the report prints."""

    k: int
    task_scores: list[TaskScore] = field(default_factory=list)
    model_scores: list[ModelScore] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Per model, for on-demand MCP runs: proxied calls and how many of them failed.
    proxied: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Per model, for runs that offered codemode and used it.
    scripts: dict[str, ScriptUse] = field(default_factory=dict)


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


def token_split(row: Mapping[str, object]) -> TokenSplit:
    """Count a run's tokens the same way whichever provider reported them.

    Providers disagree on what `input_tokens` covers: OpenAI's includes the tokens
    read from its cache and Anthropic's leaves them out, along with those written
    to it. Wisp's cost accounting already sorts each provider's usage into the
    same parts, so this reuses it.

    Args:
        row (Mapping[str, object]): A run from `runs.jsonl` or a baseline. A row
            recorded before `cache_write_tokens` existed counts no cache writes.

    Returns:
        TokenSplit: The run's input, cached and output tokens. A provider Wisp's
        cost accounting does not know keeps the counts it reported.
    """

    billable = _billable(row)
    if billable is None:
        return TokenSplit(
            input=_int(row["input_tokens"]),
            cached=_int(row.get("cache_read_tokens", 0)),
            output=_int(row["output_tokens"]),
        )
    return TokenSplit(
        input=billable.input_tokens
        + billable.cache_read_input_tokens
        + billable.cache_write_input_tokens,
        cached=billable.cache_read_input_tokens,
        output=billable.output_tokens,
    )


def estimated_usd(row: Mapping[str, object], models: ModelRegistry) -> Decimal | None:
    """Estimate the cost of a run that had no price when it ran.

    The run's tokens are priced at today's API list price for its model; a
    subscription provider's model is looked up under `LIST_PRICE_PROVIDERS`. The
    base price band is used: a row holds a run's summed tokens, which could select
    a long-context band that no single request reached.

    A run with some priced responses and some unpriced ones gets no estimate: a row
    does not say which tokens the known cost covers.

    Args:
        row (Mapping[str, object]): A run from `runs.jsonl` or a baseline.
        models (ModelRegistry): The catalog whose prices to use.

    Returns:
        Decimal | None: The estimate, or None when the run already has a cost, used
        no tokens, or the catalog has no price for its model.
    """

    billable = _billable(row)
    if billable is None or Decimal(str(row["known_usd"])) > 0:
        return None
    provider = str(row["provider"])
    priced = models.pricing(
        LIST_PRICE_PROVIDERS.get(provider, provider), str(row["model"]), input_tokens=0
    )
    if priced is None:
        return None
    pricing = priced[1]
    parts = (
        (billable.input_tokens, pricing.input_usd_per_million),
        (billable.cache_read_input_tokens, pricing.cache_read_usd_per_million),
        (billable.cache_write_input_tokens, pricing.cache_write_usd_per_million),
        (billable.output_tokens, pricing.output_usd_per_million),
    )
    if not any(tokens for tokens, _ in parts):
        return None
    if any(tokens and rate is None for tokens, rate in parts):
        return None
    return sum(
        (Decimal(tokens) * rate / _MILLION for tokens, rate in parts if rate is not None),
        Decimal(0),
    )


def build_report(
    rows: Sequence[dict[str, object]],
    *,
    k: int = 3,
    seed: int = 0,
    models: ModelRegistry | None = None,
) -> Report:
    """Score every task and model in ``rows``.

    Args:
        rows (Sequence[dict[str, object]]): Rows from `runs.jsonl`.
        k (int): The k in pass^k.
        seed (int): Seed for the bootstrap, so a report is reproducible.
        models (ModelRegistry | None): The catalog that prices unpriced runs;
            Wisp's built-in catalog when None.

    Returns:
        Report: Per-task and per-model scores, and warnings about the runs.
    """

    models = models or ModelRegistry(builtin_catalog())
    report = Report(k=k)
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["task"]), f"{row['provider']}::{row['model']}")].append(row)
    for (task, model), runs in sorted(groups.items()):
        report.task_scores.append(_score_task(task, model, runs, k, models))
    rng = random.Random(seed)
    for model in sorted({score.model for score in report.task_scores}):
        scored = [
            score
            for score in report.task_scores
            if score.model == model and score.pass_1 is not None
        ]
        report.model_scores.append(_score_model(model, scored, rng))
    report.warnings.extend(_setup_warnings(rows))
    report.warnings.extend(_cache_write_warnings(rows))
    for row in rows:
        model = f"{row['provider']}::{row['model']}"
        calls = _int(row.get("proxied_calls", 0))
        if calls:
            seen_calls, seen_errors = report.proxied.get(model, (0, 0))
            errors = _int(row.get("proxied_errors", 0))
            report.proxied[model] = (seen_calls + calls, seen_errors + errors)
        scripts = _int(row.get("scripts", 0))
        if scripts:
            use = report.scripts.setdefault(model, ScriptUse())
            use.scripts += scripts
            use.failed += _int(row.get("failed_scripts", 0))
            use.reasons.update(_counts(row.get("script_failures", {})))
    return report


def render_markdown(report: Report) -> str:
    """Render a report as Markdown tables."""

    lines = [
        "| Task | Model | Mean calls | Tool errors | Mean input | Cached | Mean output | Passed "
        f"| pass^1 | pass^{report.k} | Errored | Timed out | Known USD | Est. USD |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for score in report.task_scores:
        estimate = "—" if score.estimated_usd is None else f"~{score.estimated_usd:.4f}"
        lines.append(
            f"| {score.task} | {score.model} | {_number(score.mean_calls, 1)} "
            f"| {_number(score.mean_tool_errors, 1)} | {_number(score.mean_input_tokens)} "
            f"| {_number(score.mean_cached_tokens)} | {_number(score.mean_output_tokens)} "
            f"| {score.passed}/{score.scored} | {_percent(score.pass_1)} "
            f"| {_percent(score.pass_k)} | {score.errored} | {score.timed_out} "
            f"| {score.known_usd} | {estimate} |"
        )
    if any(score.estimated_usd is not None for score in report.task_scores):
        mapped = ", ".join(f"{name} at {priced}'s" for name, priced in LIST_PRICE_PROVIDERS.items())
        lines += [
            "",
            "Est. USD prices the runs that had no price when they ran at today's API list "
            f"price for the same model ({mapped}). It is an estimate, not what was charged.",
        ]
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
    if report.proxied:
        lines += ["", "| Model | Proxied MCP calls | Failed |", "|---|---|---|"]
        for label, (calls, errors) in sorted(report.proxied.items()):
            lines.append(f"| {label} | {calls} | {errors} ({errors / calls:.0%}) |")
    if report.scripts:
        lines += ["", "| Model | Scripts | Failed | Why they failed |", "|---|---|---|---|"]
        for label, use in sorted(report.scripts.items()):
            lines.append(f"| {label} | {use.scripts} | {use.failed} | {_reasons(use)} |")
    if report.warnings:
        lines += ["", "Warnings:", *(f"- {warning}" for warning in report.warnings)]
    return "\n".join(lines)


def _score_task(
    task: str, model: str, runs: Sequence[dict[str, object]], k: int, models: ModelRegistry
) -> TaskScore:
    scored = [run for run in runs if run["status"] != "errored"]
    passed = sum(1 for run in scored if run["status"] == "passed")
    tokens = [token_split(run) for run in scored]
    estimates = [estimate for run in runs if (estimate := estimated_usd(run, models)) is not None]
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
        mean_input_tokens=fmean(split.input for split in tokens) if tokens else None,
        mean_cached_tokens=fmean(split.cached for split in tokens) if tokens else None,
        mean_output_tokens=fmean(split.output for split in tokens) if tokens else None,
        known_usd=sum((Decimal(str(run["known_usd"])) for run in runs), Decimal(0)),
        estimated_usd=sum(estimates, Decimal(0)) if estimates else None,
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


def _cache_write_warnings(rows: Iterable[Mapping[str, object]]) -> list[str]:
    """Flag runs whose input tokens may leave out cache writes.

    Only rows recorded before `cache_write_tokens` existed, from a provider that
    counts cached tokens outside `input_tokens`, can be missing any. Such a run can
    have written to the cache without reading from it, so its own counts do not say.
    """

    missing = sum(
        1
        for row in rows
        if "cache_write_tokens" not in row and _counts_cache_outside_input(str(row["provider"]))
    )
    if not missing:
        return []
    return [
        "runs recorded before cache writes were counted, whose input tokens leave "
        f"them out: {missing}"
    ]


def _counts_cache_outside_input(provider: str) -> bool:
    """Whether a provider reports cached tokens beside `input_tokens`, not within it."""

    cached = TokenUsage(input_tokens=1, output_tokens=0, total_tokens=1, cache_read_input_tokens=1)
    billable = _billable_parts(provider, None, None, cached).billable
    # A provider that counts them within its input bills that one token as cached only.
    return billable is not None and billable.input_tokens == 1


def _billable(row: Mapping[str, object]) -> BillableTokenUsage | None:
    """Sort a row's token counts into the parts its provider bills, or None when it cannot."""

    reported_input, output = _int(row["input_tokens"]), _int(row["output_tokens"])
    usage = TokenUsage(
        input_tokens=reported_input,
        output_tokens=output,
        total_tokens=reported_input + output,
        cache_read_input_tokens=_int(row.get("cache_read_tokens", 0)),
        cache_write_input_tokens=_int(row.get("cache_write_tokens", 0)),
    )
    return _billable_parts(str(row["provider"]), None, None, usage).billable


def _reasons(use: ScriptUse) -> str:
    """List a model's script failures by reason, most frequent first."""

    listed = [f"{reason} ({count})" for reason, count in use.reasons.most_common()]
    unrecorded = use.failed - sum(use.reasons.values())
    if unrecorded:
        listed.append(f"not recorded ({unrecorded})")
    return ", ".join(listed) or "—"


def _int(value: object) -> int:
    return int(str(value))


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _number(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def count_sum(counts: object) -> int:
    """Sum a row's per-name counts, such as `tool_calls`."""

    return sum(_counts(counts).values())


def _counts(counts: object) -> dict[str, int]:
    if not isinstance(counts, dict):
        raise ValueError(f"expected a mapping of counts, got {counts!r}")
    return {str(name): int(count) for name, count in counts.items()}
