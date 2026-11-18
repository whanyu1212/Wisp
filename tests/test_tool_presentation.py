"""Failure explanations stay aligned between Python and the native frontend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

from wisp.tool_presentation import tool_failure_reason
from wisp.tool_types import ToolFailureCode


def test_failure_reasons_match_shared_fixtures() -> None:
    cases = json.loads((Path(__file__).parent / "fixtures/tool_failure_reasons.json").read_text())
    seen_codes = set()
    for case in cases:
        expected = case.pop("expected")
        assert (
            tool_failure_reason(
                case["is_error"],
                case["exit_code"],
                failure_code=case["failure_code"],
                process_state=case["process_state"],
            )
            == expected
        )
        if case["failure_code"] is not None:
            seen_codes.add(case["failure_code"])
    assert seen_codes == set(get_args(ToolFailureCode))
