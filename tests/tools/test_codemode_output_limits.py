"""Sandbox output limits cover returned values as well as printed text."""

import anyio
import pytest

from wisp.tools.codemode.sandbox import ScriptLimits, ScriptRun, run_script

pytest.importorskip("pydantic_monty")


@pytest.mark.parametrize(
    ("code", "prefix", "failure"),
    [
        ("'é' * 6", "é" * 5, "limit"),
        ("print('abc')\n'12345678'", "abc\n123456", "limit"),
        ("'1234567890'", "1234567890", None),
    ],
)
def test_returned_output_is_bounded(code: str, prefix: str, failure: str | None) -> None:
    async def run() -> ScriptRun:
        return await run_script(
            code, functions={}, stubs="", limits=ScriptLimits(max_printed_bytes=10)
        )

    result = anyio.run(run)
    assert result.failure == failure
    retained = result.printed if failure else result.value
    assert retained == prefix
    if failure:
        assert result.error and "later output was not saved" in result.error
