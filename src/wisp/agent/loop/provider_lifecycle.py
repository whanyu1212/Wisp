"""Validate one provider response's lifecycle and assemble its terminal result."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from wisp.providers.base import ProviderProtocolError
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ToolCall,
)


def require_provider_response_started(started: bool) -> None:
    """Require a start event before accepting response data.

    Args:
        started (bool): Whether a response start has been observed.

    Raises:
        ProviderProtocolError: No response start has been observed.
    """
    if not started:
        raise ProviderProtocolError("Provider emitted response data before response_started")


def resolve_provider_response_id(
    *,
    started_response_id: str | None,
    terminal_response_id: str | None,
    tool_calls: Sequence[ToolCall],
) -> str | None:
    """Resolve one consistent response ID across start, terminal, and tool events.

    Args:
        started_response_id (str | None): ID reported when the response opened.
        terminal_response_id (str | None): ID reported by the terminal response.
        tool_calls (Sequence[ToolCall]): Calls whose response IDs must agree with the stream.

    Returns:
        str | None: First supplied ID when all supplied IDs agree; None when none exists.

    Examples:
        >>> resolve_provider_response_id(
        ...     started_response_id="r1", terminal_response_id=None, tool_calls=()
        ... )
        'r1'
        >>> resolve_provider_response_id(
        ...     started_response_id=None, terminal_response_id=None, tool_calls=()
        ... ) is None
        True

    Raises:
        ProviderProtocolError: Two supplied IDs disagree. No synthetic ID is invented.
    """

    candidates = [
        ("response_started", started_response_id),
        ("terminal response", terminal_response_id),
        *((f"tool call {tool_call.call_id}", tool_call.response_id) for tool_call in tool_calls),
    ]
    supplied = [
        (source, response_id) for source, response_id in candidates if response_id is not None
    ]
    if not supplied:
        return None

    resolved_source, resolved_id = supplied[0]
    for source, response_id in supplied[1:]:
        if response_id != resolved_id:
            raise ProviderProtocolError(
                "Provider emitted conflicting response ids: "
                f"{resolved_source}={resolved_id!r}, {source}={response_id!r}"
            )
    return resolved_id


@dataclass(frozen=True, slots=True)
class CompletedProviderResponse:
    """Carry a validated successful provider response.

    Attributes:
        response (ProviderResponseCompleted): Original successful terminal event.
        content (str): Terminal text or, when empty, accumulated streamed text.
        response_id (str | None): Consistent ID resolved across the stream.
        response_model (str | None): Model reported by the start event.
    """

    response: ProviderResponseCompleted
    content: str
    response_id: str | None
    response_model: str | None


@dataclass(frozen=True, slots=True)
class FailedProviderResponse:
    """Carry a validated provider failure and any partial answer.

    Attributes:
        response (ProviderResponseFailed): Original failed terminal event.
        content (str): Terminal partial text or accumulated streamed text.
        response_id (str | None): Consistent ID resolved across the stream.
    """

    response: ProviderResponseFailed
    content: str
    response_id: str | None


@dataclass(slots=True)
class ProviderResponseLifecycle:
    """Own and validate the state transitions for one provider response."""

    started: bool = False
    started_response_id: str | None = None
    response_model: str | None = None
    terminal: ProviderResponseCompleted | ProviderResponseFailed | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def require_open(self) -> None:
        """Reject any event received after a terminal response.

        Raises:
            ProviderProtocolError: A terminal event has already been recorded.
        """
        if self.terminal is not None:
            raise ProviderProtocolError("Provider emitted an event after its terminal response")

    def start(self, event: ProviderResponseStarted) -> None:
        """Record the response's initial model and identifier.

        Args:
            event (ProviderResponseStarted): Provider start event.

        Raises:
            ProviderProtocolError: The response has already started or terminated.
        """
        self.require_open()
        if self.started:
            raise ProviderProtocolError("Provider emitted response_started more than once")
        self.started = True
        self.started_response_id = event.response_id
        self.response_model = event.model

    def retry(self) -> None:
        """Validate retry progress before the response has opened.

        Raises:
            ProviderProtocolError: A response has already started or terminated.
        """
        self.require_open()
        if self.started:
            raise ProviderProtocolError("Provider emitted retry progress after response_started")

    def add_text(self, delta: str) -> None:
        """Append an answer-text fragment to an active response.

        Args:
            delta (str): Text fragment in stream order.

        Raises:
            ProviderProtocolError: The response has not started or has terminated.
        """
        self.require_open()
        require_provider_response_started(self.started)
        self.text.append(delta)

    def add_thinking(self, delta: str) -> None:
        """Validate a thinking fragment without adding it to answer text.

        Args:
            delta (str): Thinking content forwarded separately by the stream adapter.

        Raises:
            ProviderProtocolError: The response has not started or has terminated.
        """
        self.require_open()
        require_provider_response_started(self.started)
        del delta

    def add_tool_call(self, tool_call: ToolCall) -> None:
        """Record a completed tool call for terminal-response validation.

        Args:
            tool_call (ToolCall): Provider call in its observed stream order.

        Raises:
            ProviderProtocolError: The response has not started or has terminated.
        """
        self.require_open()
        require_provider_response_started(self.started)
        self.tool_calls.append(tool_call)

    def complete(self, event: ProviderResponseCompleted | ProviderResponseFailed) -> None:
        """Record the terminal response before validating the complete stream.

        Args:
            event (ProviderResponseCompleted | ProviderResponseFailed): Terminal event.
                A failed event may arrive before a response start.

        Raises:
            ProviderProtocolError: A terminal event already exists, or success arrives
                before the response has started.
        """
        self.require_open()
        if isinstance(event, ProviderResponseCompleted):
            require_provider_response_started(self.started)
        self.terminal = event

    def finish(self) -> CompletedProviderResponse | FailedProviderResponse:
        """Validate stream consistency and assemble its terminal result.

        Returns:
            CompletedProviderResponse | FailedProviderResponse: Validated terminal data,
                resolved response ID, and terminal text with streamed-text fallback.

        Examples:
            >>> lifecycle = ProviderResponseLifecycle()
            >>> lifecycle.start(ProviderResponseStarted(model="test", response_id="r1"))
            >>> lifecycle.add_text("Hello")
            >>> lifecycle.complete(ProviderResponseCompleted(content="", response_id="r1"))
            >>> lifecycle.finish().content
            'Hello'

        Raises:
            ProviderProtocolError: The stream lacks required lifecycle events, IDs
                conflict, streamed and terminal tool calls differ, or the finish reason
                contradicts the presence of tool calls.
        """
        if self.terminal is None:
            if not self.started:
                raise ProviderProtocolError("Provider stream ended before response_started")
            raise ProviderProtocolError("Provider stream ended without a terminal response")
        if isinstance(self.terminal, ProviderResponseFailed):
            response_id = resolve_provider_response_id(
                started_response_id=self.started_response_id,
                terminal_response_id=self.terminal.response_id,
                tool_calls=self.tool_calls,
            )
            return FailedProviderResponse(
                response=self.terminal,
                content=self.terminal.partial_content or "".join(self.text),
                response_id=response_id,
            )
        if not self.started:
            raise ProviderProtocolError("Provider stream ended before response_started")
        if tuple(self.tool_calls) != self.terminal.tool_calls:
            raise ProviderProtocolError(
                "Provider terminal tool calls do not match streamed tool calls"
            )
        has_tool_calls = bool(self.terminal.tool_calls)
        if self.terminal.finish_reason == "tool_calls" and not has_tool_calls:
            raise ProviderProtocolError(
                "Provider finish reason 'tool_calls' requires at least one tool call"
            )
        if self.terminal.finish_reason == "stop" and has_tool_calls:
            raise ProviderProtocolError("Provider finish reason 'stop' cannot include tool calls")
        response_id = resolve_provider_response_id(
            started_response_id=self.started_response_id,
            terminal_response_id=self.terminal.response_id,
            tool_calls=self.terminal.tool_calls,
        )
        return CompletedProviderResponse(
            response=self.terminal,
            content=self.terminal.content or "".join(self.text),
            response_id=response_id,
            response_model=self.response_model,
        )
