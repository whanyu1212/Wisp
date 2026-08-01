"""Deterministic coverage for the public in-process embedding API."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.config import WispConfig
from wisp.events import (
    RpcCommandFinished,
    RpcCommandStarted,
    RpcStateReported,
    TrustRequested,
    TrustResolved,
)
from wisp.sdk import InProcessOptions, InProcessWisp


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


def test_in_process_sdk_surfaces_and_resolves_project_trust(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "fake")
    monkeypatch.setenv("WISP_TRUST_FILE", str(tmp_path / "trust.json"))
    monkeypatch.delenv("WISP_TRUST", raising=False)

    async def scenario() -> None:
        controller = await InProcessWisp.from_environment(
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(project_context_root=tmp_path),
        )
        prompt_id = await controller.prompt("hello", command_id="prompt-1")
        shutdown_id: str | None = None
        events = []
        try:
            async for event in controller.events():
                events.append(event)
                if isinstance(event, TrustRequested):
                    await controller.trust(
                        event.request_id,
                        trusted=False,
                        command_id="trust-1",
                    )
                elif isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    shutdown_id = await controller.shutdown(command_id="shutdown-1")
                elif isinstance(event, RpcCommandFinished) and event.command_id == shutdown_id:
                    break
        finally:
            await controller.aclose()

        resolved = next(event for event in events if isinstance(event, TrustResolved))
        assert resolved.trusted is False
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == "trust-1" and event.ok
            for event in events
        )
        assert any(
            isinstance(event, RpcCommandFinished) and event.command_id == prompt_id and event.ok
            for event in events
        )

    anyio.run(scenario)


def test_in_process_sdk_close_denies_pending_trust_without_hanging(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("WISP_PROVIDER", "fake")
    monkeypatch.setenv("WISP_TRUST_FILE", str(tmp_path / "trust.json"))
    monkeypatch.delenv("WISP_TRUST", raising=False)

    async def scenario() -> None:
        controller = await InProcessWisp.from_environment(
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(project_context_root=tmp_path),
        )
        await controller.prompt("hello", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, TrustRequested):
                    break
        finally:
            await controller.aclose()

    anyio.run(scenario)


def test_in_process_sdk_allows_one_event_consumer_and_idempotent_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        controller.events()
        with pytest.raises(RuntimeError, match="only be consumed once"):
            controller.events()
        await controller.aclose()
        await controller.aclose()

    anyio.run(scenario)


def test_in_process_sdk_cleanup_is_safe_from_nested_or_other_task(tmp_path: Path) -> None:
    async def start_controller() -> InProcessWisp:
        return await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )

    async def scenario() -> None:
        nested_controller = await start_controller()
        # The controller's owner task, not this nested cancel scope, owns its
        # internal task group. Exiting here must not violate AnyIO's LIFO scope
        # rules.
        with anyio.CancelScope():
            await nested_controller.aclose()

        other_task_controller = await start_controller()
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(other_task_controller.aclose)

    anyio.run(scenario)


def test_sdk_and_host_do_not_depend_on_cli_modules() -> None:
    sdk_source = Path(__import__("wisp.sdk").sdk.__file__).read_text(encoding="utf-8")
    host_source = Path(__import__("wisp.rpc.host", fromlist=["host"]).__file__).read_text(
        encoding="utf-8"
    )

    assert "wisp.cli" not in sdk_source
    assert "wisp.cli" not in host_source
