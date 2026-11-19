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
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from tests.support.pytest_sharding import parse_shard_spec, partition_shard


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register CLI options shared across the test suite."""

    parser.addoption(
        "--shard",
        action="store",
        default=None,
        help="Run 1-based shard INDEX/TOTAL of collected tests.",
    )


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


def pytest_configure(config: pytest.Config) -> None:
    """Check agent-loop invariants on every loop stream the session drives."""

    raw_shard = config.getoption("--shard")
    if raw_shard is not None:
        try:
            parse_shard_spec(str(raw_shard))
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from exc
    from tests.support.loop_stream_checks import install_loop_stream_checks

    install_loop_stream_checks()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Filter collected items to the requested ``--shard`` after marker/keyword filtering."""

    raw_shard = config.getoption("--shard")
    if raw_shard is None:
        return
    shard_index, shard_count = parse_shard_spec(str(raw_shard))
    selected, deselected = partition_shard(items, shard_index, shard_count)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(autouse=True)
def _check_agent_loop_streams(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test if any agent-loop stream it drives breaks the loop's invariants."""

    from tests.support.loop_stream_checks import collecting, is_exempt

    violations: list[str] = []
    with collecting(None if is_exempt(request.node) else violations.append):
        yield
    if violations:
        pytest.fail(
            "Agent-loop stream broke loop invariants (mark the test with "
            "loop_invariants_exempt(reason=...) only if that is intended):\n"
            + "\n".join(f"- {violation}" for violation in violations)
        )
