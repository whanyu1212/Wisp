from __future__ import annotations

from typing import cast

import anyio
from google import genai
from google.genai import types as genai_types

from tests.providers.google_support import (
    StubGenaiClient,
    StubGoogleProvider,
    StubModels,
    text_chunk,
)
from wisp.messages import Message as WispMessage
from wisp.providers.base import ToolSpec
from wisp.providers.google import GoogleProvider


def test_google_provider_splits_system_messages_from_conversation() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [
                WispMessage(role="system", content="instructions"),
                WispMessage(role="system", content="context"),
                WispMessage(role="user", content="hello"),
            ],
            model="gemini-test",
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    assert len(stub_models.calls) == 1
    call = stub_models.calls[0]
    assert call["model"] == "gemini-test"
    assert call["contents"] == [
        genai_types.Content(role="user", parts=[genai_types.Part(text="hello")])
    ]
    assert call["config"].system_instruction == "instructions\n\ncontext"


def test_google_provider_omits_system_when_no_system_messages() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="gemini-test",
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    assert stub_models.calls[0]["config"].system_instruction is None


def test_google_provider_maps_assistant_role_to_model() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [
                WispMessage(role="user", content="hi"),
                WispMessage(role="assistant", content="hello there"),
            ],
            model="gemini-test",
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    assert stub_models.calls[0]["contents"] == [
        genai_types.Content(role="user", parts=[genai_types.Part(text="hi")]),
        genai_types.Content(role="model", parts=[genai_types.Part(text="hello there")]),
    ]


def test_google_provider_serializes_tool_specs_without_sanitizing_schema() -> None:
    # Regression test: the official SDK's parameters_json_schema field
    # accepts standard JSON Schema verbatim (confirmed live against the
    # Gemini API) -- unlike a raw-httpx integration against the older
    # `parameters` field, additionalProperties/$schema must NOT be stripped.
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#",
        },
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="gemini-test",
            tools=[tool],
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    tools = stub_models.calls[0]["config"].tools
    assert tools is not None
    declaration = tools[0].function_declarations[0]
    assert declaration.name == "lookup"
    assert declaration.description == "Look something up."
    assert declaration.parameters_json_schema == {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    }


def test_google_provider_sends_thinking_level_when_effort_provided() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="gemini-test",
            effort="MEDIUM",
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    thinking_config = stub_models.calls[0]["config"].thinking_config
    assert thinking_config.thinking_level == genai_types.ThinkingLevel.MEDIUM


def test_google_provider_omits_thinking_level_when_effort_not_provided() -> None:
    stub_models = StubModels()
    provider = GoogleProvider(
        api_key="test-key",
        client=cast(genai.Client, StubGenaiClient(stub_models)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="gemini-test",
        )
        assert [chunk async for chunk in stream] == []

    anyio.run(run)

    thinking_config = stub_models.calls[0]["config"].thinking_config
    assert thinking_config.thinking_level is None


def test_google_provider_stream_forwards_effort_to_create_stream() -> None:
    provider = StubGoogleProvider([text_chunk("hi", finish_reason=genai_types.FinishReason.STOP)])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="hello")], effort="HIGH"
            )
        ]

    anyio.run(run)

    assert provider.seen_effort == "HIGH"


def test_google_provider_rejects_structured_tool_replacement() -> None:
    provider = GoogleProvider(api_key="test-key")

    assert not provider.supports_structured_tool_replacement(effort=None)
    assert not provider.supports_structured_tool_replacement(effort="HIGH")
