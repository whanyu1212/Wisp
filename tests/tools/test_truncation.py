from __future__ import annotations

import pytest

from wisp.tools.truncation import truncate_text_tail

pytestmark = pytest.mark.process


def test_tail_truncation_with_marker_only_budget_stays_bounded() -> None:
    result = truncate_text_tail("diagnostic tail", max_bytes=12, max_lines=10)

    assert result.text == "[truncated]"
    assert len(result.text.encode("utf-8")) <= 12
    assert result.truncated is True
