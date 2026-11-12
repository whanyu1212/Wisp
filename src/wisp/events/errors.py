"""Error and warning events shared by every frontend."""

from __future__ import annotations

from typing import Literal

from wisp.events._base import WispEvent


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str


class WarningEvent(WispEvent):
    """Something the user should know that does not stop or fail the run."""

    type: Literal["warning"] = "warning"
    message: str
