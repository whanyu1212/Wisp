from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import anyio
import httpx
import pytest

from tests.providers.openai_codex_support import (
    StubOpenAICodexProvider,
    completed_event,
    store_with_oauth,
)
from wisp.agent.harness import AgentHarness, AgentHarnessConfig
from wisp.coding import CodingSession
from wisp.events import ToolCallSnapshot, ToolExecutionEnded
from wisp.messages import Message, NativeOutput
from wisp.providers.auth import StoredProviderAuthResolver
from wisp.providers.base import (
    ProviderProtocolError,
    ToolCall,
    ToolCallResult,
    ToolSpec,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.openai_codex import OpenAICodexProvider
from wisp.retry import RetryPolicy
from wisp.sessions import JsonlSessionStore, MessageSessionEntry
from wisp.tools.context import ToolContext


def test_openai_codex_provider_replays_active_turn_tool_call_from_history(
    tmp_path: Path,
) -> None:
    """A structured assistant ``tool_calls`` row (plus its paired ``tool`` result)
    passed in ``messages`` — not carried via ``previous_response_id`` continuation —
    must serialize to native ``function_call``/``function_call_output`` items.

    This is the shape Wisp sends after a mid-turn transcript rebuild: the Responses
    API `store: false` continuation chain does not survive a fresh
    ``run_agent_loop`` call (each call starts ``previous_response_id=None``), so an
    in-flight tool round from the turn currently running has to be replayed through
    plain message history instead. Losing the structure here means the model has no
    record of having made the call and repeats it.
    """

    store = store_with_oauth(tmp_path)
    provider = StubOpenAICodexProvider(
        [completed_event("response-id")],
        auth_resolver=StoredProviderAuthResolver(store),
    )

    history = [
        Message(role="user", content="search"),
        Message(
            role="assistant",
            content="checking",
            tool_calls=(
                ToolCallSnapshot(call_id="call-1", name="lookup", arguments={"query": "wisp"}),
            ),
        ),
        Message(role="tool", content="found it", tool_name="lookup", tool_call_id="call-1"),
    ]

    async def run() -> None:
        _events = [event async for event in provider.stream(history)]

    anyio.run(run)

    assert provider.seen_body is not None
    assert provider.seen_body["input"] == [
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": "checking"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"query":"wisp"}',
        },
        {"type": "function_call_output", "call_id": "call-1", "output": "found it"},
    ]


def test_openai_codex_provider_preserves_continuation_after_opening_failure(
    tmp_path: Path,
) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"query":"wisp"}',
                },
            },
            completed_event("response-1"),
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
        retry_policy=RetryPolicy(max_retries=0),
    )
    messages = [Message(role="user", content="hi")]
    tool_result = ToolCallResult(call_id="call-1", output="found")

    async def run() -> None:
        async for _event in provider.stream(messages, prompt_cache_key="wisp:session-1"):
            pass

        provider.opening_errors.append(httpx.ConnectError("temporary failure"))
        failed = [
            event
            async for event in provider.stream(
                messages,
                tool_results=[tool_result],
                previous_response_id="response-1",
                prompt_cache_key="wisp:session-1",
            )
        ]
        assert len(failed) == 1
        assert isinstance(failed[0], ProviderResponseFailed)

        provider.events = [
            {"type": "response.created", "response": {"id": "response-2"}},
            {"type": "response.output_text.delta", "delta": "recovered"},
            completed_event("response-2"),
        ]
        assert [
            event
            async for event in provider.stream(
                messages,
                tool_results=[tool_result],
                previous_response_id="response-1",
                prompt_cache_key="wisp:session-1",
            )
        ] == [
            ProviderResponseStarted(model="gpt-test"),
            ProviderTextDelta(delta="recovered"),
            ProviderResponseCompleted(content="recovered", response_id="response-2"),
        ]

        with pytest.raises(
            ProviderProtocolError,
            match="continuation state is unavailable for response-1",
        ):
            async for _event in provider.stream(
                messages,
                tool_results=[tool_result],
                previous_response_id="response-1",
            ):
                pass

    anyio.run(run)

    failed_body, retry_body = provider.seen_bodies[-2:]
    assert failed_body == retry_body
    assert "previous_response_id" not in retry_body
    assert retry_body["prompt_cache_key"] == "wisp:session-1"


def test_openai_codex_provider_preserves_continuation_after_context_overflow(
    tmp_path: Path,
) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": "{}",
                },
            },
            completed_event("response-1"),
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    messages = [Message(role="user", content="hi")]

    async def run() -> list[object]:
        async for _event in provider.stream(messages):
            pass
        provider.events = [{"type": "error", "message": "maximum context length exceeded"}]
        return [
            event
            async for event in provider.stream(
                messages,
                tool_results=[ToolCallResult(call_id="call-1", output="found")],
                previous_response_id="response-1",
            )
        ]

    events = anyio.run(run)

    assert isinstance(events[-1], ProviderResponseFailed)
    assert provider._continuations.get("response-1") is not None  # noqa: SLF001


def test_openai_codex_provider_replays_multi_round_tool_history(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        [
            {"type": "response.created", "response": {"id": "response-1"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "reasoning-1",
                    "encrypted_content": "encrypted-1",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-1",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"query":"first"}',
                },
            },
            completed_event("response-1"),
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    messages = [Message(role="user", content="hi")]

    async def run() -> None:
        async for _event in provider.stream(messages):
            pass

        provider.events = [
            {"type": "response.created", "response": {"id": "response-2"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "reasoning-2",
                    "encrypted_content": "encrypted-2",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item-2",
                    "call_id": "call-2",
                    "name": "lookup",
                    "arguments": '{"query":"second"}',
                },
            },
            completed_event("response-2"),
        ]
        async for _event in provider.stream(
            messages,
            tool_results=[ToolCallResult(call_id="call-1", output="first result")],
            previous_response_id="response-1",
        ):
            pass

        provider.events = [
            {"type": "response.created", "response": {"id": "response-3"}},
            {"type": "response.output_text.delta", "delta": "done"},
            completed_event("response-3"),
        ]
        async for _event in provider.stream(
            messages,
            tool_results=[ToolCallResult(call_id="call-2", output="second result")],
            previous_response_id="response-2",
        ):
            pass

    anyio.run(run)

    assert provider.seen_bodies[-1]["input"] == [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "encrypted_content": "encrypted-1"},
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "lookup",
            "arguments": '{"query":"first"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "first result",
        },
        {"type": "reasoning", "encrypted_content": "encrypted-2"},
        {
            "type": "function_call",
            "call_id": "call-2",
            "name": "lookup",
            "arguments": '{"query":"second"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-2",
            "output": "second result",
        },
    ]
    assert all("previous_response_id" not in body for body in provider.seen_bodies)


class _ScriptedCodexProvider(StubOpenAICodexProvider):
    """Serve one scripted response per request, in request order."""

    def __init__(
        self,
        responses: Sequence[Sequence[dict[str, object]]],
        *,
        auth_resolver: StoredProviderAuthResolver,
    ) -> None:
        super().__init__((), auth_resolver=auth_resolver)
        self.responses = [list(response) for response in responses]

    @asynccontextmanager
    async def _create_stream(
        self,
        *,
        body: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> AsyncIterator[AsyncIterator[dict[str, object]]]:
        self.events = self.responses.pop(0)
        async with super()._create_stream(body=body, headers=headers) as stream:
            yield stream


class _EchoToolExecutor:
    async def execute(self, tool_call: ToolCall) -> AsyncIterator[ToolExecutionEnded]:
        yield ToolExecutionEnded(
            call_id=tool_call.call_id,
            name=tool_call.name,
            output=f"result for {tool_call.call_id}",
            is_error=False,
        )


def _message_item(item_id: str, text: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": item_id,
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _text_response(response_id: str, text: str) -> list[dict[str, object]]:
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": f"rs-{response_id}", "encrypted_content": "e"},
        },
        {"type": "response.output_text.delta", "delta": text},
        {"type": "response.output_item.done", "item": _message_item(f"msg-{response_id}", text)},
        completed_event(response_id),
    ]


def test_second_prompt_resends_the_first_prompts_native_output(tmp_path: Path) -> None:
    """The next prompt's input starts with the previous run's final request input."""

    provider = _ScriptedCodexProvider(
        [
            [
                {"type": "response.created", "response": {"id": "response-1"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "reasoning",
                        "id": "rs-1",
                        "summary": [],
                        "encrypted_content": "encrypted-1",
                    },
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": "fc-1",
                        "status": "completed",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": '{"query":"first"}',
                    },
                },
                completed_event("response-1"),
            ],
            _text_response("response-2", "found it"),
            _text_response("response-3", "again"),
        ],
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    harness = AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            tool_executor=_EchoToolExecutor(),
            tools=(ToolSpec(name="lookup", description="Look up", input_schema={}),),
        )
    )

    async def run() -> None:
        async for _event in harness.prompt("first question"):
            pass
        async for _event in harness.prompt("second question"):
            pass

    anyio.run(run)

    first_run_final_input = cast(list[object], provider.seen_bodies[1]["input"])
    second_run_input = cast(list[object], provider.seen_bodies[2]["input"])
    final_response_items = [
        {"type": "reasoning", "encrypted_content": "e"},
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "found it", "annotations": []}],
        },
    ]
    assert second_run_input == [
        *first_run_final_input,
        *final_response_items,
        {"role": "user", "content": "second question"},
    ]
    assert {"type": "reasoning", "summary": [], "encrypted_content": "encrypted-1"} in (
        second_run_input
    )


def test_native_output_is_replayed_only_for_the_model_that_produced_it(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        _text_response("response-1", "hello"),
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    history = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello", response_id="response-1"),
        Message(role="user", content="next"),
    ]

    async def run() -> None:
        async for _event in provider.stream(history[:1], model="gpt-a"):
            pass
        provider.events = _text_response("response-2", "ok")
        async for _event in provider.stream(history, model="gpt-a"):
            pass
        provider.events = _text_response("response-3", "ok")
        async for _event in provider.stream(history, model="gpt-b"):
            pass

    anyio.run(run)

    same_model, other_model = (body["input"] for body in provider.seen_bodies[1:])
    assert same_model == [
        {"role": "user", "content": "hi"},
        {"type": "reasoning", "encrypted_content": "e"},
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "hello", "annotations": []}],
        },
        {"role": "user", "content": "next"},
    ]
    assert other_model == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]


def test_rows_without_a_recorded_response_keep_their_portable_form(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        _text_response("response-2", "ok"),
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    history = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="from a restart", response_id="response-unknown"),
        Message(role="user", content="next"),
    ]

    async def run() -> None:
        async for _event in provider.stream(history):
            pass

    anyio.run(run)

    assert provider.seen_bodies[0]["input"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "from a restart"},
        {"role": "user", "content": "next"},
    ]


def test_a_restarted_session_resends_the_saved_native_output(tmp_path: Path) -> None:
    """Items saved with the session replay from a new provider instance."""

    auth = StoredProviderAuthResolver(store_with_oauth(tmp_path))
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()

    def process(provider: OpenAICodexProvider) -> CodingSession:
        # A fresh provider and CodingSession stand in for a new process.
        return CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tool_context=ToolContext(cwd=tmp_path),
        )

    before = _ScriptedCodexProvider(
        [_text_response("response-1", "first answer")], auth_resolver=auth
    )
    after = _ScriptedCodexProvider(
        [_text_response("response-2", "second answer")], auth_resolver=auth
    )

    async def run() -> None:
        _ = [event async for event in process(before).run("first", session=session)]
        resumed = JsonlSessionStore(tmp_path / "sessions").load(session.session_id)
        _ = [event async for event in process(after).run("second", session=resumed)]

    anyio.run(run)

    saved = [
        entry.message
        for entry in store.load(session.session_id).read_entries()
        if isinstance(entry, MessageSessionEntry) and entry.message.response_id == "response-1"
    ]
    assert saved[0].native_output is not None
    assert saved[0].native_output.provider == "openai-codex"
    assert after.seen_bodies[0]["input"] == [
        *cast(list[object], before.seen_bodies[0]["input"]),
        {"type": "reasoning", "encrypted_content": "e"},
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "first answer", "annotations": []}],
        },
        {"role": "user", "content": "second"},
    ]


def test_saved_native_output_replays_only_for_its_provider_and_model(tmp_path: Path) -> None:
    provider = StubOpenAICodexProvider(
        _text_response("response-2", "ok"),
        auth_resolver=StoredProviderAuthResolver(store_with_oauth(tmp_path)),
    )
    items: tuple[dict[str, object], ...] = (
        {"type": "reasoning", "encrypted_content": "saved"},
        _message_item("msg-1", "hello"),
    )

    def history(native: NativeOutput) -> list[Message]:
        return [
            Message(role="user", content="hi"),
            Message(
                role="assistant", content="hello", response_id="response-1", native_output=native
            ),
            Message(role="user", content="next"),
        ]

    async def run() -> None:
        for native in (
            NativeOutput(provider="openai-codex", model="gpt-test", items=items),
            NativeOutput(provider="openai-codex", model="other-model", items=items),
            NativeOutput(provider="other-provider", model="gpt-test", items=items),
        ):
            provider.events = _text_response("response-2", "ok")
            async for _event in provider.stream(history(native)):
                pass

    anyio.run(run)

    replayed, other_model, other_provider = (body["input"] for body in provider.seen_bodies)
    assert replayed == [
        {"role": "user", "content": "hi"},
        *items,
        {"role": "user", "content": "next"},
    ]
    portable = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "next"},
    ]
    assert other_model == portable
    assert other_provider == portable
