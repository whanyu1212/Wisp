"""Tool execution context."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolContext:
    """Ambient state shared with tool invocations."""

    cwd: Path
    max_output_bytes: int = 50_000
    max_output_lines: int = 2_000

    @classmethod
    def default(cls) -> ToolContext:
        """Create a context rooted at the current working directory."""

        return cls(cwd=Path.cwd())
