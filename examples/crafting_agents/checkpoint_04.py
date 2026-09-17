"""Replay provider failures offline, or opt into a live Responses API request."""

import argparse
import asyncio
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

from examples.crafting_agents.checkpoint_03 import (
    PROJECT_GUIDANCE,
    TASK,
    TOOLS,
    ContextTools,
    create_context_fixture,
)
from examples.crafting_agents.context import build_instructions
from examples.crafting_agents.core import Provider, RunResult, ToolCall, ToolSpec, run_agent
from examples.crafting_agents.responses import ResponseFailure, ResponsesProvider
from examples.crafting_agents.stream_replay import SCENARIOS, no_wait, scenario_transport


class LiveFixtureTools:
    """Enforce live execution opt-in independently of the provider's tool catalog."""

    def __init__(self, root: Path, *, allow_execution: bool) -> None:
        self.tools = ContextTools(root, approve_edits=allow_execution)
        self.allow_execution = allow_execution

    # ANCHOR: permission
    def execute(self, call: ToolCall) -> str:
        if call.name in {"edit", "test"} and not self.allow_execution:
            return "error: live edits and test execution require --allow-execution"
        return self.tools.execute(call)

    # ANCHOR_END: permission


async def run_checkpoint(
    root: Path,
    provider: Provider,
    tools: Sequence[ToolSpec],
    execute: Callable[[ToolCall], str],
    *,
    read_only: bool = False,
    report: Callable[[str], None] = print,
) -> RunResult:
    """Run one already-created fixture with bounded turns and prepared context.

    Args:
        root (Path): Disposable directory created by create_context_fixture.
        provider (Provider): Streaming adapter with the complete-response contract.
        tools (Sequence[ToolSpec]): Exposed tool catalog.
        execute (Callable[[ToolCall], str]): Host-owned executor.
        read_only (bool): Ask for diagnosis when live execution is not authorized.
        report (Callable[[str], None]): Trace observer.

    Returns:
        RunResult: Completed conversation and a distinct stop reason.

    Raises:
        ResponseFailure: A response failed before tool dispatch.
    """
    prompt = (
        "Inspect the addition bug and explain a fix. Editing and test execution are unavailable."
        if read_only
        else TASK
    )
    return await run_agent(
        prompt,
        provider,
        tools,
        execute,
        instructions=build_instructions(root, tools, trusted=True),
        max_turns=10,
        report=report,
    )


async def main(*, scenario: str, live: bool, model: str, allow_execution: bool) -> int:
    with TemporaryDirectory(prefix="crafting-provider-") as directory:
        root = Path(directory)
        create_context_fixture(root)
        # Chapter 3's deliberate permission-injection exercise is not this chapter's task.
        (root / "AGENTS.md").write_text(PROJECT_GUIDANCE, encoding="utf-8")
        if live:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                print("OPENAI_API_KEY is required for --live")
                return 2
            # Offline commands do not import the optional SDK or read credentials.
            from examples.crafting_agents.openai_transport import OpenAITransport

            transport = OpenAITransport(key)
            tools = TOOLS if allow_execution else TOOLS[:2]
            executor = LiveFixtureTools(root, allow_execution=allow_execution)
            try:
                result = await run_checkpoint(
                    root,
                    ResponsesProvider(transport.open, model=model),
                    tools,
                    executor.execute,
                    read_only=not allow_execution,
                )
            except ResponseFailure as exc:
                print(f"stopped: provider_failure ({exc})")
                print(
                    f"final calculator.py:\n{(root / 'calculator.py').read_text(encoding='utf-8')}"
                )
                return 1
            finally:
                await transport.aclose()
        else:
            replay = scenario_transport(scenario)
            fixture = ContextTools(root, approve_edits=True)
            executed: list[str] = []

            def execute(call: ToolCall) -> str:
                executed.append(call.name)
                return fixture.execute(call)

            try:
                result = await run_checkpoint(
                    root,
                    ResponsesProvider(replay.open, sleep=no_wait),
                    TOOLS,
                    execute,
                )
            except ResponseFailure as exc:
                print(f"stopped: provider_failure ({exc})")
                print(f"executed tools: {len(executed)}")
                print(
                    f"final calculator.py:\n{(root / 'calculator.py').read_text(encoding='utf-8')}"
                )
                if scenario not in {"disconnect", "malformed", "output-limit"} or executed:
                    raise
                # Expected failure demonstrations count as successful checkpoint runs.
                return 0
            if scenario in {"disconnect", "malformed", "output-limit"}:
                raise AssertionError("failure scenario unexpectedly accepted a response")
        print(f"stopped: {result.stop_reason}")
        print(f"final calculator.py:\n{(root / 'calculator.py').read_text(encoding='utf-8')}")
        return 0 if result.stop_reason == "model_finished" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="repair")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--allow-execution", action="store_true")
    args = parser.parse_args()
    if args.allow_execution and not args.live:
        parser.error("--allow-execution applies only to --live")
    if args.live and args.scenario != "repair":
        parser.error("--scenario failure demonstrations are offline only")
    raise SystemExit(
        asyncio.run(
            main(
                scenario=args.scenario,
                live=args.live,
                model=args.model,
                allow_execution=args.allow_execution,
            )
        )
    )
