"""Typed process-local user submissions shared by TUI input and renderers."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import NewType

SubmissionId = NewType("SubmissionId", int)
_SUBMISSION_IDS = count(1)


def new_submission_id() -> SubmissionId:
    """Return a process-unique identity for one accepted frontend submission."""

    return SubmissionId(next(_SUBMISSION_IDS))


@dataclass(frozen=True)
class PendingSubmissionView:
    """Renderer-safe projection of one accepted prompt waiting to start."""

    id: SubmissionId
    display: str


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
]
