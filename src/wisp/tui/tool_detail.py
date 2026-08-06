"""Typed detail variants shared by tool-result renderers and Textual cards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PrettyToolOutput:
    """A bounded JSON object or array rendered by Textual's ``Pretty`` widget.

    Tool output crosses the agent/RPC boundary as text. The UI recognizes only a
    complete JSON object or array in the generic fallback path, parses it once,
    and retains this local presentation value. It is never persisted or sent back
    to the model.
    """

    value: object
    kind: Literal["object", "array"]
    item_count: int

    @property
    def summary(self) -> str:
        """Return the concise, non-content summary displayed while collapsed."""

        noun = "key" if self.kind == "object" else "item"
        plural = "" if self.item_count == 1 else "s"
        return f"structured JSON {self.kind} ({self.item_count} {noun}{plural})"


__all__ = ["PrettyToolOutput"]
