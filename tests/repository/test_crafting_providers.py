"""Completion-before-dispatch and retry ownership for the chapter 4 adapter."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from examples.crafting_agents.checkpoint_01 import BUGGY_SOURCE
from examples.crafting_agents.checkpoint_03 import TOOLS, ContextTools, create_context_fixture
from examples.crafting_agents.checkpoint_04 import LiveFixtureTools, main, run_checkpoint
from examples.crafting_agents.core import Message, ToolCall, run_agent
from examples.crafting_agents.openai_transport import OpenAITransport
from examples.crafting_agents.responses import (
    EventStream,
    OpenFailure,
    Payload,
    ResponseFailure,
    ResponsesProvider,
    build_request,
)
from examples.crafting_agents.stream_replay import (
    ReplayStream,
    ReplayTransport,
    ReplayTurn,
    no_wait,
    response_events,
    scenario_transport,
)

EDIT = ToolCall(
    "edit-1", "edit", {"path": "calculator.py", "old": "return a - b", "new": "return a + b"}
)


def test_native_request_preserves_call_result_correlation_and_strict_schemas() -> None:
    request = build_request(
        (
            Message("system", "instructions"),
            Message("user", "fix"),
            Message("assistant", "edit", (EDIT,)),
            Message("tool", "edited", tool_call_id=EDIT.id),
        ),
        TOOLS,
        "fixture-model",
    )
    items = request["input"]
    assert isinstance(items, list)
    assert items[0] == {"role": "system", "content": "instructions"}
    assert items[-2] == {
        "type": "function_call",
        "call_id": "edit-1",
        "name": "edit",
        "arguments": json.dumps(EDIT.arguments),
    }
    assert items[-1] == {"type": "function_call_output", "call_id": "edit-1", "output": "edited"}
    functions = request["tools"]
    assert isinstance(functions, list)
    assert functions[2]["parameters"] == {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ("path", "old", "new")},
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    }
    assert functions[2]["strict"] is True
    assert request["store"] is False
    assert "previous_response_id" not in request


@pytest.mark.process
@pytest.mark.parametrize("scenario", ["repair", "retry"])
def test_offline_streamed_repair_uses_real_fixture_observations(
    tmp_path: Path, scenario: str
) -> None:
    create_context_fixture(tmp_path)
    replay = scenario_transport(scenario)
    trace: list[str] = []
    result = asyncio.run(
        run_checkpoint(
            tmp_path,
            ResponsesProvider(replay.open, sleep=no_wait, report=trace.append),
            TOOLS,
            ContextTools(tmp_path, approve_edits=True).execute,
            report=trace.append,
        )
    )
    assert result.stop_reason == "model_finished"
    assert "return a + b" in (tmp_path / "calculator.py").read_text(encoding="utf-8")
    assert "\nOK" in result.history[-2].content
    assert replay.attempts == (8 if scenario == "retry" else 7)
    assert all(stream.closed for stream in replay.streams)
    assert any(line.startswith("text delta:") for line in trace)
    assert any(line.startswith("arguments buffered:") for line in trace)
    if scenario == "retry":
        assert trace[0] == "retry opening: attempt 2/3"
        assert replay.requests[0] == replay.requests[1]


@pytest.mark.parametrize("scenario", ["disconnect", "malformed", "output-limit"])
def test_failed_response_never_reaches_executor_or_retries(tmp_path: Path, scenario: str) -> None:
    create_context_fixture(tmp_path)
    replay = scenario_transport(scenario)
    called: list[ToolCall] = []

    def execute(call: ToolCall) -> str:
        called.append(call)
        return ContextTools(tmp_path, approve_edits=True).execute(call)

    with pytest.raises(ResponseFailure):
        asyncio.run(
            run_checkpoint(
                tmp_path,
                ResponsesProvider(replay.open, sleep=no_wait, report=lambda _: None),
                TOOLS,
                execute,
                report=lambda _: None,
            )
        )
    assert called == []
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == BUGGY_SOURCE
    assert replay.attempts == 1
    assert replay.streams[0].closed


@pytest.mark.parametrize(
    "bad_call",
    [
        ToolCall("bad", "unknown", {}),
        ToolCall("bad", "edit", {"path": "calculator.py"}),
        ToolCall("bad", "read", {"path": 123}),
        ToolCall("edit-1", "read", {"path": "calculator.py"}),
    ],
)
def test_entire_response_is_validated_before_first_call_runs(bad_call: ToolCall) -> None:
    replay = ReplayTransport(
        (ReplayTurn(response_events(Message("assistant", "", (EDIT, bad_call)))),)
    )
    executed: list[str] = []

    def execute(call: ToolCall) -> str:
        executed.append(call.name)
        return "unexpected"

    with pytest.raises(ResponseFailure):
        asyncio.run(
            run_agent(
                "fix",
                ResponsesProvider(replay.open, report=lambda _: None),
                TOOLS,
                execute,
                report=lambda _: None,
            )
        )
    assert not executed
    assert replay.streams[0].closed


@pytest.mark.parametrize(
    "fault",
    [
        "no-start",
        "duplicate-start",
        "wrong-response",
        "missing-call",
        "changed-call",
        "reasoning",
        "non-object",
    ],
)
def test_inconsistent_or_unsupported_native_response_fails_closed(fault: str) -> None:
    events = list(response_events(Message("assistant", "preview", (EDIT,))))
    response = events[-1]["response"]
    assert isinstance(response, dict)
    if fault == "no-start":
        events.pop(0)
    elif fault == "duplicate-start":
        events.insert(1, events[0])
    elif fault == "wrong-response":
        response["id"] = "other"
    elif fault == "missing-call":
        response["output"] = []
    elif fault == "reasoning":
        response["output"] = [{"type": "reasoning", "id": "reason-1", "summary": []}]
    else:
        output = response["output"]
        assert isinstance(output, list)
        output[-1]["arguments"] = "[]"
        if fault == "non-object":
            deltas = [
                event
                for event in events
                if event["type"] == "response.function_call_arguments.delta"
            ]
            deltas[0]["delta"], deltas[1]["delta"] = "[", "]"
    replay = ReplayTransport((ReplayTurn(tuple(events)),))
    with pytest.raises(ResponseFailure):
        asyncio.run(
            ResponsesProvider(replay.open, report=lambda _: None).complete(
                (Message("user", "fix"),), TOOLS
            )
        )
    assert replay.attempts == 1
    assert replay.streams[0].closed


def test_opening_retries_have_a_finite_attempt_and_delay_budget() -> None:
    replay = ReplayTransport((), opening_failures=10)
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(OpenFailure):
        asyncio.run(
            ResponsesProvider(replay.open, sleep=sleep, report=lambda _: None).complete((), TOOLS)
        )
    assert replay.attempts == 3
    assert len(delays) == 2
    assert 0.225 <= delays[0] <= 0.275
    assert 0.45 <= delays[1] <= 0.55


def test_connection_loss_during_iteration_is_not_an_opening_retry() -> None:
    class BrokenStream(ReplayStream):
        async def __anext__(self) -> Payload:
            raise OpenFailure("lost after opening", retryable=True)

    stream = BrokenStream(())
    attempts = 0

    async def open_stream(request: Payload) -> EventStream:
        nonlocal attempts
        attempts += 1
        return stream

    with pytest.raises(OpenFailure):
        asyncio.run(ResponsesProvider(open_stream, sleep=no_wait).complete((), TOOLS))
    assert attempts == 1
    assert stream.closed


def test_cleanup_failure_prevents_tool_dispatch() -> None:
    class BrokenClose(ReplayStream):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    stream = BrokenClose(response_events(Message("assistant", "", (EDIT,))))

    async def open_stream(request: Payload) -> EventStream:
        return stream

    def execute(call: ToolCall) -> str:
        raise AssertionError("tool executed before cleanup succeeded")

    with pytest.raises(RuntimeError, match="close failed"):
        asyncio.run(
            run_agent(
                "fix",
                ResponsesProvider(open_stream, report=lambda _: None),
                TOOLS,
                execute,
                report=lambda _: None,
            )
        )
    assert stream.closed


def test_cancellation_closes_owned_stream() -> None:
    class WaitingStream(ReplayStream):
        def __init__(self) -> None:
            super().__init__(())
            self.waiting = asyncio.Event()

        async def __anext__(self) -> Payload:
            self.waiting.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        stream = WaitingStream()

        async def open_stream(request: Payload) -> EventStream:
            return stream

        task = asyncio.create_task(ResponsesProvider(open_stream).complete((), TOOLS))
        await stream.waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed

    asyncio.run(scenario())


def test_live_permission_gate_blocks_both_edits_and_code_execution(tmp_path: Path) -> None:
    create_context_fixture(tmp_path)
    executor = LiveFixtureTools(tmp_path, allow_execution=False)
    assert "require --allow-execution" in executor.execute(EDIT)
    assert "require --allow-execution" in executor.execute(ToolCall("t", "test", {}))
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == BUGGY_SOURCE
    assert "calculator.py" in executor.execute(ToolCall("d", "discover", {}))


@pytest.mark.parametrize("allow_execution", [False, True])
def test_live_entry_selects_catalog_reports_failure_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], allow_execution: bool
) -> None:
    from examples.crafting_agents import openai_transport

    replay = scenario_transport("disconnect")

    class StubTransport:
        closed = False

        async def open(self, request: Payload) -> EventStream:
            return await replay.open(request)

        async def aclose(self) -> None:
            self.closed = True

    transport = StubTransport()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-live-test-key")
    monkeypatch.setattr(openai_transport, "OpenAITransport", lambda key: transport)
    exit_code = asyncio.run(
        main(scenario="repair", live=True, model="fixture-model", allow_execution=allow_execution)
    )
    assert exit_code == 1
    assert transport.closed
    assert replay.streams[0].closed
    functions = replay.requests[0]["tools"]
    assert isinstance(functions, list)
    assert [tool["name"] for tool in functions] == (
        ["discover", "read", "edit", "test"] if allow_execution else ["discover", "read"]
    )
    output = capsys.readouterr().out
    assert "stopped: provider_failure" in output
    assert output.rstrip().endswith("return a - b")
    assert "synthetic-live-test-key" not in output


def test_real_sdk_decodes_mock_http_sse_without_network() -> None:
    events = response_events(
        Message("assistant", "hello", (ToolCall("r", "read", {"path": "calculator.py"}),))
    )
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async def scenario() -> Message:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        transport = OpenAITransport("synthetic-test-key", http_client=client)
        try:
            assert transport.client.max_retries == 0
            result = await ResponsesProvider(transport.open, report=lambda _: None).complete(
                (Message("user", "read"),), TOOLS
            )
            return result
        finally:
            await transport.aclose()
            assert client.is_closed

    result = asyncio.run(scenario())
    assert result.content == "hello"
    assert result.tool_calls[0].arguments == {"path": "calculator.py"}
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.openai.com/v1/responses"
    assert json.loads(requests[0].content)["max_output_tokens"] == 2_048


@pytest.mark.parametrize("status,attempts", [(401, 1), (429, 1), (503, 3)])
def test_sdk_opening_failures_use_only_adapter_retry_policy(status: int, attempts: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status, json={"error": {"message": "synthetic failure", "type": "fixture"}}
        )

    async def scenario() -> None:
        transport = OpenAITransport(
            "synthetic-test-key",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        try:
            with pytest.raises(OpenFailure):
                await ResponsesProvider(
                    transport.open, sleep=no_wait, report=lambda _: None
                ).complete((), TOOLS)
        finally:
            await transport.aclose()

    asyncio.run(scenario())
    assert len(requests) == attempts
