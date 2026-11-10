from __future__ import annotations

from copy import deepcopy
from typing import cast

import anyio
import pytest
from anthropic import AsyncAnthropic

from tests.providers.anthropic_support import (
    StubAnthropicProvider,
    StubAsyncAnthropic,
    StubMessagesResource,
    message_delta_event,
    text_delta_event,
)
from wisp.messages import Message as WispMessage
from wisp.providers.anthropic import (
    AnthropicCacheTtl,
    AnthropicProvider,
    _tool_specs_to_anthropic_tools,
)
from wisp.providers.base import (
    ToolCallResult,
    ToolSpec,
)
from wisp.tools.shell.tool import BashTool


@pytest.mark.parametrize("effort", [None, "high"])
def test_anthropic_provider_rejects_structured_replacement_even_without_effort(
    effort: str | None,
) -> None:
    provider = AnthropicProvider(api_key="test-key", default_model="claude-fable-5-1")

    assert not provider.supports_structured_tool_replacement(effort=effort)


def test_anthropic_provider_splits_system_messages_from_conversation() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [
                WispMessage(role="system", content="instructions"),
                WispMessage(role="system", content="context"),
                WispMessage(role="user", content="hello"),
            ],
            model="claude-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
            "cache_control": {"type": "ephemeral"},
            "system": "instructions\n\ncontext",
        }
    ]


def test_anthropic_provider_omits_system_when_no_system_messages() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="claude-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_anthropic_provider_folds_tool_results_into_one_trailing_user_message() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    tool_results = [
        ToolCallResult(call_id="call-1", output="result one"),
        ToolCallResult(call_id="call-2", output="failed", is_error=True),
    ]

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="assistant", content="using tools")],
            model="claude-test",
            tool_results=tool_results,
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "using tools"}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": "result one",
                            "is_error": False,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-2",
                            "content": "failed",
                            "is_error": True,
                        },
                    ],
                },
            ],
            "stream": True,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_anthropic_provider_serializes_tool_specs() -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="claude-test",
            tools=[tool],
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
            "cache_control": {"type": "ephemeral"},
            "tools": [
                {
                    "name": "lookup",
                    "description": "Look something up.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (
            # `bash`'s own schema: per-operation variants under a top-level oneOf.
            BashTool.input_schema,
            {key: value for key, value in BashTool.input_schema.items() if key != "oneOf"},
        ),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "anyOf": [{"required": ["a"]}],
            },
            {"type": "object", "properties": {"a": {"type": "string"}}},
        ),
        (
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
                "allOf": [
                    {"properties": {"b": {"type": "integer"}}, "required": ["b", "a"]},
                    {"properties": {"a": {"type": "number"}}},
                ],
            },
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        ),
        (
            # Nested combinators are accepted, so they stay.
            {
                "type": "object",
                "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            },
            {
                "type": "object",
                "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            },
        ),
    ],
    ids=["bash", "any-of", "all-of", "nested"],
)
def test_tool_schema_has_no_top_level_combinator(
    schema: dict[str, object], expected: dict[str, object]
) -> None:
    """Anthropic rejects a request whose tool schema has a top-level combinator."""

    original = deepcopy(schema)
    tool = ToolSpec(name="t", description="T.", input_schema=schema)

    (sent,) = _tool_specs_to_anthropic_tools([tool])

    assert sent["input_schema"] == expected
    assert schema == original


def test_anthropic_provider_sends_output_config_effort_when_provided() -> None:
    # Regression test: Anthropic's migration guide pairs output_config.effort
    # with thinking: {"type": "adaptive"} in every documented example and
    # describes effort as controlling "thinking depth" -- without adaptive
    # thinking enabled, effort has nothing to modulate.
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="claude-test",
            effort="high",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls == [
        {
            "model": "claude-test",
            "max_tokens": 64000,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "stream": True,
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": "high"},
            "thinking": {"type": "adaptive"},
        }
    ]


def test_anthropic_provider_stream_forwards_effort_to_create_stream() -> None:
    provider = StubAnthropicProvider([text_delta_event("hi"), message_delta_event("end_turn")])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="hello")], effort="medium"
            )
        ]

    anyio.run(run)

    assert provider.seen_effort == "medium"


@pytest.mark.parametrize(
    ("cache_ttl", "cache_control"),
    [
        pytest.param("5m", {"type": "ephemeral"}, id="five-minutes-sends-no-ttl"),
        pytest.param("1h", {"type": "ephemeral", "ttl": "1h"}, id="one-hour"),
    ],
)
def test_anthropic_provider_sends_configured_cache_ttl(
    cache_ttl: AnthropicCacheTtl, cache_control: dict[str, str]
) -> None:
    messages_resource = StubMessagesResource()
    provider = AnthropicProvider(
        api_key="test-key",
        client=cast(AsyncAnthropic, StubAsyncAnthropic(messages_resource)),
        cache_ttl=cache_ttl,
    )

    async def run() -> None:
        stream = await provider._create_stream(  # noqa: SLF001
            [WispMessage(role="user", content="hello")],
            model="claude-test",
        )
        assert [event async for event in stream] == []

    anyio.run(run)

    assert messages_resource.calls[0]["cache_control"] == cache_control


def test_anthropic_provider_rejects_unknown_cache_ttl() -> None:
    with pytest.raises(ValueError, match="Unsupported Anthropic cache TTL"):
        AnthropicProvider(api_key="test-key", cache_ttl=cast(AnthropicCacheTtl, "24h"))
