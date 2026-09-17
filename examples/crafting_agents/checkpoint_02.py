"""Read, edit, and test a disposable fixture using the same teaching loop."""

import argparse
import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.crafting_agents.checkpoint_01 import BUGGY_SOURCE, READ
from examples.crafting_agents.core import (
    Message,
    ScriptedProvider,
    ScriptStep,
    ToolCall,
    ToolFailure,
    ToolSpec,
    run_agent,
)

TOOLS = (
    READ,
    ToolSpec("edit", "Replace exactly one occurrence in calculator.py.", ("path", "old", "new")),
    ToolSpec("test", "Run the fixture's fixed addition tests."),
)
TEST_SOURCE = """import runpy
import unittest

add = runpy.run_path("calculator.py")["add"]

class AdditionTests(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_negative(self):
        self.assertEqual(add(-2, -3), -5)

if __name__ == "__main__":
    unittest.main()
"""


@dataclass(frozen=True)
class ToolResult:
    text: str
    truncated: bool = False


# ANCHOR: budget
def bound_output(text: str, *, max_bytes: int = 2_000, max_lines: int = 30) -> ToolResult:
    """Cap model-visible text by both UTF-8 bytes and lines.

    Args:
        text (str): Complete output from this small, trusted fixture.
        max_bytes (int): Positive byte cap on returned text.
        max_lines (int): Positive line cap on returned text.

    Returns:
        ToolResult: A valid UTF-8 prefix and an explicit truncation flag.

    Raises:
        ValueError: Either budget is non-positive.
    """
    if max_bytes < 1 or max_lines < 1:
        raise ValueError("output budgets must be positive")
    prefix = "".join(text.splitlines(keepends=True)[:max_lines])
    bounded = prefix.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return ToolResult(bounded, truncated=bounded != text)


# ANCHOR_END: budget


class FixtureTools:
    """Execute only the teaching fixture's operations; this is not an OS sandbox."""

    def __init__(self, root: Path, *, approve_edits: bool = True) -> None:
        self.root = root
        self.approve_edits = approve_edits

    # ANCHOR: dispatch
    def execute(self, call: ToolCall) -> str:
        """Validate and execute a fixture call, bounding successes and expected errors.

        Args:
            call (ToolCall): Name and decoded arguments from the provider.

        Returns:
            str: Bounded text plus a separate truncation-status header.
        """
        try:
            spec = next((tool for tool in TOOLS if tool.name == call.name), None)
            if spec is None:
                raise ToolFailure(f"unknown tool: {call.name}")
            if set(call.arguments) != set(spec.parameters):
                raise ToolFailure(f"expected arguments: {', '.join(spec.parameters) or '(none)'}")
            if not all(isinstance(value, str) for value in call.arguments.values()):
                raise ToolFailure("all arguments must be strings")
            args = {key: value for key, value in call.arguments.items() if isinstance(value, str)}
            text = self._run(call.name, args)
        except (ToolFailure, OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
            text = f"error: {exc}"
        result = bound_output(text)
        return f"truncated={str(result.truncated).lower()}\n{result.text}"

    # ANCHOR_END: dispatch

    # ANCHOR: operations
    def _run(self, name: str, args: dict[str, str]) -> str:
        if name == "test":
            # Fixed command over a tiny trusted fixture, not a general shell tool.
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "test_calculator.py"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return f"exit_code={completed.returncode}\n{completed.stdout}{completed.stderr}"
        if args["path"] != "calculator.py":
            raise ToolFailure("only calculator.py is exposed")
        path = self.root / "calculator.py"
        if path.is_symlink():
            raise ToolFailure("fixture file must not be a symlink")
        if name == "read":
            return path.read_text(encoding="utf-8")
        if not self.approve_edits:
            raise ToolFailure("edit denied by the host")
        original = path.read_text(encoding="utf-8")
        if not args["old"] or original.count(args["old"]) != 1:
            raise ToolFailure("old text must match exactly once; reread calculator.py")
        path.write_text(original.replace(args["old"], args["new"], 1), encoding="utf-8")
        return "edited calculator.py"

    # ANCHOR_END: operations


def make_provider(*, approve_edits: bool = True) -> ScriptedProvider:
    """Return a known repair exchange, or a known denial exchange."""
    steps = [
        ScriptStep(Message("assistant", "Reproduce the bug.", (ToolCall("1", "test", {}),))),
        ScriptStep(
            Message(
                "assistant",
                "Read the implementation.",
                (ToolCall("2", "read", {"path": "calculator.py"}),),
            ),
            after="FAILED",
        ),
        ScriptStep(
            Message(
                "assistant",
                "Replace subtraction with addition.",
                (
                    ToolCall(
                        "3",
                        "edit",
                        {"path": "calculator.py", "old": "return a - b", "new": "return a + b"},
                    ),
                ),
            ),
            after="return a - b",
        ),
    ]
    if approve_edits:
        steps.extend(
            (
                ScriptStep(
                    Message("assistant", "Check the change.", (ToolCall("4", "test", {}),)),
                    after="edited calculator.py",
                ),
                ScriptStep(
                    Message("assistant", "The two fixture tests pass after the edit."), after="\nOK"
                ),
            )
        )
    else:
        steps.append(
            ScriptStep(Message("assistant", "Edit denied; the bug remains."), after="edit denied")
        )
    return ScriptedProvider(steps)


def create_fixture(root: Path) -> None:
    """Create the known fixture in a caller-owned empty directory.

    Args:
        root (Path): Existing empty directory for the disposable example.

    Raises:
        FileExistsError: A fixture file already exists; existing files are not overwritten.
    """
    for name, text in (("calculator.py", BUGGY_SOURCE), ("test_calculator.py", TEST_SOURCE)):
        with (root / name).open("x", encoding="utf-8") as file:
            file.write(text)


async def main(*, approve_edits: bool = True) -> None:
    with TemporaryDirectory(prefix="crafting-agent-") as directory:
        root = Path(directory)
        create_fixture(root)
        tools = FixtureTools(root, approve_edits=approve_edits)
        result = await run_agent(
            "Fix add(2, 3), which should return 5.",
            make_provider(approve_edits=approve_edits),
            TOOLS,
            tools.execute,
        )
        print(f"stopped: {result.stop_reason}")
        print(f"final calculator.py:\n{(root / 'calculator.py').read_text(encoding='utf-8')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deny-edits", action="store_true")
    asyncio.run(main(approve_edits=not parser.parse_args().deny_edits))
