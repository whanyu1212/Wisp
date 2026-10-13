"""Shared test fixtures.

Wisp reads configuration from both ``WISP_*`` environment variables and settings
files under the user home and project directory. Without isolation, a developer's
active Wisp process or local settings could change test behavior. The autouse
fixture below clears inherited Wisp configuration, then redirects ``HOME`` and the
working directory to fresh temporary directories for every test. Tests that need
specific configuration opt in explicitly with ``monkeypatch.setenv()`` or by
writing settings files into those directories.

A second autouse fixture checks agent-loop invariants on every loop stream a test
drives; see ``tests/support/loop_stream_checks.py``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _isolate_test_environment(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: MonkeyPatch,
) -> None:
    """Start each test without inherited Wisp configuration or settings files."""

    # Unset means "undecided" for trust; forcing WISP_TRUST=0 would suppress the
    # trust-request flow that several tests intentionally exercise.
    for name in tuple(os.environ):
        if name.startswith("WISP_"):
            monkeypatch.delenv(name, raising=False)

    home = tmp_path_factory.mktemp("home")
    workdir = tmp_path_factory.mktemp("cwd")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows equivalent of HOME
    monkeypatch.chdir(workdir)
    # Some code resolves the home directory via Path.home(); ensure it agrees with
    # the patched HOME rather than the real user home on every platform.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


@pytest.fixture(autouse=True)
def _check_agent_loop_streams(
    request: pytest.FixtureRequest,
    monkeypatch: MonkeyPatch,
) -> Iterator[None]:
    """Fail the test if any agent-loop stream it drives breaks the loop's invariants.

    Only modules that are already imported are patched, so this fixture never
    imports Wisp code a test did not load itself.
    """

    loop_runner = sys.modules.get("wisp.agent.loop.runner")
    if loop_runner is None:
        yield
        return

    from tests.support.loop_stream_checks import (
        LOOP_CALLER_MODULES,
        checked_run_agent_loop,
        is_exempt,
    )

    if is_exempt(request.node):
        yield
        return

    real_run_agent_loop = loop_runner.run_agent_loop
    violations: list[str] = []
    checked = checked_run_agent_loop(real_run_agent_loop, violations.append)
    caller_modules = [sys.modules.get(name) for name in LOOP_CALLER_MODULES]
    for module in (*caller_modules, request.module):
        if module is not None and getattr(module, "run_agent_loop", None) is real_run_agent_loop:
            monkeypatch.setattr(module, "run_agent_loop", checked)
    yield
    if violations:
        pytest.fail(
            "Agent-loop stream broke loop invariants (mark the test with "
            "loop_invariants_exempt(reason=...) only if that is intended):\n"
            + "\n".join(f"- {violation}" for violation in violations)
        )
