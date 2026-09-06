"""Provider credential mutation and device-code login for the RPC frontend."""

from __future__ import annotations

from collections.abc import Callable

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.agent.messages import Message
from wisp.auth.connections import (
    DEVICE_CODE_PROVIDER,
    supports_api_key,
)
from wisp.auth.openai_codex import DeviceCodeInfo, login_openai_codex_device_code
from wisp.auth.storage import ApiKeyCredential, JsonAuthStore
from wisp.events import (
    ErrorEvent,
    RpcCommandFinished,
    RpcConnectionCatalogReported,
    RpcDeviceCodeProgressReported,
    RpcDeviceCodeReported,
)
from wisp.rpc.commands import (
    BeginDeviceCodeCommand,
    DisconnectProviderCommand,
    StoreApiKeyCommand,
)
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.inspection import _sanitized_auth_error, rpc_connection_catalog_snapshot
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime

type RunningCommandFactory = Callable[..., _RpcRunningCommand]
type CommandCompletedFactory = Callable[..., _RpcCommandCompleted]


def handle_rpc_store_api_key_command(
    command: StoreApiKeyCommand,
    *,
    running_command: _RpcRunningCommand | None,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Persist one API key and return the refreshed connection catalog."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    if running_command is not None:
        lifecycle.fail("Cannot store credentials while another RPC operation is active")
        return
    provider = command.provider
    api_key = command.api_key
    if not api_key.strip():
        lifecycle.fail("RPC store_api_key command requires a non-empty api_key")
        return
    openai_compatible_provider = (
        runtime.openai_compatible_provider if runtime.openai_compatible_requires_api_key else None
    )
    if not supports_api_key(
        provider,
        openai_compatible_provider=openai_compatible_provider,
    ) or not runtime.providers.is_registered(provider):
        lifecycle.fail(f"API-key connection is not supported for {provider}.")
        return
    store = runtime.auth_store
    if store is None:
        lifecycle.fail("RPC store_api_key command requires an auth store")
        return
    try:
        store.set(provider, ApiKeyCredential(key=api_key.strip()))
    except Exception as exc:
        lifecycle.fail(_sanitized_auth_error(exc))
        return
    _write_connection_catalog_after_mutation(
        runtime=runtime,
        command_id=command_id,
        outcome="API key stored",
        write_event=write_event,
    )
    lifecycle.finish()


def handle_rpc_disconnect_provider_command(
    command: DisconnectProviderCommand,
    *,
    running_command: _RpcRunningCommand | None,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
) -> None:
    """Remove stored credentials and return the refreshed connection catalog."""

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    if running_command is not None:
        lifecycle.fail("Cannot disconnect credentials while another RPC operation is active")
        return
    provider = command.provider
    store = runtime.auth_store
    if store is None:
        lifecycle.fail("RPC disconnect_provider command requires an auth store")
        return
    try:
        store.delete(provider)
    except Exception as exc:
        lifecycle.fail(_sanitized_auth_error(exc))
        return
    _write_connection_catalog_after_mutation(
        runtime=runtime,
        command_id=command_id,
        outcome="Credentials disconnected",
        write_event=write_event,
    )
    lifecycle.finish()


def start_rpc_device_code_command(
    command: BeginDeviceCodeCommand,
    *,
    running_command: _RpcRunningCommand | None,
    runtime: WispRuntime,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand | None:
    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    if running_command is not None:
        lifecycle.fail("Cannot start a device-code login while another RPC operation is active")
        return running_command
    provider = command.provider
    if provider != DEVICE_CODE_PROVIDER:
        lifecycle.fail(f"OAuth connection is not supported for {provider}.")
        return None
    store = runtime.auth_store
    if store is None:
        lifecycle.fail("RPC begin_device_code command requires an auth store")
        return None
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        run_rpc_device_code_command,
        runtime,
        store,
        provider,
        command_id,
        session_state.history,
        session_state.entry_count,
        cancel_scope,
        send.clone(),
        write_event,
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type="begin_device_code",
        cancel_scope=cancel_scope,
    )


async def run_rpc_device_code_command(
    runtime: WispRuntime,
    store: JsonAuthStore,
    provider: str,
    command_id: str,
    history: tuple[Message, ...],
    entry_count: int,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> None:
    error: str | None = None
    error_rendered = False

    def show_device_code(info: DeviceCodeInfo) -> None:
        write_event(
            RpcDeviceCodeReported(
                command_id=command_id,
                provider=provider,
                verification_uri=info.verification_uri,
                user_code=info.user_code,
            )
        )

    def show_progress(attempt: int) -> None:
        write_event(
            RpcDeviceCodeProgressReported(
                command_id=command_id,
                provider=provider,
                attempt=attempt,
            )
        )

    try:
        with cancel_scope:
            try:
                credential = await login_openai_codex_device_code(
                    on_device_code=show_device_code,
                    on_progress=show_progress,
                )
                store.set(provider, credential)
                _write_connection_catalog_after_mutation(
                    runtime=runtime,
                    command_id=command_id,
                    outcome="Device login completed",
                    write_event=write_event,
                )
            except anyio.get_cancelled_exc_class():
                error = f"RPC command cancelled: {command_id}"
            except Exception as exc:  # noqa: BLE001 - command failures must not stop RPC
                error = _sanitized_auth_error(exc)
    finally:
        async with send:
            if error is not None and not error_rendered:
                write_event(ErrorEvent(message=error))
            write_event(
                RpcCommandFinished(
                    command_id=command_id,
                    command_type="begin_device_code",
                    ok=error is None,
                    error=error,
                )
            )
            await send.send(
                command_completed_factory(
                    command_id=command_id,
                    command_type="begin_device_code",
                    ok=error is None,
                    history=history,
                    entry_count=entry_count,
                )
            )


def _write_connection_catalog_after_mutation(
    *,
    runtime: WispRuntime,
    command_id: str,
    outcome: str,
    write_event: RpcEventWriter,
) -> None:
    """Report status when available without rewriting a completed mutation as failed."""

    try:
        catalog = rpc_connection_catalog_snapshot(runtime)
    except Exception:  # noqa: BLE001 - mutation already committed; report safely
        write_event(
            ErrorEvent(message=f"{outcome}; connection catalog unavailable: status refresh failed")
        )
        return
    write_event(RpcConnectionCatalogReported(command_id=command_id, catalog=catalog))
