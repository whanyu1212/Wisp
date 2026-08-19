"""Typed process-local user submissions shared by TUI input and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import NewType

SubmissionId = NewType("SubmissionId", int)
_SUBMISSION_IDS = count(1)
_PENDING_PREVIEW_ITEM_LIMIT = 3
_PENDING_PREVIEW_LINE_LIMIT = 2


def new_submission_id() -> SubmissionId:
    """Return a process-unique identity for one accepted frontend submission."""

    return SubmissionId(next(_SUBMISSION_IDS))


@dataclass(frozen=True)
class PendingSubmissionView:
    """Renderer-safe projection of one accepted prompt waiting to start."""

    id: SubmissionId
    display: str


def pending_submission_preview_lines(
    submissions: tuple[PendingSubmissionView, ...],
    *,
    edit_hint: str,
) -> tuple[str, ...]:
    """Return a bounded preview focused on the newest editable submissions."""

    if not submissions:
        return ()
    omitted = max(0, len(submissions) - _PENDING_PREVIEW_ITEM_LIMIT)
    lines = ["Queued follow-ups"]
    if omitted:
        lines.append(f"… {omitted} earlier queued")
    for submission in submissions[-_PENDING_PREVIEW_ITEM_LIMIT:]:
        display_lines = submission.display.splitlines() or [""]
        visible_lines = display_lines[:_PENDING_PREVIEW_LINE_LIMIT]
        if len(display_lines) > _PENDING_PREVIEW_LINE_LIMIT:
            visible_lines[-1] = f"{visible_lines[-1]} …"
        lines.extend(
            f"{'↳' if index == 0 else ' '} {line}" for index, line in enumerate(visible_lines)
        )
    lines.append(edit_hint)
    return tuple(lines)


class TuiSubmission(str):
    """One accepted prompt with stable identity and separate display content.

    Subclassing ``str`` preserves the prompt-reader contract for injected and
    legacy frontends while carrying identity and compact display metadata through
    the owned-input adapters.
    """

    id: SubmissionId
    display: str

    def __new__(
        cls,
        *,
        id: SubmissionId,
        content: str,
        display: str,
    ) -> TuiSubmission:
        instance = super().__new__(cls, content)
        instance.id = id
        instance.display = display
        return instance

    @property
    def content(self) -> str:
        return str(self)

    def pending_view(self) -> PendingSubmissionView:
        return PendingSubmissionView(id=self.id, display=self.display)


__all__ = [
    "PendingSubmissionView",
    "SubmissionId",
    "TuiSubmission",
    "new_submission_id",
    "pending_submission_preview_lines",
]
