"""Report where the prompt cache stopped on each new prompt in Wisp session logs.

Reads session JSONL files (read-only) and classifies the first response to every
follow-up prompt by how much of the request prefix the provider reported as
cached:

* ``previous-tail``: the cache reached the previous prompt's final request.
* ``previous-start``: it stopped at the previous prompt's first request, so the
  previous run's tool turns were resent in a different shape (HY-9).
* ``instructions``: it covered roughly the tool schemas and system sections only.
* ``partial``: it stopped somewhere else.
* ``zero``: nothing was cached.

Each prompt also lists recorded changes that can explain a miss: the first
changed system section, a compaction, a model change, or a long idle gap.
Responses after the first one in a run are summarized separately.

Usage::

    uv run python scripts/cache_reuse_report.py [PATH ...] [--split-at ISO_TIME]
        [--details] [--json]

``PATH`` may be session files or directories; it defaults to the configured
session directory. ``--split-at`` reports before/after columns, for example
around the merge of a caching fix.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from wisp.agent.messages import Message
from wisp.config.runtime import default_session_dir
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    MessageSessionEntry,
    SessionEntry,
    is_session_tree_entry,
    session_entry_from_json,
)
from wisp.sessions.errors import SessionError

Outcome = Literal["previous-tail", "previous-start", "instructions", "partial", "zero"]
OUTCOMES: tuple[Outcome, ...] = (
    "previous-tail",
    "previous-start",
    "instructions",
    "partial",
    "zero",
)
MISS_OUTCOMES: tuple[Outcome, ...] = ("instructions", "partial", "zero")
WithinRunOutcome = Literal["reached-previous", "partial", "zero"]
WITHIN_RUN_OUTCOMES: tuple[WithinRunOutcome, ...] = ("reached-previous", "partial", "zero")

# Providers cache in fixed-size blocks and may leave the last partial block
# uncached, so "reached N tokens" allows a small shortfall.
REACHED_MIN_SHORTFALL_TOKENS = 512
REACHED_SHORTFALL_RATIO = 0.02
# Tool schemas are not persisted, and system-section tokens are estimated from
# UTF-8 bytes, so the instructions boundary is approximate.
INSTRUCTIONS_SLACK_TOKENS = 2048
DEFAULT_IDLE_MINUTES = 60.0
NO_RECORDED_CAUSE = "no recorded cause"


@dataclass(frozen=True)
class Response:
    """Usage reported for one successful model response."""

    input_tokens: int
    cached_tokens: int
    model: str | None
    created_at: datetime
    # Compactions seen earlier in the same prompt; a change marks a replaced transcript.
    compactions_before: int


@dataclass
class Prompt:
    """One user prompt: its system sections and the responses of its run."""

    session: str
    entry_id: str
    started_at: datetime
    system_sections: tuple[str, ...]
    previous: Prompt | None
    responses: list[Response] = field(default_factory=list)
    compactions: int = 0


@dataclass(frozen=True)
class PromptReuse:
    """Classification of the first response to one follow-up prompt."""

    session: str
    started_at: datetime
    outcome: Outcome
    cached_tokens: int
    input_tokens: int
    previous_first_input_tokens: int
    previous_last_input_tokens: int
    estimated_instruction_tokens: int
    idle_minutes: float
    causes: tuple[str, ...]


def read_session_entries(path: Path) -> list[SessionEntry]:
    """Decode a session file without modifying it.

    ``JsonlSession.read_entries`` repairs an incomplete final record by
    truncating the file, which would alter a session another Wisp process is
    still writing. This reader skips that record instead.

    Args:
        path (Path): Session JSONL file.

    Returns:
        list[SessionEntry]: Entries in append order, decoded through the same
            compatibility path as the session store.

    Raises:
        SessionError: If a complete record is malformed or unsupported.
    """

    entries: list[SessionEntry] = []
    leaf_id: str | None = None
    with path.open("r", encoding="utf-8") as session_file:
        for line_number, line in enumerate(session_file, start=1):
            if not line.endswith("\n"):
                break
            if not line.strip():
                continue
            entry = session_entry_from_json(
                line,
                source=f"{path}:{line_number}",
                legacy_parent_id=leaf_id,
            )
            entries.append(entry)
            if is_session_tree_entry(entry):
                leaf_id = entry.id
            elif isinstance(entry, ActiveLeafSessionEntry):
                leaf_id = entry.active_leaf_id
    return entries


def split_prompts(entries: Sequence[SessionEntry], *, session: str) -> list[Prompt]:
    """Group session entries into prompts linked to the prompt they continue.

    Wisp writes a block of system messages before every user prompt. The first
    system entry's parent is the last entry of the prompt it continues, which
    also holds after branching.

    Args:
        entries (Sequence[SessionEntry]): Entries in append order.
        session (str): Label used in the report.

    Returns:
        list[Prompt]: Prompts in append order.
    """

    prompts: list[Prompt] = []
    prompt_by_entry_id: dict[str, Prompt] = {}
    system_block: list[MessageSessionEntry] = []
    current: Prompt | None = None
    previous_kind: str | None = None

    for entry in entries:
        if isinstance(entry, MessageSessionEntry):
            message = entry.message
            if message.role == "system":
                if previous_kind != "system":
                    system_block = []
                system_block.append(entry)
            elif message.role == "user" and previous_kind == "system":
                parent_id = system_block[0].parent_id
                current = Prompt(
                    session=session,
                    entry_id=entry.id,
                    started_at=entry.created_at,
                    system_sections=tuple(item.message.content for item in system_block),
                    previous=prompt_by_entry_id.get(parent_id) if parent_id else None,
                )
                prompts.append(current)
                for item in system_block:
                    prompt_by_entry_id[item.id] = current
            elif message.role == "assistant" and current is not None:
                response = _response(message, entry.created_at, current.compactions)
                if response is not None:
                    current.responses.append(response)
            previous_kind = message.role
        elif isinstance(entry, CompactionSessionEntry):
            if current is not None:
                current.compactions += 1
            previous_kind = "compaction"
        if current is not None and is_session_tree_entry(entry):
            prompt_by_entry_id.setdefault(entry.id, current)
    return prompts


def _response(message: Message, created_at: datetime, compactions: int) -> Response | None:
    usage = message.usage
    if usage is None or usage.input_tokens <= 0:
        return None
    model = message.cost.model if message.cost is not None else None
    if model is None and message.context_observation is not None:
        model = message.context_observation.model
    return Response(
        input_tokens=usage.input_tokens,
        cached_tokens=usage.cache_read_input_tokens or 0,
        model=model,
        created_at=created_at,
        compactions_before=compactions,
    )


def classify_prompt(prompt: Prompt, *, idle_minutes: float) -> PromptReuse | None:
    """Classify where the cache stopped on a follow-up prompt's first response.

    Args:
        prompt (Prompt): Prompt to classify.
        idle_minutes (float): Gap after which idle time is reported as a cause.

    Returns:
        PromptReuse | None: The classification, or None for a session's first
            prompt and for prompts where either run has no usable response.
    """

    previous = prompt.previous
    if previous is None or not previous.responses or not prompt.responses:
        return None
    first = prompt.responses[0]
    previous_first = previous.responses[0]
    previous_last = previous.responses[-1]
    instruction_tokens = estimate_tokens(prompt.system_sections)
    idle = (prompt.started_at - previous_last.created_at).total_seconds() / 60

    outcome: Outcome
    if first.cached_tokens == 0:
        outcome = "zero"
    elif reached(first.cached_tokens, previous_last.input_tokens):
        outcome = "previous-tail"
    elif stopped_at(first.cached_tokens, previous_first.input_tokens):
        outcome = "previous-start"
    elif first.cached_tokens <= instruction_tokens + INSTRUCTIONS_SLACK_TOKENS:
        outcome = "instructions"
    else:
        outcome = "partial"

    causes: list[str] = []
    changed_section = first_changed_section(previous.system_sections, prompt.system_sections)
    if changed_section is not None:
        causes.append(f"system changed: {changed_section}")
    compacted_during_previous = previous.compactions > previous_first.compactions_before
    if compacted_during_previous or first.compactions_before > 0:
        causes.append("compaction")
    if previous_last.model and first.model and previous_last.model != first.model:
        causes.append("model changed")
    if idle >= idle_minutes:
        causes.append(f"idle >= {idle_minutes:g}m")

    return PromptReuse(
        session=prompt.session,
        started_at=prompt.started_at,
        outcome=outcome,
        cached_tokens=first.cached_tokens,
        input_tokens=first.input_tokens,
        previous_first_input_tokens=previous_first.input_tokens,
        previous_last_input_tokens=previous_last.input_tokens,
        estimated_instruction_tokens=instruction_tokens,
        idle_minutes=round(idle, 1),
        causes=tuple(causes),
    )


def classify_within_run(prompt: Prompt) -> Counter[WithinRunOutcome]:
    """Count whether each later response in a run reused the previous request.

    Pairs separated by a compaction are skipped because the transcript was
    replaced between them.

    Args:
        prompt (Prompt): Prompt whose run is summarized.

    Returns:
        Counter[WithinRunOutcome]: Response counts by outcome.
    """

    counts: Counter[WithinRunOutcome] = Counter()
    for before, after in zip(prompt.responses, prompt.responses[1:], strict=False):
        if after.compactions_before != before.compactions_before:
            continue
        if after.cached_tokens == 0:
            counts["zero"] += 1
        elif reached(after.cached_tokens, before.input_tokens):
            counts["reached-previous"] += 1
        else:
            counts["partial"] += 1
    return counts


def reached(cached_tokens: int, target_tokens: int) -> bool:
    """Return whether a cached prefix covers a previous request's input."""

    return cached_tokens >= target_tokens - _shortfall(target_tokens)


def stopped_at(cached_tokens: int, target_tokens: int) -> bool:
    """Return whether a cached prefix ends at a previous request's input."""

    return abs(cached_tokens - target_tokens) <= _shortfall(target_tokens)


def _shortfall(target_tokens: int) -> float:
    return max(REACHED_MIN_SHORTFALL_TOKENS, target_tokens * REACHED_SHORTFALL_RATIO)


def estimate_tokens(sections: Sequence[str]) -> int:
    """Estimate tokens at four UTF-8 bytes each, as Wisp's context budget does."""

    return sum(
        math.ceil(len(section.encode("utf-8", "backslashreplace")) / 4) for section in sections
    )


def first_changed_section(before: Sequence[str], after: Sequence[str]) -> str | None:
    """Return a label for the first system section that differs, if any."""

    for old, new in zip(before, after, strict=False):
        if old != new:
            return section_label(new)
    if len(before) != len(after):
        return "sections added or removed"
    return None


def section_label(content: str) -> str:
    """Return a section's ``[WISP ...]`` tag, or the start of its first line."""

    first_line = content.lstrip().split("\n", 1)[0]
    if first_line.startswith("[") and "]" in first_line:
        return first_line[: first_line.index("]") + 1]
    return first_line[:40]


def session_files(paths: Sequence[Path]) -> list[Path]:
    """Expand files and directories into session JSONL files."""

    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
        else:
            files.append(path)
    return files


@dataclass
class PeriodReport:
    """Aggregated results for one reporting period."""

    prompts: list[PromptReuse] = field(default_factory=list)
    within_run: Counter[WithinRunOutcome] = field(default_factory=Counter)


def build_report(
    files: Sequence[Path],
    *,
    split_at: datetime | None,
    idle_minutes: float,
) -> dict[str, PeriodReport]:
    """Classify every session file into one or two periods.

    Args:
        files (Sequence[Path]): Session JSONL files.
        split_at (datetime | None): Prompts starting before this go to
            ``before``, the rest to ``after``; None reports one ``all`` period.
        idle_minutes (float): Gap after which idle time is reported as a cause.

    Returns:
        dict[str, PeriodReport]: Reports keyed by period name.
    """

    periods = (
        {"before": PeriodReport(), "after": PeriodReport()} if split_at else {"all": PeriodReport()}
    )
    for path in files:
        try:
            entries = read_session_entries(path)
        except (SessionError, OSError, UnicodeDecodeError) as exc:
            # One unreadable or malformed file should not hide the rest.
            print(f"warning: skipped {path}: {exc}", file=sys.stderr)
            continue
        for prompt in split_prompts(entries, session=path.stem):
            period = periods[_period_name(prompt.started_at, split_at)]
            period.within_run.update(classify_within_run(prompt))
            reuse = classify_prompt(prompt, idle_minutes=idle_minutes)
            if reuse is not None:
                period.prompts.append(reuse)
    return periods


def _period_name(started_at: datetime, split_at: datetime | None) -> str:
    if split_at is None:
        return "all"
    return "before" if started_at < split_at else "after"


def render_markdown(periods: dict[str, PeriodReport], *, details: bool) -> str:
    """Render the summary tables, and optionally one row per missed prompt."""

    names = list(periods)
    lines = ["## First response to a follow-up prompt", ""]
    lines += _table(
        "Outcome",
        names,
        [(outcome, [_count(periods[n].prompts, outcome) for n in names]) for outcome in OUTCOMES],
        totals=[len(periods[n].prompts) for n in names],
    )

    for name in names:
        misses = [p for p in periods[name].prompts if p.outcome in MISS_OUTCOMES]
        causes = sorted({cause for p in misses for cause in (p.causes or (NO_RECORDED_CAUSE,))})
        heading = "Recorded causes of misses" + ("" if name == "all" else f" ({name})")
        lines += ["", f"## {heading}", "", "A prompt can have several causes.", ""]
        lines += _table(
            "Cause",
            list(MISS_OUTCOMES),
            [
                (cause, [_cause_count(misses, outcome, cause) for outcome in MISS_OUTCOMES])
                for cause in causes
            ],
        )

    lines += ["", "## Later responses within a run", ""]
    lines += _table(
        "Outcome",
        names,
        [
            (outcome, [periods[n].within_run[outcome] for n in names])
            for outcome in WITHIN_RUN_OUTCOMES
        ],
        totals=[sum(periods[n].within_run.values()) for n in names],
    )

    if details:
        lines += ["", "## Prompts that did not reach the previous tail", ""]
        lines.append(
            "| Period | Session | Started | Outcome | Cached | Input | Prev first | "
            "Prev last | Instr (est.) | Idle (m) | Causes |"
        )
        lines.append("| -- | -- | -- | -- | --: | --: | --: | --: | --: | --: | -- |")
        for name in names:
            for p in periods[name].prompts:
                if p.outcome == "previous-tail":
                    continue
                lines.append(
                    f"| {name} | {p.session} | {p.started_at:%Y-%m-%d %H:%M} | {p.outcome} | "
                    f"{p.cached_tokens} | {p.input_tokens} | {p.previous_first_input_tokens} | "
                    f"{p.previous_last_input_tokens} | {p.estimated_instruction_tokens} | "
                    f"{p.idle_minutes:g} | {', '.join(p.causes) or '—'} |"
                )
    return "\n".join(lines) + "\n"


def _table(
    label: str,
    columns: Sequence[str],
    rows: Sequence[tuple[str, Sequence[int]]],
    *,
    totals: Sequence[int] | None = None,
) -> list[str]:
    lines = [f"| {label} | " + " | ".join(columns) + " |", "| -- |" + " --: |" * len(columns)]
    lines += [f"| {name} | " + " | ".join(str(v) for v in values) + " |" for name, values in rows]
    if totals is not None:
        lines.append("| **total** | " + " | ".join(str(v) for v in totals) + " |")
    return lines


def _count(prompts: Sequence[PromptReuse], outcome: Outcome) -> int:
    return sum(1 for p in prompts if p.outcome == outcome)


def _cause_count(prompts: Sequence[PromptReuse], outcome: Outcome, cause: str) -> int:
    return sum(
        1 for p in prompts if p.outcome == outcome and cause in (p.causes or (NO_RECORDED_CAUSE,))
    )


def render_json(periods: dict[str, PeriodReport]) -> str:
    """Render every classified prompt and within-run count as JSON."""

    payload = {
        name: {
            "prompts": [asdict(p) for p in report.prompts],
            "within_run": {outcome: report.within_run[outcome] for outcome in WITHIN_RUN_OUTCOMES},
        }
        for name, report in periods.items()
    }
    return json.dumps(payload, default=str, indent=2) + "\n"


def parse_time(value: str) -> datetime:
    """Parse an ISO time, treating one without an offset as UTC."""

    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the report and write it to standard output."""

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("paths", nargs="*", type=Path, help="session files or directories")
    parser.add_argument("--split-at", type=parse_time, help="report before/after this ISO time")
    parser.add_argument(
        "--idle-minutes",
        type=float,
        default=DEFAULT_IDLE_MINUTES,
        help="idle gap reported as a cause (default: %(default)s)",
    )
    parser.add_argument("--details", action="store_true", help="list each missed prompt")
    parser.add_argument("--json", action="store_true", help="write JSON instead of markdown")
    args = parser.parse_args(argv)

    files = session_files(args.paths or [default_session_dir()])
    periods = build_report(files, split_at=args.split_at, idle_minutes=args.idle_minutes)
    output = render_json(periods) if args.json else render_markdown(periods, details=args.details)
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
