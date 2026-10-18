"""Run bounded read-only Git queries for project discovery and context."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

GIT_CONTEXT_TIMEOUT_SECONDS = 2.0


_GIT_CONTEXT_DEADLINE: ContextVar[float | None] = ContextVar(
    "wisp_git_context_deadline",
    default=None,
)


@contextmanager
def git_deadline() -> Iterator[None]:
    """Bound every `run_git` call in this block by one shared deadline.

    An already active deadline is kept rather than extended, so a prompt build
    that discovers the project root and then reads the status spends at most
    `GIT_CONTEXT_TIMEOUT_SECONDS` on Git in total.

    Yields:
        None: Control while the deadline applies; the previous deadline is
            restored on exit.
    """

    if _GIT_CONTEXT_DEADLINE.get() is not None:
        yield
        return
    token = _GIT_CONTEXT_DEADLINE.set(time.monotonic() + GIT_CONTEXT_TIMEOUT_SECONDS)
    try:
        yield
    finally:
        _GIT_CONTEXT_DEADLINE.reset(token)


def run_git(cwd: Path, *args: str) -> str | None:
    """Run one Git command within the active deadline.

    Args:
        cwd (Path): Directory to run Git in.
        *args (str): Git subcommand and arguments.

    Returns:
        str | None: Stripped standard output, or None when Git fails, is missing,
            or runs past the deadline (or one second without a deadline).
    """

    deadline = _GIT_CONTEXT_DEADLINE.get()
    remaining = deadline - time.monotonic() if deadline is not None else 1.0
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(
            ("git", "-C", str(cwd), *args),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=min(1.0, remaining),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
