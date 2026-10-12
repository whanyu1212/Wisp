"""In-process SDK commands, prompt failures, and module boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from tests.support.rpc import guard_rpc_command_serialization
from wisp.agent.messages import Message
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.config.runtime import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    RpcStateReported,
)
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.commands import StoreApiKeyCommand
from wisp.rpc.coordinator import (
    _RpcInputClosed,
    _RpcInputCommand,
)
from wisp.rpc.session import run as rpc_session_run_module
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSession


def test_in_process_sdk_recovers_after_unexpected_prompt_exception(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        host = controller._in_process_transport._host

        def fail_run(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unexpected SDK prompt failure")

        monkeypatch.setattr(host.agent, "run", fail_run)
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        state_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    state_id = await controller.get_state(command_id="state-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == state_id:
                    break
        finally:
            await controller.aclose()

        terminals = [event for event in events if isinstance(event, RpcCommandFinished)]
        assert [(event.command_id, event.ok, event.error) for event in terminals] == [
            ("prompt-1", False, "unexpected SDK prompt failure"),
            ("state-1", True, None),
        ]
        assert any(
            isinstance(event, RpcStateReported) and event.command_id == "state-1"
            for event in events
        )

    anyio.run(scenario)


def test_in_process_sdk_runs_the_shared_command_event_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        state_id: str | None = None
        shutdown_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    state_id = await controller.get_state(command_id="state-1")
                elif isinstance(event, RpcStateReported) and event.command_id == state_id:
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id:
                    break
        finally:
            await controller.aclose()

        assert [event.command_id for event in events if isinstance(event, RpcCommandStarted)] == [
            "prompt-1",
            "state-1",
            "shutdown-1",
        ]
        assert any(event.type == "message.delta" for event in events)
        state = next(event for event in events if isinstance(event, RpcStateReported))
        assert state.state.provider == "fake"
        assert state.state.session_id is not None

    anyio.run(scenario)


def test_in_process_sdk_preserves_typed_secret_command_until_storage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        secret = "sentinel-secret-key"
        auth_path = tmp_path / "auth.json"
        controller = await InProcessWisp.start(
            WispConfig(
                provider="fake",
                session_dir=tmp_path / "sessions",
                auth_path=auth_path,
            ),
            options=InProcessOptions(startup_trusted=True),
        )
        transport = controller._in_process_transport
        original_control_send = transport._control_send
        captured: list[_RpcInputCommand] = []

        class InspectingSend:
            async def send(self, event: _RpcInputCommand | _RpcInputClosed) -> None:
                if isinstance(event, _RpcInputCommand):
                    captured.append(event)
                await original_control_send.send(event)

            async def aclose(self) -> None:
                await original_control_send.aclose()

        transport._control_send = cast(Any, InspectingSend())

        try:
            command_id = await controller.store_api_key(
                "anthropic",
                secret,
                command_id="store-1",
            )
            events = []
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                    break
        finally:
            await controller.aclose()

        assert len(captured) == 1
        parsed = captured[0].command
        assert isinstance(parsed.known, StoreApiKeyCommand)
        assert parsed.known.api_key == secret
        assert secret not in repr(captured[0])
        assert secret not in repr(parsed)
        assert secret not in repr(parsed.known)
        assert JsonAuthStore(auth_path).get("anthropic") == ApiKeyCredential(key=secret)
        assert all(secret not in repr(event) for event in events)
        assert isinstance(events[-1], RpcCommandFinished)
        assert events[-1].ok is True

    anyio.run(scenario)


def test_in_process_sdk_reports_prompt_final_state_refresh_errors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def failed_updated_state(
        _session: JsonlSession,
        _committed_history: tuple[Message, ...],
        _entry_start: int,
    ) -> tuple[int, tuple[Message, ...]]:
        raise OSError("session state unavailable")

    monkeypatch.setattr(sdk_module, "build_runtime_for_config", build_scripted_runtime)
    monkeypatch.setattr(rpc_session_run_module, "updated_rpc_session_state", failed_updated_state)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    break
        finally:
            await controller.aclose()

        finished = next(
            event
            for event in events
            if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id
        )
        assert finished.ok is False
        assert finished.error == "session state unavailable"

    anyio.run(scenario)


def test_sdk_and_host_do_not_depend_on_cli_modules() -> None:
    sdk_path = sdk_module.__file__
    host_module = __import__("wisp.rpc.host", fromlist=["host"])
    host_path = host_module.__file__
    assert sdk_path is not None
    assert host_path is not None

    sdk_source = Path(sdk_path).read_text(encoding="utf-8")
    host_source = Path(host_path).read_text(encoding="utf-8")

    assert "wisp.cli" not in sdk_source
    assert "wisp.cli" not in host_source


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
