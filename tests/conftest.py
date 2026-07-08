"""Shared test fixtures.

The settings layer (:mod:`wisp.settings`) reads ``~/.wisp/settings.json`` and
``./.wisp/settings.json`` by default. Without isolation, a developer who happens
to have real settings files would see the suite behave differently from CI. The
autouse fixture below redirects ``HOME`` and the working directory to a fresh temp
directory for every test, so settings resolution starts from a clean slate; a test
that wants specific settings writes them into that temp directory itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path_factory: pytest.TempPathFactory, monkeypatch: MonkeyPatch) -> None:
    """Point HOME and the cwd at an empty temp dir so real settings never leak in."""

    home = tmp_path_factory.mktemp("home")
    workdir = tmp_path_factory.mktemp("cwd")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows equivalent of HOME
    monkeypatch.chdir(workdir)
    # Some code resolves the home directory via Path.home(); ensure it agrees with
    # the patched HOME rather than the real user home on every platform.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
