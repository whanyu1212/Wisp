"""Typed configuration transitions for :class:`~wisp.coding.CodingSession`."""

from __future__ import annotations

from dataclasses import dataclass, field

from wisp.config import WispConfig
from wisp.providers.base import Provider
from wisp.providers.catalog import ModelRegistry, startup_effort
from wisp.runtime.registry import ProviderRegistry
from wisp.skills.models import SkillCatalog
from wisp.tools.context import ToolContext


@dataclass(frozen=True, slots=True)
class CodingSessionConfiguration:
    """The runtime-derived settings that may change between agent operations.

    Session persistence, event delivery, tool exposure, and approval grants are
    deliberately absent: they are owned by a live :class:`CodingSession` and
    must survive a trusted-project transition.
    """

    provider: Provider
    model: str | None
    effort: str | None
    models: ModelRegistry | None
    tool_context: ToolContext
    trusted: bool
    context_reserve_tokens: int
    auto_compaction_enabled: bool
    skill_catalog: SkillCatalog = field(default_factory=SkillCatalog)


def resolve_coding_session_configuration(
    config: WispConfig,
    *,
    providers: ProviderRegistry,
    models: ModelRegistry | None,
    trusted: bool,
    skill_catalog: SkillCatalog | None = None,
    provider_name: str | None = None,
    model: str | None = None,
    has_model: bool = False,
    effort: str | None = None,
    has_effort: bool = False,
) -> CodingSessionConfiguration:
    """Resolve one authoritative configuration for a coding session operation.

    Explicit in-session provider, model, and effort choices outrank the supplied
    config. Persisted/default effort remains scoped to its effective provider and
    model; explicit effort is intentionally permissive for catalog-unknown models.
    """

    provider = providers.get(provider_name or config.provider)
    selected_model = model if has_model else config.model
    if has_effort:
        selected_effort = effort
    elif models is None:
        # Embedders may construct a CodingSession without catalog metadata. Keep
        # their configured value rather than rejecting a tier we cannot validate.
        selected_effort = config.effort
    else:
        selected_effort = startup_effort(
            models,
            provider_name=provider.name,
            model=selected_model,
            default_model=provider.default_model,
            effort=config.effort,
        )
    return CodingSessionConfiguration(
        provider=provider,
        model=selected_model,
        effort=selected_effort,
        models=models,
        tool_context=ToolContext.from_config(config),
        skill_catalog=skill_catalog or SkillCatalog(),
        trusted=trusted,
        context_reserve_tokens=config.context_reserve_tokens,
        auto_compaction_enabled=config.auto_compaction_enabled,
    )
