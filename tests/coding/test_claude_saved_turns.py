"""A finished Claude tool exchange is sent to later prompts as its own run sent it.

Anthropic reads its prompt cache for a request that begins the way an earlier
one did, and writes it again otherwise, so these tests compare what consecutive
requests carry, message by message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import RawMessageStreamEvent

from tests.providers.anthropic_support import (
    StubAsyncAnthropic,
    StubMessagesResource,
    input_json_delta_event,
    message_delta_event,
    message_start_event,
    text_delta_event,
    thinking_delta_event,
    tool_use_start_event,
)
from tests.providers.test_anthropic_replay import (
    _signature_delta,
    _text_block_start,
    _thinking_block_start,
)
from wisp.agent.loop import AgentLoopConfig
from wisp.agent.loop.boundary_transitions import validate_replacement_messages
from wisp.agent.request_boundary import RequestBoundaryUnsupportedError
from wisp.coding.session import CodingSession
from wisp.events import ToolCallSnapshot, WispEvent
from wisp.messages import Message, NativeOutput
from wisp.messages.history import normalize_provider_history
from wisp.providers.anthropic import AnthropicProvider
from wisp.providers.base import prepare_provider_history
from wisp.runtime.registry import ToolRegistry
from wisp.sessions import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.base import ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult

_MODEL = "claude-test"


class _Lookup:
    name = "lookup"
    safety = "read"
    description = "Look something up."
    input_schema = {"type": "object", "properties": {"q": {"type": "string"}}}

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        if arguments.get("q") == "b":
            raise ToolError("nothing under b")
        return ToolResult(text=f"found {arguments.get('q')}")


def _calls(response_id: str) -> list[RawMessageStreamEvent]:
    """A response that thinks under a signature, then calls the tool twice.

    The second call fails, so the exchange carries an error result too.
    """

    return [
        message_start_event(response_id),
        _thinking_block_start(index=0),
        thinking_delta_event("considering", index=0),
        _signature_delta("signed", index=0),
        tool_use_start_event(f"{response_id}-a", "lookup", index=1),
        input_json_delta_event('{"q": "a"}', index=1),
        tool_use_start_event(f"{response_id}-b", "lookup", index=2),
        input_json_delta_event('{"q": "b"}', index=2),
        message_delta_event("tool_use"),
    ]


def _answer(response_id: str) -> list[RawMessageStreamEvent]:
    return [
        message_start_event(response_id),
        _text_block_start(index=0),
        text_delta_event("the answer", index=0),
        message_delta_event("end_turn"),
    ]


def _session(
    store: JsonlSessionStore,
    tmp_path: Path,
    responses: list[list[RawMessageStreamEvent]],
    *,
    model: str = _MODEL,
) -> tuple[CodingSession, StubMessagesResource]:
    resource = StubMessagesResource(responses)
    registry = ToolRegistry()
    registry.register(_Lookup())
    session = CodingSession(
        provider=AnthropicProvider(
            api_key="test-key", client=cast(AsyncAnthropic, StubAsyncAnthropic(resource))
        ),
        sessions=store,
        model=model,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
    )
    return session, resource


def _run(session: CodingSession, saved: JsonlSession, prompt: str) -> list[WispEvent]:
    async def collect() -> list[WispEvent]:
        return [event async for event in session.run(prompt, session=saved)]

    return anyio.run(collect)


def _sent(call: dict[str, Any]) -> list[dict[str, Any]]:
    """One request's messages, copied as they were when it was sent."""

    return cast("list[dict[str, Any]]", json.loads(json.dumps(call["messages"])))


def _kinds(message: dict[str, Any]) -> list[str]:
    return [block["type"] for block in message["content"]]


def test_next_prompt_extends_last_request(tmp_path: Path) -> None:
    """The next prompt's first request starts with everything the run last sent."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session, resource = _session(store, tmp_path, [_calls("r1"), _answer("r2"), _answer("r3")])
    saved = store.create()

    _run(session, saved, "first")
    _run(session, saved, "second")

    _, after_tools, next_prompt = (_sent(call) for call in resource.calls)
    assert next_prompt[: len(after_tools)] == after_tools
    # The signed turn and its results, not a text summary of them.
    assert _kinds(next_prompt[1]) == ["thinking", "tool_use", "tool_use"]
    assert next_prompt[1]["content"][0]["signature"] == "signed"
    assert _kinds(next_prompt[2]) == ["tool_result", "tool_result"]


def test_restart_replays_saved_turn(tmp_path: Path) -> None:
    """A new process sends the same messages, from the session file alone."""

    store = JsonlSessionStore(tmp_path / "sessions")
    first, first_resource = _session(store, tmp_path, [_calls("r1"), _answer("r2")])
    saved = store.create()
    _run(first, saved, "first")

    restarted, resource = _session(store, tmp_path, [_answer("r3")])
    _run(restarted, store.latest(), "second")

    after_tools = _sent(first_resource.calls[-1])
    assert _sent(resource.calls[0])[: len(after_tools)] == after_tools


def test_other_model_gets_text(tmp_path: Path) -> None:
    """Signed thinking belongs to its model, so another model is sent the flattened form."""

    store = JsonlSessionStore(tmp_path / "sessions")
    first, _ = _session(store, tmp_path, [_calls("r1"), _answer("r2")])
    saved = store.create()
    _run(first, saved, "first")

    other, resource = _session(store, tmp_path, [_answer("r3")], model="claude-other")
    _run(other, store.latest(), "second")

    history = _sent(resource.calls[0])
    assert all(_kinds(message) == ["text"] for message in history)
    assert "found a" in json.dumps(history)


def test_events_omit_saved_turn(tmp_path: Path) -> None:
    """The turn is saved with the row and never published."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session, _ = _session(store, tmp_path, [_calls("r1"), _answer("r2")])
    saved = store.create()

    events = _run(session, saved, "first")

    published = "".join(event.model_dump_json() for event in events)
    assert "native_output" not in published and "signed" not in published
    rows = [
        entry.message
        for entry in saved.read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "assistant"
    ]
    # Only the tool-use turn is saved; the final answer keeps its portable form.
    assert [row.native_output is not None for row in rows] == [True, False]


def _exchange(native: NativeOutput | None) -> list[Message]:
    return [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            content="",
            tool_calls=(ToolCallSnapshot(call_id="c1", name="lookup", arguments={"q": "a"}),),
            response_id="r1",
            native_output=native,
        ),
        Message(role="tool", content="found a", tool_call_id="c1", tool_name="lookup"),
        Message(role="assistant", content="done"),
    ]


def _turn(*uses: tuple[str, str], role: str = "assistant") -> dict[str, object]:
    return {
        "role": role,
        "content": [
            {"type": "thinking", "thinking": "considering", "signature": "signed"},
            *(
                {"type": "tool_use", "id": call_id, "name": name, "input": {"q": "a"}}
                for call_id, name in uses
            ),
        ],
    }


_GOOD = _turn(("c1", "lookup"))


def _turn_with_search(*, answered: bool) -> dict[str, object]:
    """The usable turn with a server web search added, with or without its result."""

    search: list[dict[str, object]] = [
        {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {"query": "q"}}
    ]
    if answered:
        search.append({"type": "web_search_tool_result", "tool_use_id": "srv1", "content": []})
    return {"role": "assistant", "content": [*search, *cast("list[object]", _GOOD["content"])]}


@pytest.mark.parametrize(
    "native",
    [
        None,
        NativeOutput(provider="google", model=_MODEL, items=(_GOOD,)),
        NativeOutput(provider="anthropic", model="claude-other", items=(_GOOD,)),
        # A saved turn that does not make the row's calls cannot be answered by its results.
        NativeOutput(provider="anthropic", model=_MODEL, items=(_turn(("c1", "other_tool")),)),
        NativeOutput(provider="anthropic", model=_MODEL, items=(_turn(("c2", "lookup")),)),
        NativeOutput(
            provider="anthropic",
            model=_MODEL,
            items=(_turn(("c1", "lookup"), ("c2", "lookup")),),
        ),
        NativeOutput(
            provider="anthropic", model=_MODEL, items=(_turn(("c1", "lookup"), role="user"),)
        ),
        NativeOutput(provider="anthropic", model=_MODEL, items=({"role": "assistant"},)),
        NativeOutput(provider="anthropic", model=_MODEL, items=(_GOOD, _GOOD)),
        # A search started beside the call is answered in the next response, which
        # is not replayed, so the turn alone would carry a search with no result.
        NativeOutput(
            provider="anthropic", model=_MODEL, items=(_turn_with_search(answered=False),)
        ),
    ],
    ids=[
        "none",
        "other-provider",
        "other-model",
        "other-call",
        "other-id",
        "extra-call",
        "wrong-role",
        "malformed",
        "two-items",
        "unanswered-search",
    ],
)
def test_unusable_saved_turn_is_flattened(native: NativeOutput | None) -> None:
    """An exchange is replayed only from a turn this provider and model made for it."""

    provider = AnthropicProvider(api_key="test-key")
    messages = _exchange(native)

    assert not provider.replays_saved_tool_exchange(messages[1], model=_MODEL)
    normalized = prepare_provider_history(messages, provider=provider, effort=None, model=_MODEL)
    # No structured tool use is left for Anthropic to receive without its thinking.
    assert all(message.tool_calls is None and message.role != "tool" for message in normalized)
    with pytest.raises(RequestBoundaryUnsupportedError):
        validate_replacement_messages(_loop_config(provider), messages)


def test_usable_saved_turn_stays_structured() -> None:
    provider = AnthropicProvider(api_key="test-key", default_model=_MODEL)
    messages = _exchange(NativeOutput(provider="anthropic", model=_MODEL, items=(_GOOD,)))

    # With no model named, the provider's default is the one asked.
    assert provider.replays_saved_tool_exchange(messages[1], model=None)
    normalized = normalize_provider_history(
        messages,
        replays_exchange=lambda message: provider.replays_saved_tool_exchange(message, model=None),
    )
    assert [message.role for message in normalized] == ["user", "assistant", "tool", "assistant"]
    # A rebased request, as after compaction, may carry the exchange.
    assert validate_replacement_messages(_loop_config(provider), normalized) == normalized


def test_answered_search_is_replayed() -> None:
    """A search that finished within the turn travels with it."""

    provider = AnthropicProvider(api_key="test-key")
    native = NativeOutput(
        provider="anthropic", model=_MODEL, items=(_turn_with_search(answered=True),)
    )

    assert provider.replays_saved_tool_exchange(_exchange(native)[1], model=_MODEL)


class _NoTools:
    async def execute(self, tool_call: object) -> Any:
        raise AssertionError("no tool runs in these tests")


def _loop_config(provider: AnthropicProvider) -> AgentLoopConfig:
    return AgentLoopConfig(provider=provider, tool_executor=_NoTools(), model=_MODEL)
