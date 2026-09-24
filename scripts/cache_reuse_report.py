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
changed system section, a compaction, a provider or model change, or a long
idle gap. Responses after the first one in a run are summarized separately.
Prompts copied into clones or forks are counted once.

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
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from wisp.agent.messages import Message
from wisp.config.runtime import default_session_dir
from wisp.sessions.entries import (
    ActiveLeafSessionEntry,
    CompactionSessionEntry,
    EventSessionEntry,
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
# Providers whose adapter declares `supports_prompt_cache_key = True`, so the loop
# sends them the session-derived `prompt_cache_key` (kept in sync by a test).
CACHE_KEYED_PROVIDERS = frozenset({"openai", "openai-codex"})
NO_RECORDED_CAUSE = "no recorded cause"


@dataclass(frozen=True)
class Response:
    """Usage reported for one successful model response."""

    # Both are None when the provider did not report them ("unknown", not zero).
    input_tokens: int | None
    cached_tokens: int | None
    provider: str | None
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
    # Copied into this file by a clone or fork: written before the file existed.
    copied: bool = False
    # The previous prompt's run as it stood at the branch point: navigating to a
    # response mid-run and prompting from there abandons the responses after it.
    previous_responses: tuple[Response, ...] = ()
    previous_compactions: int = 0
    # Responses in append order; `pairs` holds each later response with the
    # response right before it on its own branch, for the within-run summary.
    responses: list[Response] = field(default_factory=list)
    pairs: list[tuple[Response, Response]] = field(default_factory=list)
    compactions: int = 0


@dataclass(frozen=True)
class PromptReuse:
    """Classification of the first response to one follow-up prompt."""

    session: str
    started_at: datetime
    outcome: Outcome
    cached_tokens: int
    # None when the provider reported no usage for that request; a reported zero
    # cache read is still classified as `zero` without them.
    input_tokens: int | None
    previous_first_input_tokens: int | None
    previous_last_input_tokens: int | None
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


def split_prompts(
    entries: Sequence[SessionEntry],
    *,
    session: str,
    copied_entry_ids: AbstractSet[str] = frozenset(),
) -> list[Prompt]:
    """Group session entries into prompts linked to the prompt they continue.

    A run appends its system messages (possibly none, for an SDK prompt
    replacement) and then the user message, all under one operation ID. A prompt
    therefore starts at a user message that begins a new operation: steering
    messages inside a run share its ID and stay part of it. The prompt's system
    sections are the system entries directly before it with the same ID, and its
    previous prompt is found through the parent of the first of them (or of the
    user message itself when there are none), which also holds after branching.
    System entries belong to the prompt *before* their user message: editing a
    historical message branches from the entry just before it, so the new
    prompt continues the conversation up to there.

    Entries without operation IDs start a prompt by the parent's shape instead
    (see ``_starts_prompt``).

    A clone or fork copies entries from another session. A system block never
    spans copied and fresh entries: a fork from a user message ends with that
    prompt's copied block, and the edited prompt's fresh block follows it.

    Args:
        entries (Sequence[SessionEntry]): Entries in append order.
        session (str): Label used in the report.
        copied_entry_ids (AbstractSet[str]): Entries this file copied from another
            session (see ``copied_entry_ids``); empty when unknown.

    Returns:
        list[Prompt]: Prompts in append order.
    """

    prompts: list[Prompt] = []
    prompt_by_entry_id: dict[str, Prompt] = {}
    # Each entry's own branch within its prompt: the responses and compaction
    # count on the path from the prompt's user message to that entry. Anything
    # appended to an entry extends exactly that path, so abandoned siblings from
    # tree navigation never leak into a later branch.
    branch_by_entry_id: dict[str, _Branch] = {}
    entry_by_id: dict[str, SessionEntry] = {}
    system_block: list[MessageSessionEntry] = []

    for entry in entries:
        if not is_session_tree_entry(entry):
            continue
        parent = entry_by_id.get(entry.parent_id) if entry.parent_id else None
        entry_by_id[entry.id] = entry
        role = entry.message.role if isinstance(entry, MessageSessionEntry) else None

        if isinstance(entry, MessageSessionEntry) and role == "system":
            # A block continues along its parent chain within one operation, and
            # never across the boundary between copied and fresh entries.
            continues_block = (
                bool(system_block)
                and entry.parent_id == system_block[-1].id
                and entry.operation_id == system_block[-1].operation_id
                and (entry.id in copied_entry_ids) == (system_block[-1].id in copied_entry_ids)
            )
            if not continues_block:
                system_block = []
            system_block.append(entry)
            # A system entry belongs to the prompt it continues, at the progress of
            # its parent. Recording this now (not when the user message arrives)
            # also covers a fork's copied block, which has no user message after it.
            continued = prompt_by_entry_id.get(entry.parent_id) if entry.parent_id else None
            if continued is not None and entry.parent_id is not None:
                prompt_by_entry_id[entry.id] = continued
                branch_by_entry_id[entry.id] = branch_by_entry_id[entry.parent_id]
        elif isinstance(entry, MessageSessionEntry) and _starts_prompt(entry, parent, entry_by_id):
            # The prompt's own sections end right before it, in the same operation.
            own_block = (
                system_block
                if system_block
                and system_block[-1].id == entry.parent_id
                and system_block[-1].operation_id == entry.operation_id
                and (entry.id in copied_entry_ids) == (system_block[-1].id in copied_entry_ids)
                else []
            )
            system_block = []
            parent_id = own_block[0].parent_id if own_block else entry.parent_id
            previous = prompt_by_entry_id.get(parent_id) if parent_id else None
            branch_point = (
                branch_by_entry_id[parent_id] if previous is not None and parent_id else _Branch()
            )
            prompt = Prompt(
                session=session,
                entry_id=entry.id,
                copied=entry.id in copied_entry_ids,
                started_at=entry.created_at,
                system_sections=tuple(item.message.content for item in own_block),
                previous=previous,
                previous_responses=branch_point.responses,
                previous_compactions=branch_point.compactions,
            )
            prompts.append(prompt)
            prompt_by_entry_id[entry.id] = prompt
            branch_by_entry_id[entry.id] = _Branch()
        else:
            # Every other entry extends the branch of its parent, which is not always
            # the last one appended: navigating to an older point and then compacting
            # or prompting appends onto an earlier prompt or response.
            owner = prompt_by_entry_id.get(entry.parent_id) if entry.parent_id else None
            if owner is not None and entry.parent_id is not None:
                branch = branch_by_entry_id[entry.parent_id]
                if isinstance(entry, CompactionSessionEntry):
                    owner.compactions += 1
                    branch = _Branch(branch.responses, branch.compactions + 1)
                elif isinstance(entry, MessageSessionEntry) and role == "assistant":
                    response = _response(entry.message, entry.created_at, branch.compactions)
                    owner.responses.append(response)
                    if branch.responses:
                        owner.pairs.append((branch.responses[-1], response))
                    branch = _Branch((*branch.responses, response), branch.compactions)
                prompt_by_entry_id[entry.id] = owner
                branch_by_entry_id[entry.id] = branch
    return prompts


def _follows_run_end(entry: SessionEntry | None, entry_by_id: Mapping[str, SessionEntry]) -> bool:
    """Return whether the nearest message above ``entry`` ended its run.

    Walks up past events and compactions. A persisted ``error`` event is the
    terminal of a failed or cancelled run, so it ends the run even when the
    message before it is a user or tool row. Otherwise the run ended when the
    nearest message is a system message or a final assistant answer (one without
    tool calls), or when there is no message at all.

    Called for a user message without an operation ID. When the nearest message
    has one, it was written by a different, earlier run, so that run has ended.
    """

    while entry is not None and not isinstance(entry, MessageSessionEntry):
        if isinstance(entry, EventSessionEntry) and entry.event.payload.get("type") == "error":
            return True
        parent_id = entry.parent_id if is_session_tree_entry(entry) else None
        entry = entry_by_id.get(parent_id) if parent_id else None
    if entry is None or entry.operation_id is not None:
        return True
    role = entry.message.role
    return role == "system" or (role == "assistant" and not entry.message.tool_calls)


def _starts_prompt(
    entry: MessageSessionEntry,
    parent: SessionEntry | None,
    entry_by_id: Mapping[str, SessionEntry],
) -> bool:
    """Return whether a user message starts a new prompt rather than steering one.

    With operation IDs, a prompt starts at a user message whose parent belongs to
    a different operation, or which has no parent. Steering messages share the
    running operation's ID.

    Without IDs (legacy entries, or SDK runs that pass none), the log does not
    record which run wrote a message, so a prompt starts where the previous run
    visibly ended (see ``_follows_run_end``): after nothing, a system message, a
    final assistant answer, or a terminal ``error`` event, looking through other
    events and compactions. Steering is injected after tool results, so a user
    message there stays in the running prompt. A queued follow-up after a final
    answer is indistinguishable from a new run and is counted as a prompt.
    """

    if entry.message.role != "user":
        return False
    if entry.operation_id is None:
        return _follows_run_end(parent, entry_by_id)
    return (
        parent is None
        or parent.operation_id != entry.operation_id
        or (isinstance(parent, MessageSessionEntry) and parent.message.role == "system")
    )


@dataclass(frozen=True)
class _Branch:
    """The responses and compactions on one path through a prompt's run."""

    responses: tuple[Response, ...] = ()
    compactions: int = 0


def _response(message: Message, created_at: datetime, compactions: int) -> Response:
    """Project one assistant message's usage.

    Every assistant message keeps its position in the run. When the provider
    reported no usage at all, or no cache reads, the unknown values are None, so
    the response is neither counted as a zero-cache miss nor skipped over when
    finding a run's first response or adjacent pairs.
    """

    usage = message.usage
    input_tokens = request_input_tokens(message) if usage is not None else None
    observation = message.context_observation
    cost = message.cost
    model = cost.model if cost is not None else None
    provider = cost.provider if cost is not None else None
    if observation is not None:
        model = model or observation.model
        provider = provider or observation.provider
    return Response(
        input_tokens=input_tokens if input_tokens and input_tokens > 0 else None,
        cached_tokens=usage.cache_read_input_tokens if usage is not None else None,
        provider=provider,
        model=model,
        created_at=created_at,
        compactions_before=compactions,
    )


def request_input_tokens(message: Message) -> int:
    """Return the full size of the request that produced an assistant message.

    Anthropic reports ``input_tokens`` excluding cache reads and writes. The loop
    records the full request size in ``context_observation.input_tokens``; older
    records without an observation fall back to adding the cache counts back for
    Anthropic. Other providers report cached tokens as part of ``input_tokens``.
    """

    usage = message.usage
    assert usage is not None
    if message.context_observation is not None:
        return message.context_observation.input_tokens
    provider = message.cost.provider if message.cost is not None else None
    if provider == "anthropic":
        return (
            usage.input_tokens
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_write_input_tokens or 0)
        )
    return usage.input_tokens


def classify_prompt(prompt: Prompt, *, idle_minutes: float) -> PromptReuse | None:
    """Classify where the cache stopped on a follow-up prompt's first response.

    Args:
        prompt (Prompt): Prompt to classify.
        idle_minutes (float): Gap after which idle time is reported as a cause.

    Returns:
        PromptReuse | None: The classification, or None for a session's first
            prompt, for prompts where either run has no response, when the first
            response's cache reads are unknown, and when a nonzero cache read
            cannot be compared because a request size is unknown.
    """

    previous = prompt.previous
    previous_responses = prompt.previous_responses
    if previous is None or not previous_responses or not prompt.responses:
        return None
    first = prompt.responses[0]
    if first.cached_tokens is None:
        return None
    previous_first = previous_responses[0]
    previous_last = previous_responses[-1]
    instruction_tokens = estimate_tokens(prompt.system_sections)
    idle = (prompt.started_at - previous_last.created_at).total_seconds() / 60

    outcome: Outcome
    if first.cached_tokens == 0:
        # A reported zero is a miss whatever the request sizes were.
        outcome = "zero"
    elif previous_first.input_tokens is None or previous_last.input_tokens is None:
        return None
    elif reached(first.cached_tokens, previous_last.input_tokens):
        outcome = "previous-tail"
    elif stopped_at(first.cached_tokens, previous_first.input_tokens):
        outcome = "previous-start"
    elif first.cached_tokens <= instruction_tokens + INSTRUCTIONS_SLACK_TOKENS:
        outcome = "instructions"
    else:
        outcome = "partial"

    causes: list[str] = []
    # The first new prompt of a clone or fork continues copied history under a new
    # session ID. Only providers that receive `prompt_cache_key` get a
    # session-derived cache namespace, so only they can miss because of it.
    if previous.copied and not prompt.copied and first.provider in CACHE_KEYED_PROVIDERS:
        causes.append("session changed")
    changed_section = first_changed_section(previous.system_sections, prompt.system_sections)
    if changed_section is not None:
        causes.append(f"system changed: {changed_section}")
    compacted_during_previous = prompt.previous_compactions > previous_first.compactions_before
    if compacted_during_previous or first.compactions_before > 0:
        causes.append("compaction")
    # Providers never share a prompt cache, even when the model name matches.
    if previous_last.provider and first.provider and previous_last.provider != first.provider:
        causes.append("provider changed")
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

    Each response is compared with the response right before it on its own
    branch. Pairs separated by a compaction are skipped because the transcript
    was replaced between them. A pair is skipped when the later response's cache
    reads are unknown; a reported zero is counted even if the earlier request's
    size is unknown, and any other result needs that size. Unknown responses
    still separate their neighbours, so only adjacent requests are compared.

    Args:
        prompt (Prompt): Prompt whose run is summarized.

    Returns:
        Counter[WithinRunOutcome]: Response counts by outcome.
    """

    counts: Counter[WithinRunOutcome] = Counter()
    for before, after in prompt.pairs:
        if after.compactions_before != before.compactions_before or after.cached_tokens is None:
            continue
        if after.cached_tokens == 0:
            counts["zero"] += 1
        elif before.input_tokens is None:
            continue
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
    for prompt in _unique_prompts(files):
        period = periods[_period_name(prompt.started_at, split_at)]
        period.within_run.update(classify_within_run(prompt))
        reuse = classify_prompt(prompt, idle_minutes=idle_minutes)
        if reuse is not None:
            period.prompts.append(reuse)
    return periods


def _unique_prompts(files: Sequence[Path]) -> list[Prompt]:
    """Return each prompt once, choosing its most complete copy across files.

    Clones and forks copy history with the original entry IDs, and a copy can be
    truncated mid-run (a clone of an intermediate entry has fewer responses). The
    copy with the most responses wins regardless of file order; on a tie the
    first one read is kept. Copies still serve as ``previous`` within their file.
    """

    sessions: list[tuple[Path, list[SessionEntry]]] = []
    for path in files:
        try:
            sessions.append((path, read_session_entries(path)))
        except (SessionError, OSError, UnicodeDecodeError) as exc:
            # One unreadable or malformed file should not hide the rest.
            print(f"warning: skipped {path}: {exc}", file=sys.stderr)

    copied = copied_entry_ids([(path, entries) for path, entries in sessions])
    best: dict[str, Prompt] = {}
    for (path, entries), copied_here in zip(sessions, copied, strict=True):
        for prompt in split_prompts(entries, session=path.stem, copied_entry_ids=copied_here):
            kept = best.get(prompt.entry_id)
            if kept is None or len(prompt.responses) > len(kept.responses):
                best[prompt.entry_id] = prompt
    return list(best.values())


def copied_entry_ids(
    sessions: Sequence[tuple[Path, Sequence[SessionEntry]]],
) -> list[frozenset[str]]:
    """Return, per session file, the entry IDs it copied from another file.

    A clone or fork copies one root-to-leaf path of its source, with the original
    entry IDs, into a new file that starts with exactly that path. An entry is a
    copy when another file holds the same ID and is the original: its creation
    second is earlier, or, when the seconds tie and both files continued past
    the shared path, its continuation was written first. Creation times come
    from store file names, floored to the second.

    Only entries shared with another file are considered: a session's own first
    message can predate its file's name, because the user message is prepared
    before the session file is created. So a copy whose source is not in the
    report is not detected, nor are renamed files, nor a same-second copy when
    only one of the two files continued. These gaps only omit a ``session
    changed`` cause; the rules never mark an original as a copy.

    Args:
        sessions (Sequence[tuple[Path, Sequence[SessionEntry]]]): Each session
            file with its entries.

    Returns:
        list[frozenset[str]]: Copied entry IDs, in the order of ``sessions``.
    """

    created = [session_file_created_at(path) for path, _ in sessions]
    tree_ids = [
        [entry.id for entry in entries if is_session_tree_entry(entry)] for _, entries in sessions
    ]
    holders: dict[str, list[int]] = {}
    for index, ids in enumerate(tree_ids):
        for entry_id in ids:
            holders.setdefault(entry_id, []).append(index)
    # For each pair of files sharing entries: when the first entry of `a` that `b`
    # lacks was written.
    first_unshared: dict[tuple[int, int], datetime] = {}
    for a, (_, entries) in enumerate(sessions):
        partners = {b for entry_id in tree_ids[a] for b in holders[entry_id] if b != a}
        for b in partners:
            b_ids = set(tree_ids[b])
            first = next(
                (e.created_at for e in entries if is_session_tree_entry(e) and e.id not in b_ids),
                None,
            )
            if first is not None:
                first_unshared[(a, b)] = first

    def is_original_of(other: int, index: int, entry_id: str) -> bool:
        other_created, own_created = created[other], created[index]
        if other_created is None or own_created is None:
            return False
        if other_created != own_created:
            return other_created < own_created
        # Same creation second: both files begin with the shared path, so compare
        # what follows it. When both continued, the source's own entries were
        # written before the copy existed and the copy's after, so the earlier
        # continuation is the original. This covers a continued fork from a user
        # message, where a missed copy would merge two system blocks. When only
        # one continued, it may be the source (continued after the copy) or the
        # copy (continued after cloning); that is ambiguous, so neither is marked.
        own_first = first_unshared.get((index, other))
        other_first = first_unshared.get((other, index))
        if own_first is None or other_first is None:
            return False
        return other_first < own_first

    result: list[frozenset[str]] = []
    for index, (created_at, (_, entries)) in enumerate(zip(created, sessions, strict=True)):
        if created_at is None:
            result.append(frozenset())
            continue
        result.append(
            frozenset(
                entry.id
                for entry in entries
                if any(
                    is_original_of(other, index, entry.id)
                    for other in holders.get(entry.id, ())
                    if other != index
                )
            )
        )
    return result


def session_file_created_at(path: Path) -> datetime | None:
    """Return the UTC creation second encoded in a store-named session file."""

    match = _SESSION_FILE_NAME.fullmatch(path.stem)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)


# `JsonlSessionStore.create` names files `YYYYMMDD-HHMMSS-<first 8 hex of the id>`.
_SESSION_FILE_NAME = re.compile(r"(\d{8}-\d{6})-[0-9a-f]{8}")


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
