from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import anyio
import httpx
import pytest
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pytest import MonkeyPatch

from wisp.agent.messages import Message as WispMessage
from wisp.providers.base import (
    ProviderConfigurationError,
    ToolCall,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderRetrying,
    ProviderTextDelta,
    ProviderThinkingDelta,
    ProviderToolCallCompleted,
)
from wisp.providers.google import GoogleProvider
from wisp.retry import RetryPolicy


class StubGoogleProvider(GoogleProvider):
    def __init__(self, chunks: Sequence[genai_types.GenerateContentResponse]) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")
        self.chunks = chunks
        self.seen_model: str | None = None
        self.seen_messages: Sequence[WispMessage] | None = None
        self.seen_tools: Sequence[ToolSpec] | None = None
        self.seen_tool_results: Sequence[ToolCallResult] | None = None
        self.seen_effort: str | None = None

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        self.seen_model = model
        self.seen_messages = messages
        self.seen_tools = tools
        self.seen_tool_results = tool_results
        self.seen_effort = effort

        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            for chunk in self.chunks:
                yield chunk

        return stream()


class FailingGoogleProvider(GoogleProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test-key", default_model="default-test-model")

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            yield _text_chunk("partial")
            raise httpx.ConnectError("boom")

        return stream()


class FlakyGoogleProvider(GoogleProvider):
    def __init__(self, failures: int, *, retry_policy: RetryPolicy) -> None:
        super().__init__(
            api_key="test-key",
            default_model="default-test-model",
            retry_policy=retry_policy,
        )
        self.failures = failures
        self.attempts = 0

    async def _create_stream(
        self,
        messages: Sequence[WispMessage],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise httpx.ConnectError("boom")

        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            yield _text_chunk("recovered", finish_reason=genai_types.FinishReason.STOP)

        return stream()


def test_google_provider_streams_text_deltas() -> None:
    provider = StubGoogleProvider(
        [
            _text_chunk("hello", response_id="response-id"),
            _text_chunk(" world", finish_reason=genai_types.FinishReason.STOP),
        ]
    )
    messages = [WispMessage(role="user", content="Say hello")]

    async def run() -> list[object]:
        return [event async for event in provider.stream(messages, model="gemini-test")]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="gemini-test"),
        ProviderTextDelta(delta="hello"),
        ProviderTextDelta(delta=" world", content_index=1),
        ProviderResponseCompleted(content="hello world", response_id="response-id"),
    ]
    assert provider.seen_model == "gemini-test"
    assert provider.seen_messages == messages


def test_google_provider_uses_default_model_when_model_is_not_provided() -> None:
    provider = StubGoogleProvider(
        [_text_chunk("hello", finish_reason=genai_types.FinishReason.STOP)]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="hello"),
        ProviderResponseCompleted(content="hello"),
    ]
    assert provider.seen_model == "default-test-model"


def test_google_provider_streams_thinking_deltas() -> None:
    provider = StubGoogleProvider(
        [
            _part_chunk(genai_types.Part(text="let me think", thought=True)),
            _text_chunk("the answer", finish_reason=genai_types.FinishReason.STOP),
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    assert anyio.run(run) == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderThinkingDelta(delta="let me think", content_index=0),
        ProviderTextDelta(delta="the answer", content_index=1),
        ProviderResponseCompleted(content="the answer"),
    ]


def test_google_provider_streams_tool_calls() -> None:
    provider = StubGoogleProvider(
        [
            _part_chunk(
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id="call-id", name="lookup", args={"query": "wisp"}
                    )
                ),
                response_id="response-id",
                finish_reason=genai_types.FinishReason.STOP,
            )
        ]
    )
    tool = ToolSpec(
        name="lookup",
        description="Look something up.",
        input_schema={"type": "object", "properties": {}},
    )

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="hello")],
                tools=[tool],
            )
        ]

    events = anyio.run(run)

    tool_call = ToolCall(
        call_id="call-id",
        name="lookup",
        arguments={"query": "wisp"},
        raw_arguments='{"query": "wisp"}',
        response_id="response-id",
    )
    assert events == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderToolCallCompleted(tool_call=tool_call),
        ProviderResponseCompleted(
            content="",
            tool_calls=(tool_call,),
            response_id="response-id",
            finish_reason="tool_calls",
        ),
    ]
    assert provider.seen_tools == [tool]


def test_google_provider_generates_unique_fallback_ids_for_parallel_calls_without_an_id() -> None:
    # Regression test: confirmed live against gemini-2.5-flash that a
    # parallel response with two calls to the same tool both return
    # function_call.id == None. Falling back to a name-only id would
    # collapse both calls to the same ToolCall.call_id, corrupting
    # parallel tool execution and functionResponse matching on replay.
    provider = StubGoogleProvider(
        [
            _parts_chunk(
                [
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name="lookup", args={"query": "wisp"}
                        )
                    ),
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(name="lookup", args={"query": "fog"})
                    ),
                ],
                response_id="response-id",
                finish_reason=genai_types.FinishReason.STOP,
            )
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    call_ids = [call.call_id for call in completed.tool_calls]
    assert len(call_ids) == 2
    assert len(set(call_ids)) == 2


def test_google_provider_does_not_execute_a_tool_call_truncated_by_max_tokens() -> None:
    # Regression test, mirroring the same fix applied to AnthropicProvider: a
    # function_call part that arrived before Gemini hit MAX_TOKENS must never
    # be surfaced as an executable tool call -- only finish_reason == STOP
    # means Gemini actually finished the turn with that tool call.
    provider = StubGoogleProvider(
        [
            _part_chunk(
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id="call-id", name="lookup", args={"query": "wisp"}
                    )
                ),
                response_id="response-id",
                finish_reason=genai_types.FinishReason.MAX_TOKENS,
            )
        ]
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert events == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderResponseCompleted(
            content="",
            response_id="response-id",
            finish_reason="length",
        ),
    ]


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        (genai_types.FinishReason.STOP, "stop"),
        (genai_types.FinishReason.MAX_TOKENS, "length"),
        (genai_types.FinishReason.SAFETY, "length"),
        (genai_types.FinishReason.RECITATION, "length"),
    ],
)
def test_google_provider_maps_finish_reasons(
    finish_reason: genai_types.FinishReason, expected: str
) -> None:
    provider = StubGoogleProvider([_text_chunk("hi", finish_reason=finish_reason)])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    completed = events[-1]
    assert isinstance(completed, ProviderResponseCompleted)
    assert completed.finish_reason == expected


def test_google_provider_emits_failed_terminal_on_stream_error() -> None:
    provider = FailingGoogleProvider()

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert events[:2] == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="partial"),
    ]
    assert isinstance(events[2], ProviderResponseFailed)
    assert events[2].partial_content == "partial"


def test_google_provider_emits_failed_terminal_when_stream_ends_without_finish_reason() -> None:
    # Regression guard, mirroring AnthropicProvider's equivalent: the stream
    # ended (cleanly or not) without Gemini ever reporting a finish_reason.
    # Silently completing here would report a truncated answer as success.
    provider = StubGoogleProvider([_text_chunk("partial", response_id="response-id")])

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)
    assert isinstance(events[-1], ProviderResponseFailed)
    assert events[-1].partial_content == "partial"


def test_google_provider_retries_request_opening_failure_before_start() -> None:
    provider = FlakyGoogleProvider(
        1,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    events = anyio.run(run)

    assert provider.attempts == 2
    assert isinstance(events[0], ProviderRetrying)
    assert events[0].attempt == 2
    assert events[0].max_attempts == 2
    assert events[0].reason == "network"
    assert events[1:] == [
        ProviderResponseStarted(model="default-test-model"),
        ProviderTextDelta(delta="recovered"),
        ProviderResponseCompleted(content="recovered"),
    ]


def test_google_provider_raises_after_exhausting_opening_retries() -> None:
    provider = FlakyGoogleProvider(
        2,
        retry_policy=RetryPolicy(max_retries=1, base_delay_seconds=0.0001, max_delay_seconds=1),
    )

    async def run() -> list[object]:
        events: list[object] = []
        with pytest.raises(httpx.ConnectError):
            async for event in provider.stream([WispMessage(role="user", content="hello")]):
                events.append(event)
        return events

    events = anyio.run(run)

    assert provider.attempts == 2
    assert len(events) == 1
    assert isinstance(events[0], ProviderRetrying)


def test_wisp_owned_google_client_leaves_sdk_retries_unset() -> None:
    # google-genai defaults retry_options to None, which its own retry_args()
    # resolves to "never retry" (stop_after_attempt(1)) -- Wisp owns retries
    # via RetryPolicy, so no explicit opt-out is needed, unlike the
    # max_retries=0 construction AnthropicProvider/OpenAIProvider require.
    provider = GoogleProvider(api_key="test-key")

    client = provider._client_or_create()  # noqa: SLF001

    assert client._api_client._http_options.retry_options is None  # noqa: SLF001


def test_google_provider_requires_api_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GoogleProvider()

    async def run() -> list[object]:
        return [
            event async for event in provider.stream([WispMessage(role="user", content="hello")])
        ]

    with pytest.raises(
        ProviderConfigurationError, match="GOOGLE_API_KEY or GEMINI_API_KEY is required"
    ):
        anyio.run(run)


def test_google_provider_falls_back_to_gemini_api_key(monkeypatch: MonkeyPatch) -> None:
    # Regression test: mirrors google.genai's own env-var resolution order
    # (get_env_api_key) -- a user who already has Gemini configured with only
    # GEMINI_API_KEY should not be rejected before the SDK ever sees it.
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-key")
    provider = GoogleProvider()

    client = provider._client_or_create()  # noqa: SLF001

    assert client._api_client.api_key == "gemini-env-key"  # noqa: SLF001


def test_google_provider_prefers_google_api_key_over_gemini_api_key(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "google-env-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env-key")
    provider = GoogleProvider()

    client = provider._client_or_create()  # noqa: SLF001

    assert client._api_client.api_key == "google-env-key"  # noqa: SLF001


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
    provider = StubGoogleProvider([_text_chunk("hi", finish_reason=genai_types.FinishReason.STOP)])

    async def run() -> list[object]:
        return [
            event
            async for event in provider.stream(
                [WispMessage(role="user", content="hello")], effort="HIGH"
            )
        ]

    anyio.run(run)

    assert provider.seen_effort == "HIGH"


def test_google_provider_replays_model_turn_before_tool_results() -> None:
    # Regression test: Gemini requires the model-role turn that produced a
    # tool call (including its thought_signature parts) to be resent ahead
    # of the tool's functionResponse -- but AgentHarness's `messages` never
    # grows across tool rounds, so GoogleProvider must reconstruct it from
    # its own replay cache, exactly like AnthropicProvider's _replays.
    stub_models = StubModels(
        responses=[
            [
                _part_chunk(
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
                _part_chunk(
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


class StubGenaiClient:
    def __init__(self, models: StubModels) -> None:
        self.aio = _StubAio(models)


class _StubAio:
    def __init__(self, models: StubModels) -> None:
        self.models = models


class StubModels:
    def __init__(
        self, responses: Sequence[Sequence[genai_types.GenerateContentResponse]] | None = None
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses) if responses is not None else None

    async def generate_content_stream(
        self, **kwargs: object
    ) -> AsyncIterator[genai_types.GenerateContentResponse]:
        self.calls.append(dict(kwargs))
        chunks = self._responses.pop(0) if self._responses else ()

        async def stream() -> AsyncIterator[genai_types.GenerateContentResponse]:
            for chunk in chunks:
                yield chunk

        return stream()


def _text_chunk(
    text: str,
    *,
    response_id: str | None = None,
    finish_reason: genai_types.FinishReason | None = None,
) -> genai_types.GenerateContentResponse:
    return _part_chunk(
        genai_types.Part(text=text), response_id=response_id, finish_reason=finish_reason
    )


def _part_chunk(
    part: genai_types.Part,
    *,
    response_id: str | None = None,
    finish_reason: genai_types.FinishReason | None = None,
) -> genai_types.GenerateContentResponse:
    return _parts_chunk([part], response_id=response_id, finish_reason=finish_reason)


def _parts_chunk(
    parts: Sequence[genai_types.Part],
    *,
    response_id: str | None = None,
    finish_reason: genai_types.FinishReason | None = None,
) -> genai_types.GenerateContentResponse:
    return genai_types.GenerateContentResponse(
        response_id=response_id,
        candidates=[
            genai_types.Candidate(
                content=genai_types.Content(role="model", parts=list(parts)),
                finish_reason=finish_reason,
            )
        ],
    )


def test_google_retry_decision_classifies_client_and_server_errors() -> None:
    from wisp.providers.google import _google_retry_decision

    timeout_decision = _google_retry_decision(httpx.TimeoutException("slow"))
    assert timeout_decision is not None
    assert timeout_decision.reason == "timeout"

    connect_decision = _google_retry_decision(httpx.ConnectError("down"))
    assert connect_decision is not None
    assert connect_decision.reason == "network"

    server_error = genai_errors.ServerError(
        503,
        {"error": {"message": "unavailable", "status": "UNAVAILABLE"}},
        response=httpx.Response(503, headers={}, request=httpx.Request("POST", "https://x")),
    )
    server_decision = _google_retry_decision(server_error)
    assert server_decision is not None
    assert server_decision.reason == "server_error"

    client_error = genai_errors.ClientError(
        400,
        {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}},
        response=httpx.Response(400, headers={}, request=httpx.Request("POST", "https://x")),
    )
    assert _google_retry_decision(client_error) is None
