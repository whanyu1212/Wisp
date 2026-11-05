"""Call Wisp's built-in tools on a small temporary project and print every result.

Unlike the checkpoints, this tour imports Wisp itself, so run it from a development
checkout: ``uv run python -m examples.crafting_agents.builtin_tools_tour``.

The calls go straight to each tool's ``run`` method with a ``ToolContext``, the same
way Wisp's executor calls them after policy and approval checks. No model, provider,
or approval prompt is involved, so the tour shows exactly what each tool returns.
"""

import argparse
import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from wisp.tools.base import Tool, ToolArguments
from wisp.tools.builtin import builtin_tools
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult
from wisp.tools.seen_files import SeenFiles
from wisp.tools.shell.supervisor import ProcessSupervisor

# ANCHOR: project
PROJECT_FILES = {
    "README.md": """# Greeter

A tiny project for touring Wisp's built-in tools.

Run the tests from the project root:

    ./run-tests.sh
""",
    # Prints only unittest's verdict line, whose text does not vary between runs,
    # and exits with unittest's status.
    "run-tests.sh": """#!/bin/sh
output=$(python3 -m unittest discover -s tests -q 2>&1)
status=$?
printf '%s\\n' "$output" | tail -n 1
exit "$status"
""",
    "src/greeting.py": """def greet(name):
    # TODO: ignore surrounding whitespace
    return f"Hello, {name}!"


def farewell(name):
    return f"Goodbye, {name}!"
""",
    "src/names.py": """DEFAULT_NAME = "world"


def normalize(name):
    return name.strip().title()
""",
    "tests/test_greeting.py": """import sys
import unittest

sys.path.insert(0, "src")
from greeting import greet


class GreetTests(unittest.TestCase):
    def test_ignores_surrounding_whitespace(self):
        self.assertEqual(greet("  Ada "), "Hello, Ada!")
""",
    # A generated copy that .gitignore excludes from grep and find.
    "build/greeting.py": "# TODO: generated copy, do not edit\n",
    ".gitignore": "build/\n",
    # A secret that the default protected paths hide from every file tool.
    ".env": "API_TOKEN=example-secret\n",
}
# ANCHOR_END: project

TEST_COMMAND = "./run-tests.sh"
PROCESS_ID_PLACEHOLDER = "<process_id>"


@dataclass(frozen=True)
class TourStep:
    """One tool call and the text the model would receive for it.

    Attributes:
        anchor (str): Name the documentation uses to include this step.
        call (str): The tool name and its JSON arguments.
        output (str): The result text, or the error the tool raised.
    """

    anchor: str
    call: str
    output: str


def create_project(root: Path) -> None:
    """Write the tour's files under ``root``.

    Args:
        root (Path): An empty directory.
    """

    for relative_path, content in PROJECT_FILES.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "run-tests.sh").chmod(0o755)
    # An empty .git directory is enough for the search tools to treat the folder as a
    # repository, which is when they apply .gitignore.
    (root / ".git").mkdir()


def format_error(error: ToolError) -> str:
    """Render a tool error as the tour prints it."""

    code = f" ({error.failure_code})" if error.failure_code else ""
    hint = f"\nhint: {error.recovery_hint}" if error.recovery_hint else ""
    return f"error{code}: {error}{hint}"


class Tour:
    """Run tool calls in order and record what each one returned."""

    def __init__(self, tools: Mapping[str, Tool], context: ToolContext) -> None:
        self._tools = tools
        self._context = context
        self.steps: list[TourStep] = []

    async def call(self, anchor: str, name: str, arguments: ToolArguments) -> ToolResult:
        """Run one tool call; a ``ToolError`` is recorded as its output.

        Args:
            anchor (str): Documentation anchor for the step.
            name (str): Built-in tool name.
            arguments (ToolArguments): Arguments as a model would send them.

        Returns:
            ToolResult: The result, or an empty one when the tool raised ``ToolError``.
        """

        try:
            result = await self._tools[name].run(arguments, self._context)
        except ToolError as error:
            result = ToolResult(text="")
            output = format_error(error)
        else:
            output = result.text
        self.steps.append(TourStep(anchor, f"{name} {json.dumps(dict(arguments))}", output))
        return result


# ANCHOR: tour
async def run_tour(project: Path) -> list[TourStep]:
    """Explore, break, repair, and extend the project with the built-in tools.

    Args:
        project (Path): A resolved directory prepared by ``create_project``.

    Returns:
        list[TourStep]: Every call in order. Random process IDs are replaced with a
        placeholder so the transcript is the same on every run.
    """

    supervisor = ProcessSupervisor()
    tools = {tool.name: tool for tool in builtin_tools(process_supervisor=supervisor)}
    # SeenFiles turns on the read-before-write check, as in a coding session.
    tour = Tour(tools, ToolContext(cwd=project, seen_files=SeenFiles()))
    try:
        # Explore: list, find, and search without reading whole files.
        await tour.call("ls", "ls", {})
        await tour.call("ls-all", "ls", {"all": True})
        await tour.call("find", "find", {"pattern": "*.py"})
        await tour.call("grep", "grep", {"pattern": "TODO"})
        await tour.call(
            "grep-context", "grep", {"pattern": r"def \w+", "path": "src", "context": 1}
        )
        await tour.call("read-page", "read", {"path": "README.md", "limit": 3})
        await tour.call("read-protected", "read", {"path": ".env"})

        # Reproduce the bug, then repair it.
        await tour.call("bash-fail", "bash", {"command": TEST_COMMAND})
        new_return = '    return f"Hello, {name.strip()}!"\n'
        repair = {
            "oldText": '    # TODO: ignore surrounding whitespace\n    return f"Hello, {name}!"\n',
            "newText": new_return,
        }
        await tour.call("edit-unread", "edit", {"path": "src/greeting.py", "edits": [repair]})
        await tour.call("read", "read", {"path": "src/greeting.py"})
        ambiguous = {"oldText": 'return f"', "newText": 'return f"> '}
        await tour.call("edit-ambiguous", "edit", {"path": "src/greeting.py", "edits": [ambiguous]})
        await tour.call("edit", "edit", {"path": "src/greeting.py", "edits": [repair]})
        await tour.call("bash-pass", "bash", {"command": TEST_COMMAND})

        # Create a file, then refuse to overwrite it.
        changelog = {"path": "docs/CHANGELOG.md", "content": "- greet() ignores whitespace\n"}
        await tour.call("write", "write", changelog)
        await tour.call("write-exists", "write", {**changelog, "overwrite": False})

        # Start a long-running process, read its first output, and stop it.
        started = await tour.call(
            "bash-start",
            "bash",
            {"operation": "start", "command": "echo 'server ready'; sleep 30", "yield_seconds": 5},
        )
        process_id = str(started.data["process_id"])
        await tour.call("bash-cancel", "bash", {"operation": "cancel", "process_id": process_id})
    finally:
        await supervisor.aclose()
    return [
        replace(
            step,
            call=step.call.replace(process_id, PROCESS_ID_PLACEHOLDER),
            output=step.output.replace(process_id, PROCESS_ID_PLACEHOLDER),
        )
        for step in tour.steps
    ]


# ANCHOR_END: tour


def format_transcript(steps: list[TourStep], *, anchors: bool = False) -> str:
    """Render the steps as ``> call`` lines followed by their output.

    Args:
        steps (list[TourStep]): Steps from ``run_tour``.
        anchors (bool): Whether to add the ANCHOR lines that documentation includes
            select steps by, as in the checked-in transcript.

    Returns:
        str: The transcript, ending in a newline.
    """

    blocks: list[str] = []
    if anchors:
        blocks.append(
            "# Output of: uv run python -m examples.crafting_agents.builtin_tools_tour "
            "--transcript\n# tests/docs/crafting/test_builtin_tools_tour.py keeps it current."
        )
    for step in steps:
        block = f"> {step.call}\n{step.output}".rstrip("\n")
        if anchors:
            block = f"# ANCHOR: {step.anchor}\n{block}\n# ANCHOR_END: {step.anchor}"
        blocks.append(block)
    return "\n\n".join(blocks) + "\n"


def run_in_temporary_project() -> list[TourStep]:
    """Create the project in a temporary directory, run the tour, and clean up."""

    with TemporaryDirectory(prefix="wisp-tour-") as directory:
        project = Path(directory).resolve()
        create_project(project)
        return asyncio.run(run_tour(project))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--transcript",
        action="store_true",
        help="print the anchored transcript that the documentation includes",
    )
    arguments = parser.parse_args()
    print(format_transcript(run_in_temporary_project(), anchors=arguments.transcript), end="")


if __name__ == "__main__":
    main()
