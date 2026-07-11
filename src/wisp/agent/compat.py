"""Temporary legacy `Agent` facade over the coding-session coordinator."""

from __future__ import annotations

from wisp.coding.session import PERSISTED_SESSION_EVENT_TYPES, CodingSession


class Agent(CodingSession):
    """Preserve the original public class name during frontend migration."""


__all__ = ["Agent", "PERSISTED_SESSION_EVENT_TYPES"]
