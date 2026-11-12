"""A finished Gemini tool exchange is sent to later prompts as its own run sent it.

Gemini's implicit cache matches a request that begins the way an earlier one did,
so these tests compare what consecutive requests carry, item by item.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from google import genai
from google.genai import types as genai_types

from tests.providers.google_support import StubGenaiClient, StubModels, parts_chunk
from wisp.agent.loop import AgentLoopConfig
from wisp.agent.loop.boundary_transitions import validate_replacement_messages
from wisp.agent.request_boundary import RequestBoundaryUnsupportedError
from wisp.agent.tool_contracts import ToolExecutionEvent
from wisp.coding.session import CodingSession
from wisp.events import WispEvent
from wisp.messages import Message, NativeOutput
from wisp.messages.history import normalize_provider_history
from wisp.providers.google import GoogleProvider
from wisp.runtime.registry import ToolRegistry
from wisp.sessions import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.base import ToolArguments
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolError, ToolResult

# Not valid UTF-8, so saving it as text instead of bytes would not survive.
_SIGNATURE = b"\xff\x00signature"
_MODEL = "gemini-test"


class _Lookup:
    name = "lookup"
    safety = "read"
    description = "Look something up."
    input_schema = {"type": "object", "properties": {"q": {"type": "string"}}}

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        if arguments.get("q") == "b":
            raise ToolError("nothing under b")
        return ToolResult(text=f"found {arguments.get('q')}")


def _calls(response_id: str, *, ids: bool = True) -> list[genai_types.GenerateContentResponse]:
    """A response that thinks, then calls the tool twice under one signature.

    The second call fails, so the exchange carries an error result too. Without
    ``ids`` Gemini names no call, as its older models do.
    """

    return [
        parts_chunk(
            [
                genai_types.Part(text="considering", thought=True),
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id=f"{response_id}-a" if ids else None, name="lookup", args={"q": "a"}
                    ),
                    thought_signature=_SIGNATURE,
                ),
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id=f"{response_id}-b" if ids else None, name="lookup", args={"q": "b"}
                    )
                ),
            ],
            response_id=response_id,
            finish_reason=genai_types.FinishReason.STOP,
        )
    ]


def _answer(response_id: str) -> list[genai_types.GenerateContentResponse]:
    return [
        parts_chunk(
            [genai_types.Part(text="the answer")],
            response_id=response_id,
            finish_reason=genai_types.FinishReason.STOP,
        )
    ]


def _session(
    store: JsonlSessionStore,
    tmp_path: Path,
    responses: list[list[genai_types.GenerateContentResponse]],
    *,
    model: str = _MODEL,
) -> tuple[CodingSession, StubModels]:
    models = StubModels(responses)
    registry = ToolRegistry()
    registry.register(_Lookup())
    session = CodingSession(
        provider=GoogleProvider(
            api_key="test-key", client=cast(genai.Client, StubGenaiClient(models))
        ),
        sessions=store,
        model=model,
        tool_registry=registry,
        tool_context=ToolContext(cwd=tmp_path),
    )
    return session, models


def _run(session: CodingSession, saved: JsonlSession, prompt: str) -> list[WispEvent]:
    async def collect() -> list[WispEvent]:
        return [event async for event in session.run(prompt, session=saved)]

    return anyio.run(collect)


def _sent(call: dict[str, Any]) -> list[dict[str, Any]]:
    """One request's conversation items, as they go on the wire."""

    return [content.model_dump(mode="json", exclude_none=True) for content in call["contents"]]


def _kinds(item: dict[str, Any]) -> list[str]:
    return ["+".join(sorted(part)) for part in item["parts"]]


@pytest.mark.parametrize("ids", [True, False], ids=["gemini-ids", "no-ids"])
def test_next_prompt_extends_last_request(tmp_path: Path, ids: bool) -> None:
    """The next prompt's first request starts with everything the run last sent."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session, models = _session(
        store, tmp_path, [_calls("r1", ids=ids), _answer("r2"), _answer("r3")]
    )
    saved = store.create()

    _run(session, saved, "first")
    _run(session, saved, "second")

    _, after_tools, next_prompt = (_sent(call) for call in models.calls)
    assert next_prompt[: len(after_tools)] == after_tools
    # The signed model turn and its results, not a text summary of them.
    assert _kinds(next_prompt[1]) == [
        "text+thought",
        "function_call+thought_signature",
        "function_call",
    ]
    assert _kinds(next_prompt[2]) == ["function_response", "function_response"]


def test_restart_replays_saved_turn(tmp_path: Path) -> None:
    """A new process sends the same items, from the session file alone."""

    store = JsonlSessionStore(tmp_path / "sessions")
    first, first_models = _session(store, tmp_path, [_calls("r1"), _answer("r2")])
    saved = store.create()
    _run(first, saved, "first")

    restarted, models = _session(store, tmp_path, [_answer("r3")])
    _run(restarted, store.latest(), "second")

    after_tools = _sent(first_models.calls[-1])
    assert _sent(models.calls[0])[: len(after_tools)] == after_tools
    signed = models.calls[0]["contents"][1].parts[1]
    assert signed.thought_signature == _SIGNATURE


def test_other_model_gets_text(tmp_path: Path) -> None:
    """Signatures belong to their model, so another model is sent the flattened form."""

    store = JsonlSessionStore(tmp_path / "sessions")
    first, _ = _session(store, tmp_path, [_calls("r1"), _answer("r2")])
    saved = store.create()
    _run(first, saved, "first")

    other, models = _session(store, tmp_path, [_answer("r3")], model="gemini-other")
    _run(other, store.latest(), "second")

    history = _sent(models.calls[0])
    assert all(_kinds(item) == ["text"] for item in history)
    assert "found a" in json.dumps(history)


def test_events_omit_saved_turn(tmp_path: Path) -> None:
    """The turn is saved with the row and never published."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session, _ = _session(store, tmp_path, [_calls("r1"), _answer("r2")])
    saved = store.create()

    events = _run(session, saved, "first")

    published = "".join(event.model_dump_json() for event in events)
    assert "native_output" not in published and "thought_signature" not in published
    rows = [
        entry.message
        for entry in saved.read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.message.role == "assistant"
    ]
    # Only the tool-call turn is saved; the final answer keeps its portable form.
    assert [row.native_output is not None for row in rows] == [True, False]


def _exchange(native: NativeOutput | None) -> list[Message]:
    from wisp.events import ToolCallSnapshot

    return [
        Message(role="user", content="go"),
        Message(
            role="assistant",
            content="",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="c1", name="lookup", arguments={"q": "a"}, provider_call_id="p1"
                ),
            ),
            response_id="r1",
            native_output=native,
        ),
        Message(role="tool", content="found a", tool_call_id="c1", tool_name="lookup"),
        Message(role="assistant", content="done"),
    ]


def _turn(*names: str, role: str = "model") -> dict[str, object]:
    return genai_types.Content(
        role=role,
        parts=[
            genai_types.Part(
                function_call=genai_types.FunctionCall(id="p1", name=name, args={"q": "a"}),
                thought_signature=_SIGNATURE,
            )
            for name in names
        ],
    ).model_dump(mode="json", exclude_none=True)


@pytest.mark.parametrize(
    "native",
    [
        None,
        NativeOutput(provider="openai", model=_MODEL, items=(_turn("lookup"),)),
        NativeOutput(provider="google", model="gemini-other", items=(_turn("lookup"),)),
        # A saved turn that does not make the row's calls cannot be answered by its results.
        NativeOutput(provider="google", model=_MODEL, items=(_turn("other_tool"),)),
        NativeOutput(provider="google", model=_MODEL, items=(_turn("lookup", "lookup"),)),
        NativeOutput(provider="google", model=_MODEL, items=(_turn("lookup", role="user"),)),
        NativeOutput(provider="google", model=_MODEL, items=({"parts": "not a list"},)),
        NativeOutput(provider="google", model=_MODEL, items=(_turn("lookup"), _turn("lookup"))),
    ],
    ids=[
        "none",
        "other-provider",
        "other-model",
        "other-call",
        "extra-call",
        "wrong-role",
        "malformed",
        "two-items",
    ],
)
def test_unusable_saved_turn_is_flattened(native: NativeOutput | None) -> None:
    """An exchange is replayed only from a turn this provider and model made for it."""

    provider = GoogleProvider(api_key="test-key")
    messages = _exchange(native)

    assert not provider.replays_saved_tool_exchange(messages[1], model=_MODEL)
    normalized = normalize_provider_history(
        messages,
        replays_exchange=lambda message: provider.replays_saved_tool_exchange(
            message, model=_MODEL
        ),
    )
    # No structured call is left for Gemini to reject for want of a signature.
    assert all(message.tool_calls is None and message.role != "tool" for message in normalized)


def test_usable_saved_turn_stays_structured() -> None:
    provider = GoogleProvider(api_key="test-key", default_model=_MODEL)
    native = NativeOutput(provider="google", model=_MODEL, items=(_turn("lookup"),))
    messages = _exchange(native)

    # With no model named, the provider's default is the one asked.
    assert provider.replays_saved_tool_exchange(messages[1], model=None)
    normalized = normalize_provider_history(
        messages,
        replays_exchange=lambda message: provider.replays_saved_tool_exchange(message, model=None),
    )
    assert [message.role for message in normalized] == ["user", "assistant", "tool", "assistant"]


def _loop_config(model: str) -> AgentLoopConfig:
    provider = GoogleProvider(api_key="test-key")
    return AgentLoopConfig(provider=provider, tool_executor=_NoTools(), model=model)


class _NoTools:
    async def execute(self, tool_call: object) -> AsyncIterator[ToolExecutionEvent]:
        raise AssertionError("no tool runs in these tests")
        yield  # pragma: no cover


def test_compaction_base_keeps_saved_exchange() -> None:
    """A rebased request may carry an exchange the provider replays from its row."""

    native = NativeOutput(provider="google", model=_MODEL, items=(_turn("lookup"),))
    base = normalize_provider_history(
        _exchange(native),
        replays_exchange=lambda message: message.native_output is not None,
    )

    assert validate_replacement_messages(_loop_config(_MODEL), base) == base


@pytest.mark.parametrize(
    ("native", "model"),
    [
        (None, _MODEL),
        (NativeOutput(provider="google", model=_MODEL, items=(_turn("lookup"),)), "gemini-other"),
    ],
    ids=["no-saved-turn", "other-model"],
)
def test_unsigned_exchange_still_refused(native: NativeOutput | None, model: str) -> None:
    """A structured exchange Gemini has no signed turn for is never let through."""

    with pytest.raises(RequestBoundaryUnsupportedError):
        validate_replacement_messages(_loop_config(model), _exchange(native))
