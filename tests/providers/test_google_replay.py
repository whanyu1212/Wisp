from __future__ import annotations

from typing import cast

import anyio
import pytest
from google import genai
from google.genai import types as genai_types

from tests.providers.google_support import (
    StubGenaiClient,
    StubModels,
    part_chunk,
    text_chunk,
)
from wisp.events import ToolCallSnapshot
from wisp.messages import Message as WispMessage
from wisp.providers.base import (
    ProviderProtocolError,
    ToolCallResult,
)
from wisp.providers.events import ProviderResponseCompleted
from wisp.providers.google import GoogleProvider


def test_google_provider_serializes_active_tool_exchange_in_fresh_context() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    messages = [
        WispMessage(role="user", content="search"),
        WispMessage(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="call-1",
                    name="lookup",
                    arguments={"query": "wisp"},
                    provider_call_id="provider-1",
                ),
                ToolCallSnapshot(
                    call_id="call-2",
                    name="read",
                    arguments={"path": "README.md"},
                    provider_call_id="provider-2",
                ),
            ),
        ),
        WispMessage(role="tool", content="found it", tool_call_id="call-1", tool_name="lookup"),
        WispMessage(
            role="tool",
            content="missing",
            tool_call_id="call-2",
            tool_name="read",
            is_error=True,
        ),
    ]

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            messages,
            model="gemini-test",
            extra_messages=[WispMessage(role="user", content="steered")],
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    contents = stub_models.calls[0]["contents"]
    assert contents[0] == genai_types.Content(role="user", parts=[genai_types.Part(text="search")])
    assert contents[1].role == "model"
    assert contents[1].parts[0].text == "checking"
    assert contents[1].parts[1].function_call == genai_types.FunctionCall(
        id="provider-1", name="lookup", args={"query": "wisp"}
    )
    assert contents[1].parts[2].function_call == genai_types.FunctionCall(
        id="provider-2", name="read", args={"path": "README.md"}
    )
    assert len(contents[2].parts) == 2
    assert contents[2].parts[0].function_response == genai_types.FunctionResponse(
        id="provider-1", name="lookup", response={"output": "found it"}
    )
    assert contents[2].parts[1].function_response == genai_types.FunctionResponse(
        id="provider-2", name="read", response={"error": "missing"}
    )
    assert contents[3] == genai_types.Content(role="user", parts=[genai_types.Part(text="steered")])


def test_google_provider_scopes_reused_call_ids_to_each_fresh_batch() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    messages = [
        WispMessage(
            role="assistant",
            content="",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="reused",
                    name="lookup",
                    arguments={},
                    provider_call_id="provider-old",
                ),
            ),
        ),
        WispMessage(role="tool", content="first", tool_call_id="reused", tool_name=None),
        WispMessage(
            role="assistant",
            content="",
            tool_calls=(
                ToolCallSnapshot(
                    call_id="reused",
                    name="read",
                    arguments={},
                    provider_call_id="provider-new",
                ),
            ),
        ),
        WispMessage(role="tool", content="second", tool_call_id="reused", tool_name=None),
    ]

    async def run() -> None:
        stream = await provider._create_stream(messages, model="gemini-test")  # noqa: SLF001
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    contents = stub_models.calls[0]["contents"]
    first = contents[1].parts[0].function_response
    second = contents[3].parts[0].function_response
    assert (first.id, first.name) == ("provider-old", "lookup")
    assert (second.id, second.name) == ("provider-new", "read")


def test_google_provider_omits_synthetic_ids_in_fresh_tool_exchange() -> None:
    """Fresh replacement context must not invent Gemini function-call IDs."""

    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    messages = [
        WispMessage(
            role="assistant",
            content="",
            tool_calls=(ToolCallSnapshot(call_id="call-lookup-0", name="lookup", arguments={}),),
        ),
        WispMessage(
            role="tool",
            content="found it",
            tool_call_id="call-lookup-0",
            tool_name="lookup",
        ),
    ]

    async def run() -> None:
        stream = await provider._create_stream(messages, model="gemini-test")  # noqa: SLF001
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    model_call = stub_models.calls[0]["contents"][0].parts[0].function_call
    tool_result = stub_models.calls[0]["contents"][1].parts[0].function_response
    assert model_call is not None
    assert model_call.id is None
    assert tool_result is not None
    assert tool_result.id is None
    assert tool_result.name == "lookup"


def test_google_provider_replays_clean_response_before_appended_user_message() -> None:
    stub_models = StubModels(
        responses=[
            [
                text_chunk(
                    "first",
                    response_id="response-id",
                    finish_reason=genai_types.FinishReason.STOP,
                )
            ],
            [],
        ]
    )
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="gemini-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="gemini-test",
            previous_response_id=completed.response_id,
            extra_messages=[WispMessage(role="user", content="steered")],
        ):
            pass

    anyio.run(run)

    assert stub_models.calls[1]["contents"] == [
        genai_types.Content(role="user", parts=[genai_types.Part(text="hello")]),
        genai_types.Content(role="model", parts=[genai_types.Part(text="first")]),
        genai_types.Content(role="user", parts=[genai_types.Part(text="steered")]),
    ]


def test_google_provider_replays_model_turn_before_tool_results() -> None:
    # Regression test: Gemini requires the model-role turn that produced a
    # tool call (including its thought_signature parts) to be resent ahead
    # of the tool's functionResponse -- but AgentHarness's `messages` never
    # grows across tool rounds, so GoogleProvider must reconstruct it from
    # its own replay cache, exactly like AnthropicProvider's _replays.
    stub_models = StubModels(
        responses=[
            [
                part_chunk(
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            id="call-id", name="lookup", args={}
                        ),
                        thought_signature=b"sig-bytes",
                    ),
                    response_id="response-id",
                    finish_reason=genai_types.FinishReason.STOP,
                )
            ],
            [],
        ]
    )
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="gemini-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        async for _event in provider.stream(
            messages,
            model="gemini-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
            previous_response_id=completed.response_id,
        ):
            pass

    anyio.run(run)

    replay_contents = stub_models.calls[1]["contents"]
    # [user hello, model turn (echoed verbatim, incl. thought_signature),
    #  functionResponse] -- the tool_use turn must immediately precede its
    # matching tool result, same requirement as Anthropic's Messages API.
    assert len(replay_contents) == 3
    assert replay_contents[0] == genai_types.Content(
        role="user", parts=[genai_types.Part(text="hello")]
    )
    model_turn = replay_contents[1]
    assert model_turn.role == "model"
    assert model_turn.parts[0].function_call.name == "lookup"
    assert model_turn.parts[0].thought_signature == b"sig-bytes"
    function_response_part = replay_contents[2].parts[0]
    assert function_response_part.function_response.id == "call-id"
    assert function_response_part.function_response.name == "lookup"
    assert function_response_part.function_response.response == {"output": "ok"}


def test_google_provider_omits_function_response_id_when_gemini_never_issued_one() -> None:
    # Regression test: for models where Gemini omits function_call.id (the
    # ToolCall.call_id is then Wisp's own synthetic fallback -- see
    # _tool_call_from_google), sending that synthetic value back as
    # functionResponse.id would claim it as a real Gemini-issued id it never
    # was. Only echo id when Gemini actually issued one.
    stub_models = StubModels(
        responses=[
            [
                part_chunk(
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(name="lookup", args={})
                    ),
                    response_id="response-id",
                    finish_reason=genai_types.FinishReason.STOP,
                )
            ],
            [],
        ]
    )
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    messages = [WispMessage(role="user", content="hello")]

    async def run() -> None:
        first_events = [event async for event in provider.stream(messages, model="gemini-test")]
        completed = first_events[-1]
        assert isinstance(completed, ProviderResponseCompleted)
        synthetic_call_id = completed.tool_calls[0].call_id
        async for _event in provider.stream(
            messages,
            model="gemini-test",
            tool_results=[ToolCallResult(call_id=synthetic_call_id, output="ok")],
            previous_response_id=completed.response_id,
        ):
            pass

    anyio.run(run)

    replay_contents = stub_models.calls[1]["contents"]
    function_response_part = replay_contents[2].parts[0]
    assert function_response_part.function_response.id is None
    assert function_response_part.function_response.name == "lookup"


def test_google_provider_rejects_missing_continuation_state() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = provider.stream(
            [WispMessage(role="user", content="hello")],
            model="gemini-test",
            previous_response_id="missing-response",
            extra_messages=[WispMessage(role="user", content="steered")],
        )
        await anext(stream)

    with pytest.raises(
        ProviderProtocolError,
        match="Google continuation state is unavailable for missing-response",
    ):
        anyio.run(run)
    assert stub_models.calls == []


def test_google_provider_tool_results_without_a_replay_omit_the_model_turn() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="gemini-test",
            tool_results=[ToolCallResult(call_id="call-id", output="ok")],
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    contents = stub_models.calls[0]["contents"]
    assert len(contents) == 2
    assert contents[0] == genai_types.Content(role="user", parts=[genai_types.Part(text="hello")])
    assert contents[1].parts[0].function_response.name == "call-id"
