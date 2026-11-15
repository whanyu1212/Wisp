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
        incomplete (bool): Whether the text is only part of the tool's result: cut
            at the size limit, or stopped early by the tool, as `grep` does at
            ``max_results``.
    """

    text: str
    is_error: bool = False
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class ResultLimits:
    """The most of one tool's result a nested call returns.

    Attributes:
        max_bytes (int): UTF-8 bytes of text.
        max_lines (int): Lines of text.
    """

    max_bytes: int
    max_lines: int


# What a script's calls return. Only what a script prints and returns reaches the
# model, so its calls may return more than a direct call does, for the script to
# filter. Kept under the 1 MiB a script may print, so printing one whole result
# never ends the script; 200 results of this size also fit its 128 MiB.
SCRIPT_RESULT_LIMITS = ResultLimits(max_bytes=512 * 1024, max_lines=50_000)


# Runs a call a script made, by tool name and keyword arguments, under the run's
# tool policy. The executor provides it; see `ConfiguredToolExecutor.run_nested`.
NestedToolCaller = TypeAliasType(
    "NestedToolCaller", Callable[[str, Mapping[str, object]], Awaitable[NestedCallResult]]
)
