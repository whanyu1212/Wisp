"""What a codemode script's tool calls exchange with the executor that runs them."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from typing_extensions import TypeAliasType

CODEMODE = "codemode"


@dataclass(frozen=True, slots=True)
class NestedCallResult:
    """The outcome of one tool call a script made.

    Attributes:
        text (str): What the tool returned, or why the call failed.
        is_error (bool): Whether the call failed.
    """

    text: str
    is_error: bool = False


# Runs a call a script made, by tool name and keyword arguments, under the run's
# tool policy. The executor provides it; see `ConfiguredToolExecutor.run_nested`.
NestedToolCaller = TypeAliasType(
    "NestedToolCaller", Callable[[str, Mapping[str, object]], Awaitable[NestedCallResult]]
)
