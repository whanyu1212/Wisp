"""Typed update-prompt actions shared by the shell and renderers."""

from __future__ import annotations

from enum import StrEnum


class UpdatePromptAction(StrEnum):
    """One explicit choice made from an available-update prompt."""

    update_and_restart = "update_and_restart"
    later = "later"
    skip_version = "skip_version"


__all__ = ["UpdatePromptAction"]
