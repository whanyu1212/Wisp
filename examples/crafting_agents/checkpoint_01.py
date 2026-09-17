"""Observe a read failure and recovery without touching the filesystem."""

import asyncio

from examples.crafting_agents.core import (
    Message,
    ScriptedProvider,
    ScriptStep,
    ToolCall,
    ToolFailure,
    ToolSpec,
    run_agent,
)

BUGGY_SOURCE = "def add(a, b):\n    return a - b\n"
READ = ToolSpec("read", "Read the fixture source file.", ("path",))


def read_fixture(call: ToolCall) -> str:
    """Return the sole in-memory file, or an expected tool failure.

    Args:
        call (ToolCall): Requested tool name and decoded arguments.

    Returns:
        str: The buggy calculator source.

    Raises:
        ToolFailure: The request does not name the fixture's read operation.
    """
    if call.name != "read" or call.arguments != {"path": "calculator.py"}:
        raise ToolFailure("use read with path='calculator.py'")
    return BUGGY_SOURCE


# ANCHOR: script
def make_provider() -> ScriptedProvider:
    """Return the checkpoint's fixed read-error, read-success, diagnosis exchange."""
    return ScriptedProvider(
        (
            ScriptStep(
                Message(
                    "assistant",
                    "Locate the function.",
                    (ToolCall("1", "read", {"path": "sum.py"}),),
                )
            ),
            ScriptStep(
                Message(
                    "assistant",
                    "Try the path from the error.",
                    (ToolCall("2", "read", {"path": "calculator.py"}),),
                ),
                after="error: use read",
            ),
            ScriptStep(
                Message(
                    "assistant", "add subtracts b. It needs addition; no file has been changed."
                ),
                after="return a - b",
            ),
        )
    )


# ANCHOR_END: script


async def main() -> None:
    result = await run_agent(
        "Fix add(2, 3), which should return 5.", make_provider(), (READ,), read_fixture
    )
    print(f"stopped: {result.stop_reason}")


if __name__ == "__main__":
    asyncio.run(main())
