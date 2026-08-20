"""Typed process-local submissions shared by TUI input and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import NewType

from rich.cells import cell_len, set_cell_size

SubmissionId = NewType("SubmissionId", int)
_SUBMISSION_IDS = count(1)
PENDING_PREVIEW_ITEM_LIMIT = 3
PENDING_PREVIEW_LINE_LIMIT = 2
PENDING_PREVIEW_FALLBACK_WIDTH = 120


def new_submission_id() -> SubmissionId:
    """Return a process-unique identity for one accepted frontend submission."""

    return SubmissionId(next(_SUBMISSION_IDS))


@dataclass(frozen=True)
class PendingSubmissionView:
    """Renderer-safe projection of one accepted prompt waiting to start."""

    id: SubmissionId
    display: str


class TuiSubmission(str):
    """Accepted prompt with stable identity and frontend presentation metadata.

    Subclassing ``str`` keeps injected ``PromptReader`` implementations and the
    shell's existing parsing surface compatible while owned input adapters carry
    exact submission-time state through their asynchronous queues.
    """

    id: SubmissionId
    display: str
    input_mode: str

    def __new__(
        cls,
        *,
        id: SubmissionId,
        content: str,
        display: str,
        input_mode: str,
    ) -> TuiSubmission:
        instance = super().__new__(cls, content)
        instance.id = id
        instance.display = display
        instance.input_mode = input_mode
        return instance

    @property
    def content(self) -> str:
        return str(self)

    def pending_view(self) -> PendingSubmissionView:
        return PendingSubmissionView(id=self.id, display=self.display)


def _truncate_preview_line(line: str, width: int) -> str:
    bounded = max(1, width)
    if cell_len(line) <= bounded:
        return line
    if bounded == 1:
        return "…"
    return f"{set_cell_size(line, bounded - 1).rstrip()}…"


def pending_submission_preview_lines(
    submissions: tuple[PendingSubmissionView, ...],
    *,
    width: int | None,
) -> tuple[str, ...]:
    """Build a row- and cell-width-bounded preview of the newest submissions."""

    if not submissions:
        return ()
    selected_width = width or PENDING_PREVIEW_FALLBACK_WIDTH
    selected_width = max(1, selected_width)
    omitted = max(0, len(submissions) - PENDING_PREVIEW_ITEM_LIMIT)
    lines = [_truncate_preview_line("Queued follow-ups", selected_width)]
    if omitted:
        lines.append(_truncate_preview_line(f"… {omitted} earlier queued", selected_width))
    for submission in submissions[-PENDING_PREVIEW_ITEM_LIMIT:]:
        display_lines = submission.display.splitlines() or [""]
        visible = display_lines[:PENDING_PREVIEW_LINE_LIMIT]
        for index, line in enumerate(visible):
            prefix = "↳ " if index == 0 else "  "
            suffix = " …" if index == len(visible) - 1 and len(display_lines) > len(visible) else ""
            lines.append(_truncate_preview_line(f"{prefix}{line}{suffix}", selected_width))
    return tuple(lines)


__all__ = [
    "PENDING_PREVIEW_FALLBACK_WIDTH",
    "PENDING_PREVIEW_ITEM_LIMIT",
    "PENDING_PREVIEW_LINE_LIMIT",
    "PendingSubmissionView",
    "SubmissionId",
    "TuiSubmission",
    "new_submission_id",
    "pending_submission_preview_lines",
]
