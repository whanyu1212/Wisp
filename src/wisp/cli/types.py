"""Shared CLI types for Wisp."""

from __future__ import annotations

from enum import StrEnum

from wisp.providers.base import ProviderError


class OutputMode(StrEnum):
    """CLI output/application modes."""

    text = "text"
    json = "json"
    rpc = "rpc"
    tui = "tui"


class _JsonOutputModeError(ProviderError):
    """Raised after JSONL output has already emitted a model-visible error event."""


class _RenderedPrintError(ProviderError):
    """Raised after print mode has already rendered the terminal failure."""
