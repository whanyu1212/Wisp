"""Unit tests for request context estimation, observation fingerprints, and budgets."""

from __future__ import annotations

import json
import math

import pytest

from wisp.agent.context_budget import (
    build_context_budget,
    context_fingerprint,
    estimate_context,
    estimate_context_budget,
    observe_context,
)
from wisp.messages import Message, NativeOutput
from wisp.providers.base import ToolSpec


def test_estimate_counts_system_messages_and_tools() -> None:
    messages = (
        Message(role="system", content="system"),
        Message(role="user", content="hello"),
    )
    tools = (
        ToolSpec(
            name="lookup",
            description="Look up a value",
            input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        ),
    )

    initial = estimate_context(messages, tools)
    continued = estimate_context(
        (
            *messages,
            Message(role="tool", content="a large tool result" * 20, tool_call_id="call-1"),
        ),
        tools,
    )

    assert initial.method == "utf8_bytes_div_4_v2"
    assert initial.system_tokens > 0
    assert initial.message_tokens > 0
    assert initial.tool_schema_tokens > 0
    assert initial.total_tokens == (
        initial.system_tokens + initial.message_tokens + initial.tool_schema_tokens
    )
    assert continued.message_tokens > initial.message_tokens
    assert continued.total_tokens > initial.total_tokens


def _serialized_tokens(payload: object) -> int:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return math.ceil(len(text.encode("utf-8")) / 4)


def test_observed_prefix_plus_trailing_estimate() -> None:
    prefix = (Message(role="system", content="system"), Message(role="user", content="hello"))
    trailing = Message(role="assistant", content="world 🌍")
    observation = observe_context(
        prefix,
        provider="test",
        model="model",
        input_tokens=123,
    )

    budget = estimate_context_budget(
        (*prefix, trailing),
        context_window=1_000,
        reserve_tokens=100,
        observation=observation,
        provider="test",
        model="model",
    )

    trailing_tokens = estimate_context((trailing,)).total_tokens
    assert budget.observed_tokens == 123
    assert budget.observed_is_current is True
    assert budget.trailing_estimated_tokens == trailing_tokens
    assert budget.effective_tokens == 123 + trailing_tokens
    assert budget.accounting_method == "provider_observed_plus_estimate"
    assert budget.remaining_tokens == 1_000 - 100 - 123 - trailing_tokens


@pytest.mark.parametrize("change", ["prefix", "tools", "provider", "model"])
def test_stale_observation_uses_full_estimate(change: str) -> None:
    prefix = (Message(role="user", content="hello"),)
    tools = (ToolSpec(name="read", description="Read", input_schema={"type": "object"}),)
    observation = observe_context(prefix, tools, provider="test", model="model", input_tokens=10)
    messages = (Message(role="user", content="changed"),) if change == "prefix" else prefix
    active_tools = (
        (ToolSpec(name="grep", description="Search", input_schema={"type": "object"}),)
        if change == "tools"
        else tools
    )
    provider = "other" if change == "provider" else "test"
    model = "other" if change == "model" else "model"

    budget = estimate_context_budget(
        messages,
        active_tools,
        context_window=100,
        reserve_tokens=10,
        observation=observation,
        provider=provider,
        model=model,
    )

    assert budget.observed_is_current is False
    assert budget.effective_tokens == budget.estimate.total_tokens
    assert budget.accounting_method == "fully_estimated"


@pytest.mark.parametrize(
    "content",
    ["plain ASCII", "漢字かな", "👩‍💻🚀", "e\u0301", "ASCII と emoji 🌍"],
    ids=["ascii", "cjk", "emoji-zwj", "combining-mark", "mixed"],
)
def test_messages_use_utf8_size(content: str) -> None:
    payload = [{"role": "user", "content": content}]

    first = estimate_context((Message(role="user", content=content),))
    second = estimate_context((Message(role="user", content=content),))

    assert first == second
    assert first.message_tokens == _serialized_tokens(payload)
    assert first.message_tokens >= math.ceil(
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)) / 4
    )


def test_estimate_and_fingerprint_escape_lone_surrogates() -> None:
    content = "invalid surrogate: \ud800"
    payload = [{"role": "user", "content": content}]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    expected_tokens = math.ceil(len(serialized.encode("utf-8", errors="backslashreplace")) / 4)
    message = Message(role="user", content=content)

    estimate = estimate_context((message,))
    first_fingerprint = context_fingerprint((message,))

    assert estimate.message_tokens == expected_tokens
    assert first_fingerprint == context_fingerprint((message,))


def test_every_payload_uses_utf8_size() -> None:
    messages = (
        Message(role="system", content="系统 🌐"),
        Message(role="user", content="質問 e\u0301"),
        Message(role="tool", content="結果 ✅", tool_call_id="呼出"),
    )
    tool = ToolSpec(
        name="検索",
        description="探す 🔎",
        input_schema={"type": "object", "properties": {"値": {"type": "string"}}},
    )

    estimate = estimate_context(messages, (tool,))

    assert estimate.system_tokens == _serialized_tokens([{"role": "system", "content": "系统 🌐"}])
    assert estimate.message_tokens == _serialized_tokens(
        [
            {"role": "user", "content": "質問 e\u0301"},
            {"role": "tool", "content": "結果 ✅", "tool_call_id": "呼出"},
        ]
    )
    assert estimate.tool_schema_tokens == _serialized_tokens(
        [
            {
                "name": "検索",
                "description": "探す 🔎",
                "input_schema": {
                    "type": "object",
                    "properties": {"値": {"type": "string"}},
                },
            }
        ]
    )
    assert estimate.total_tokens == (
        estimate.system_tokens + estimate.message_tokens + estimate.tool_schema_tokens
    )


def test_native_output_keeps_fingerprint() -> None:
    """Rows reloaded with saved provider items keep the same context identity."""

    plain = Message(role="assistant", content="answer", response_id="response-1")
    native = plain.model_copy(
        update={
            "native_output": NativeOutput(
                provider="openai-codex",
                model="gpt-test",
                items=({"type": "reasoning", "encrypted_content": "e"},),
            )
        }
    )

    assert context_fingerprint([native]) == context_fingerprint([plain])


def test_unicode_context_fingerprint_remains_compatible() -> None:
    messages = (
        Message(role="system", content="系统"),
        Message(role="user", content="café 👩‍💻 e\u0301"),
    )
    tools = (
        ToolSpec(
            name="検索",
            description="探す 🔎",
            input_schema={"type": "object", "properties": {"値": {"type": "string"}}},
        ),
    )

    assert context_fingerprint(messages, tools) == (
        "6b0bd768cecdf716c02766c3fdc84a68853b1b9cdc86b49355caba3980e1c2cd"
    )


def test_allows_unknown_models_and_tracks_reserve() -> None:
    estimate = estimate_context((Message(role="user", content="x" * 400),))

    unknown = build_context_budget(estimate, context_window=None, reserve_tokens=16)
    constrained = build_context_budget(estimate, context_window=100, reserve_tokens=16)

    assert unknown.context_window is None
    assert unknown.remaining_tokens is None
    assert unknown.over_budget is None
    assert constrained.remaining_tokens == 100 - 16 - estimate.total_tokens
    assert constrained.over_budget is (constrained.remaining_tokens <= 0)
