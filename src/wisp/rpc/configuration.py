"""Runtime and project-configuration transitions for the RPC frontend."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

import anyio
from typing_extensions import TypeAliasType

from wisp.coding import (
    CodingSession,
    CodingSessionConfiguration,
    resolve_coding_session_configuration,
)
from wisp.config.runtime import WispConfig
from wisp.events import ProjectConfigApplied
from wisp.runtime.api import WispRuntime
from wisp.runtime.registry import ProviderRegistry
from wisp.skills.lifecycle import discover_skill_catalog
from wisp.tools.context import ToolContext

RuntimeBuilder = TypeAliasType("RuntimeBuilder", Callable[[WispConfig], Awaitable[WispRuntime]])


@dataclass(frozen=True)
class _ConfigOverrides:
    """Explicit CLI overrides retained for a post-trust config rebuild."""

    provider: str | None = None
    model: str | None = None
    session_dir: Path | None = None
    auth_path: Path | None = None

    def build(self, *, trusted: bool, project_dir: Path | None = None) -> WispConfig:
        return WispConfig.from_env(
            provider=self.provider,
            model=self.model,
            session_dir=self.session_dir,
            auth_path=self.auth_path,
            project_dir=project_dir,
            trusted=trusted,
        )


@dataclass
class _RpcConfigureOverrides:
    """Successful in-session RPC configure choices that outrank project settings."""

    provider: str | None = None
    model: str | None = None
    has_model: bool = False
    effort: str | None = None
    has_effort: bool = False
    auto_compaction_enabled: bool | None = None
    has_auto_compaction_enabled: bool = False
    web_search: bool | None = None

    def effective_provider(self, default: str) -> str:
        return self.provider or default

    def effective_model(self, default: str | None) -> str | None:
        return self.model if self.has_model else default

    def effective_effort(self, default: str | None) -> str | None:
        return self.effort if self.has_effort else default


@dataclass
class RpcProjectConfiguration:
    """Own the transitions between project configurations.

    The session starts in ``project_context_root`` with ``startup_config``. Trusting
    that project, or moving the session into another one, applies that project's
    configuration; ``active_config`` and ``trusted`` describe the one in effect.
    """

    startup_config: WispConfig
    startup_trusted: bool
    config_overrides: _ConfigOverrides | None
    project_context_root: Path
    runtime_builder: RuntimeBuilder
    configure_overrides: _RpcConfigureOverrides = field(default_factory=_RpcConfigureOverrides)
    # The configuration in effect, and whether its project is trusted. Provider
    # adapters are rebuilt only when a new configuration differs from this one.
    active_config: WispConfig = field(init=False)
    trusted: bool = field(init=False)

    def __post_init__(self) -> None:
        self.active_config = self.startup_config
        self.trusted = self.startup_trusted

    async def apply_trusted_project(
        self,
        *,
        runtime: WispRuntime,
        agent: CodingSession,
        reserve_protected_paths: Callable[[tuple[str, ...]], None] | None = None,
    ) -> ProjectConfigApplied | None:
        """Apply the current project's trusted layer once its trust is granted.

        The active session store is intentionally not relocated. Its path was
        selected before the first prompt and changing it mid-process would split
        one logical session across stores.

        Args:
            runtime (WispRuntime): Live runtime whose provider configuration is adopted.
            agent (CodingSession): Session reconfigured after successful adoption.
            reserve_protected_paths (Callable | None): Reserves candidate secrets before
                adoption can change the auth store. Protection survives later failure.

        Returns:
            ProjectConfigApplied | None: Updated frontend configuration, or None when
                the project was already trusted or its configuration is unchanged.
        """

        if self.trusted:
            return None
        return await self.apply_project(
            root=self.project_context_root,
            trusted=True,
            runtime=runtime,
            cwd=agent.tool_context.cwd,
            retained_protected_paths=agent.tool_context.protected_paths,
            commit=agent.reconfigure,
            reserve_protected_paths=reserve_protected_paths,
        )

    async def apply_project(
        self,
        *,
        root: Path,
        trusted: bool,
        runtime: WispRuntime,
        cwd: Path,
        retained_protected_paths: tuple[str, ...],
        commit: Callable[[CodingSessionConfiguration], None],
        record: Callable[[], Awaitable[object]] | None = None,
        reserve_protected_paths: Callable[[tuple[str, ...]], None] | None = None,
    ) -> ProjectConfigApplied | None:
        """Apply ``root``'s configuration, trusted or not, to the session.

        Everything that can fail without side effects runs first: building the
        configuration, discovering skills, and staging provider adapters. Then
        ``record`` runs (a move saves itself there), the live runtime adopts the
        staged providers when the configuration changed them, and ``commit`` hands
        the new session configuration to the agent.

        Args:
            root (Path): The project root to apply.
            trusted (bool): Whether ``root`` is trusted; an untrusted project applies
                no project settings layer, skills, or instruction files.
            runtime (WispRuntime): Live runtime whose provider configuration is adopted.
            cwd (Path): The working directory the new configuration's tools use.
            retained_protected_paths (tuple[str, ...]): Paths the tools protect now.
                They stay protected alongside the new configuration's, so a credential
                file only the previous project's settings named (its ``auth_path``)
                is not exposed by a move whose tools can reach it.
            commit (Callable): Applies the session configuration to the agent.
            record (Callable | None): Runs before any live change, such as saving a
                move. Defaults to None.
            reserve_protected_paths (Callable | None): Reserves candidate secrets before
                adoption can change the auth store. Protection survives later failure.

        Returns:
            ProjectConfigApplied | None: Updated frontend configuration, or None when
                the configuration in effect is unchanged.
        """

        config = await self._config_for(root, trusted=trusted)
        if reserve_protected_paths is not None:
            # Reserve before building/adopting providers: adoption can change the
            # auth store before cleanup or agent reconfiguration succeeds.
            protected_context = await anyio.to_thread.run_sync(
                partial(ToolContext.from_config, config, cwd=cwd),
                abandon_on_cancel=True,
            )
            reserve_protected_paths(protected_context.protected_paths)
        skill_catalog = await discover_skill_catalog(
            project_root=root,
            trusted=trusted,
            protected_paths=config.protected_paths,
        )
        overrides = self.configure_overrides
        effective_provider = overrides.effective_provider(config.provider)
        effective_model = overrides.effective_model(config.model)

        def configuration_for(providers: ProviderRegistry) -> CodingSessionConfiguration:
            configuration = resolve_coding_session_configuration(
                config,
                providers=providers,
                models=runtime.models,
                trusted=trusted,
                cwd=cwd,
                project_root=root,
                skill_catalog=skill_catalog,
                provider_name=effective_provider,
                model=effective_model,
                has_model=overrides.has_model,
                effort=overrides.effort,
                has_effort=overrides.has_effort,
            )
            if (
                overrides.has_auto_compaction_enabled
                and overrides.auto_compaction_enabled is not None
            ):
                configuration = replace(
                    configuration,
                    auto_compaction_enabled=overrides.auto_compaction_enabled,
                )
            if overrides.web_search is not None:
                configuration = replace(configuration, web_search=overrides.web_search)
            tool_context = configuration.tool_context
            protected_paths = tuple(
                dict.fromkeys((*tool_context.protected_paths, *retained_protected_paths))
            )
            return replace(
                configuration,
                tool_context=replace(tool_context, protected_paths=protected_paths),
            )

        changed = config != self.active_config
        if not changed:
            configuration = configuration_for(runtime.providers)
            if record is not None:
                await record()
        else:
            # This temporary runtime exists only to refresh provider adapters. The
            # live runtime already owns the configured MCP processes.
            provider_config = config.model_copy(update={"mcp_servers": ()})
            staged_runtime = await self.runtime_builder(provider_config)
            try:
                staged_providers = ProviderRegistry()
                staged_providers.replace_all(runtime.providers_for_configuration(staged_runtime))
                configuration = configuration_for(staged_providers)
                if record is not None:
                    await record()
                await runtime.adopt_provider_configuration(staged_runtime)
            finally:
                await staged_runtime.aclose()
        commit(configuration)
        self.active_config = config
        self.project_context_root = root
        self.trusted = trusted
        if not changed:
            return None
        return ProjectConfigApplied(
            provider=effective_provider,
            model=effective_model,
            effort=configuration.effort,
            auto_compaction_enabled=configuration.auto_compaction_enabled,
            auth_path=config.auth_path,
        )

    async def _config_for(self, root: Path, *, trusted: bool) -> WispConfig:
        """Build the configuration a session in ``root`` runs with.

        An embedder that passed an explicit configuration has no settings layers to
        rebuild, so its configuration applies to every project.
        """

        if self.config_overrides is None:
            return self.startup_config
        return await anyio.to_thread.run_sync(
            partial(self.config_overrides.build, trusted=trusted, project_dir=root),
            abandon_on_cancel=True,
        )


__all__ = ["RpcProjectConfiguration"]
