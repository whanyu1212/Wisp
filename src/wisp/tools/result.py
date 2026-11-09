"""Tool execution result types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from wisp.tool_types import ToolFailureCode


@dataclass(frozen=True)
class ToolResult:
    """Result returned by a Wisp tool invocation.

    Attributes:
        text (str): What the model reads.
        data (Mapping[str, object]): Structured facts for summaries and the UI.
        truncated (bool): Whether the tool capped its own output.
        images (tuple[bytes, ...]): Complete image files to attach to the result.
            The executor validates and stores them; an image it refuses fails the
            call.
    """

    text: str
    data: Mapping[str, object] = field(default_factory=dict)
    truncated: bool = False
    images: tuple[bytes, ...] = ()


_INVALID_ARGUMENT_RECOVERY_HINT = "Retry with arguments that match the tool's input schema."


class ToolError(RuntimeError):
    """A safe, optionally actionable tool failure returned to the model."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: ToolFailureCode | None = None,
        retryable: bool = False,
        recovery_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.retryable = retryable
        self.recovery_hint = recovery_hint


class ToolArgumentError(ToolError):
    """A schema-valid call whose argument values fail tool-level validation."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            failure_code="invalid_arguments",
            retryable=True,
            recovery_hint=_INVALID_ARGUMENT_RECOVERY_HINT,
        )
