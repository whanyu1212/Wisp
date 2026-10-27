from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.rpc.execution_support import (
    dispatch_parsed,
    parsed_command,
)
from tests.support.rpc import (
    build_rpc_executor_fixture,
    guard_rpc_command_serialization,
)
from wisp.auth.openai_codex import DeviceCodeInfo
from wisp.auth.storage import ApiKeyCredential, OAuthCredential
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcCommandStarted,
    RpcConnectionCatalogReported,
    RpcConnectionCatalogSnapshot,
    RpcDeviceCodeProgressReported,
    RpcDeviceCodeReported,
    WispEvent,
)
from wisp.openai_compatible import OpenAICompatibleSettings
from wisp.rpc.commands import StoreApiKeyCommand
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcRunningCommand,
)
from wisp.rpc.handlers import connections as rpc_connections_module
from wisp.rpc.handlers import inspection as rpc_inspection_module
from wisp.rpc.handlers.connections import handle_rpc_store_api_key_command
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


def test_executor_reports_connection_catalog_without_replacing_running_command(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "connections-1", "type": "get_connection_catalog"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            RpcConnectionCatalogReported,
            RpcCommandFinished,
        ]
        report = fixture.events[1]
        assert isinstance(report, RpcConnectionCatalogReported)
        assert report.catalog.providers
        assert fixture.runtime.openai_compatible_provider is None
        assert "openai-compatible" not in {provider.id for provider in report.catalog.providers}
        assert all(
            method.environment_variable is None or "KEY" in method.environment_variable
            for family in report.catalog.providers
            for method in family.methods
        )

    anyio.run(scenario)


@pytest.mark.parametrize("fail_catalog", [False, True])
def test_executor_typed_connection_catalog_never_reports_credentials(
    tmp_path: Path, monkeypatch: MonkeyPatch, fail_catalog: bool
) -> None:
    secret = "sentinel-inspection-secret"

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        store.set("anthropic", ApiKeyCredential(key=secret))
        if fail_catalog:

            def fail_snapshot(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
                raise RuntimeError(secret)

            monkeypatch.setattr(
                rpc_inspection_module, "rpc_connection_catalog_snapshot", fail_snapshot
            )

        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"type": "get_connection_catalog", "id": "connections-1"},
                running,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is running
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent if fail_catalog else RpcConnectionCatalogReported,
            RpcCommandFinished,
        ]
        assert secret not in "\n".join(event.model_dump_json() for event in fixture.events)
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "connections-1"
        assert finished.command_type == "get_connection_catalog"
        assert finished.ok is not fail_catalog
        assert finished.error == ("Provider connection failed" if fail_catalog else None)

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command_type", ["store_api_key", "disconnect_provider", "begin_device_code"]
)
@pytest.mark.parametrize("id_fields", [{}, {"id": None}, {"id": "connection-1"}])
def test_typed_connection_commands_preserve_lifecycle_without_legacy_conversion(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str, id_fields: dict[str, object]
) -> None:
    async def fake_login(
        *, on_device_code: Callable[[DeviceCodeInfo], None], on_progress: Callable[[int], None]
    ) -> OAuthCredential:
        on_device_code(DeviceCodeInfo("CODE-123", "https://example.test/device", 1, 900))
        on_progress(1)
        return OAuthCredential(
            access="sentinel-access", refresh="sentinel-refresh", expires=4_102_444_800_000
        )

    monkeypatch.setattr(rpc_connections_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        store.set("anthropic", ApiKeyCredential(key="old-key"))
        payload: dict[str, object] = {
            "type": command_type,
            "provider": "openai-codex" if command_type == "begin_device_code" else "anthropic",
            **id_fields,
        }
        if command_type == "store_api_key":
            payload["api_key"] = "  sentinel-new-key \t"
        parsed = parsed_command(payload)
        assert parsed.known is not None
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            executor = fixture.executor(task_group=task_group, send=send)
            result = await executor.dispatch_parsed(parsed, None)
            if command_type == "begin_device_code":
                with anyio.fail_after(2):
                    completed = await receive.receive()
                assert isinstance(completed, _RpcCommandCompleted)
                assert result.running_command is not None
                assert completed.command_id == result.running_command.command_id
                assert completed.history == fixture.session_state.history
                assert completed.entry_count == fixture.session_state.entry_count
                assert completed.ok is True
            else:
                assert result.running_command is None
            task_group.cancel_scope.cancel()
        expected: list[type[WispEvent]] = [RpcCommandStarted]
        if command_type == "begin_device_code":
            expected.extend([RpcDeviceCodeReported, RpcDeviceCodeProgressReported])
        expected.extend([RpcConnectionCatalogReported, RpcCommandFinished])
        assert [type(event) for event in fixture.events] == expected
        started = fixture.events[0]
        finished = fixture.events[-1]
        assert isinstance(started, RpcCommandStarted)
        assert isinstance(finished, RpcCommandFinished)
        assert started.command_id
        assert started.command_type == finished.command_type == command_type
        if command_type == "begin_device_code":
            progress = fixture.events[2]
            assert isinstance(progress, RpcDeviceCodeProgressReported)
            assert progress.attempt == 1
        assert all(
            event.model_dump()["command_id"] == started.command_id for event in fixture.events
        )
        if id_fields.get("id") is not None:
            assert started.command_id == id_fields["id"]
        assert finished.ok is True
        if command_type == "store_api_key":
            assert store.get("anthropic") == ApiKeyCredential(key="sentinel-new-key")
        elif command_type == "disconnect_provider":
            assert store.get("anthropic") is None
        else:
            assert store.get("openai-codex") == OAuthCredential(
                access="sentinel-access", refresh="sentinel-refresh", expires=4_102_444_800_000
            )
        rendered = repr(parsed) + "".join(event.model_dump_json() for event in fixture.events)
        assert all(
            secret not in rendered
            for secret in ("sentinel-new-key", "sentinel-access", "sentinel-refresh")
        )

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command_type", ["store_api_key", "disconnect_provider", "begin_device_code"]
)
@pytest.mark.parametrize("failure", ["missing-store", "storage-error"])
def test_typed_connection_storage_failures_preserve_credentials(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str, failure: str
) -> None:
    async def fake_login(**_kwargs: object) -> OAuthCredential:
        return OAuthCredential(
            access="sentinel-new-access", refresh="sentinel-new-refresh", expires=4_102_444_800_000
        )

    def fail_mutation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sentinel-storage-secret")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        provider = "openai-codex" if command_type == "begin_device_code" else "anthropic"
        existing = ApiKeyCredential(key="sentinel-old-key")
        store.set(provider, existing)
        if failure == "missing-store":
            fixture.runtime = replace(fixture.runtime, auth_store=None)
        else:
            monkeypatch.setattr(
                store, "delete" if command_type == "disconnect_provider" else "set", fail_mutation
            )
        monkeypatch.setattr(rpc_connections_module, "login_openai_codex_device_code", fake_login)
        payload: dict[str, object] = {"type": command_type, "provider": provider, "id": "failure-1"}
        if command_type == "store_api_key":
            payload["api_key"] = "sentinel-new-key"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send), payload
            )
            if result.running_command is not None:
                with anyio.fail_after(2):
                    await receive.receive()
            task_group.cancel_scope.cancel()
        assert store.get(provider) == existing
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.command_id == "failure-1"
        assert finished.ok is False
        assert finished.error == (
            f"RPC {command_type} command requires an auth store"
            if failure == "missing-store"
            else "Provider connection failed"
        )
        assert "sentinel-" not in "".join(event.model_dump_json() for event in fixture.events)

    anyio.run(scenario)


@pytest.mark.parametrize("provider", ["anthropic", "unsupported-provider"])
def test_typed_store_rejects_whitespace_before_provider_checks(
    tmp_path: Path, provider: str
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        existing = ApiKeyCredential(key="old-key")
        store.set(provider, existing)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "type": "store_api_key",
                    "id": "blank-1",
                    "provider": provider,
                    "api_key": " \t\n",
                },
            )
            task_group.cancel_scope.cancel()
        assert store.get(provider) == existing
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.error == "RPC store_api_key command requires a non-empty api_key"
        assert finished.ok is False

    anyio.run(scenario)


def test_executor_stores_api_key_without_leaking_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        secret = "sentinel-secret-key"
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "anthropic",
                    "api_key": secret,
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert result.running_command is None
        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped
        report = next(
            event for event in fixture.events if isinstance(event, RpcConnectionCatalogReported)
        )
        anthropic = next(family for family in report.catalog.providers if family.id == "anthropic")
        assert anthropic.methods[0].has_stored_credential is True
        assert fixture.runtime.auth_store is not None
        credential = fixture.runtime.auth_store.get("anthropic")
        assert credential is not None

    anyio.run(scenario)


@pytest.mark.parametrize("command_type", ["store_api_key", "begin_device_code"])
def test_typed_connection_rejects_unsupported_provider_without_side_effects(
    tmp_path: Path, monkeypatch: MonkeyPatch, command_type: str
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unsupported providers must not start login or write credentials")

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        monkeypatch.setattr(store, "set", unexpected)
        monkeypatch.setattr(rpc_connections_module, "login_openai_codex_device_code", unexpected)
        payload: dict[str, object] = {
            "type": command_type,
            "provider": "unsupported",
            "id": "unsupported-1",
        }
        if command_type == "store_api_key":
            payload["api_key"] = "sentinel-unsupported-key"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send), payload
            )
            task_group.cancel_scope.cancel()
        assert result.running_command is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is False
        assert finished.error == (
            "API-key connection is not supported for unsupported."
            if command_type == "store_api_key"
            else "OAuth connection is not supported for unsupported."
        )
        assert "sentinel-unsupported-key" not in "".join(
            event.model_dump_json() for event in fixture.events
        )

    anyio.run(scenario)


def test_executor_rejects_api_key_for_unregistered_openai_compatible_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        secret = "unregistered-provider-secret"
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "openai-compatible",
                    "api_key": secret,
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store is not None
        assert fixture.runtime.auth_store.get("openai-compatible") is None
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is False
        assert finished.error == ("API-key connection is not supported for openai-compatible.")
        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped

    anyio.run(scenario)


def test_executor_rejects_api_key_for_keyless_openai_compatible_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await build_runtime(
            auth_path=tmp_path / "auth.json",
            openai_compatible=OpenAICompatibleSettings(
                provider_name="local-models",
                base_url="http://localhost:11434/v1",
                default_model="local-model",
                requires_api_key=False,
            ),
        )
        events: list[WispEvent] = []
        secret = "irrelevant-secret"
        try:
            handle_rpc_store_api_key_command(
                StoreApiKeyCommand(id="store-1", provider="local-models", api_key=secret),
                running_command=None,
                runtime=runtime,
                write_event=events.append,
            )

            assert runtime.auth_store is not None
            assert runtime.auth_store.get("local-models") is None
            finished = events[-1]
            assert isinstance(finished, RpcCommandFinished)
            assert finished.ok is False
            assert finished.error == "API-key connection is not supported for local-models."
            dumped = json.dumps([event.model_dump(mode="json") for event in events])
            assert secret not in dumped
        finally:
            await runtime.aclose()

    anyio.run(scenario)


def test_executor_keeps_api_key_success_when_catalog_refresh_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    secret = "sentinel-refresh-secret"

    def fail_catalog(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
        raise OverflowError(secret)

    monkeypatch.setattr(rpc_connections_module, "rpc_connection_catalog_snapshot", fail_catalog)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "id": "store-1",
                    "type": "store_api_key",
                    "provider": "anthropic",
                    "api_key": secret,
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store is not None
        assert fixture.runtime.auth_store.get("anthropic") == ApiKeyCredential(key=secret)
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        warning = fixture.events[1]
        assert isinstance(warning, ErrorEvent)
        assert warning.message == (
            "API key stored; connection catalog unavailable: status refresh failed"
        )
        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_keeps_disconnect_success_when_catalog_refresh_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_catalog(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
        raise OverflowError("unsafe diagnostic")

    monkeypatch.setattr(rpc_connections_module, "rpc_connection_catalog_snapshot", fail_catalog)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        assert fixture.runtime.auth_store is not None
        fixture.runtime.auth_store.set("anthropic", ApiKeyCredential(key="stored-key"))
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {
                    "id": "disconnect-1",
                    "type": "disconnect_provider",
                    "provider": "anthropic",
                },
                None,
            )
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store.get("anthropic") is None
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        warning = fixture.events[1]
        assert isinstance(warning, ErrorEvent)
        assert warning.message == (
            "Credentials disconnected; connection catalog unavailable: status refresh failed"
        )
        assert "unsafe diagnostic" not in warning.message
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_keeps_device_login_success_when_catalog_refresh_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    credential = OAuthCredential(access="access", refresh="refresh", expires=4_102_444_800_000)

    async def fake_login(**_kwargs: object) -> OAuthCredential:
        return credential

    def fail_catalog(_runtime: WispRuntime) -> RpcConnectionCatalogSnapshot:
        raise OverflowError("unsafe diagnostic")

    monkeypatch.setattr(rpc_connections_module, "login_openai_codex_device_code", fake_login)
    monkeypatch.setattr(rpc_connections_module, "rpc_connection_catalog_snapshot", fail_catalog)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await receive.receive()
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store is not None
        assert fixture.runtime.auth_store.get("openai-codex") == credential
        assert [type(event) for event in fixture.events] == [
            RpcCommandStarted,
            ErrorEvent,
            RpcCommandFinished,
        ]
        warning = fixture.events[1]
        assert isinstance(warning, ErrorEvent)
        assert warning.message == (
            "Device login completed; connection catalog unavailable: status refresh failed"
        )
        finished = fixture.events[-1]
        assert isinstance(finished, RpcCommandFinished)
        assert finished.ok is True

    anyio.run(scenario)


def test_executor_sanitizes_device_code_provider_failures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    secret = "sentinel-provider-secret"

    async def fake_login(**_kwargs: object) -> OAuthCredential:
        raise RuntimeError(secret)

    monkeypatch.setattr(rpc_connections_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await receive.receive()
            task_group.cancel_scope.cancel()

        dumped = json.dumps([event.model_dump(mode="json") for event in fixture.events])
        assert secret not in dumped
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert finished.error == "Provider connection failed"

    anyio.run(scenario)


def test_executor_cancels_device_code_without_replacing_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    started = anyio.Event()

    async def fake_login(**_kwargs: object) -> OAuthCredential:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("unreachable")

    monkeypatch.setattr(rpc_connections_module, "login_openai_codex_device_code", fake_login)

    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        assert fixture.runtime.auth_store is not None
        existing = OAuthCredential(access="old", refresh="old", expires=4_102_444_800_000)
        fixture.runtime.auth_store.set("openai-codex", existing)
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
                None,
            )
            await started.wait()
            assert result.running_command is not None
            result.running_command.cancel_scope.cancel()
            await receive.receive()
            task_group.cancel_scope.cancel()

        assert fixture.runtime.auth_store.get("openai-codex") == existing
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert finished.error == "RPC command cancelled: device-1"

    anyio.run(scenario)


@pytest.mark.parametrize(
    "command",
    [
        {"id": "store-1", "type": "store_api_key", "provider": "anthropic", "api_key": "secret"},
        {"id": "store-blank", "type": "store_api_key", "provider": "unsupported", "api_key": "   "},
        {"id": "disconnect-1", "type": "disconnect_provider", "provider": "anthropic"},
        {"id": "device-1", "type": "begin_device_code", "provider": "openai-codex"},
    ],
)
def test_executor_rejects_connection_mutations_while_an_operation_is_active(
    tmp_path: Path,
    command: dict[str, object],
    monkeypatch: MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fixture = await build_rpc_executor_fixture(tmp_path)
        store = fixture.runtime.auth_store
        assert store is not None
        existing = ApiKeyCredential(key="old-key")
        store.set(str(command["provider"]), existing)

        def unexpected_side_effect(*_args: object, **_kwargs: object) -> None:
            pytest.fail("Busy connection commands must not mutate credentials or start login")

        monkeypatch.setattr(store, "set", unexpected_side_effect)
        monkeypatch.setattr(store, "delete", unexpected_side_effect)
        monkeypatch.setattr(
            rpc_connections_module, "login_openai_codex_device_code", unexpected_side_effect
        )
        running = _RpcRunningCommand("active-1", "prompt", anyio.CancelScope())
        send, receive = anyio.create_memory_object_stream(1)
        async with send, receive, anyio.create_task_group() as task_group:
            result = await dispatch_parsed(
                fixture.executor(task_group=task_group, send=send),
                dict(command),
                running,
            )
            task_group.cancel_scope.cancel()

        assert store.get(str(command["provider"])) == existing
        assert result.running_command is running
        finished = next(event for event in fixture.events if isinstance(event, RpcCommandFinished))
        assert finished.ok is False
        assert "another RPC operation is active" in (finished.error or "")

    anyio.run(scenario)
