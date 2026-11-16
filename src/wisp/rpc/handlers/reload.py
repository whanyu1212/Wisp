"""Prepare and atomically apply an idle session's file-based settings reload."""

from __future__ import annotations

from dataclasses import dataclass, replace

import anyio

from wisp.coding import CodingSession, CodingSessionConfiguration
from wisp.config.runtime import WispConfig
from wisp.config.settings import ResolvedSettings, resolve_settings
from wisp.events import RpcModelSelectionSnapshot, RpcSettingsReloaded
from wisp.providers.catalog import startup_effort
from wisp.rpc.configuration import RpcProjectConfiguration
from wisp.runtime.api import WispRuntime

# Everything else stays on the startup/runtime value until restart. In particular,
# reload never replaces tools, credentials, trust, permissions, or path protections.
LIVE_RELOAD_FIELDS = (
    "provider",
    "model",
    "effort",
    "context_reserve_tokens",
    "auto_compaction_enabled",
    "web_search",
    "codemode",
    "subagents",
    "subagent_model",
)


@dataclass(frozen=True, slots=True)
class _ReloadPlan:
    configuration: CodingSessionConfiguration
    codemode: bool | None
    subagents: bool
    subagent_model: str | None
    active_config: WispConfig
    report: RpcSettingsReloaded


@dataclass
class RpcSettingsReloader:
    """Reload safe settings while retaining the host's configuration provenance."""

    project: RpcProjectConfiguration
    startup_keybindings: dict[str, list[str]]

    def _candidate(self) -> tuple[WispConfig, ResolvedSettings]:
        overrides = self.project.config_overrides
        if overrides is None:
            raise ValueError(
                "File-based reload is unavailable for an explicit embedded configuration"
            )
        settings = resolve_settings(
            project_dir=self.project.project_context_root,
            trust_project=self.project.trusted,
            strict=True,
        )
        return overrides.build(
            trusted=self.project.trusted,
            project_dir=self.project.project_context_root,
            settings=settings,
        ), settings

    async def reload(
        self,
        *,
        agent: CodingSession,
        runtime: WispRuntime,
        command_id: str,
    ) -> RpcSettingsReloaded:
        """Validate a candidate, then commit live settings without an intervening await.

        Args:
            agent (CodingSession): Idle session whose next run uses these settings.
            runtime (WispRuntime): Existing providers; no connections are rebuilt.
            command_id (str): Identifier correlating the reload report.

        Returns:
            RpcSettingsReloaded: Applied, unchanged, overridden and restart-only fields.

        Raises:
            RuntimeError: If a run or startup MCP connection is active.
            SettingsDocumentError: If a settings layer cannot be read or validated.
            ValueError: If the candidate cannot be applied; live settings are kept.
        """
        if agent.busy or runtime.mcp_connecting:
            raise RuntimeError("Settings reload is idle-only; wait for active work to finish")
        config, settings = await anyio.to_thread.run_sync(self._candidate)
        plan = self._prepare(
            agent=agent, runtime=runtime, config=config, settings=settings, command_id=command_id
        )
        # No await or fallible preparation below: a prompt cannot observe a partial reload.
        agent.reconfigure(plan.configuration)
        agent.codemode = plan.codemode
        agent.subagents = plan.subagents
        agent.subagent_model = plan.subagent_model
        self.project.active_config = plan.active_config
        return plan.report

    def _prepare(
        self,
        *,
        agent: CodingSession,
        runtime: WispRuntime,
        config: WispConfig,
        settings: ResolvedSettings,
        command_id: str,
    ) -> _ReloadPlan:
        """Resolve and validate every change before committing any live state."""
        choices = self.project.configure_overrides
        current = agent.configuration
        provider_name = (
            current.provider.name
            if choices.has_model and choices.provider is None
            else choices.effective_provider(config.provider)
        )
        provider = runtime.providers.get(provider_name)
        model = choices.effective_model(config.model)
        effort = startup_effort(
            runtime.models,
            provider_name=provider.name,
            model=model,
            default_model=provider.default_model,
            effort=choices.effective_effort(config.effort),
        )
        # File settings bypass ConfigureCommand's bounds; validate public selection
        # values before they can enter a frontend report or the live session.
        RpcModelSelectionSnapshot(provider=provider.name, model=model, effort=effort)
        configuration = replace(
            current,
            provider=provider,
            model=model,
            effort=effort,
            context_reserve_tokens=config.context_reserve_tokens,
            auto_compaction_enabled=(
                choices.auto_compaction_enabled
                if choices.has_auto_compaction_enabled
                and choices.auto_compaction_enabled is not None
                else config.auto_compaction_enabled
            ),
            web_search=config.web_search if choices.web_search is None else choices.web_search,
        )
        codemode = choices.codemode if choices.has_codemode else config.codemode
        subagents = config.subagents if choices.subagents is None else choices.subagents
        subagent_model = (
            choices.subagent_model if choices.has_subagent_model else config.subagent_model
        )
        RpcModelSelectionSnapshot(provider=provider.name, model=subagent_model)
        if (
            subagent_model is not None
            and any(entry.name == provider.name for entry in runtime.models.providers())
            and not runtime.models.knows_model(provider.name, subagent_model)
        ):
            raise ValueError("Subagent model is unavailable on the selected provider")
        overridden = {
            "provider": choices.provider is not None or choices.has_model,
            "model": choices.has_model,
            "effort": choices.has_effort,
            "auto_compaction_enabled": choices.has_auto_compaction_enabled,
            "web_search": choices.web_search is not None,
            "codemode": choices.has_codemode,
            "subagents": choices.subagents is not None,
            "subagent_model": choices.has_subagent_model,
        }
        before = {
            "provider": current.provider.name,
            "model": current.model,
            "effort": current.effort,
            "context_reserve_tokens": current.context_reserve_tokens,
            "auto_compaction_enabled": current.auto_compaction_enabled,
            "web_search": current.web_search,
            "codemode": agent.codemode,
            "subagents": agent.subagents,
            "subagent_model": agent.subagent_model,
        }
        after = {
            "provider": provider.name,
            "model": model,
            "effort": effort,
            "context_reserve_tokens": configuration.context_reserve_tokens,
            "auto_compaction_enabled": configuration.auto_compaction_enabled,
            "web_search": configuration.web_search,
            "codemode": codemode,
            "subagents": subagents,
            "subagent_model": subagent_model,
        }
        # Project transitions rebuild providers, but never move the session store
        # or change the startup wake/exposure policy. MCP can be reconnected through
        # its own command, so compare against the definitions last started there.
        restart_baseline = {
            "session_dir": self.project.startup_config.session_dir,
            "wake_on_process_exit": self.project.startup_config.wake_on_process_exit,
            "mcp_tool_exposure": self.project.startup_config.mcp_tool_exposure,
            "mcp_servers": runtime.mcp_server_configs,
        }
        restart = tuple(
            name
            for name in type(config).model_fields
            if name not in LIVE_RELOAD_FIELDS
            and getattr(config, name)
            != restart_baseline.get(name, getattr(self.project.active_config, name))
        )
        if settings.tui_keybindings != self.startup_keybindings:
            restart += ("tui_keybindings",)
        report = RpcSettingsReloaded(
            command_id=command_id,
            state=agent.state_snapshot(None).model_copy(
                update={
                    "provider": provider.name,
                    "model": model or provider.default_model,
                    "effort": effort,
                    "auto_compaction_enabled": configuration.auto_compaction_enabled,
                    "web_search": configuration.web_search,
                }
            ),
            applied=tuple(
                name
                for name in LIVE_RELOAD_FIELDS
                if not overridden.get(name) and before[name] != after[name]
            ),
            unchanged=tuple(
                name
                for name in LIVE_RELOAD_FIELDS
                if not overridden.get(name) and before[name] == after[name]
            ),
            overridden=tuple(name for name in LIVE_RELOAD_FIELDS if overridden.get(name)),
            restart_required=restart,
        )
        active_config = self.project.active_config.model_copy(
            update={name: getattr(config, name) for name in LIVE_RELOAD_FIELDS}
        )
        return _ReloadPlan(
            configuration, codemode, subagents, subagent_model, active_config, report
        )
