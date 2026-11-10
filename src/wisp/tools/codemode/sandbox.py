"""Run one codemode script in monty, Pydantic's Python sandbox.

The script runs in a monty worker process with no filesystem, environment, network
or imports beyond monty's own small standard library. Its only way out is the async
functions it is given. Every limit here is on by default: monty bounds the
script's own computing time, memory and number of calls, and a wall-clock deadline
here bounds the whole run, tool calls included. Leaving the deadline, or cancelling
the run, kills the worker and cancels every tool call still running: monty starts
each call as its own task, which would otherwise outlive the script.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import anyio

type ScriptFunction = Callable[..., Awaitable[str]]
type ScriptFailure = Literal["unavailable", "syntax", "type", "runtime", "timeout"]


@dataclass(frozen=True, slots=True)
class ScriptLimits:
    """The bounds a script runs under.

    Attributes:
        wall_seconds (float): The whole run, tool calls included.
        compute_seconds (float): The script's own computing, not counting the time
            it waits for tool calls.
        memory_bytes (int): The script's heap.
        max_calls (int): Tool calls the script may make.
        max_printed_bytes (int): What the script may print.
    """

    wall_seconds: float = 120.0
    compute_seconds: float = 10.0
    memory_bytes: int = 128 * 1024 * 1024
    max_calls: int = 200
    max_printed_bytes: int = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScriptRun:
    """How a script ended.

    Attributes:
        printed (str): What the script printed, up to the point it ended.
        value (str | None): Its final expression, as text; None when it had none.
        failure (ScriptFailure | None): Why it failed, or None when it finished.
        error (str | None): The failure's message, for the model to act on.
    """

    printed: str
    value: str | None = None
    failure: ScriptFailure | None = None
    error: str | None = None


async def run_script(
    code: str,
    *,
    functions: Mapping[str, ScriptFunction],
    stubs: str,
    limits: ScriptLimits,
) -> ScriptRun:
    """Type-check ``code`` against ``stubs``, then run it with ``functions``.

    Args:
        code (str): The script. Its last expression, if any, is its value.
        functions (Mapping[str, ScriptFunction]): The async functions the script
            may call, by name; each returns the text of one tool call.
        stubs (str): Python declarations of ``functions``, which the script is
            type-checked against before anything runs.
        limits (ScriptLimits): The bounds it runs under.

    Returns:
        ScriptRun: What it printed and returned, or why it failed. A failure inside
        the script, such as an exception it did not catch, is a result, not an
        exception.

    Raises:
        anyio.get_cancelled_exc_class(): When the caller is cancelled; the worker is
            killed first.
    """

    try:
        from pydantic_monty import (
            AsyncMonty,
            CollectString,
            MontyError,
            MontySyntaxError,
            MontyTypingError,
            ResourceLimits,
        )
    except ImportError:
        return ScriptRun(
            printed="",
            failure="unavailable",
            error="codemode needs the `codemode` extra: install wisp-ai[codemode].",
        )

    calls = _ScriptCalls()
    printed = CollectString(max_bytes=limits.max_printed_bytes)
    resource_limits = ResourceLimits(
        max_feed_duration_secs=limits.compute_seconds,
        max_memory=limits.memory_bytes,
        max_suspensions=limits.max_calls,
    )
    try:
        with anyio.fail_after(limits.wall_seconds):
            async with AsyncMonty() as pool:
                async with pool.checkout(
                    limits=resource_limits, type_check=True, type_check_stubs=stubs
                ) as session:
                    value = await session.feed_run(
                        code,
                        external_lookup={
                            name: calls.guarded(function) for name, function in functions.items()
                        },
                        print_callback=printed,
                    )
    except TimeoutError:
        return ScriptRun(
            printed=printed.output,
            failure="timeout",
            error=f"The script did not finish within {limits.wall_seconds:g} seconds.",
        )
    except MontySyntaxError as exc:
        return ScriptRun(printed="", failure="syntax", error=str(exc))
    except MontyTypingError as exc:
        return ScriptRun(printed="", failure="type", error=exc.display())
    except MontyError as exc:
        return ScriptRun(printed=printed.output, failure="runtime", error=str(exc))
    finally:
        calls.end()
    return ScriptRun(printed=printed.output, value=_as_text(value))


class _ScriptCalls:
    """The tool calls one script has running, so its end can cancel them."""

    def __init__(self) -> None:
        self._running: set[anyio.CancelScope] = set()
        self._ended = False

    def guarded(self, function: ScriptFunction) -> ScriptFunction:
        """Wrap ``function`` so a call still running when the script ends is cancelled."""

        async def call(*args: object, **kwargs: object) -> str:
            if self._ended:
                raise RuntimeError("The script has ended")
            with anyio.CancelScope() as scope:
                self._running.add(scope)
                try:
                    return await function(*args, **kwargs)
                finally:
                    self._running.discard(scope)
            raise RuntimeError("The script ended before this call finished")

        return call

    def end(self) -> None:
        """Refuse new calls and cancel the running ones."""

        self._ended = True
        for scope in tuple(self._running):
            scope.cancel()


def _as_text(value: object) -> str | None:
    """The script's final value as the model reads it: text, or JSON for data."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)
