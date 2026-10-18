"""In-process SDK shutdown cancelling in-flight state refreshes, session reads, and streams."""

from __future__ import annotations

from pathlib import Path
from threading import Event

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.sdk as sdk_module
from tests.support.rpc import guard_rpc_command_serialization
from wisp.config.runtime import WispConfig
from wisp.events import RpcCommandFinished
from wisp.messages import Message
from wisp.providers.events import (
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderTextDelta,
)
from wisp.providers.fake import ScriptedProvider
from wisp.rpc.session import run as rpc_session_run_module
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.entries import MessageSessionEntry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore


@pytest.mark.slow
def test_in_process_sdk_shutdown_cancels_prompt_final_state_refresh(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    original_updated_state = rpc_session_run_module.updated_rpc_session_state

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def blocked_updated_state(
        session: JsonlSession,
        committed_history: tuple[Message, ...],
        entry_start: int,
    ) -> tuple[int, tuple[Message, ...]]:
        started.set()
        release.wait(timeout=5)
        return original_updated_state(session, committed_history, entry_start)

    monkeypatch.setattr(sdk_module, "build_session_runtime", build_scripted_runtime)
    monkeypatch.setattr(rpc_session_run_module, "updated_rpc_session_state", blocked_updated_state)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        await controller.prompt("hello", command_id="prompt-1")
        try:
            with anyio.fail_after(1):
                while not started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(1):
                await controller.aclose()
        finally:
            release.set()

    anyio.run(scenario)


@pytest.mark.slow
def test_in_process_sdk_shutdown_cancels_compact_final_state_refresh(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="first answer"),
                ProviderResponseCompleted(content="first answer"),
            ],
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="second answer"),
                ProviderResponseCompleted(content="second answer"),
            ],
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(
                    delta=(
                        "## Goal\n"
                        "Preserve the completed work.\n"
                        "## Constraints & Preferences\n"
                        "Keep the existing behavior.\n"
                        "## Progress\n"
                        "### Done\n"
                        "Completed two turns.\n"
                        "### In Progress\n"
                        "None.\n"
                        "### Blocked\n"
                        "None.\n"
                        "## Already Investigated\n"
                        "Reviewed the prior transcript.\n"
                        "## Key Decisions\n"
                        "Use a durable checkpoint.\n"
                        "## Next Steps\n"
                        "Continue from the checkpoint.\n"
                        "## Critical Context\n"
                        "The session contains two completed turns."
                    )
                ),
                ProviderResponseCompleted(
                    content=(
                        "## Goal\n"
                        "Preserve the completed work.\n"
                        "## Constraints & Preferences\n"
                        "Keep the existing behavior.\n"
                        "## Progress\n"
                        "### Done\n"
                        "Completed two turns.\n"
                        "### In Progress\n"
                        "None.\n"
                        "### Blocked\n"
                        "None.\n"
                        "## Already Investigated\n"
                        "Reviewed the prior transcript.\n"
                        "## Key Decisions\n"
                        "Use a durable checkpoint.\n"
                        "## Next Steps\n"
                        "Continue from the checkpoint.\n"
                        "## Critical Context\n"
                        "The session contains two completed turns."
                    )
                ),
            ],
        ]
    )
    original_updated_state = rpc_session_run_module.updated_rpc_session_state

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def blocked_updated_state(
        session: JsonlSession,
        committed_history: tuple[Message, ...],
        entry_start: int,
    ) -> tuple[int, tuple[Message, ...]]:
        started.set()
        release.wait(timeout=5)
        return original_updated_state(session, committed_history, entry_start)

    monkeypatch.setattr(sdk_module, "build_session_runtime", build_scripted_runtime)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        completed_ids: set[str] = set()

        async def consume_events() -> None:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished):
                    completed_ids.add(event.command_id)

        async def wait_for_completion(command_id: str) -> None:
            with anyio.fail_after(1):
                while command_id not in completed_ids:
                    await anyio.sleep(0.01)

        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(consume_events)
                await controller.prompt("first", command_id="prompt-1")
                await wait_for_completion("prompt-1")
                await controller.prompt("second", command_id="prompt-2")
                await wait_for_completion("prompt-2")

                monkeypatch.setattr(
                    rpc_session_run_module,
                    "updated_rpc_session_state",
                    blocked_updated_state,
                )
                await controller.compact(command_id="compact-1")

                with anyio.fail_after(1):
                    while not started.is_set():
                        await anyio.sleep(0.01)
                with anyio.fail_after(1):
                    await controller.aclose()
        finally:
            release.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_clone_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(JsonlSession, "read_active_leaf_id", blocked_read_active_leaf_id)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    await controller.clone_session(command_id="clone-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_fork_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(JsonlSession, "read_active_leaf_id", blocked_read_active_leaf_id)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    entry = next(
                        entry
                        for entry in source.read_entries()
                        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
                    )
                    await controller.fork_session(entry.id, command_id="fork-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_navigation_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(JsonlSession, "read_active_leaf_id", blocked_read_active_leaf_id)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    entry = next(
                        entry
                        for entry in source.read_entries()
                        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
                    )
                    await controller.navigate_session_tree(entry.id, command_id="navigate-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_unrevert_precommit_read(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    read_started = Event()
    release_read = Event()
    original_read_active_leaf_id = JsonlSession.read_active_leaf_id

    def blocked_read_active_leaf_id(session: JsonlSession) -> str | None:
        read_started.set()
        release_read.wait(timeout=5)
        return original_read_active_leaf_id(session)

    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        navigation_id: str | None = None
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    entry = next(
                        entry
                        for entry in source.read_entries()
                        if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
                    )
                    navigation_id = await controller.navigate_session_tree(
                        entry.id,
                        command_id="navigate-1",
                    )
                elif (
                    navigation_id is not None
                    and isinstance(event, RpcCommandFinished)
                    and event.command_id == navigation_id
                ):
                    monkeypatch.setattr(
                        JsonlSession,
                        "read_active_leaf_id",
                        blocked_read_active_leaf_id,
                    )
                    await controller.unrevert_session_tree(command_id="unrevert-1")
                    break
            with anyio.fail_after(1):
                while not read_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_read.set()

    anyio.run(scenario)


def test_in_process_sdk_shutdown_cancels_session_name_load(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    load_started = Event()
    release_load = Event()
    original_load = JsonlSessionStore.load

    def blocked_load(store: JsonlSessionStore, reference: str | Path) -> JsonlSession:
        load_started.set()
        release_load.wait(timeout=5)
        return original_load(store, reference)

    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        prompt_id = await controller.prompt("seed", command_id="prompt-1")
        try:
            async for event in controller.events():
                if isinstance(event, RpcCommandFinished) and event.command_id == prompt_id:
                    session_state = controller._in_process_transport._host.coordinator.session_state
                    source = session_state.session
                    assert source is not None
                    monkeypatch.setattr(JsonlSessionStore, "load", blocked_load)
                    await controller.set_session_name(
                        "renamed",
                        session_id=source.session_id,
                        command_id="rename-1",
                    )
                    break
            with anyio.fail_after(1):
                while not load_started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(0.5):
                await controller.aclose()
        finally:
            release_load.set()

    anyio.run(scenario)


@pytest.mark.slow
def test_in_process_sdk_shutdown_cancels_prompt_start_snapshot(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                ProviderTextDelta(delta="hello"),
                ProviderResponseCompleted(content="hello"),
            ]
        ]
    )
    original_run_start = rpc_session_run_module.rpc_session_run_start

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    def blocked_run_start(session: JsonlSession, entry_start: int) -> tuple[int, str | None]:
        started.set()
        release.wait(timeout=5)
        return original_run_start(session, entry_start)

    monkeypatch.setattr(sdk_module, "build_session_runtime", build_scripted_runtime)
    monkeypatch.setattr(rpc_session_run_module, "rpc_session_run_start", blocked_run_start)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        await controller.prompt("hello", command_id="prompt-1")
        try:
            with anyio.fail_after(1):
                while not started.is_set():
                    await anyio.sleep(0.01)
            with anyio.fail_after(1):
                await controller.aclose()
        finally:
            release.set()

    anyio.run(scenario)


@pytest.mark.production_fault
def test_in_process_sdk_shutdown_cancels_backpressured_stream(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    provider = ScriptedProvider(
        [
            [
                ProviderResponseStarted(model="scripted"),
                *(
                    ProviderTextDelta(delta="x")
                    for _ in range(sdk_module._STREAM_EVENT_BUFFER_CAPACITY + 10)
                ),
                ProviderResponseCompleted(content="done"),
            ]
        ]
    )

    async def build_scripted_runtime(_config: WispConfig) -> WispRuntime:
        runtime = await build_runtime()
        runtime.providers.register(provider)
        return runtime

    monkeypatch.setattr(sdk_module, "build_session_runtime", build_scripted_runtime)
    monkeypatch.setattr(sdk_module, "_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="scripted", session_dir=tmp_path),
            options=InProcessOptions(startup_trusted=True),
        )
        await controller.prompt("burst", command_id="prompt-1")

        event_output = controller._in_process_transport._event_output
        with anyio.fail_after(1):
            while event_output._buffered_event_count() < sdk_module._STREAM_EVENT_BUFFER_CAPACITY:
                await anyio.sleep(0.01)
        with anyio.fail_after(0.5):
            await controller.aclose()

    anyio.run(scenario)


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)
