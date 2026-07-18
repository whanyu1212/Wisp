from __future__ import annotations

import pytest
from anthropic.types import (
    Message,
    MessageDeltaUsage,
    OutputTokensDetails,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
from google.genai import types as genai_types
from openai.types.responses import Response
from openai.types.responses.response_usage import ResponseUsage
from pydantic import ValidationError

from wisp.events import MessageCompleted, TokenUsage, wisp_event_from_json
from wisp.providers.anthropic import (
    _usage_from_anthropic_delta,
    _usage_from_anthropic_start,
)
from wisp.providers.events import ProviderUsage
from wisp.providers.google import _usage_from_google
from wisp.providers.openai import _usage_from_openai
from wisp.providers.openai_codex import _usage_from_codex


def test_openai_usage_preserves_cache_and_reasoning_details() -> None:
    usage = ResponseUsage.model_validate(
        {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 19,
        }
    )
    response = Response.model_construct(usage=usage)

    assert _usage_from_openai(response) == ProviderUsage(
        input_tokens=12,
        output_tokens=7,
        total_tokens=19,
        cache_read_input_tokens=4,
        reasoning_output_tokens=3,
    )


def test_openai_usage_allows_missing_optional_details() -> None:
    usage = ResponseUsage.model_construct(
        input_tokens=12,
        input_tokens_details=None,
        output_tokens=7,
        output_tokens_details=None,
        total_tokens=19,
    )
    response = Response.model_construct(usage=usage)

    assert _usage_from_openai(response) == ProviderUsage(
        input_tokens=12,
        output_tokens=7,
        total_tokens=19,
    )


def test_codex_usage_validates_required_counts_and_preserves_details() -> None:
    assert _usage_from_codex(
        {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 3},
            "total_tokens": 19,
        }
    ) == ProviderUsage(
        input_tokens=12,
        output_tokens=7,
        total_tokens=19,
        cache_read_input_tokens=4,
        reasoning_output_tokens=3,
    )
    assert _usage_from_codex({"input_tokens": 12}) is None


def test_anthropic_usage_combines_start_and_terminal_delta() -> None:
    start = RawMessageStartEvent(
        message=Message(
            id="response-id",
            content=[],
            model="claude-test",
            role="assistant",
            type="message",
            usage=Usage(
                input_tokens=50,
                output_tokens=0,
                cache_read_input_tokens=100_000,
                cache_creation_input_tokens=2_000,
                output_tokens_details=OutputTokensDetails(thinking_tokens=0),
            ),
        ),
        type="message_start",
    )
    delta = RawMessageDeltaEvent(
        delta=Delta.model_construct(stop_reason="end_turn"),
        type="message_delta",
        usage=MessageDeltaUsage(
            input_tokens=50,
            output_tokens=7,
            cache_read_input_tokens=100_000,
            cache_creation_input_tokens=2_000,
            output_tokens_details=OutputTokensDetails(thinking_tokens=5),
        ),
    )

    initial = _usage_from_anthropic_start(start)
    assert initial.total_tokens == 102_050
    completed = _usage_from_anthropic_delta(delta, initial)
    assert completed == ProviderUsage(
        input_tokens=50,
        output_tokens=7,
        total_tokens=102_057,
        cache_read_input_tokens=100_000,
        cache_write_input_tokens=2_000,
        reasoning_output_tokens=5,
    )

    sparse_delta = RawMessageDeltaEvent(
        delta=Delta.model_construct(stop_reason="end_turn"),
        type="message_delta",
        usage=MessageDeltaUsage(output_tokens=9),
    )
    assert _usage_from_anthropic_delta(sparse_delta, completed) == ProviderUsage(
        input_tokens=50,
        output_tokens=9,
        total_tokens=102_059,
        cache_read_input_tokens=100_000,
        cache_write_input_tokens=2_000,
        reasoning_output_tokens=5,
    )


def test_google_usage_preserves_authoritative_total_and_optional_details() -> None:
    metadata = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=12,
        candidates_token_count=7,
        total_token_count=22,
        cached_content_token_count=4,
        thoughts_token_count=3,
    )

    assert _usage_from_google(metadata) == ProviderUsage(
        input_tokens=12,
        output_tokens=7,
        total_tokens=22,
        cache_read_input_tokens=4,
        reasoning_output_tokens=3,
    )


def test_google_usage_preserves_thinking_only_response_without_candidates() -> None:
    metadata = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=12,
        candidates_token_count=None,
        thoughts_token_count=3,
        total_token_count=15,
    )

    assert _usage_from_google(metadata) == ProviderUsage(
        input_tokens=12,
        output_tokens=0,
        total_tokens=15,
        reasoning_output_tokens=3,
    )


def test_google_usage_counts_tool_results_as_input() -> None:
    metadata = genai_types.GenerateContentResponseUsageMetadata(
        prompt_token_count=12,
        tool_use_prompt_token_count=5,
        candidates_token_count=7,
        thoughts_token_count=3,
        total_token_count=27,
    )

    assert _usage_from_google(metadata) == ProviderUsage(
        input_tokens=17,
        output_tokens=7,
        total_tokens=27,
        reasoning_output_tokens=3,
    )


def test_token_usage_round_trips_on_current_schema_events() -> None:
    event = MessageCompleted(
        turn=1,
        content="done",
        finish_reason="stop",
        usage=TokenUsage(input_tokens=12, output_tokens=7, total_tokens=19),
    )

    assert event.schema_version == 8
    assert wisp_event_from_json(event.model_dump_json()) == event


def test_schema_v5_events_remain_readable() -> None:
    event = MessageCompleted(
        schema_version=5,
        turn=1,
        content="done",
        finish_reason="stop",
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_schema_v6_events_remain_readable() -> None:
    event = MessageCompleted(
        schema_version=6,
        turn=1,
        content="done",
        finish_reason="stop",
    )

    assert wisp_event_from_json(event.model_dump_json()) == event


def test_provider_adapters_normalize_negative_counts() -> None:
    assert _usage_from_codex(
        {
            "input_tokens": -12,
            "input_tokens_details": {"cached_tokens": -4},
            "output_tokens": -7,
            "output_tokens_details": {"reasoning_tokens": -3},
            "total_tokens": -19,
        }
    ) == ProviderUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_read_input_tokens=0,
        reasoning_output_tokens=0,
    )

    metadata = genai_types.GenerateContentResponseUsageMetadata.model_construct(
        prompt_token_count=-12,
        candidates_token_count=-7,
        total_token_count=-19,
        cached_content_token_count=-4,
        thoughts_token_count=-3,
    )
    assert _usage_from_google(metadata) == ProviderUsage(
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cache_read_input_tokens=0,
        reasoning_output_tokens=0,
    )


def test_token_usage_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1, output_tokens=0, total_tokens=0)
