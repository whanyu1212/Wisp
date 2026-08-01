"""Errors shared by RPC execution adapters."""

from __future__ import annotations

from wisp.providers.base import ProviderError


class RpcOutputAlreadyReportedError(ProviderError):
    """Raised when an event renderer already emitted an operation error."""
