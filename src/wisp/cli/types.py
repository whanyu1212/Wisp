"""Shared CLI types for Wisp."""

from __future__ import annotations

from enum import StrEnum

from wisp.providers.base import ProviderError
from wisp.rpc.errors import RpcOutputAlreadyReportedError


class OutputMode(StrEnum):
    """CLI output/application modes."""

    text = "text"
    json = "json"
    rpc = "rpc"
    tui = "tui"


# Compatibility name retained for CLI renderers.  RPC execution uses the
# transport-neutral class directly.
_JsonOutputModeError = RpcOutputAlreadyReportedError


class _RenderedPrintError(ProviderError):
    """Raised after print mode has already rendered the terminal failure."""
