"""Tests for `ModelRegistry`: resolving model ids and listing catalog contents."""

from __future__ import annotations

import pytest

from wisp.providers.catalog import (
    AmbiguousModelError,
    ModelCatalog,
    ModelRegistry,
    UnknownModelError,
    startup_effort,
)


def _catalog(*providers: dict[str, object]) -> ModelCatalog:
    return ModelCatalog.model_validate({"schema_version": 1, "providers": list(providers)})


def _provider(
    name: str,
    models: list[str],
    *,
    default_model: str | None = None,
    effort_levels: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "display_name": name.title(),
        "default_model": default_model or models[0],
        "docs_url": "https://example.com/docs",
        "models": models,
        "effort_levels": effort_levels or {},
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


def test_supports_effort_true_for_a_listed_tier() -> None:
    registry = ModelRegistry(
        _catalog(_provider("acme", ["acme-1"], effort_levels={"acme-1": ["low", "high"]}))
    )

    assert registry.supports_effort("acme", "acme-1", "high") is True


def test_supports_effort_false_for_an_unlisted_tier() -> None:
    registry = ModelRegistry(
        _catalog(_provider("acme", ["acme-1"], effort_levels={"acme-1": ["low", "high"]}))
    )

    assert registry.supports_effort("acme", "acme-1", "medium") is False


def test_supports_effort_false_when_provider_uses_different_vocabulary() -> None:
    # Regression fixture: effort tiers are provider-native strings (Anthropic's
    # lowercase "high" vs. Google's uppercase "HIGH") -- a tier valid for one
    # provider/model must not be treated as valid for another just because the
    # spelling happens to collide in a differently-cased catalog.
    registry = ModelRegistry(
        _catalog(
            _provider("google", ["gemini-x"], effort_levels={"gemini-x": ["LOW", "HIGH"]}),
            _provider("openai", ["gpt-x"], effort_levels={"gpt-x": ["low", "high"]}),
        )
    )

    assert registry.supports_effort("openai", "gpt-x", "HIGH") is False
    assert registry.supports_effort("google", "gemini-x", "high") is False


def test_supports_effort_false_for_a_model_with_no_effort_levels_entry() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    assert registry.supports_effort("acme", "acme-1", "high") is False


def test_supports_effort_false_for_an_unknown_provider() -> None:
    registry = ModelRegistry(
        _catalog(_provider("acme", ["acme-1"], effort_levels={"acme-1": ["high"]}))
    )

    assert registry.supports_effort("nonexistent", "acme-1", "high") is False


def test_knows_model_true_for_a_listed_model() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    assert registry.knows_model("acme", "acme-1") is True


def test_knows_model_false_for_an_unlisted_model() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    assert registry.knows_model("acme", "acme-2") is False


def test_knows_model_false_for_an_unknown_provider() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    assert registry.knows_model("nonexistent", "acme-1") is False


def test_startup_effort_none_passes_through_as_none() -> None:
    registry = ModelRegistry(
        _catalog(_provider("acme", ["acme-1"], effort_levels={"acme-1": ["high"]}))
    )

    result = startup_effort(
        registry, provider_name="acme", model="acme-1", default_model="acme-1", effort=None
    )

    assert result is None


def test_startup_effort_keeps_a_tier_valid_for_the_explicit_model() -> None:
    registry = ModelRegistry(
        _catalog(_provider("acme", ["acme-1"], effort_levels={"acme-1": ["low", "high"]}))
    )

    result = startup_effort(
        registry, provider_name="acme", model="acme-1", default_model="acme-1", effort="high"
    )

    assert result == "high"


def test_startup_effort_falls_back_to_provider_default_model_when_model_is_none() -> None:
    registry = ModelRegistry(
        _catalog(_provider("acme", ["acme-1"], effort_levels={"acme-1": ["high"]}))
    )

    result = startup_effort(
        registry, provider_name="acme", model=None, default_model="acme-1", effort="high"
    )

    assert result == "high"


def test_startup_effort_drops_a_tier_from_a_different_providers_vocabulary() -> None:
    # Regression test (Codex review on #125): persisted effort has no
    # provider/model scope -- a global settings.json string chosen while on
    # Google (uppercase "HIGH") must not survive into a session that starts on
    # a provider/model whose catalog entry doesn't recognize that string,
    # rather than being sent verbatim to that provider's API.
    registry = ModelRegistry(
        _catalog(_provider("openai", ["gpt-x"], effort_levels={"gpt-x": ["low", "high"]}))
    )

    result = startup_effort(
        registry, provider_name="openai", model="gpt-x", default_model="gpt-x", effort="HIGH"
    )

    assert result is None


def test_startup_effort_drops_a_tier_the_model_does_not_support_at_all() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    result = startup_effort(
        registry, provider_name="acme", model="acme-1", default_model="acme-1", effort="high"
    )

    assert result is None


def test_startup_effort_drops_a_tier_for_an_unresolvable_model() -> None:
    registry = ModelRegistry(_catalog(_provider("acme", ["acme-1"])))

    result = startup_effort(
        registry,
        provider_name="acme",
        model="nonexistent-model",
        default_model="acme-1",
        effort="high",
    )

    assert result is None
