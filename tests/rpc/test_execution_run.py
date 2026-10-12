from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import (
    dispatch_parsed,
    enable_project_init,
)
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.events import (
    ErrorEvent,
    MessageCompleted,
    RpcCommandFinished,
    RpcCommandStarted,
    SessionStatsReported,
    WispEvent,
)
from wisp.rpc.commands import (
    ParsedRpcCommand,
    RpcCommandAdapter,
)
from wisp.rpc.coordinator import _RpcCommandCompleted
from wisp.rpc.execution import RpcCommandExecutor
from wisp.rpc.host import RpcHost
from wisp.rpc.session import run as rpc_session_run_module
from wisp.sessions.jsonl import JsonlSession


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_prompt_worker_converts_unexpected_exception_to_failed_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)

        def fail_run(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unexpected prompt failure")

        monkeypatch.setattr(fixture.agent, "run", fail_run)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(
                executor, {"id": "prompt-1", "type": "prompt", "prompt": "hello"}, None
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.command_id == "prompt-1"
        assert completed.ok is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("prompt-1", False, "unexpected prompt failure")
        ]

    anyio.run(scenario)


def test_prompt_worker_converts_unexpected_renderer_exception_to_failed_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)

        async def fail_render(_events: AsyncIterator[WispEvent]) -> None:
            raise RuntimeError("renderer failed")

        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = RpcCommandExecutor(
                agent=fixture.agent,
                runtime=fixture.runtime,
                sessions=fixture.sessions,
                session_state=fixture.session_state,
                task_group=task_group,
                send=send,
                approval_policy=fixture.approval_policy,
                trust_gate=fixture.trust_gate,
                configure_overrides=fixture.configure_overrides,
                coordinator=fixture.coordinator,
                write_event=fixture.writer,
                render_events=fail_render,
            )
            result = await dispatch_parsed(
                executor, {"id": "prompt-1", "type": "prompt", "prompt": "hello"}, None
            )
            completed = await receive.receive()
            task_group.cancel_scope.cancel()

        assert result.running_command is not None
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.ok is False
        finished = [event for event in fixture.events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in finished] == [
            ("prompt-1", False, "renderer failed")
        ]

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "run-1"}])
@pytest.mark.parametrize(
    "payload",
    [
        {"type": "prompt", "prompt": ""},
        {"type": "prompt", "prompt": " \t"},
        {"type": "prompt", "prompt": "你好 🌸"},
        {"type": "init"},
        *[
            {"type": "compact", **fields}
            for fields in (
                {},
                {"instructions": None},
                {"instructions": ""},
                {"instructions": " \t"},
                {"instructions": "  retain 日本語  "},
            )
        ],
        {"type": "get_session_stats"},
    ],
)
def test_run_commands_stay_typed_through_completion(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    id_fields: dict[str, object],
    payload: dict[str, object],
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path / "sessions")
        if payload["type"] == "init":
            project = tmp_path / "project"
            project.mkdir()
            enable_project_init(fixture, project)
        selected = fixture.sessions.create()
        selected.path.parent.mkdir(parents=True, exist_ok=True)
        selected.path.touch()
        fixture.session_state.session = selected
        calls: list[object] = []

        async def run(prompt: str, **kwargs: object) -> AsyncIterator[WispEvent]:
            calls.append((prompt, kwargs["operation_id"]))
            yield MessageCompleted(turn=1, content="done", finish_reason="stop")

        async def compact(
            session: JsonlSession, *, instructions: str | None = None
        ) -> AsyncIterator[WispEvent]:
            assert session is selected
            calls.append(instructions)
            if False:
                yield MessageCompleted(turn=1, content="done", finish_reason="stop")

        monkeypatch.setattr(fixture.agent, "run", run)
        monkeypatch.setattr(fixture.agent, "compact", compact)
        send, receive = anyio.create_memory_object_stream(10)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            command = RpcCommandAdapter.validate_python({**payload, **id_fields})
            result = await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), None)
            with anyio.fail_after(2):
                completed = await receive.receive()
                while not isinstance(completed, _RpcCommandCompleted):
                    completed = await receive.receive()
            task_group.cancel_scope.cancel()
        started = fixture.events[0]
        finished = fixture.events[-1]
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(finished, RpcCommandFinished)
        assert result.running_command is not None
        assert (
            started.command_id
            == result.running_command.command_id
            == completed.command_id
            == finished.command_id
        )
        assert (
            started.command_type
            == result.running_command.command_type
            == completed.command_type
            == finished.command_type
            == payload["type"]
        )
        assert started.command_id
        if id_fields.get("id") is not None:
            assert started.command_id == id_fields["id"]
        # The fake init run creates no guidance; retain its init completion failure.
        assert finished.ok is completed.ok is (payload["type"] != "init")
        if payload["type"] in {"prompt", "init"}:
            assert calls == [(payload.get("prompt", "/init"), started.command_id)]
            assert result.selected_session is selected
        elif payload["type"] == "compact":
            raw = payload.get("instructions")
            assert calls == [raw.strip() or None if isinstance(raw, str) else None]
        else:
            assert [type(event) for event in fixture.events] == [
                RpcCommandStarted,
                SessionStatsReported,
                RpcCommandFinished,
            ]
            assert fixture.events[1].command_id == started.command_id

    anyio.run(scenario)


@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "rejected-1"}])
@pytest.mark.parametrize("command_type", ["init", "compact"])
def test_typed_run_preflight_failure_does_not_start_work(
    tmp_path: Path, monkeypatch: MonkeyPatch, id_fields: dict[str, object], command_type: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)

        def fail(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Preflight rejection must neither convert nor launch work")

        monkeypatch.setattr(fixture.sessions, "create", fail)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            monkeypatch.setattr(task_group, "start_soon", fail)
            command = RpcCommandAdapter.validate_python({"type": command_type, **id_fields})
            result = await executor.dispatch_parsed(ParsedRpcCommand.from_known(command), None)
            assert result.running_command is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        first, _, last = fixture.events
        assert first.command_id == last.command_id
        assert first.command_type == last.command_type == command_type
        assert last.ok is False
        if id_fields.get("id") is not None:
            assert first.command_id == id_fields["id"]

    anyio.run(scenario)


@pytest.mark.parametrize("has_session", [False, True])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_typed_statistics_worker_outcomes(
    tmp_path: Path, monkeypatch: MonkeyPatch, has_session: bool, outcome: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create() if has_session else None
        if selected is not None:
            selected.path.touch()
        fixture.session_state.session = selected
        entered = anyio.Event()
        original = fixture.agent.get_session_stats

        async def stats(session: JsonlSession | None = None) -> object:
            assert session is selected
            entered.set()
            if outcome == "failure":
                raise RuntimeError("stats failed")
            if outcome == "cancel":
                await anyio.sleep_forever()
            return await original(session)

        monkeypatch.setattr(fixture.agent, "get_session_stats", stats)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await dispatch_parsed(executor, {"type": "get_session_stats", "id": "stats"})
            assert result.running_command is not None
            await entered.wait()
            if outcome == "cancel":
                result.running_command.cancel_scope.cancel()
            with anyio.fail_after(2):
                completed = await receive.receive()
            task_group.cancel_scope.cancel()
        assert isinstance(completed, _RpcCommandCompleted)
        assert completed.command_id == "stats"
        assert completed.ok is (outcome == "success")
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            *([SessionStatsReported] if outcome == "success" else []),
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert (
            finished.error
            == {
                "success": None,
                "failure": "stats failed",
                "cancel": "RPC get_session_stats command cancelled",
            }[outcome]
        )

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["prompt", "init", "compact", "get_session_stats"])
def test_run_workers_wait_for_started_event_flush(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        if command_type == "init":
            project = tmp_path / "project"
            project.mkdir()
            enable_project_init(fixture, project)
        selected = fixture.sessions.create()
        selected.path.touch()
        fixture.session_state.session = selected
        rendered: list[WispEvent] = []
        entered = anyio.Event()

        async def worker(*args: object) -> None:
            assert len(rendered) == 1
            assert isinstance(rendered[0], RpcCommandStarted)
            assert rendered[0].command_type == command_type
            entered.set()
            # The starter clones the completion stream; this fake worker owns that clone.
            for arg in args:
                if isinstance(arg, anyio.streams.memory.MemoryObjectSendStream):
                    await arg.aclose()

        async def render(events: AsyncIterator[WispEvent]) -> None:
            async for event in events:
                assert not entered.is_set()
                await anyio.lowlevel.checkpoint()
                assert not entered.is_set()
                rendered.append(event)

        worker_name = {
            "prompt": "prompt",
            "init": "prompt",
            "compact": "compact",
            "get_session_stats": "session_stats",
        }[command_type]
        monkeypatch.setattr(rpc_session_run_module, f"run_rpc_{worker_name}_command", worker)
        payload = {"type": command_type, **({"prompt": "text"} if command_type == "prompt" else {})}

        async def run(_receive: object, *, dispatch: object, reject: object) -> bool:
            result = await dispatch(
                ParsedRpcCommand.from_known(RpcCommandAdapter.validate_python(payload)), None
            )
            assert result.running_command is not None
            with anyio.fail_after(2):
                await entered.wait()
            return False

        monkeypatch.setattr(fixture.coordinator, "run", run)
        host = RpcHost(
            runtime=fixture.runtime,
            sessions=fixture.sessions,
            agent=fixture.agent,
            approval_policy=fixture.approval_policy,
            trust_gate=fixture.trust_gate,
            configure_overrides=fixture.configure_overrides,
            coordinator=fixture.coordinator,
            write_event=fixture.events.append,
            render_events=render,
        )
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await host.run_with_streams(receive, send=send, task_group=task_group)
            task_group.cancel_scope.cancel()
        assert entered.is_set()

    anyio.run(scenario)


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_typed_compact_rejects_non_file_session(
    tmp_path: Path, monkeypatch: MonkeyPatch, path_kind: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        selected = fixture.sessions.create()
        if path_kind == "directory":
            selected.path.mkdir()
        fixture.session_state.session = selected

        def fail(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Invalid persisted session must not launch a worker")

        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            monkeypatch.setattr(task_group, "start_soon", fail)
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"type": "compact", "id": "compact"},
            )
            assert result.running_command is None
        assert (
            fixture.events[-1].error == "RPC compact command requires an existing persisted session"
        )

    anyio.run(scenario)
