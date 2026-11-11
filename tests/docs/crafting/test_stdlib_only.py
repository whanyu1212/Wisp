"""The offline crafting-agents checkpoints run on the standard library alone.

The docs workflow runs them with a bare ``python`` before installing anything. The live
transport and the built-in tools tour are left out: they use Wisp and its dependencies.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.support.paths import REPO_ROOT

_CHECKPOINTS = sorted(
    path.stem for path in (REPO_ROOT / "examples" / "crafting_agents").glob("checkpoint_*.py")
)


@pytest.mark.process
@pytest.mark.parametrize("module", _CHECKPOINTS)
def test_imports_without_site_packages(module: str) -> None:
    """Each checkpoint imports with ``-S``, which leaves out every installed package."""

    result = subprocess.run(
        [sys.executable, "-S", "-c", f"import examples.crafting_agents.{module}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
