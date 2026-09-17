"""Authored native-shaped event fixtures; no SDK, credentials, or network required."""

import json
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from dataclasses import dataclass

from examples.crafting_agents.checkpoint_03 import make_provider
from examples.crafting_agents.core import Message, ToolCall
from examples.crafting_agents.responses import EventStream, OpenFailure, Payload

SCENARIOS = ("repair", "retry", "disconnect", "malformed", "output-limit")


def response_events(message: Message, response_id: str = "response-demo") -> tuple[Payload, ...]:
    """Encode an authored assistant decision as Responses-style streaming events.

    Args:
        message (Message): Complete text and calls whose wire arguments will be fragmented.
        response_id (str): Fixture response identity.

    Returns:
        tuple[Payload, ...]: Native-shaped events ending with response.completed.
    """
    events: list[Payload] = [{"type": "response.created", "response": {"id": response_id}}]
    output: list[Payload] = []
    if message.content:
        midpoint = len(message.content) // 2
        for delta in (message.content[:midpoint], message.content[midpoint:]):
            events.append({"type": "response.output_text.delta", "delta": delta})
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": message.content}],
            }
        )
    for call in message.tool_calls:
        item_id = f"item-{call.id}"
        arguments = json.dumps(call.arguments)
        item: Payload = {
            "type": "function_call",
            "id": item_id,
            "call_id": call.id,
            "name": call.name,
            "arguments": "",
        }
        events.append({"type": "response.output_item.added", "item": item})
        midpoint = len(arguments) // 2
        for delta in (arguments[:midpoint], arguments[midpoint:]):
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item_id,
                    "delta": delta,
                }
            )
        final_item = {**item, "arguments": arguments}
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "item_id": item_id,
                "arguments": arguments,
            }
        )
        events.append({"type": "response.output_item.done", "item": final_item})
        output.append(final_item)
    events.append(
        {
            "type": "response.completed",
            "response": {"id": response_id, "status": "completed", "output": output},
        }
    )
    return tuple(events)


@dataclass(frozen=True)
class ReplayTurn:
    events: tuple[Payload, ...]
    after: str | None = None


class ReplayStream:
    def __init__(self, events: Sequence[Payload]) -> None:
        self.events = iter(deepcopy(events))
        self.closed = False

    def __aiter__(self) -> AsyncIterator[Payload]:
        return self

    async def __anext__(self) -> Payload:
        if self.closed:
            raise StopAsyncIteration
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class ReplayTransport:
    """Open deterministic turns and check the prior wire-level tool observation."""

    def __init__(self, turns: Sequence[ReplayTurn], *, opening_failures: int = 0) -> None:
        self.turns = iter(turns)
        self.opening_failures = opening_failures
        self.attempts = 0
        self.streams: list[ReplayStream] = []
        self.requests: list[Payload] = []

    async def open(self, request: Payload) -> EventStream:
        self.attempts += 1
        self.requests.append(deepcopy(request))
        if self.opening_failures:
            self.opening_failures -= 1
            raise OpenFailure("simulated connection failure", retryable=True)
        try:
            turn = next(self.turns)
        except StopIteration as exc:
            raise AssertionError("replay has no remaining response") from exc
        if turn.after is not None:
            items = request["input"]
            if not isinstance(items, list) or not items or not isinstance(items[-1], dict):
                raise AssertionError("replay expected a tool result")
            last = items[-1]
            if last.get("type") != "function_call_output" or turn.after not in str(
                last.get("output", "")
            ):
                raise AssertionError(f"replay expected observation containing {turn.after!r}")
        stream = ReplayStream(turn.events)
        self.streams.append(stream)
        return stream


def scenario_transport(scenario: str) -> ReplayTransport:
    """Build a successful repair or one deliberately unexecutable response.

    Args:
        scenario (str): A name from SCENARIOS.

    Returns:
        ReplayTransport: Authored wire events using the same parser as live traffic.

    Raises:
        ValueError: The scenario is unknown.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    if scenario in {"repair", "retry"}:
        turns = tuple(
            ReplayTurn(response_events(step.response, f"response-{index}"), step.after)
            for index, step in enumerate(make_provider(approve_edits=True).steps)
        )
        return ReplayTransport(turns, opening_failures=int(scenario == "retry"))
    call = ToolCall(
        "edit-1", "edit", {"path": "calculator.py", "old": "return a - b", "new": "return a + b"}
    )
    events = list(response_events(Message("assistant", "Preparing an edit.", (call,))))
    if scenario == "disconnect":
        # All argument/item done events arrived, but response.completed did not.
        events.pop()
    elif scenario == "output-limit":
        events[-1] = {
            "type": "response.incomplete",
            "response": {
                "id": "response-demo",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        }
    else:
        # Keep streamed and terminal bytes consistent, but make the JSON invalid.
        for event in events:
            if event["type"] == "response.function_call_arguments.delta":
                event["delta"] = "{"
            elif event["type"] == "response.function_call_arguments.done":
                event["arguments"] = "{{"
        response = events[-1]["response"]
        assert isinstance(response, dict)
        output = response["output"]
        assert isinstance(output, list)
        output[-1]["arguments"] = "{{"
    return ReplayTransport((ReplayTurn(tuple(events)),))


async def no_wait(seconds: float) -> None:
    """Skip backoff wall time in an offline demonstration."""
    del seconds
