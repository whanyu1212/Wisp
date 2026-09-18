"""Terminal error event shared by every frontend."""

from __future__ import annotations

from typing import Literal

from wisp.events._base import WispEvent


class ErrorEvent(WispEvent):
    type: Literal["error"] = "error"
    message: str
