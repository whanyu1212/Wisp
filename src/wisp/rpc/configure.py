"""Session configure command execution and model auto-switching for the RPC frontend."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from wisp.coding.session import CodingSession
from wisp.events import (
    ErrorEvent,
    ModelProviderAutoSwitched,
    RpcModelCatalogReported,
    RpcModelCatalogSnapshot,
)
from wisp.providers.base import Provider
from wisp.providers.catalog import AmbiguousModelError, UnknownModelError, startup_effort
from wisp.rpc.commands import ConfigureCommand
from wisp.rpc.configuration import _RpcConfigureOverrides
from wisp.rpc.inspection import rpc_model_catalog_snapshot
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime
from wisp.runtime.registry import UnknownProviderError


def handle_rpc_configure_command(
    command: ConfigureCommand,
    *,
    command_id: str,
    provided_fields: frozenset[str],
    agent: CodingSession,
    runtime: WispRuntime,
    write_event: RpcEventWriter,
    configure_overrides: _RpcConfigureOverrides | None = None,
) -> None:
    lifecycle = RpcCommandLifecycle.bind(
        command_id=command_id,
        command_type="configure",
        write_event=write_event,
    )
    provider = command.provider
    model = command.model
    effort = command.effort
    auto_compaction_enabled = command.auto_compaction_enabled
    mode = command.mode
    clear_effort = command.clear_effort
    has_provider = "provider" in provided_fields
    has_model = "model" in provided_fields
    has_effort = "effort" in provided_fields or clear_effort
    has_auto_compaction_enabled = "auto_compaction_enabled" in provided_fields
    has_mode = "mode" in provided_fields
    if has_mode and mode is None:
        lifecycle.fail("RPC configure command field mode must be 'build' or 'plan'")
        return
    if has_auto_compaction_enabled and auto_compaction_enabled is None:
        lifecycle.fail("RPC configure command field auto_compaction_enabled must be a boolean")
        return
    configuration = agent.configuration
    selected_provider = configuration.provider
    selected_model = configuration.model
    selected_effort = configuration.effort
    selected_auto_compaction_enabled = configuration.auto_compaction_enabled
    auto_switched_provider: str | None = None
    if auto_compaction_enabled is not None:
        selected_auto_compaction_enabled = auto_compaction_enabled
    if provider is not None:
        try:
            selected_provider = runtime.providers.get(provider)
        except UnknownProviderError as exc:
            lifecycle.fail(str(exc))
            return
        if not has_model:
            selected_model = None
        if not has_effort:
            selected_effort = None
    if has_model and provider is None and model is not None:
        try:
            selected_provider = auto_switch_provider_for_model(
                model,
                current_provider=selected_provider,
                runtime=runtime,
            )
            if selected_provider.name != configuration.provider.name:
                auto_switched_provider = selected_provider.name
        except AmbiguousModelError as exc:
            lifecycle.fail(f"{exc}; specify provider explicitly")
            return
        except UnknownProviderError as exc:
            lifecycle.fail(
                f"Model {model!r} resolves to provider {exc.name!r}, which is not available"
            )
            return
        if not has_effort:
            selected_effort = None
    if has_model:
        selected_model = model
    if has_effort:
        selected_effort = None if clear_effort else effort
    selected_effort = startup_effort(
        runtime.models,
        provider_name=selected_provider.name,
        model=selected_model,
        default_model=selected_provider.default_model,
        effort=selected_effort,
    )
    selection_changed = (
        has_provider or has_model or has_effort or (selected_effort != configuration.effort)
    )
    model_catalog: RpcModelCatalogSnapshot | None = None
    model_catalog_error: str | None = None
    if selection_changed:
        try:
            model_catalog = rpc_model_catalog_snapshot(
                runtime=runtime,
                provider=selected_provider,
                model=selected_model,
                effort=selected_effort,
            )
        except Exception as exc:
            # Catalog bounds protect RPC consumers, not provider configuration.
            model_catalog_error = str(exc)
    try:
        agent.reconfigure(
            replace(
                configuration,
                provider=selected_provider,
                model=selected_model,
                effort=selected_effort,
                models=runtime.models,
                auto_compaction_enabled=selected_auto_compaction_enabled,
            )
        )
    except RuntimeError as exc:
        lifecycle.fail(str(exc))
        return
    if mode is not None:
        agent.set_mode(mode)
    if auto_switched_provider is not None:
        write_event(
            ModelProviderAutoSwitched(
                command_id=command_id,
                provider=auto_switched_provider,
                model=cast(str, model),
            )
        )
    if configure_overrides is not None:
        if has_provider or selected_provider.name != configuration.provider.name:
            configure_overrides.provider = selected_provider.name
        if has_model or has_provider:
            configure_overrides.model = selected_model
            configure_overrides.has_model = True
        if has_effort or selected_effort != configuration.effort:
            configure_overrides.effort = selected_effort
            configure_overrides.has_effort = True
        if has_auto_compaction_enabled:
            configure_overrides.auto_compaction_enabled = selected_auto_compaction_enabled
            configure_overrides.has_auto_compaction_enabled = True
    if model_catalog is not None:
        write_event(RpcModelCatalogReported(command_id=command_id, catalog=model_catalog))
    elif model_catalog_error is not None:
        write_event(
            ErrorEvent(
                message=f"Configuration applied; model catalog unavailable: {model_catalog_error}"
            )
        )
    lifecycle.finish()


def auto_switch_provider_for_model(
    model: str,
    *,
    current_provider: Provider,
    runtime: WispRuntime,
) -> Provider:
    try:
        resolved_provider, _entry = runtime.models.resolve(model, prefer=current_provider.name)
    except UnknownModelError:
        return current_provider
    if resolved_provider == current_provider.name:
        return current_provider
    return runtime.providers.get(resolved_provider)
