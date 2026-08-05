"""Shared test fixtures.

Wisp reads configuration from both ``WISP_*`` environment variables and settings
files under the user home and project directory. Without isolation, a developer's
active Wisp process or local settings could change test behavior. The autouse
fixture below clears inherited Wisp configuration, then redirects ``HOME`` and the
working directory to fresh temporary directories for every test. Tests that need
specific configuration opt in explicitly with ``monkeypatch.setenv()`` or by
writing settings files into those directories.
"""

from __future__ import annotations

import os
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
