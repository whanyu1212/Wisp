"""Compare two sets of runs task by task, and save a set as a committed baseline.

`compare` pairs runs by task and model. For each metric it reports the change in
the mean and a 95% bootstrap interval for that change, resampling each side's
runs, so a reader can tell a real difference from run-to-run noise. With three
runs a side the intervals are wide: only large effects stand out.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median

from evals.tool_use.report import count_sum

_BOOTSTRAP_SAMPLES = 2000

# The fields a baseline keeps from each run. Folders, error text and credential
# sources stay out of the repository.
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

type Row = Mapping[str, object]


def _calls(row: Row) -> float:
    return float(count_sum(row["tool_calls"]))


def _tool_errors(row: Row) -> float:
    return float(count_sum(row["tool_errors"]))


def _tokens(row: Row) -> float:
    return float(int(str(row["input_tokens"])) + int(str(row["output_tokens"])))


@dataclass(frozen=True, slots=True)
class Metric:
    """A compared metric: its name, per-run value, and decimals shown."""

    name: str
    value: Callable[[Row], float]
    digits: int


METRICS = (
    Metric("calls", _calls, 1),
    Metric("tool errors", _tool_errors, 1),
    Metric("tokens", _tokens, 0),
)


@dataclass(frozen=True, slots=True)
class MetricChange:
    """How one metric moved for one task and model.

    Attributes:
        metric (str): The metric's name, from `METRICS`.
        before (float): Mean over the earlier runs.
        after (float): Mean over the later runs.
        low (float): Lower end of the 95% interval for ``after - before``.
        high (float): Upper end of that interval.
    """

    metric: str
    before: float
    after: float
    low: float
    high: float

    @property
    def change(self) -> float:
        return self.after - self.before

    @property
    def beyond_noise(self) -> bool:
        """Whether the interval excludes no change."""

        return self.low > 0 or self.high < 0


@dataclass(frozen=True, slots=True)
class PairComparison:
    """One task and model, before and after.

    Attributes:
        task (str): Task id.
        model (str): `provider::model`.
        passed_before (tuple[int, int]): Passed and scored runs before.
        passed_after (tuple[int, int]): Passed and scored runs after.
        changes (tuple[MetricChange, ...]): One per metric in `METRICS`.
        tools_differ (bool): Whether the two sides offered different tool sets,
            as expected when the change under test is the tools themselves.
    """

    task: str
    model: str
    passed_before: tuple[int, int]
    passed_after: tuple[int, int]
    changes: tuple[MetricChange, ...]
    tools_differ: bool


@dataclass(frozen=True, slots=True)
class Comparison:
    """Every paired task and model, and what could not be paired.

    Attributes:
        pairs (tuple[PairComparison, ...]): Paired results, sorted by task and model.
        unpaired (tuple[str, ...]): `task with model` entries scored on one side only.
        noise_floor (Mapping[str, float]): Per metric, the median half-width of
            the change interval as a fraction of the earlier mean: changes
            smaller than this are usually within noise at these run counts.
    """

    pairs: tuple[PairComparison, ...]
    unpaired: tuple[str, ...]
    noise_floor: Mapping[str, float]


def compare_runs(before: Sequence[Row], after: Sequence[Row], *, seed: int = 0) -> Comparison:
    """Pair two sets of runs by task and model and measure each metric's change.

    Errored runs are left out, as in the report: they say nothing about the task.

    Args:
        before (Sequence[Row]): Runs of the earlier setup, such as a baseline's.
        after (Sequence[Row]): Runs of the setup under test.
        seed (int): Seed for the bootstrap, so a comparison is reproducible.

    Returns:
        Comparison: Paired results, unpaired entries, and the noise floor.
    """

    rng = random.Random(seed)
    earlier, later = _scored_groups(before), _scored_groups(after)
    pairs: list[PairComparison] = []
    for key in sorted(earlier.keys() & later.keys()):
        old, new = earlier[key], later[key]
        pairs.append(
            PairComparison(
                task=key[0],
                model=key[1],
                passed_before=(_passed(old), len(old)),
                passed_after=(_passed(new), len(new)),
                changes=tuple(
                    _change(
                        metric.name,
                        [metric.value(run) for run in old],
                        [metric.value(run) for run in new],
                        rng,
                    )
                    for metric in METRICS
                ),
                tools_differ=_tool_sets(old) != _tool_sets(new),
            )
        )
    unpaired = tuple(
        f"{task} with {model}" for task, model in sorted(earlier.keys() ^ later.keys())
    )
    return Comparison(pairs=tuple(pairs), unpaired=unpaired, noise_floor=_noise_floor(pairs))


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
        passed = (
            f"{pair.passed_before[0]}/{pair.passed_before[1]} → "
            f"{pair.passed_after[0]}/{pair.passed_after[1]}"
        )
        task = f"{pair.task} (tools differ)" if pair.tools_differ else pair.task
        lines.append(f"| {task} | {pair.model} | {passed} | " + " | ".join(cells) + " |")
    lines += ["", "`*` marks a change whose interval excludes zero."]
    if comparison.noise_floor:
        floors = ", ".join(
            f"{name} ±{fraction:.0%}" for name, fraction in comparison.noise_floor.items()
        )
        lines.append(
            f"Noise floor at these run counts (median interval half-width over the earlier "
            f"mean): {floors}. Read smaller changes as no change."
        )
    if comparison.unpaired:
        lines += ["", "Scored on one side only:", *(f"- {entry}" for entry in comparison.unpaired)]
    return "\n".join(lines)


def baseline_document(
    rows: Sequence[Row], *, label: str, date: str, suite: Mapping[str, object] | None
) -> dict[str, object]:
    """Build a baseline: the suite's settings and each run's `BASELINE_FIELDS`.

    Args:
        rows (Sequence[Row]): Runs from one `runs.jsonl`.
        label (str): What the baseline measures, such as `current-tools`.
        date (str): The date it was recorded, `YYYY-MM-DD`.
        suite (Mapping[str, object] | None): The run's `suite.json`, if any.

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
        "suite": dict(suite or {}),
        "runs": [{field: row[field] for field in BASELINE_FIELDS} for row in rows],
    }


def write_baseline(path: Path, document: Mapping[str, object]) -> None:
    """Write a baseline document, refusing to replace an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=1, sort_keys=True) + "\n")


def _scored_groups(rows: Sequence[Row]) -> dict[tuple[str, str], list[Row]]:
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        if row["status"] != "errored":
            groups[(str(row["task"]), f"{row['provider']}::{row['model']}")].append(row)
    return groups


def _passed(rows: Sequence[Row]) -> int:
    return sum(1 for row in rows if row["status"] == "passed")


def _tool_sets(rows: Sequence[Row]) -> set[object]:
    return {row["tools_sha256"] for row in rows if row["tools_sha256"] is not None}


def _change(
    name: str, before: Sequence[float], after: Sequence[float], rng: random.Random
) -> MetricChange:
    differences = sorted(
        fmean(rng.choices(after, k=len(after))) - fmean(rng.choices(before, k=len(before)))
        for _ in range(_BOOTSTRAP_SAMPLES)
    )
    return MetricChange(
        metric=name,
        before=fmean(before),
        after=fmean(after),
        low=differences[int(0.025 * len(differences))],
        high=differences[int(0.975 * len(differences)) - 1],
    )


def _noise_floor(pairs: Sequence[PairComparison]) -> dict[str, float]:
    """Per metric, the median relative half-width over pairs whose runs varied.

    A metric no pair varied on, such as tool errors that were zero throughout,
    has no measured noise and is left out rather than shown as ±0%.
    """

    floors: dict[str, float] = {}
    for index, metric in enumerate(METRICS):
        changes = [pair.changes[index] for pair in pairs]
        fractions = [
            (change.high - change.low) / 2 / change.before
            for change in changes
            if change.before > 0 and change.high > change.low
        ]
        if fractions:
            floors[metric.name] = median(fractions)
    return floors


def _change_cell(change: MetricChange, digits: int) -> str:
    marker = " *" if change.beyond_noise else ""
    f = f",.{digits}f"
    return (
        f"{change.before:{f}} → {change.after:{f}} ({change.change:+{f}}; "
        f"{change.low:+{f}} to {change.high:+{f}}){marker}"
    )
