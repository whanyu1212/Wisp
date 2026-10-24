"""Unit tests for one provider response's lifecycle validation and response-id resolution."""

from __future__ import annotations

import pytest

from wisp.agent.loop.provider_lifecycle import (
    ProviderResponseLifecycle,
    resolve_provider_response_id,
)
from wisp.providers.base import ProviderProtocolError
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ToolCall,
)


def _call(*, call_id: str = "call-1", response_id: str | None = None) -> ToolCall:
    return ToolCall(call_id=call_id, name="noop", arguments={}, response_id=response_id)


def test_resolves_agreeing_response_ids() -> None:
    assert (
        resolve_provider_response_id(
            started_response_id="resp-1",
            terminal_response_id="resp-1",
            tool_calls=(_call(response_id="resp-1"),),
        )
        == "resp-1"
    )
    assert (
        resolve_provider_response_id(
            started_response_id=None,
            terminal_response_id=None,
            tool_calls=(),
        )
        is None
    )


def test_rejects_conflicting_response_ids() -> None:
    with pytest.raises(ProviderProtocolError, match="conflicting response ids"):
        resolve_provider_response_id(
            started_response_id="started",
            terminal_response_id="terminal",
            tool_calls=(),
        )


def test_rejects_finish_reason_tool_mismatch() -> None:
    call = _call()
    empty = ProviderResponseLifecycle()
    empty.start(ProviderResponseStarted(model="test"))
    empty.complete(ProviderResponseCompleted(content="", finish_reason="tool_calls"))
    with pytest.raises(ProviderProtocolError, match="requires at least one tool call"):
        empty.finish()

    with_calls = ProviderResponseLifecycle()
    with_calls.start(ProviderResponseStarted(model="test"))
    with_calls.add_tool_call(call)
    with_calls.complete(
        ProviderResponseCompleted(content="", tool_calls=(call,), finish_reason="stop")
    )
    with pytest.raises(ProviderProtocolError, match="cannot include tool calls"):
        with_calls.finish()


def test_call_ids_unique_per_response() -> None:
    lifecycle = ProviderResponseLifecycle()
    lifecycle.start(ProviderResponseStarted(model="test"))
    lifecycle.add_tool_call(ToolCall(call_id="call-1", name="bash", arguments={}))
    with pytest.raises(ProviderProtocolError, match="'call-1' more than once"):
        lifecycle.add_tool_call(ToolCall(call_id="call-1", name="bash", arguments={"n": 2}))

    # Uniqueness is per response: the next response may reuse the ID.
    later = ProviderResponseLifecycle()
    later.start(ProviderResponseStarted(model="test"))
    later.add_tool_call(ToolCall(call_id="call-1", name="bash", arguments={}))
    assert [call.call_id for call in later.tool_calls] == ["call-1"]


def test_failed_response_needs_no_start() -> None:
    lifecycle = ProviderResponseLifecycle()
    lifecycle.complete(ProviderResponseFailed(message="upstream failed"))
    failed = lifecycle.finish()
    assert failed.content == ""
    assert failed.response.message == "upstream failed"


def test_records_text_not_thinking() -> None:
    lifecycle = ProviderResponseLifecycle()
    lifecycle.start(ProviderResponseStarted(model="test"))
    lifecycle.add_text("hello")
    lifecycle.accept_thinking()
    lifecycle.complete(ProviderResponseCompleted(content=""))
    completed = lifecycle.finish()
    assert completed.content == "hello"
    assert "".join(lifecycle.text) == "hello"


def test_rejects_data_before_start() -> None:
    lifecycle = ProviderResponseLifecycle()
    with pytest.raises(ProviderProtocolError, match="before response_started"):
        lifecycle.add_text("nope")
