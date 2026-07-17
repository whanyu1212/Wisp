"""Model catalog: per-provider model metadata for validation and `/model` listing.

Describes which models exist and which provider they belong to. It does not
configure providers -- base URLs, auth env vars, and wire formats stay owned by
each :class:`~wisp.providers.base.Provider` implementation. The catalog is pure
reference data consumed by :class:`ModelRegistry`.
"""

from __future__ import annotations

import sys
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

_CATALOG_RESOURCE_PACKAGE = "wisp.providers.data"
_CATALOG_RESOURCE_NAME = "catalog.toml"
_USER_CATALOG_RELATIVE_PATH = Path(".wisp") / "catalog.toml"


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


class ModelCatalogProviderEntry(BaseModel):
    """Catalog metadata for one provider's models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    display_name: str
    default_model: str
    docs_url: str
    models: tuple[str, ...]
    context_windows: dict[str, int] = {}
    # Per-model reasoning-effort tiers, in the exact wire-format strings each
    # provider's API accepts verbatim (e.g. Anthropic's "low"/"medium"/"high"/
    # "xhigh"/"max", Google's "LOW"/"HIGH", OpenAI's "none"/"minimal"/"low"/
    # "medium"/"high"/"xhigh") -- deliberately not normalized to a shared
    # vocabulary, since the tiers differ per provider and per model within a
    # provider (e.g. Claude Haiku supports none at all). A model absent from
    # this table has no settable effort level.
    effort_levels: dict[str, tuple[str, ...]] = {}

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
        return self


class ModelCatalog(BaseModel):
    """A validated set of provider model catalogs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
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

    A model id is not guaranteed unique across the whole catalog: the same model
    name can legitimately be served by more than one provider (for example
    ``openai`` and ``openai-codex`` both accept the same OpenAI model names,
    reached through different auth). :meth:`resolve` returns every provider that
    claims a given id rather than silently picking one.
    """

    def __init__(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog
        self._by_id: dict[str, tuple[ModelCatalogProviderEntry, ...]] = {}
        for provider in catalog.providers:
            for model_id in provider.models:
                self._by_id[model_id] = (*self._by_id.get(model_id, ()), provider)

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
        """Return ``(provider_name, model_id)`` pairs, optionally filtered by provider."""

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
            return effort in entry.effort_levels.get(model_id, ())
        return False

    def knows_model(self, provider_name: str, model_id: str) -> bool:
        """Return whether ``provider_name``'s catalog entry lists ``model_id`` at all."""

        for entry in self._catalog.providers:
            if entry.name != provider_name:
                continue
            return model_id in entry.models
        return False


def startup_effort(
    registry: ModelRegistry,
    *,
    provider_name: str,
    model: str | None,
    default_model: str | None,
    effort: str | None,
) -> str | None:
    """Return ``effort`` if valid for the startup provider/model, else ``None``.

    Persisted effort (see :func:`wisp.settings.persist_user_effort`) is a
    single global string with no provider/model scope -- a tier chosen for
    one provider/model (e.g. Google's uppercase ``"HIGH"``) is not just
    unlikely to suit whatever provider/model a later session actually starts
    on, it can be an outright invalid wire value there. Called once at
    session construction, before the first prompt, so a stale persisted tier
    never reaches a provider it was never chosen for. Permissive by design:
    ``effort=None`` or an unresolvable model both pass through as ``None``
    rather than raising -- this is a startup safety net, not a validator that
    should block launch.
    """

    if effort is None:
        return None
    effective_model = model if model is not None else default_model
    if effective_model is None:
        return None
    if registry.supports_effort(provider_name, effective_model, effort):
        return effort
    return None


__all__ = [
    "AmbiguousModelError",
    "CatalogError",
    "ModelCatalog",
    "ModelCatalogProviderEntry",
    "ModelRegistry",
    "UnknownModelError",
    "builtin_catalog",
    "effective_catalog",
    "startup_effort",
    "user_catalog_path",
]
