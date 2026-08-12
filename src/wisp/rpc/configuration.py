"""Runtime and project-configuration transitions for the RPC frontend."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

import anyio

from wisp.coding import CodingSession, resolve_coding_session_configuration
from wisp.config import WispConfig
from wisp.events import ProjectConfigApplied
from wisp.runtime.api import WispRuntime
from wisp.runtime.registry import ProviderRegistry
from wisp.skills.lifecycle import discover_skill_catalog

type RuntimeBuilder = Callable[[WispConfig], Awaitable[WispRuntime]]


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

    def effective_provider(self, default: str) -> str:
        return self.provider or default

    def effective_model(self, default: str | None) -> str | None:
        return self.model if self.has_model else default

    def effective_effort(self, default: str | None) -> str | None:
        return self.effort if self.has_effort else default


@dataclass
class RpcProjectConfiguration:
    """Own the trust-time transition from startup config to project config."""

    startup_config: WispConfig
    startup_trusted: bool
    config_overrides: _ConfigOverrides | None
    project_context_root: Path
    runtime_builder: RuntimeBuilder
    configure_overrides: _RpcConfigureOverrides = field(default_factory=_RpcConfigureOverrides)

    async def apply_trusted_project(
        self,
        *,
        runtime: WispRuntime,
        agent: CodingSession,
    ) -> ProjectConfigApplied | None:
        """Apply the trusted project layer and return its frontend event, if changed.

        The active session store is intentionally not relocated. Its path was
        selected before the first prompt and changing it mid-process would split
        one logical session across stores.
        """

        if self.startup_trusted:
            return None
        if self.config_overrides is None:
            trusted_config = self.startup_config
        else:
            trusted_config = await anyio.to_thread.run_sync(
                partial(
                    self.config_overrides.build,
                    trusted=True,
                    project_dir=self.project_context_root,
                ),
                abandon_on_cancel=True,
            )
        skill_catalog = await discover_skill_catalog(
            project_root=self.project_context_root,
            trusted=True,
            protected_paths=trusted_config.protected_paths,
        )
        overrides = self.configure_overrides
        effective_provider = overrides.effective_provider(trusted_config.provider)
        effective_model = overrides.effective_model(trusted_config.model)
        if trusted_config == self.startup_config:
            configuration = resolve_coding_session_configuration(
                trusted_config,
                providers=runtime.providers,
                models=runtime.models,
                trusted=True,
                skill_catalog=skill_catalog,
                provider_name=effective_provider,
                model=effective_model,
                has_model=overrides.has_model,
                effort=overrides.effort,
                has_effort=overrides.has_effort,
            )
        else:
            # This temporary runtime exists only to refresh provider adapters. The
            # live runtime already owns the configured MCP processes.
            provider_config = trusted_config.model_copy(update={"mcp_servers": ()})
            trusted_runtime = await self.runtime_builder(provider_config)
            try:
                staged_providers = ProviderRegistry()
                staged_providers.replace_all(runtime.providers_for_configuration(trusted_runtime))
                configuration = resolve_coding_session_configuration(
                    trusted_config,
                    providers=staged_providers,
                    models=runtime.models,
                    trusted=True,
                    skill_catalog=skill_catalog,
                    provider_name=effective_provider,
                    model=effective_model,
                    has_model=overrides.has_model,
                    effort=overrides.effort,
                    has_effort=overrides.has_effort,
                )
                await runtime.adopt_provider_configuration(trusted_runtime)
            finally:
                await trusted_runtime.aclose()
        if overrides.has_auto_compaction_enabled and overrides.auto_compaction_enabled is not None:
            configuration = replace(
                configuration,
                auto_compaction_enabled=overrides.auto_compaction_enabled,
            )
        agent.reconfigure(configuration)
        if trusted_config == self.startup_config:
            return None
        return ProjectConfigApplied(
            provider=effective_provider,
            model=effective_model,
            effort=agent.effort,
            auto_compaction_enabled=agent.auto_compaction_enabled,
            auth_path=trusted_config.auth_path,
        )


__all__ = ["RpcProjectConfiguration"]
