"""A narrow Responses API boundary shared by offline replays and the live checkpoint."""

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast

from examples.crafting_agents.core import Message, ToolCall, ToolSpec

type Payload = dict[str, object]


class ResponseFailure(Exception):
    """Stop the run without handing an unaccepted response to the tool executor."""


class OpenFailure(ResponseFailure):
    """Classify a failure before an upstream stream has been acquired."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EventStream(Protocol):
    def __aiter__(self) -> AsyncIterator[Payload]: ...

    async def aclose(self) -> None: ...


# ANCHOR: request
def build_request(history: Sequence[Message], tools: Sequence[ToolSpec], model: str) -> Payload:
    """Translate portable text and call/result history into a stateless Responses request.

    Args:
        history (Sequence[Message]): Instructions and complete conversation exchanges.
        tools (Sequence[ToolSpec]): Exposed functions with required string arguments.
        model (str): A text/function-call model, such as gpt-4.1-mini.

    Returns:
        Payload: Provider-native input and strict function schemas, with bounded output.

    Raises:
        ValueError: A tool result has no correlation ID.
    """
    items: list[Payload] = []
    for message in history:
        if message.role == "tool":
            if not message.tool_call_id:
                raise ValueError("tool result needs a call ID")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
        else:
            if message.content:
                items.append({"role": message.role, "content": message.content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    }
                )
    functions: list[Payload] = [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {name: {"type": "string"} for name in tool.parameters},
                "required": list(tool.parameters),
                "additionalProperties": False,
            },
        }
        for tool in tools
    ]
    return {
        "model": model,
        "input": items,
        "tools": functions,
        "stream": True,
        "store": False,
        "parallel_tool_calls": False,
        "max_output_tokens": 2_048,
    }


# ANCHOR_END: request


def _object(value: object) -> Payload:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ResponseFailure("expected a JSON object")
    return cast(Payload, value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ResponseFailure("expected a JSON array")
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ResponseFailure("expected a string")
    return value


@dataclass
class PendingCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class ResponseAssembly:
    """Accumulate previews while accepting calls only from a complete terminal response."""

    tools: Sequence[ToolSpec]
    report: Callable[[str], None]
    response_id: str | None = None
    pending: dict[str, PendingCall] = field(default_factory=dict)
    preview_chars: int = 0

    # ANCHOR: events
    def accept(self, event: Payload) -> Message | None:
        """Accept one native event, returning a decision only at successful completion.

        Args:
            event (Payload): Decoded Responses API event.

        Returns:
            Message | None: Validated assistant decision, or None for progress.

        Raises:
            ResponseFailure: Lifecycle, bounds, or completed tool arguments are invalid.
        """
        kind = _text(event.get("type"))
        if kind == "response.created":
            if self.response_id is not None:
                raise ResponseFailure("duplicate response.created")
            self.response_id = _text(_object(event.get("response")).get("id"))
            if not self.response_id:
                raise ResponseFailure("response.created needs an ID")
            return None
        if self.response_id is None:
            raise ResponseFailure("response data arrived before response.created")
        if event.get("response_id", self.response_id) != self.response_id:
            raise ResponseFailure("event response identity mismatch")
        if kind in {"response.failed", "response.incomplete", "error"}:
            raise ResponseFailure(f"provider reported {kind}; buffered calls discarded")
        if kind in {"response.output_text.delta", "response.refusal.delta"}:
            delta = _text(event.get("delta"))
            self.preview_chars += len(delta)
            if self.preview_chars > 64_000:
                raise ResponseFailure("text preview limit exceeded")
            self.report(f"text delta: {delta}")
        elif kind == "response.output_item.added":
            item = _object(event.get("item"))
            if item.get("type") == "function_call":
                item_id = _text(item.get("id"))
                if item_id in self.pending or len(self.pending) >= 8:
                    raise ResponseFailure("duplicate tool item or tool-count limit exceeded")
                self.pending[item_id] = PendingCall(
                    _text(item.get("call_id")),
                    _text(item.get("name")),
                    _text(item.get("arguments")),
                )
                if len(self.pending[item_id].arguments) > 16_000:
                    raise ResponseFailure("tool argument limit exceeded")
        elif kind == "response.function_call_arguments.delta":
            item_id = _text(event.get("item_id"))
            if item_id not in self.pending:
                raise ResponseFailure("argument delta has no tool item")
            pending = self.pending[item_id]
            pending.arguments += _text(event.get("delta"))
            if len(pending.arguments) > 16_000:
                raise ResponseFailure("tool argument limit exceeded")
            self.report(f"arguments buffered: {item_id} ({len(pending.arguments)} chars)")
        elif kind == "response.completed":
            return self._completed(_object(event.get("response")))
        # Per-item *.done events are progress, not response-level authorization.
        return None

    # ANCHOR_END: events

    def _completed(self, response: Payload) -> Message:
        if response.get("id") != self.response_id or response.get("status") != "completed":
            raise ResponseFailure("terminal response identity or status mismatch")
        text: list[str] = []
        calls: list[ToolCall] = []
        seen_items: set[str] = set()
        seen_calls: set[str] = set()
        specs = {tool.name: tool for tool in self.tools}
        for raw_item in _array(response.get("output")):
            item = _object(raw_item)
            if item.get("type") == "message":
                if item.get("role") != "assistant":
                    raise ResponseFailure("terminal message is not from the assistant")
                for raw_part in _array(item.get("content")):
                    part = _object(raw_part)
                    if part.get("type") not in {"output_text", "refusal"}:
                        raise ResponseFailure("unsupported message content")
                    text.append(
                        _text(part.get("text" if part["type"] == "output_text" else "refusal"))
                    )
            elif item.get("type") == "function_call":
                item_id = _text(item.get("id"))
                pending = self.pending.get(item_id)
                call_id, name, raw = (
                    _text(item.get(key)) for key in ("call_id", "name", "arguments")
                )
                if not call_id or item_id in seen_items or call_id in seen_calls:
                    raise ResponseFailure("empty or duplicate tool identity")
                if pending != PendingCall(call_id, name, raw):
                    raise ResponseFailure("terminal call differs from buffered call")
                seen_items.add(item_id)
                seen_calls.add(call_id)
                if name not in specs:
                    raise ResponseFailure("response requested an unexposed tool")
                try:
                    arguments = _object(json.loads(raw))
                except (json.JSONDecodeError, RecursionError) as exc:
                    raise ResponseFailure("tool arguments are not complete JSON") from exc
                if set(arguments) != set(specs[name].parameters) or not all(
                    isinstance(value, str) for value in arguments.values()
                ):
                    raise ResponseFailure("tool arguments do not match the exposed schema")
                calls.append(ToolCall(call_id, name, arguments))
            else:
                # Reasoning items require native replay; do not silently discard them.
                raise ResponseFailure(
                    "unsupported output item; adapter supports only text and function calls"
                )
        if seen_items != set(self.pending):
            raise ResponseFailure("terminal response omitted a buffered call")
        if sum(map(len, text)) > 64_000:
            raise ResponseFailure("terminal text limit exceeded")
        return Message("assistant", "".join(text), tuple(calls))


class ResponsesProvider:
    """Keep the teaching loop's complete-response interface around a streaming adapter."""

    def __init__(
        self,
        open_stream: Callable[[Payload], Awaitable[EventStream]],
        *,
        model: str = "gpt-4.1-mini",
        report: Callable[[str], None] = print,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.open_stream = open_stream
        self.model = model
        self.report = report
        self.sleep = sleep

    # ANCHOR: boundary
    async def complete(self, history: Sequence[Message], tools: Sequence[ToolSpec]) -> Message:
        """Retry only opening, then consume and close one stream before releasing calls.

        Args:
            history (Sequence[Message]): Complete portable history for this turn.
            tools (Sequence[ToolSpec]): Exposed functions to serialize and validate.

        Returns:
            Message: A complete, validated response after stream cleanup succeeds.

        Raises:
            ResponseFailure: Opening, streaming, completion, or validation fails.
            Exception: Unexpected transport, observer, or cleanup failures propagate.
        """
        request = build_request(history, tools, self.model)
        for attempt in range(3):
            try:
                stream = await self.open_stream(request)
                break
            except OpenFailure as exc:
                if not exc.retryable or attempt == 2:
                    raise
                delay = (0.25 * 2**attempt) * random.uniform(0.9, 1.1)
                self.report(f"retry opening: attempt {attempt + 2}/3")
                await self.sleep(delay)
        assembly = ResponseAssembly(tools, self.report)
        try:
            async for event in stream:
                decision = assembly.accept(event)
                if decision is not None:
                    return decision
            raise ResponseFailure("stream ended without response.completed")
        finally:
            await stream.aclose()

    # ANCHOR_END: boundary
