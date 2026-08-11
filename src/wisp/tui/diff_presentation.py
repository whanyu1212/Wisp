"""Structured, renderer-neutral presentation data for Textual tool diffs.

This module deliberately contains no Textual widgets or tool execution logic. Tool
result rendering builds :class:`DiffPresentation` from already-bounded edit/write
facts, while ``ToolCard`` chooses how to paint the retained rows at its current
width. Keeping row selection here makes collapsed and expanded views deterministic,
testable, and shared by live and reconstructed historical tool cards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum


class DiffOperation(StrEnum):
    """The file-level operation represented by one diff."""

    create = "create"
    modify = "modify"


class DiffRowKind(StrEnum):
    """The semantic role of one diff row."""

    hunk = "hunk"
    context = "context"
    addition = "addition"
    deletion = "deletion"
    omission = "omission"


@dataclass(frozen=True)
class DiffRow:
    """One literal source or metadata row in a structured unified diff."""

    kind: DiffRowKind
    text: str
    old_line: int | None = None
    new_line: int | None = None
    emphasis_ranges: tuple[tuple[int, int], ...] = ()
    # Exact known-length synthetic suffix from a noteworthy line terminator.
    # It preserves literal-source boundaries without pattern-matching content.
    terminator_note_length: int = 0
    # Omission rows report the source evidence they replace. A source row has
    # these fields set only when a prior byte cap retained its prefix, so a
    # tighter later cap can avoid double-counting that pending omission.
    hidden_rows: int = 0
    hidden_bytes: int = 0

    @property
    def is_source(self) -> bool:
        """Whether this row represents a source line rather than hunk metadata."""

        return self.kind in {
            DiffRowKind.context,
            DiffRowKind.addition,
            DiffRowKind.deletion,
        }


@dataclass(frozen=True)
class DiffVisibleRow:
    """One row selected for a bounded collapsed or expanded diff view."""

    row: DiffRow
    hidden_rows: int = 0
    hidden_bytes: int = 0

    @classmethod
    def omission(cls, hidden_rows: int, hidden_bytes: int) -> DiffVisibleRow:
        """Describe omitted source evidence without pretending it was displayed."""

        parts: list[str] = []
        if hidden_rows:
            unit = "line" if hidden_rows == 1 else "lines"
            parts.append(f"{hidden_rows} {unit} hidden")
        if hidden_bytes:
            parts.append(f"{hidden_bytes} bytes hidden")
        return cls(
            DiffRow(
                DiffRowKind.omission,
                f"… {', '.join(parts) or 'content hidden'}",
                hidden_rows=hidden_rows,
                hidden_bytes=hidden_bytes,
            ),
            hidden_rows=hidden_rows,
            hidden_bytes=hidden_bytes,
        )


# These are display bounds, not diff-work bounds. The builder has already refused
# inputs that exceed its bounded SequenceMatcher workload before constructing this
# presentation. Expanded output is still capped so one card cannot dominate a
# transcript or unexpectedly consume an entire small terminal.
# Theme style variables are shared by the legacy ``Content`` renderer and the
# structured ToolCard painter. The literal +/- markers and header letters remain
# non-color cues when Textual is running without color.
DIFF_ADD_STYLE = "$text-success"
DIFF_DEL_STYLE = "$text-error"
DIFF_CONTEXT_STYLE = "$text-muted"
DIFF_META_STYLE = "$text-muted"
DIFF_INTRA_HIGHLIGHT_MODIFIER = "reverse"

DIFF_COLLAPSED_ROWS = 8
DIFF_COLLAPSED_BYTES = 2_000
DIFF_EXPANDED_ROWS = 400
DIFF_EXPANDED_BYTES = 64_000


@dataclass(frozen=True)
class DiffPresentation:
    """A literal, bounded-at-render-time view of one edit or write operation."""

    path: str | None
    operation: DiffOperation
    additions: int
    deletions: int
    rows: tuple[DiffRow, ...]
    show_line_numbers: bool

    @property
    def file_marker(self) -> str:
        """A conventional single-letter marker that survives no-color themes."""

        return "A" if self.operation is DiffOperation.create else "M"

    @property
    def file_label(self) -> str:
        """A safe fallback label when a malformed tool call omitted its path."""

        return self.path or "(unnamed file)"

    def visible_rows(self, *, expanded: bool) -> tuple[DiffVisibleRow, ...]:
        """Select a deterministic bounded preview for the requested card state."""

        return select_diff_rows(
            self.rows,
            max_rows=DIFF_EXPANDED_ROWS if expanded else DIFF_COLLAPSED_ROWS,
            max_bytes=DIFF_EXPANDED_BYTES if expanded else DIFF_COLLAPSED_BYTES,
        )

    @property
    def can_expand(self) -> bool:
        """Whether expanded mode reveals diff evidence beyond the preview."""

        return self.visible_rows(expanded=False) != self.visible_rows(expanded=True)


def select_diff_rows(
    rows: Iterable[DiffRow],
    *,
    max_rows: int,
    max_bytes: int,
) -> tuple[DiffVisibleRow, ...]:
    """Return a bounded, change-first selection while preserving diff order.

    The preview budget counts source rows, not hunk metadata. A replacement is
    allocated across its removed and added sides before another change group is
    considered, avoiding the old first-N-lines behavior that could show only
    deletions. Context is added only from immediately around selected changes.
    Omitted spans are explicit rows rather than silent clipping.
    """

    if max_rows < 1 or max_bytes < 1:
        return ()
    ordered = tuple(rows)
    selected = _selected_source_indices(ordered, max_rows)
    included = (
        selected
        | _required_hunk_indices(ordered, selected)
        | {index for index, row in enumerate(ordered) if row.kind is DiffRowKind.omission}
    )
    planned = _with_omission_rows(ordered, included)
    return _apply_byte_limit(planned, max_bytes)


def _selected_source_indices(rows: tuple[DiffRow, ...], max_rows: int) -> set[int]:
    selected: set[int] = set()
    remaining = max_rows
    for group in _change_groups(rows):
        if remaining == 0:
            break
        chosen = _choose_change_rows(rows, group, remaining)
        selected.update(chosen)
        remaining -= len(chosen)

    if remaining:
        for index in _nearby_context_indices(rows, selected):
            if remaining == 0:
                break
            selected.add(index)
            remaining -= 1
    return selected


def _change_groups(rows: tuple[DiffRow, ...]) -> tuple[tuple[int, ...], ...]:
    groups: list[tuple[int, ...]] = []
    current: list[int] = []
    for index, row in enumerate(rows):
        if row.kind in {DiffRowKind.addition, DiffRowKind.deletion}:
            current.append(index)
            continue
        if row.kind is DiffRowKind.omission:
            # An outer expanded-limit pass may already have inserted an omission
            # between the retained deletion/addition sides of one replacement.
            # It is evidence metadata, not a semantic hunk boundary, so leave the
            # change group open and let a smaller collapsed pass retain both sides.
            continue
        if current:
            groups.append(tuple(current))
            current.clear()
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _choose_change_rows(
    rows: tuple[DiffRow, ...], group: tuple[int, ...], remaining: int
) -> tuple[int, ...]:
    deletions = tuple(index for index in group if rows[index].kind is DiffRowKind.deletion)
    additions = tuple(index for index in group if rows[index].kind is DiffRowKind.addition)
    if not deletions or not additions:
        return group[:remaining]
    if remaining == 1:
        # A one-row caller cannot show both sides. Prefer the addition because it
        # describes the resulting file; normal card bounds are always larger.
        return additions[:1]

    deletion_count = min(len(deletions), max(1, remaining // 2))
    addition_count = min(len(additions), max(1, remaining - deletion_count))
    while deletion_count + addition_count < remaining:
        if deletion_count < len(deletions):
            deletion_count += 1
        elif addition_count < len(additions):
            addition_count += 1
        else:
            break
    return tuple(sorted((*deletions[:deletion_count], *additions[:addition_count])))


def _nearby_context_indices(rows: tuple[DiffRow, ...], selected: set[int]) -> tuple[int, ...]:
    if not selected:
        return ()
    candidates: list[int] = []
    selected_positions = tuple(sorted(selected))
    for distance in range(1, len(rows)):
        found_at_distance = False
        for index in selected_positions:
            for candidate in (index - distance, index + distance):
                if (
                    0 <= candidate < len(rows)
                    and candidate not in selected
                    and rows[candidate].kind is DiffRowKind.context
                    and candidate not in candidates
                ):
                    candidates.append(candidate)
                    found_at_distance = True
        if not found_at_distance and distance > 2:
            # Unified diffs contain at most two context rows at either side by
            # construction. Do not walk distant unrelated hunks for filler.
            break
    return tuple(candidates)


def _required_hunk_indices(rows: tuple[DiffRow, ...], selected: set[int]) -> set[int]:
    required: set[int] = set()
    for index in selected:
        cursor = index - 1
        while cursor >= 0 and rows[cursor].kind is not DiffRowKind.hunk:
            cursor -= 1
        if cursor < 0:
            continue
        while cursor > 0 and rows[cursor - 1].kind is DiffRowKind.hunk:
            cursor -= 1
        block_end = cursor
        while block_end + 1 < len(rows) and rows[block_end + 1].kind is DiffRowKind.hunk:
            block_end += 1
        required.update(range(cursor, block_end + 1))
    return required


def _with_omission_rows(
    rows: tuple[DiffRow, ...], included: set[int]
) -> tuple[DiffVisibleRow, ...]:
    visible: list[DiffVisibleRow] = []
    hidden_rows = 0
    hidden_bytes = 0
    pending_partial_bytes = 0

    def flush_hidden() -> None:
        nonlocal hidden_rows, hidden_bytes
        if hidden_rows:
            visible.append(DiffVisibleRow.omission(hidden_rows, hidden_bytes))
        hidden_rows = 0
        hidden_bytes = 0

    for index, row in enumerate(rows):
        if index in included:
            flush_hidden()
            if row.kind is DiffRowKind.omission and pending_partial_bytes:
                # A prior expanded byte cap already counted the pending partial
                # source row in this omission. Its retained prefix becomes
                # hidden only at this narrower selection, so merge those bytes
                # without adding the same source line a second time.
                visible.append(
                    DiffVisibleRow.omission(
                        row.hidden_rows,
                        row.hidden_bytes + pending_partial_bytes,
                    )
                )
                pending_partial_bytes = 0
            else:
                if pending_partial_bytes:
                    # Defensive fallback for a synthetic row sequence lacking
                    # the paired omission produced by _apply_byte_limit.
                    visible.append(DiffVisibleRow.omission(0, pending_partial_bytes))
                    pending_partial_bytes = 0
                visible.append(
                    DiffVisibleRow(
                        row,
                        hidden_rows=row.hidden_rows,
                        hidden_bytes=row.hidden_bytes,
                    )
                )
        elif row.is_source:
            if row.hidden_rows:
                # The paired omission already owns this line count; only this
                # retained prefix needs carrying into that omission's byte total.
                pending_partial_bytes += len(row.text.encode("utf-8"))
            else:
                hidden_rows += 1
                hidden_bytes += len(row.text.encode("utf-8"))
    flush_hidden()
    if pending_partial_bytes:
        visible.append(DiffVisibleRow.omission(0, pending_partial_bytes))
    return tuple(visible)


def _apply_byte_limit(
    rows: tuple[DiffVisibleRow, ...], max_bytes: int
) -> tuple[DiffVisibleRow, ...]:
    """Clip selected literal row text and report the evidence that did not fit."""

    visible: list[DiffVisibleRow] = []
    used = 0
    for index, visible_row in enumerate(rows):
        row = visible_row.row
        row_bytes = len(row.text.encode("utf-8"))
        if row.kind is DiffRowKind.omission:
            # Metadata is intentionally outside the source-content budget, like the
            # existing honest-truncation trailer.
            visible.append(visible_row)
            continue
        if used + row_bytes <= max_bytes:
            visible.append(visible_row)
            used += row_bytes
            continue

        remaining = max(0, max_bytes - used)
        shown_bytes = 0
        if remaining:
            clipped = _clip_to_bytes(row.text, remaining)
            if clipped:
                shown_bytes = len(clipped.encode("utf-8"))
                # A later selection may clip this retained prefix again. Keep
                # enough metadata to recognize that the following omission
                # already counts this partially shown source line, while still
                # retaining its prefix bytes for a tighter future byte window.
                partial = replace(
                    row,
                    text=clipped,
                    # A byte-clipped prefix cannot still contain the known
                    # terminator suffix from this row's end. Treat it as
                    # literal source rather than slicing real content as note.
                    terminator_note_length=0,
                    hidden_rows=1,
                    hidden_bytes=row_bytes - shown_bytes,
                )
                visible.append(
                    DiffVisibleRow(
                        partial,
                        hidden_rows=partial.hidden_rows,
                        hidden_bytes=partial.hidden_bytes,
                    )
                )
                used += shown_bytes
        hidden_rows = sum(
            rest.hidden_rows
            if rest.row.kind is DiffRowKind.omission
            else int(rest.row.is_source and not rest.row.hidden_rows)
            for rest in rows[index:]
        )
        hidden_bytes = (
            sum(
                rest.hidden_bytes
                if rest.row.kind is DiffRowKind.omission
                else len(rest.row.text.encode("utf-8"))
                for rest in rows[index:]
            )
            - shown_bytes
        )
        visible.append(DiffVisibleRow.omission(max(0, hidden_rows), max(0, hidden_bytes)))
        break
    return tuple(visible)


def _clip_to_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


__all__ = [
    "DIFF_ADD_STYLE",
    "DIFF_COLLAPSED_BYTES",
    "DIFF_COLLAPSED_ROWS",
    "DIFF_CONTEXT_STYLE",
    "DIFF_DEL_STYLE",
    "DIFF_EXPANDED_BYTES",
    "DIFF_EXPANDED_ROWS",
    "DIFF_INTRA_HIGHLIGHT_MODIFIER",
    "DIFF_META_STYLE",
    "DiffOperation",
    "DiffPresentation",
    "DiffRow",
    "DiffRowKind",
    "DiffVisibleRow",
    "select_diff_rows",
]
