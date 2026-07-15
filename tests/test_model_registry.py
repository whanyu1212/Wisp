"""Tests for `ModelRegistry`: resolving model ids and listing catalog contents."""

from __future__ import annotations

import pytest

from wisp.providers.catalog import (
    AmbiguousModelError,
    ModelCatalog,
    ModelRegistry,
    UnknownModelError,
)


def _catalog(*providers: dict[str, object]) -> ModelCatalog:
    return ModelCatalog.model_validate({"schema_version": 1, "providers": list(providers)})


def _provider(
    name: str, models: list[str], *, default_model: str | None = None
) -> dict[str, object]:
    return {
        "name": name,
        "display_name": name.title(),
        "default_model": default_model or models[0],
        "docs_url": "https://example.com/docs",
        "models": models,
    }


def test_resolve_returns_the_sole_provider_for_a_unique_model() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    provider_name, entry = registry.resolve("acme-1")

    assert provider_name == "acme"
    assert entry.name == "acme"


def test_resolve_raises_unknown_model_error_for_an_unlisted_id() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    with pytest.raises(UnknownModelError) as excinfo:
        registry.resolve("nonexistent")

    assert excinfo.value.model_id == "nonexistent"


def test_resolve_raises_ambiguous_model_error_without_a_matching_prefer() -> None:
    registry = ModelRegistry(
        _catalog(
            _provider("acme", ["shared"]),
            _provider("acme-alt", ["shared"]),
        )
    )

    with pytest.raises(AmbiguousModelError) as excinfo:
        registry.resolve("shared")

    assert excinfo.value.model_id == "shared"
    assert set(excinfo.value.provider_names) == {"acme", "acme-alt"}


def test_resolve_uses_prefer_to_disambiguate() -> None:
    registry = ModelRegistry(
        _catalog(
            _provider("acme", ["shared"]),
            _provider("acme-alt", ["shared"]),
        )
    )

    provider_name, _entry = registry.resolve("shared", prefer="acme-alt")

    assert provider_name == "acme-alt"


def test_resolve_still_raises_ambiguous_when_prefer_does_not_match_any_candidate() -> None:
    registry = ModelRegistry(
        _catalog(
            _provider("acme", ["shared"]),
            _provider("acme-alt", ["shared"]),
        )
    )

    with pytest.raises(AmbiguousModelError):
        registry.resolve("shared", prefer="unrelated-provider")


def test_resolve_prefer_is_irrelevant_when_the_model_is_unique() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    provider_name, _entry = registry.resolve("acme-1", prefer="some-other-provider")

    assert provider_name == "acme"


def test_list_models_returns_every_provider_model_pair() -> None:
    registry = ModelRegistry(
        _catalog(
            _provider("acme", ["acme-1", "acme-2"]),
            _provider("beta", ["beta-1"]),
        )
    )

    pairs = registry.list_models()

    assert set(pairs) == {("acme", "acme-1"), ("acme", "acme-2"), ("beta", "beta-1")}


def test_list_models_filters_by_provider() -> None:
    registry = ModelRegistry(
        _catalog(
            _provider("acme", ["acme-1", "acme-2"]),
            _provider("beta", ["beta-1"]),
        )
    )

    pairs = registry.list_models(provider="acme")

    assert set(pairs) == {("acme", "acme-1"), ("acme", "acme-2")}


def test_list_models_filter_on_unknown_provider_is_empty() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    assert registry.list_models(provider="nonexistent") == ()


def test_providers_returns_every_catalog_entry() -> None:
    catalog = _catalog(_provider("acme", ["acme-1"]), _provider("beta", ["beta-1"]))
    registry = ModelRegistry(catalog)

    assert {entry.name for entry in registry.providers()} == {"acme", "beta"}
