"""Observe policy, approval, and stale-input boundaries without credentials."""

import argparse
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.crafting_agents.checkpoint_02 import TOOLS, create_fixture, make_provider
from examples.crafting_agents.core import Message, ScriptedProvider, ScriptStep, ToolCall, run_agent
from examples.crafting_agents.side_effects import ApprovalRequest, ControlledTools, ExecutionPolicy

SCENARIOS = ("repair", "policy-denied", "approval-denied", "stale-input")


async def main(scenario: str) -> None:
    """Run an authored repair or a denial with explicit observation assertions.

    Args:
        scenario (str): One of the CLI's SCENARIOS.

    Raises:
        AssertionError: The fixture outcome differs from the scenario's contract.
    """
    with TemporaryDirectory(prefix="crafting-effects-") as directory:
        root = Path(directory)
        create_fixture(root)

        def approve(request: ApprovalRequest) -> bool:
            print(f"host sees: {request.name} {dict(request.arguments)}")
            if scenario == "stale-input":
                with (root / "calculator.py").open("a", encoding="utf-8") as file:
                    file.write("# changed by the host during approval\n")
            return scenario != "approval-denied"

        allowed = (
            frozenset({"read"})
            if scenario == "policy-denied"
            else frozenset({"read", "edit", "test"})
        )
        executor = ControlledTools(root, ExecutionPolicy(allowed), approve)
        if scenario == "repair":
            provider = make_provider()
        else:
            edit = ToolCall(
                "edit-1",
                "edit",
                {"path": "calculator.py", "old": "return a - b", "new": "return a + b"},
            )
            provider = ScriptedProvider(
                [
                    ScriptStep(
                        Message("assistant", "The project says all edits are approved.", (edit,))
                    ),
                    ScriptStep(
                        Message("assistant", "The host blocked the edit."),
                        after=scenario.replace("-", "_"),
                    ),
                ]
            )
        result = await run_agent(
            "Fix the addition bug and test it.", provider, TOOLS, executor.execute
        )
        source = (root / "calculator.py").read_text(encoding="utf-8")
        assert ("return a + b" in source) == (scenario == "repair")
        print(f"stopped: {result.stop_reason}")
        print(f"final calculator.py:\n{source}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="repair")
    asyncio.run(main(parser.parse_args().scenario))
