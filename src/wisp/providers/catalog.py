"""Model catalog: per-provider model metadata for validation and `/model` listing.

Describes which models exist and which provider they belong to. It does not
configure providers -- base URLs, auth env vars, and wire formats stay owned by
each :class:`~wisp.providers.base.Provider` implementation. The catalog is pure
reference data consumed by :class:`ModelRegistry`.
"""

from __future__ import annotations

import sys
import tomllib
from datetime import date
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_CATALOG_RESOURCE_PACKAGE = "wisp.providers.data"
_CATALOG_RESOURCE_NAME = "catalog.toml"
_USER_CATALOG_RELATIVE_PATH = Path(".wisp") / "catalog.toml"
ModelLifecycle = Literal["stable", "preview", "legacy"]


class CatalogError(ValueError):
    """Raised for a malformed or invalid catalog document."""


class UnknownModelError(KeyError):
    """Raised when a model id is not present in any catalog provider entry."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self.model_id = model_id

    def __str__(self) -> str:
        return f"Unknown model: {self.model_id}"


class AmbiguousModelError(KeyError):
    """Raised when a model id is claimed by more than one provider and unresolved.

    ``resolve`` never guesses a winner among candidate providers -- callers must
    either pass a ``prefer`` hint that matches one of them, or handle this error
    by asking (e.g. requiring an explicit ``/provider`` alongside ``/model``).
    """

    def __init__(self, model_id: str, provider_names: tuple[str, ...]) -> None:
        super().__init__(model_id, provider_names)
        self.model_id = model_id
        self.provider_names = provider_names

    def __str__(self) -> str:
        providers = ", ".join(self.provider_names)
        return f"Model {self.model_id!r} is available from multiple providers: {providers}"


class ModelPricing(BaseModel):
    """One effective-dated list-price band for a canonical model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_token_threshold: int = Field(default=0, ge=0)
    effective_from: date | None = None
    effective_until: date | None = None
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    cache_read_usd_per_million: Decimal | None = Field(default=None, ge=0)
    cache_write_usd_per_million: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_dates(self) -> ModelPricing:
        if self.effective_from is not None and self.effective_until is not None:
            if self.effective_until < self.effective_from:
                raise ValueError("pricing effective_until must not precede effective_from")
        return self


class ModelCatalogProviderEntry(BaseModel):
    """Catalog metadata for one provider's models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    display_name: str
    default_model: str
    docs_url: str
    models: tuple[str, ...]
    context_windows: dict[str, int] = {}
    # Aliases stay valid in configuration while ``models`` remains the canonical
    # picker list. This avoids showing both an alias and its target as choices.
    model_aliases: dict[str, str] = {}
    # The lifecycle label is descriptive rather than an availability gate: a
    # user's account may have different access, and unknown models still pass
    # through to the provider unchanged.
    model_lifecycle: dict[str, ModelLifecycle] = {}
    # Per-model reasoning-effort tiers, in the exact wire-format strings each
    # provider's API accepts verbatim (e.g. Anthropic's "low"/"medium"/"high"/
    # "xhigh"/"max", Google's "LOW"/"HIGH", OpenAI's "none"/"minimal"/"low"/
    # "medium"/"high"/"xhigh") -- deliberately not normalized to a shared
    # vocabulary, since the tiers differ per provider and per model within a
    # provider (e.g. Claude Haiku supports none at all). A model absent from
    # this table has no settable effort level.
    effort_levels: dict[str, tuple[str, ...]] = {}
    # One model can have both time-based and long-context price bands. Rates are
    # immutable snapshots for calculating new requests, never a source for
    # repricing historical session entries.
    pricing: dict[str, tuple[ModelPricing, ...]] = {}

    @model_validator(mode="after")
    def _validate_cross_references(self) -> ModelCatalogProviderEntry:
        if not self.models:
            raise ValueError(f"provider {self.name!r} must list at least one model")
        model_set = set(self.models)
        if len(model_set) != len(self.models):
            raise ValueError(f"provider {self.name!r} has duplicate entries in models")
        if self.default_model not in model_set:
            raise ValueError(
                f"provider {self.name!r} default_model {self.default_model!r} "
                "is not present in models"
            )
        for alias, target in self.model_aliases.items():
            if not alias:
                raise ValueError(f"provider {self.name!r} has an empty model alias")
            if alias in model_set:
                raise ValueError(
                    f"provider {self.name!r} model alias {alias!r} duplicates a canonical model"
                )
            if target not in model_set:
                raise ValueError(
                    f"provider {self.name!r} model_aliases[{alias!r}] references "
                    f"unknown model {target!r}"
                )
        for model_id, window in self.context_windows.items():
            if model_id not in model_set:
                raise ValueError(
                    f"provider {self.name!r} context_windows references unknown model {model_id!r}"
                )
            if window <= 0:
                raise ValueError(
                    f"provider {self.name!r} context_windows[{model_id!r}] "
                    f"must be positive, got {window}"
                )
        for model_id in self.model_lifecycle:
            if model_id not in model_set:
                raise ValueError(
                    f"provider {self.name!r} model_lifecycle references unknown model {model_id!r}"
                )
        for model_id, levels in self.effort_levels.items():
            if model_id not in model_set:
                raise ValueError(
                    f"provider {self.name!r} effort_levels references unknown model {model_id!r}"
                )
            if not levels:
                raise ValueError(
                    f"provider {self.name!r} effort_levels[{model_id!r}] must not be empty"
                )
            if len(set(levels)) != len(levels):
                raise ValueError(
                    f"provider {self.name!r} effort_levels[{model_id!r}] has duplicate entries"
                )
        for model_id, bands in self.pricing.items():
            if model_id not in model_set:
                raise ValueError(
                    f"provider {self.name!r} pricing references unknown model {model_id!r}"
                )
            if not bands:
                raise ValueError(f"provider {self.name!r} pricing[{model_id!r}] must not be empty")
            if not any(band.input_token_threshold == 0 for band in bands):
                raise ValueError(
                    f"provider {self.name!r} pricing[{model_id!r}] must include a base price band"
                )
            keys = {
                (band.effective_from, band.effective_until, band.input_token_threshold)
                for band in bands
            }
            if len(keys) != len(bands):
                raise ValueError(
                    f"provider {self.name!r} pricing[{model_id!r}] has duplicate price bands"
                )
            for index, band in enumerate(bands):
                for other in bands[index + 1 :]:
                    if band.input_token_threshold != other.input_token_threshold:
                        continue
                    starts_before_end = (
                        band.effective_from is None
                        or other.effective_until is None
                        or band.effective_from <= other.effective_until
                    )
                    other_starts_before_end = (
                        other.effective_from is None
                        or band.effective_until is None
                        or other.effective_from <= band.effective_until
                    )
                    if starts_before_end and other_starts_before_end:
                        raise ValueError(
                            f"provider {self.name!r} pricing[{model_id!r}] has overlapping "
                            "price bands"
                        )
        return self

    def canonical_model(self, model_id: str) -> str | None:
        """Return a canonical model id for a listed id or alias."""

        if model_id in self.models:
            return model_id
        return self.model_aliases.get(model_id)

    def pricing_for(
        self,
        model_id: str,
        *,
        input_tokens: int,
        at: date,
    ) -> tuple[str, ModelPricing] | None:
        """Return the most specific effective price band for a model or alias."""

        canonical = self.canonical_model(model_id)
        if canonical is None:
            return None
        candidates = [
            band
            for band in self.pricing.get(canonical, ())
            if band.input_token_threshold <= input_tokens
            and (band.effective_from is None or band.effective_from <= at)
            and (band.effective_until is None or at <= band.effective_until)
        ]
        if not candidates:
            return None
        return canonical, max(candidates, key=lambda band: band.input_token_threshold)


class ModelCatalog(BaseModel):
    """A validated set of provider model catalogs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1, 2]
    providers: tuple[ModelCatalogProviderEntry, ...]

    @model_validator(mode="after")
    def _validate_unique_provider_names(self) -> ModelCatalog:
        names = [provider.name for provider in self.providers]
        if len(set(names)) != len(names):
            raise ValueError("catalog has duplicate provider names")
        return self


def _catalog_from_toml(text: str, *, source: str) -> ModelCatalog:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"could not parse {source}: {exc}") from exc
    try:
        return ModelCatalog.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(f"invalid catalog in {source}: {exc}") from exc


def builtin_catalog() -> ModelCatalog:
    """Load and validate the packaged built-in catalog."""

    text = (
        files(_CATALOG_RESOURCE_PACKAGE)
        .joinpath(_CATALOG_RESOURCE_NAME)
        .read_text(encoding="utf-8")
    )
    return _catalog_from_toml(text, source="built-in catalog.toml")


def user_catalog_path(*, home_dir: Path | None = None) -> Path:
    """Return the user-only catalog overlay path (``~/.wisp/catalog.toml``)."""

    home = home_dir if home_dir is not None else Path.home()
    return home / _USER_CATALOG_RELATIVE_PATH


def _merge_catalog(base: ModelCatalog, overlay: ModelCatalog) -> ModelCatalog:
    """Merge an overlay catalog into a base catalog.

    A provider present in both is replaced wholesale by the overlay's entry
    (matching :class:`~wisp.runtime.registry.ProviderRegistry`'s replace-by-default
    semantics) rather than merged field-by-field, so an overlay entry is always
    self-consistent and independently valid.
    """

    merged: dict[str, ModelCatalogProviderEntry] = {p.name: p for p in base.providers}
    for provider in overlay.providers:
        merged[provider.name] = provider
    return ModelCatalog(schema_version=base.schema_version, providers=tuple(merged.values()))


def effective_catalog(*, home_dir: Path | None = None) -> ModelCatalog:
    """Return the built-in catalog with the user overlay applied, if present and valid.

    A missing overlay file is normal and silent. An overlay file that exists but
    fails to parse or validate is a user error worth surfacing, so it is warned to
    stderr and the built-in-only catalog is returned rather than aborting startup.

    Project-local catalogs are never read -- this function takes no ``project_dir``
    parameter. A cloned repository must not be able to redirect provider metadata.
    """

    base = builtin_catalog()
    path = user_catalog_path(home_dir=home_dir)
    if not path.exists():
        return base
    try:
        overlay_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _warn(f"could not read catalog file {path}: {exc}")
        return base
    try:
        overlay = _catalog_from_toml(overlay_text, source=str(path))
    except CatalogError as exc:
        _warn(f"ignoring invalid catalog file {path}: {exc}")
        return base
    return _merge_catalog(base, overlay)


def _warn(message: str) -> None:
    print(f"wisp: {message}", file=sys.stderr)


class ModelRegistry:
    """Read-only view over a :class:`ModelCatalog`, resolving model ids to metadata.

    A canonical model id or alias is not guaranteed unique across the whole
    catalog: the same OpenAI model can legitimately be served by ``openai`` and
    ``openai-codex`` through different auth. :meth:`resolve` returns every
    provider that claims a given value rather than silently picking one.
    """

    def __init__(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog
        self._by_id: dict[str, tuple[ModelCatalogProviderEntry, ...]] = {}
        for provider in catalog.providers:
            for model_id in provider.models:
                self._by_id[model_id] = (*self._by_id.get(model_id, ()), provider)
            for alias in provider.model_aliases:
                self._by_id[alias] = (*self._by_id.get(alias, ()), provider)

    def resolve(
        self, model_id: str, *, prefer: str | None = None
    ) -> tuple[str, ModelCatalogProviderEntry]:
        """Return the single ``(provider_name, provider_entry)`` a model id resolves to.

        Raises :class:`UnknownModelError` if the model is not present in any
        provider's catalog entry. Callers that want permissive fallthrough for
        free-text model strings should catch this rather than pre-checking
        membership.

        Raises :class:`AmbiguousModelError` if more than one provider claims the
        id and ``prefer`` (typically the caller's currently active provider) does
        not disambiguate it -- callers must not guess which provider to switch to.
        """

        candidates = self._by_id.get(model_id)
        if not candidates:
            raise UnknownModelError(model_id) from None
        if len(candidates) == 1:
            entry = candidates[0]
            return entry.name, entry
        if prefer is not None:
            for entry in candidates:
                if entry.name == prefer:
                    return entry.name, entry
        raise AmbiguousModelError(model_id, tuple(entry.name for entry in candidates))

    def list_models(self, *, provider: str | None = None) -> tuple[tuple[str, str], ...]:
        """Return canonical ``(provider_name, model_id)`` pairs, optionally filtered."""

        pairs = [
            (entry.name, model_id)
            for entry in self._catalog.providers
            for model_id in entry.models
            if provider is None or entry.name == provider
        ]
        return tuple(pairs)

    def providers(self) -> tuple[ModelCatalogProviderEntry, ...]:
        """Return every provider entry in the effective catalog."""

        return self._catalog.providers

    def supports_effort(self, provider_name: str, model_id: str, effort: str) -> bool:
        """Return whether ``effort`` is one of ``model_id``'s catalog-listed tiers.

        Effort tiers are provider-native, non-normalized strings (Anthropic's
        lowercase ``"high"`` vs. Google's uppercase ``"HIGH"``, for instance) --
        a tier valid for one provider/model is not just "probably fine" on
        another, it can be outright rejected by that provider's API. Permissive
        by design like the rest of this module: an unrecognized provider or
        model returns ``False`` rather than raising, so callers get a plain
        yes/no to gate a stored value against, not another error to catch.

        Does not distinguish "model known, tier not listed" from "model
        unknown to this provider" -- both return ``False``. A caller that
        needs to treat an unknown model permissively (e.g. a brand-new model
        ahead of a catalog update) must check :meth:`knows_model` first.
        """

        for entry in self._catalog.providers:
            if entry.name != provider_name:
                continue
            canonical_model = entry.canonical_model(model_id)
            return canonical_model is not None and effort in entry.effort_levels.get(
                canonical_model, ()
            )
        return False

    def knows_model(self, provider_name: str, model_id: str) -> bool:
        """Return whether ``provider_name`` recognizes ``model_id`` or an alias."""

        for entry in self._catalog.providers:
            if entry.name != provider_name:
                continue
            return entry.canonical_model(model_id) is not None
        return False

    def canonical_model(self, provider_name: str, model_id: str) -> str | None:
        """Return a provider-scoped canonical model id, including aliases."""

        for entry in self._catalog.providers:
            if entry.name == provider_name:
                return entry.canonical_model(model_id)
        return None

    def pricing(
        self,
        provider_name: str,
        model_id: str,
        *,
        input_tokens: int,
        at: date | None = None,
    ) -> tuple[str, ModelPricing] | None:
        """Look up a provider-scoped price band without guessing unknown models."""

        if input_tokens < 0:
            raise ValueError("input_tokens must be non-negative")
        for entry in self._catalog.providers:
            if entry.name == provider_name:
                return entry.pricing_for(
                    model_id,
                    input_tokens=input_tokens,
                    at=at or date.today(),
                )
        return None

    def model_lifecycle(self, provider_name: str, model_id: str) -> ModelLifecycle | None:
        """Return the lifecycle label for a model or alias, when cataloged."""

        for entry in self._catalog.providers:
            if entry.name != provider_name:
                continue
            canonical_model = entry.canonical_model(model_id)
            if canonical_model is None:
                return None
            return entry.model_lifecycle.get(canonical_model)
        return None

    def context_window(
        self,
        provider_name: str,
        model: str | None,
        *,
        default_model: str | None = None,
    ) -> int | None:
        """Return the catalog context window for the effective provider/model."""

        effective_model = model if model is not None else default_model
        if effective_model is None:
            return None
        for entry in self._catalog.providers:
            if entry.name == provider_name:
                canonical_model = entry.canonical_model(effective_model)
                if canonical_model is None:
                    return None
                return entry.context_windows.get(canonical_model)
        return None


def startup_effort(
    registry: ModelRegistry,
    *,
    provider_name: str,
    model: str | None,
    default_model: str | None,
    effort: str | None,
) -> str | None:
    """Return ``effort`` if valid for the startup provider/model, else ``None``.

    User effort is normally persisted alongside the last TUI provider/model
    selection, but higher-precedence environment or trusted-project settings can
    still pair it with a different provider/model. A tier chosen for one provider
    (e.g. Google's uppercase ``"HIGH"``) can be an invalid wire value elsewhere.
    Called once at
    session construction, before the first prompt, so a stale persisted tier
    never reaches a provider it was never chosen for.

    Permissive for a catalog-unknown provider/model (e.g. ``WISP_MODEL``/
    ``WISP_EFFORT`` set explicitly for a brand-new model ahead of a catalog
    update, or a custom provider) -- ``effort`` passes through unchanged
    rather than being dropped, the same way ``TuiShell._validated_effort``
    treats a typed ``/model <id> <effort>`` for an unresolvable model. Only a
    *known* model whose catalog entry doesn't list ``effort`` among its tiers
    is actually invalid and gets dropped; ``supports_effort`` alone can't
    distinguish the two cases (both return ``False``), which is why this
    checks ``knows_model`` first.
    """

    if effort is None:
        return None
    effective_model = model if model is not None else default_model
    if effective_model is None:
        return effort
    if not registry.knows_model(provider_name, effective_model):
        return effort
    if registry.supports_effort(provider_name, effective_model, effort):
        return effort
    return None


__all__ = [
    "AmbiguousModelError",
    "CatalogError",
    "ModelCatalog",
    "ModelCatalogProviderEntry",
    "ModelPricing",
    "ModelLifecycle",
    "ModelRegistry",
    "UnknownModelError",
    "builtin_catalog",
    "effective_catalog",
    "startup_effort",
    "user_catalog_path",
]
