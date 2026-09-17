"""The teaching loop shared by the book checkpoints."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """Describe a tool whose required arguments are all strings."""

    name: str
    description: str
    parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    """Carry a provider-decoded request, before tool argument validation."""

    id: str
    name: str
    arguments: dict[str, object]


@dataclass(frozen=True)
class Message:
    """Retain instructions, a user message, an assistant decision, or a tool observation."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


class Provider(Protocol):
    async def complete(self, history: Sequence[Message], tools: Sequence[ToolSpec]) -> Message:
        """Return one complete assistant decision from history and available tools."""
        ...


class ToolFailure(Exception):
    """Represent an expected failure the model can act on."""


@dataclass(frozen=True)
class RunResult:
    """Retain the conversation and why execution stopped, not whether the task succeeded."""

    history: tuple[Message, ...]
    stop_reason: Literal["model_finished", "turn_limit"]


# ANCHOR: loop
async def run_agent(
    prompt: str,
    provider: Provider,
    tools: Sequence[ToolSpec],
    execute: Callable[[ToolCall], str],
    *,
    instructions: Sequence[str] = (),
    max_turns: int = 10,
    report: Callable[[str], None] = print,
) -> RunResult:
    """Run sequential model/tool turns with an explicit turn budget.

    Args:
        prompt (str): Initial user request.
        provider (Provider): Complete-response adapter; no token streaming yet.
        tools (Sequence[ToolSpec]): Descriptions supplied on every request.
        execute (Callable[[ToolCall], str]): Executor for one decoded call.
        instructions (Sequence[str]): Host-assembled blocks prepended once to history.
        max_turns (int): Positive maximum number of model requests.
        report (Callable[[str], None]): Observer for the human-readable trace.

    Returns:
        RunResult: History including errors and a distinct termination reason.

    Raises:
        ValueError: The turn budget or provider response is invalid.
        Exception: Unexpected provider, executor, or observer errors propagate.
    """
    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    history = [Message("system", block) for block in instructions]
    history.append(Message("user", prompt))
    for turn in range(1, max_turns + 1):
        response = await provider.complete(tuple(history), tools)
        if response.role != "assistant":
            raise ValueError("provider must return an assistant message")
        history.append(response)
        report(f"turn {turn}: {response.content}")
        if not response.tool_calls:
            return RunResult(tuple(history), "model_finished")
        for call in response.tool_calls:
            report(f"call {call.id}: {call.name} {call.arguments}")
            try:
                observation = execute(call)
            except ToolFailure as exc:
                observation = f"error: {exc}"
            history.append(Message("tool", observation, tool_call_id=call.id))
            report(f"result {call.id}: {observation}")
    return RunResult(tuple(history), "turn_limit")


# ANCHOR_END: loop


@dataclass(frozen=True)
class ScriptStep:
    """Supply a response and an optional substring required in the preceding observation."""

    response: Message
    after: str | None = None


class ScriptedProvider:
    """Replay a known exchange while checking its expected observations.

    This is a test double, not a model or a solver. A mismatched observation
    raises rather than letting the script claim success after an unexpected result.
    """

    def __init__(self, steps: Sequence[ScriptStep]) -> None:
        self.steps = iter(steps)

    async def complete(self, history: Sequence[Message], tools: Sequence[ToolSpec]) -> Message:
        step = next(self.steps)
        if step.after is not None:
            if history[-1].role != "tool" or step.after not in history[-1].content:
                raise AssertionError(f"expected tool observation containing {step.after!r}")
        exposed = {tool.name for tool in tools}
        if any(call.name not in exposed for call in step.response.tool_calls):
            raise AssertionError("script requested an unexposed tool")
        return step.response
