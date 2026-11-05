"""The built-in tools tour still produces the transcript the documentation shows."""

import os

import pytest

from examples.crafting_agents.builtin_tools_tour import (
    format_transcript,
    run_in_temporary_project,
)
from tests.support.paths import REPO_ROOT

TRANSCRIPT = REPO_ROOT / "examples" / "crafting_agents" / "builtin_tools_tour.txt"


@pytest.mark.skipif(os.name != "posix", reason="the tour's commands need a POSIX shell")
def test_checked_in_transcript_matches_a_fresh_run() -> None:
    fresh = format_transcript(run_in_temporary_project(), anchors=True)

    assert fresh == TRANSCRIPT.read_text(encoding="utf-8"), (
        "Regenerate with: uv run python -m examples.crafting_agents.builtin_tools_tour "
        "--transcript > examples/crafting_agents/builtin_tools_tour.txt"
    )
