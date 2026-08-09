"""Shared tool value types without runtime dependencies."""

from typing import Literal

ToolSafety = Literal["read", "mutating", "command"]
