"""Summarize `summaries.jsonl` and `continuation.jsonl` as Markdown tables."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from pathlib import Path
from statistics import mean

Row = dict[str, object]


def load_jsonl(path: Path) -> list[Row]:
    """Read one results file; a missing file has no rows."""

    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def render(results_dir: Path) -> str:
    """Render every layer that has rows in ``results_dir``."""

    sections = []
    summaries = load_jsonl(results_dir / "summaries.jsonl")
    if summaries:
        sections.append(render_summaries(summaries))
    continuation = load_jsonl(results_dir / "continuation.jsonl")
    if continuation:
        sections.append(render_continuation(continuation))
    return "\n\n".join(sections) or f"no results in {results_dir}"


def render_summaries(rows: Sequence[Row]) -> str:
    """Per model and scenario: accepted checkpoints, fact recall, size and cost."""

    lines = [
        "## Layer 1: checkpoint quality",
        "",
        "| Model | Scenario | Valid | Errored | Recall | Missed facts | Injection phrase "
        "| Buried kept | Summary chars | Output tok | USD |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (model, scenario), group in _grouped(rows, ("label", "scenario")):
        scored = [row for row in group if row["status"] != "errored"]
        valid = [row for row in scored if row["status"] == "valid"]
        scores = [row["score"] for row in valid if isinstance(row["score"], dict)]
        missed = Counter(fact for score in scores for fact in _strings(score["missed"]))
        injection = [score["injection_phrase"] for score in scores]
        buried = sum(_int(score["buried_kept"]) for score in scores)
        buried_total = sum(_int(score["buried_total"]) for score in scores)
        lines.append(
            "| "
            + " | ".join(
                (
                    model,
                    scenario,
                    f"{len(valid)}/{len(scored)}",
                    str(len(group) - len(scored)),
                    _mean(_float(score["recall"]) for score in scores),
                    ", ".join(f"{fact}×{count}" for fact, count in missed.most_common(4)) or "—",
                    f"{sum(item is True for item in injection)}/{len(injection)}"
                    if any(item is not None for item in injection)
                    else "—",
                    f"{buried}/{buried_total}" if buried_total else "—",
                    _mean((_int(row["summary_chars"]) for row in valid), digits=0),
                    _mean((_int(row["output_tokens"]) for row in scored), digits=0),
                    _usd(group),
                )
            )
            + " |"
        )
    invalid = [row for row in rows if row["status"] == "invalid"]
    if invalid:
        lines += ["", "Rejected checkpoints:", ""]
        reasons = Counter((_label(row), str(row["error"])) for row in invalid)
        lines += [
            f"- {model}: {error} ×{count}" for (model, error), count in sorted(reasons.items())
        ]
    return "\n".join(lines)


def render_continuation(rows: Sequence[Row]) -> str:
    """Per task, model and setup: passes, compactions, repeat reads, cost."""

    lines = [
        "## Layer 2: continuing after compaction",
        "",
        "Forced runs compact sessions of about 10k-20k tokens. They show the mechanism works on "
        "each provider, not checkpoint quality at real context sizes.",
        "",
        "| Task | Model | Setup | Passed | Compaction failed | Errored | Compactions "
        "| Repeat reads | Last-turn calls | Input tok | USD |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (task, model, setup), group in _grouped(rows, ("task", "label", "setup")):
        scored = [row for row in group if row["status"] != "errored"]
        compactions = [
            sum(record["outcome"] == "completed" for record in _records(row)) for row in scored
        ]
        lines.append(
            "| "
            + " | ".join(
                (
                    task,
                    model,
                    setup,
                    f"{sum(row['status'] == 'passed' for row in scored)}/{len(scored)}",
                    str(sum(row["status"] == "compaction_failed" for row in scored)),
                    str(len(group) - len(scored)),
                    _mean(compactions, digits=1),
                    _mean((_int(row["repeat_reads"]) for row in scored), digits=1),
                    _mean((_int(row["last_turn_tool_calls"]) for row in scored), digits=1),
                    _mean(
                        (
                            _int(row["input_tokens"]) + _int(row["cache_read_tokens"])
                            for row in scored
                        ),
                        digits=0,
                    ),
                    _usd(group),
                )
            )
            + " |"
        )
    errors = Counter((_label(row), str(row["error"])) for row in rows if row["status"] == "errored")
    if errors:
        lines += ["", "Errored runs:", ""]
        lines += [
            f"- {model}: {error} ×{count}" for (model, error), count in sorted(errors.items())
        ]
    failures = Counter(
        (_label(row), str(row["task"]), str(record["error"]))
        for row in rows
        for record in _records(row)
        if record["outcome"] != "completed"
    )
    if failures:
        lines += ["", "Failed compactions:", ""]
        lines += [
            f"- {model} {task}: {error} ×{count}"
            for (model, task, error), count in sorted(failures.items())
        ]
    return "\n".join(lines)


def _grouped(rows: Iterable[Row], keys: tuple[str, ...]) -> list[tuple[tuple[str, ...], list[Row]]]:
    groups: dict[tuple[str, ...], list[Row]] = defaultdict(list)
    for row in rows:
        labelled = {**row, "label": _label(row)}
        groups[tuple(str(labelled[key]) for key in keys)].append(labelled)
    return sorted(groups.items())


def _label(row: Row) -> str:
    return f"{row['provider']}::{row['model']}"


def _records(row: Row) -> list[dict[str, object]]:
    records = row.get("compactions")
    return (
        [record for record in records if isinstance(record, dict)]
        if isinstance(records, list)
        else []
    )


def _strings(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _float(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _mean(values: Iterable[float], *, digits: int = 2) -> str:
    collected = list(values)
    return f"{mean(collected):.{digits}f}" if collected else "—"


def _usd(rows: Iterable[Row]) -> str:
    total = sum((Decimal(str(row["known_usd"])) for row in rows), Decimal(0))
    return f"{total:.2f}"


def render_summary_comparison(before: Sequence[Row], after: Sequence[Row]) -> str:
    """Per model, across scenarios run on both sides: checkpoint quality before and after."""

    scenarios = {str(row["scenario"]) for row in before} & {str(row["scenario"]) for row in after}
    lines = [
        "## Layer 1: before → after",
        "",
        f"Scenarios on both sides: {', '.join(sorted(scenarios))}.",
        "",
        "| Model | Runs | Valid | Recall | Decisions kept | Buried kept | Input tok/run "
        "| USD/run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    sides = [
        dict(_grouped((row for row in side if row["scenario"] in scenarios), ("label",)))
        for side in (before, after)
    ]
    for (model,) in sorted(set(sides[0]) & set(sides[1])):
        cells = [_summary_cells(side[(model,)]) for side in sides]
        lines.append(
            f"| {model} | "
            + " | ".join(f"{old} → {new}" for old, new in zip(*cells, strict=True))
            + " |"
        )
    return "\n".join(lines)


def _summary_cells(rows: Sequence[Row]) -> tuple[str, ...]:
    scored = [row for row in rows if row["status"] != "errored"]
    scores = [row["score"] for row in scored if isinstance(row["score"], dict)]
    decisions = [
        _float(score["recall_by_kind"]["decision"])
        for score in scores
        if isinstance(score["recall_by_kind"], dict) and "decision" in score["recall_by_kind"]
    ]
    buried = sum(_int(score["buried_kept"]) for score in scores)
    buried_total = sum(_int(score["buried_total"]) for score in scores)
    usd = sum((Decimal(str(row["known_usd"])) for row in scored), Decimal(0))
    return (
        str(len(scored)),
        f"{len(scores)}/{len(scored)}",
        _mean(_float(score["recall"]) for score in scores),
        _mean(decisions),
        f"{buried}/{buried_total}",
        _mean(
            (
                _int(row["input_tokens"])
                + _int(row["cache_read_tokens"])
                + _int(row.get("cache_write_tokens"))
                for row in scored
            ),
            digits=0,
        ),
        f"{usd / len(scored):.3f}" if scored else "—",
    )
