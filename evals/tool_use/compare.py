"""Compare two sets of runs task by task, and save a set as a committed baseline.

`compare` pairs runs by task and model. For each metric it reports the change in
the mean and a 95% Welch t-interval for that change, so a reader can tell a real
difference from run-to-run noise. A t-interval, not a bootstrap: with the two or
three runs a side this suite can afford, a bootstrap interval is far too narrow,
while the t distribution widens it for the few runs it rests on. A side needs at
least two scored runs for an interval. Each interval is 95% on its own, so across
the many cells of one comparison about one in twenty is marked by chance.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median, variance

from evals.tool_use.report import count_sum

# The fields a baseline keeps from each run, and from the suite's settings.
# Folders, error text and credential sources stay out of the repository.
BASELINE_FIELDS = (
    "task",
    "provider",
    "model",
    "rep",
    "status",
    "timed_out",
    "seconds",
    "turns",
    "tool_calls",
    "tool_errors",
    "reruns",
    "used_read_output",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "known_usd",
    "tools_sha256",
    "wisp_version",
    "git_commit",
)
SUITE_FIELDS = (
    "models",
    "tasks",
    "reps",
    "wisp_version",
    "git_commit",
    "stopped_for_budget",
    "skipped_runs",
    "finished_at",
)

# Two-sided 95% critical values of Student's t for 1 to 30 degrees of freedom.
_T_95 = (
    12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
    2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
    2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
)  # fmt: skip
_CHANCE = 0.05

type Row = Mapping[str, object]


def _calls(row: Row) -> float:
    return float(count_sum(row["tool_calls"]))


def _tool_errors(row: Row) -> float:
    return float(count_sum(row["tool_errors"]))


def _tokens(row: Row) -> float:
    return float(int(str(row["input_tokens"])) + int(str(row["output_tokens"])))


@dataclass(frozen=True, slots=True)
class Metric:
    """A compared metric.

    Attributes:
        name (str): The column name.
        value (Callable[[Row], float]): The metric's value for one run.
        digits (int): Decimals shown.
        in_noise_floor (bool): Whether the noise floor covers it. Tool errors are
            mostly zero, so a share of their mean says little.
    """

    name: str
    value: Callable[[Row], float]
    digits: int
    in_noise_floor: bool


METRICS = (
    Metric("calls", _calls, 1, in_noise_floor=True),
    Metric("tool errors", _tool_errors, 1, in_noise_floor=False),
    Metric("tokens", _tokens, 0, in_noise_floor=True),
)


@dataclass(frozen=True, slots=True)
class MetricChange:
    """How one metric moved for one task and model.

    Attributes:
        metric (str): The metric's name, from `METRICS`.
        before (float): Mean over the earlier runs.
        after (float): Mean over the later runs.
        interval (tuple[float, float] | None): The 95% interval for
            ``after - before``, or None when a side has fewer than two runs.
    """

    metric: str
    before: float
    after: float
    interval: tuple[float, float] | None

    @property
    def change(self) -> float:
        return self.after - self.before

    @property
    def beyond_noise(self) -> bool:
        """Whether there is an interval and it excludes no change."""

        return self.interval is not None and (self.interval[0] > 0 or self.interval[1] < 0)


@dataclass(frozen=True, slots=True)
class PairComparison:
    """One task and model, before and after.

    Attributes:
        task (str): Task id.
        model (str): `provider::model`.
        passed_before (tuple[int, int]): Passed and scored runs before.
        passed_after (tuple[int, int]): Passed and scored runs after.
        errored (tuple[int, int]): Errored runs before and after, left out of the metrics.
        changes (tuple[MetricChange, ...]): One per metric in `METRICS`.
        tools_differ (bool): Whether the two sides offered different tool sets,
            as expected when the change under test is the tools themselves.
    """

    task: str
    model: str
    passed_before: tuple[int, int]
    passed_after: tuple[int, int]
    errored: tuple[int, int]
    changes: tuple[MetricChange, ...]
    tools_differ: bool


@dataclass(frozen=True, slots=True)
class Comparison:
    """Every paired task and model, and what could not be paired.

    Attributes:
        pairs (tuple[PairComparison, ...]): Paired results, sorted by task and model.
        only_before (tuple[str, ...]): `task with model` entries scored only before.
        only_after (tuple[str, ...]): Entries scored only after.
        never_scored (tuple[str, ...]): Entries whose every run errored on both sides.
        noise_floor (Mapping[str, float]): For `in_noise_floor` metrics, the median
            interval half-width as a fraction of the earlier mean: changes
            smaller than this are within noise at these run counts.
    """

    pairs: tuple[PairComparison, ...]
    only_before: tuple[str, ...]
    only_after: tuple[str, ...]
    never_scored: tuple[str, ...]
    noise_floor: Mapping[str, float]

    @property
    def intervals(self) -> int:
        """How many changes have an interval, so could be marked."""

        return sum(change.interval is not None for pair in self.pairs for change in pair.changes)


def compare_runs(before: Sequence[Row], after: Sequence[Row]) -> Comparison:
    """Pair two sets of runs by task and model and measure each metric's change.

    Errored runs are left out of the metrics, as in the report: they say nothing
    about the task. Each pair still counts them.

    Args:
        before (Sequence[Row]): Runs of the earlier setup, such as a baseline's.
        after (Sequence[Row]): Runs of the setup under test.

    Returns:
        Comparison: Paired results, what could not be paired, and the noise floor.
    """

    earlier, later = _groups(before), _groups(after)
    scored_before = {key for key, runs in earlier.items() if _scored(runs)}
    scored_after = {key for key, runs in later.items() if _scored(runs)}
    pairs: list[PairComparison] = []
    for key in sorted(scored_before & scored_after):
        old, new = _scored(earlier[key]), _scored(later[key])
        pairs.append(
            PairComparison(
                task=key[0],
                model=key[1],
                passed_before=(_passed(old), len(old)),
                passed_after=(_passed(new), len(new)),
                errored=(len(earlier[key]) - len(old), len(later[key]) - len(new)),
                changes=tuple(
                    _change(
                        metric.name,
                        [metric.value(run) for run in old],
                        [metric.value(run) for run in new],
                    )
                    for metric in METRICS
                ),
                tools_differ=_tool_sets(old) != _tool_sets(new),
            )
        )
    return Comparison(
        pairs=tuple(pairs),
        only_before=_labels(scored_before - scored_after),
        only_after=_labels(scored_after - scored_before),
        never_scored=_labels((earlier.keys() | later.keys()) - scored_before - scored_after),
        noise_floor=_noise_floor(pairs),
    )


def render_comparison(comparison: Comparison) -> str:
    """Render a comparison as Markdown; changes beyond noise are marked `*`."""

    lines = [
        "| Task | Model | Passed | "
        + " | ".join(f"{metric.name} (95% interval)" for metric in METRICS)
        + " |",
        "|---|---|---|" + "---|" * len(METRICS),
    ]
    for pair in comparison.pairs:
        cells = [
            _change_cell(change, metric.digits)
            for change, metric in zip(pair.changes, METRICS, strict=True)
        ]
        task = f"{pair.task} (tools differ)" if pair.tools_differ else pair.task
        lines.append(
            f"| {task} | {pair.model} | {_passed_cell(pair)} | " + " | ".join(cells) + " |"
        )
    expected = _CHANCE * comparison.intervals
    lines += [
        "",
        f"`*` marks a change whose 95% interval excludes zero. Of these {comparison.intervals} "
        f"intervals, expect about {expected:.0f} to be marked by chance alone. — means a side "
        "has fewer than two scored runs, too few for an interval.",
    ]
    if comparison.noise_floor:
        floors = ", ".join(
            f"{name} ±{fraction:.0%}" for name, fraction in comparison.noise_floor.items()
        )
        lines.append(
            f"Noise floor at these run counts (median interval half-width over the earlier "
            f"mean): {floors}. Read smaller changes as no change."
        )
    for title, entries in (
        ("Scored only before:", comparison.only_before),
        ("Scored only after:", comparison.only_after),
        ("Every run errored on both sides:", comparison.never_scored),
    ):
        if entries:
            lines += ["", title, *(f"- {entry}" for entry in entries)]
    return "\n".join(lines)


def baseline_document(
    rows: Sequence[Row], *, label: str, date: str, suite: Mapping[str, object]
) -> dict[str, object]:
    """Build a baseline: the suite's `SUITE_FIELDS` and each run's `BASELINE_FIELDS`.

    Args:
        rows (Sequence[Row]): Runs from one `runs.jsonl`.
        label (str): What the baseline measures, such as `current-tools`.
        date (str): The date it was recorded, `YYYY-MM-DD`.
        suite (Mapping[str, object]): The run's `suite.json`.

    Returns:
        dict[str, object]: The document to write as `baselines/<date>-<label>.json`.

    Raises:
        ValueError: When ``rows`` is empty.
    """

    if not rows:
        raise ValueError("a baseline needs at least one run")
    return {
        "label": label,
        "date": date,
        "suite": {field: suite[field] for field in SUITE_FIELDS if field in suite},
        "runs": [{field: row[field] for field in BASELINE_FIELDS} for row in rows],
    }


def write_baseline(path: Path, document: Mapping[str, object]) -> None:
    """Write a baseline document, refusing to replace an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=1, sort_keys=True) + "\n")


def _groups(rows: Sequence[Row]) -> dict[tuple[str, str], list[Row]]:
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        groups[(str(row["task"]), f"{row['provider']}::{row['model']}")].append(row)
    return groups


def _scored(rows: Sequence[Row]) -> list[Row]:
    return [row for row in rows if row["status"] != "errored"]


def _labels(keys: set[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(f"{task} with {model}" for task, model in sorted(keys))


def _passed(rows: Sequence[Row]) -> int:
    return sum(1 for row in rows if row["status"] == "passed")


def _tool_sets(rows: Sequence[Row]) -> set[object]:
    return {row["tools_sha256"] for row in rows if row["tools_sha256"] is not None}


def _change(name: str, before: Sequence[float], after: Sequence[float]) -> MetricChange:
    """Measure one metric's change, with a Welch t-interval when both sides allow one."""

    change = MetricChange(metric=name, before=fmean(before), after=fmean(after), interval=None)
    if len(before) < 2 or len(after) < 2:
        return change
    before_term, after_term = variance(before) / len(before), variance(after) / len(after)
    error = math.sqrt(before_term + after_term)
    if error == 0:
        # Every run on both sides gave the same value: the change is exact.
        return MetricChange(name, change.before, change.after, (change.change, change.change))
    freedom = (before_term + after_term) ** 2 / (
        before_term**2 / (len(before) - 1) + after_term**2 / (len(after) - 1)
    )
    margin = _t_critical(freedom) * error
    return MetricChange(
        name, change.before, change.after, (change.change - margin, change.change + margin)
    )


def _t_critical(freedom: float) -> float:
    """The two-sided 95% t value, rounding fractional degrees of freedom down."""

    whole = max(1, math.floor(freedom))
    if whole <= len(_T_95):
        return _T_95[whole - 1]
    return 2.000 if whole <= 60 else 1.980 if whole <= 120 else 1.960


def _noise_floor(pairs: Sequence[PairComparison]) -> dict[str, float]:
    """Per `in_noise_floor` metric, the median relative half-width over varied pairs.

    Pairs with no interval, or whose runs all gave one value, carry no measure of
    noise and are left out.
    """

    floors: dict[str, float] = {}
    for index, metric in enumerate(METRICS):
        if not metric.in_noise_floor:
            continue
        fractions = []
        for pair in pairs:
            change = pair.changes[index]
            if change.interval is not None and change.before > 0:
                low, high = change.interval
                if high > low:
                    fractions.append((high - low) / 2 / change.before)
        if fractions:
            floors[metric.name] = median(fractions)
    return floors


def _passed_cell(pair: PairComparison) -> str:
    cell = (
        f"{pair.passed_before[0]}/{pair.passed_before[1]} → "
        f"{pair.passed_after[0]}/{pair.passed_after[1]}"
    )
    if any(pair.errored):
        cell += f" (errored {pair.errored[0]} → {pair.errored[1]})"
    return cell


def _change_cell(change: MetricChange, digits: int) -> str:
    f = f",.{digits}f"
    moved = f"{change.before:{f}} → {change.after:{f}} ({change.change:+{f}}"
    if change.interval is None:
        return f"{moved}; —)"
    low, high = change.interval
    marker = " *" if change.beyond_noise else ""
    return f"{moved}; {low:+{f}} to {high:+{f}}){marker}"
