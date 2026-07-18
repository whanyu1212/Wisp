"""Tests for the model catalog: parsing, validation, and the user overlay."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import CaptureFixture

from wisp.providers.anthropic import DEFAULT_ANTHROPIC_MODEL
from wisp.providers.catalog import (
    CatalogError,
    ModelCatalog,
    ModelCatalogProviderEntry,
    ModelRegistry,
    builtin_catalog,
    effective_catalog,
    user_catalog_path,
)
from wisp.providers.google import DEFAULT_GOOGLE_MODEL
from wisp.providers.openai import DEFAULT_OPENAI_MODEL
from wisp.providers.openai_codex import DEFAULT_OPENAI_CODEX_MODEL

_MINIMAL_TOML = """
schema_version = 1

[[providers]]
name = "acme"
display_name = "Acme"
default_model = "acme-1"
docs_url = "https://example.com/docs"
models = ["acme-1", "acme-2"]

[providers.context_windows]
acme-1 = 8000
acme-2 = 16000
"""


def _write_overlay(home: Path, text: str) -> Path:
    path = user_catalog_path(home_dir=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_builtin_catalog_loads_and_validates() -> None:
    catalog = builtin_catalog()

    assert catalog.schema_version == 1
    names = {provider.name for provider in catalog.providers}
    assert {"openai", "openai-codex", "fake"} <= names


def test_minimal_catalog_parses() -> None:
    catalog = ModelCatalog.model_validate(
        {
            "schema_version": 1,
            "providers": [
                {
                    "name": "acme",
                    "display_name": "Acme",
                    "default_model": "acme-1",
                    "docs_url": "https://example.com/docs",
                    "models": ["acme-1"],
                }
            ],
        }
    )

    assert catalog.providers[0].name == "acme"
    assert catalog.providers[0].context_windows == {}
    assert catalog.providers[0].model_aliases == {}
    assert catalog.providers[0].model_lifecycle == {}
    assert catalog.providers[0].effort_levels == {}


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelCatalog.model_validate(
            {
                "schema_version": 1,
                "providers": [],
                "future_field": "nope",
            }
        )


def test_unknown_provider_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "base_url": "https://example.com",
            }
        )


def test_default_model_must_be_in_models() -> None:
    with pytest.raises(ValidationError, match="is not present in models"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-missing",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
            }
        )


def test_context_window_must_reference_a_known_model() -> None:
    with pytest.raises(ValidationError, match="unknown model"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "context_windows": {"acme-2": 1000},
            }
        )


def test_context_window_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="must be positive"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "context_windows": {"acme-1": 0},
            }
        )


def test_model_alias_must_target_a_known_canonical_model() -> None:
    with pytest.raises(ValidationError, match="model_aliases.*unknown model"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "model_aliases": {"acme-latest": "acme-missing"},
            }
        )


def test_model_alias_cannot_duplicate_a_canonical_model() -> None:
    with pytest.raises(ValidationError, match="duplicates a canonical model"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "model_aliases": {"acme-1": "acme-1"},
            }
        )


def test_model_lifecycle_must_reference_a_known_model() -> None:
    with pytest.raises(ValidationError, match="model_lifecycle references unknown model"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "model_lifecycle": {"acme-2": "stable"},
            }
        )


def test_effort_levels_must_reference_a_known_model() -> None:
    with pytest.raises(ValidationError, match="unknown model"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "effort_levels": {"acme-2": ["low", "high"]},
            }
        )


def test_effort_levels_must_not_be_empty() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "effort_levels": {"acme-1": []},
            }
        )


def test_effort_levels_rejects_duplicate_tiers() -> None:
    with pytest.raises(ValidationError, match="duplicate entries"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": ["acme-1"],
                "effort_levels": {"acme-1": ["low", "low"]},
            }
        )


def test_effort_levels_are_not_normalized_across_providers() -> None:
    # Deliberately provider-specific vocabulary: Google's "LOW"/"HIGH" and
    # Anthropic's "low"/"medium"/"high"/"xhigh"/"max" are unrelated strings,
    # not mapped onto a shared tier scheme.
    catalog = ModelCatalogProviderEntry.model_validate(
        {
            "name": "acme",
            "display_name": "Acme",
            "default_model": "acme-1",
            "docs_url": "https://example.com/docs",
            "models": ["acme-1"],
            "effort_levels": {"acme-1": ["LOW", "HIGH"]},
        }
    )
    assert catalog.effort_levels == {"acme-1": ("LOW", "HIGH")}


def test_empty_models_list_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least one model"):
        ModelCatalogProviderEntry.model_validate(
            {
                "name": "acme",
                "display_name": "Acme",
                "default_model": "acme-1",
                "docs_url": "https://example.com/docs",
                "models": [],
            }
        )


def test_duplicate_provider_names_are_rejected() -> None:
    entry = {
        "name": "acme",
        "display_name": "Acme",
        "default_model": "acme-1",
        "docs_url": "https://example.com/docs",
        "models": ["acme-1"],
    }
    with pytest.raises(ValidationError, match="duplicate provider names"):
        ModelCatalog.model_validate({"schema_version": 1, "providers": [entry, entry]})


def test_duplicate_model_ids_across_providers_are_allowed() -> None:
    # Real-world case: openai and openai-codex both accept the same model
    # names against the same underlying backend, reached via different auth.
    # The catalog does not reject this -- ModelRegistry.resolve() handles the
    # resulting ambiguity instead (see test_model_registry.py).
    catalog = ModelCatalog.model_validate(
        {
            "schema_version": 1,
            "providers": [
                {
                    "name": "acme",
                    "display_name": "Acme",
                    "default_model": "shared-1",
                    "docs_url": "https://example.com/docs",
                    "models": ["shared-1"],
                },
                {
                    "name": "acme-alt",
                    "display_name": "Acme Alt",
                    "default_model": "shared-1",
                    "docs_url": "https://example.com/docs",
                    "models": ["shared-1"],
                },
            ],
        }
    )

    assert len(catalog.providers) == 2


def test_effective_catalog_is_builtin_only_when_no_overlay(tmp_path: Path) -> None:
    catalog = effective_catalog(home_dir=tmp_path)

    assert catalog == builtin_catalog()


def test_builtin_catalog_is_a_complete_checked_in_agent_model_matrix() -> None:
    # This deliberate exact-set assertion makes vendor catalog drift visible in
    # review without making CI depend on credentials or a live vendor endpoint.
    catalog = builtin_catalog()
    providers = {provider.name: provider for provider in catalog.providers}

    assert {name: entry.models for name, entry in providers.items()} == {
        "openai": (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.4",
            "gpt-5.4-mini",
        ),
        "openai-codex": (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
        ),
        "anthropic": (
            "claude-fable-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-haiku-4-5",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
        ),
        "google": (
            "gemini-3.5-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
        ),
        "fake": ("fake",),
    }
    for entry in providers.values():
        assert set(entry.context_windows) == set(entry.models)
        assert set(entry.model_lifecycle) == set(entry.models)
    assert providers["openai"].model_aliases == {"gpt-5.6": "gpt-5.6-sol"}
    assert providers["openai-codex"].model_aliases == {"gpt-5.6": "gpt-5.6-sol"}
    assert providers["google"].model_aliases == {"gemini-flash-latest": "gemini-3.5-flash"}


def test_builtin_catalog_defaults_match_provider_implementations() -> None:
    defaults = {
        "openai": DEFAULT_OPENAI_MODEL,
        "openai-codex": DEFAULT_OPENAI_CODEX_MODEL,
        "anthropic": DEFAULT_ANTHROPIC_MODEL,
        "google": DEFAULT_GOOGLE_MODEL,
    }

    for entry in builtin_catalog().providers:
        if entry.name in defaults:
            assert entry.default_model == defaults[entry.name]


def test_builtin_codex_default_exposes_documented_reasoning_effort_levels() -> None:
    registry = ModelRegistry(builtin_catalog())

    assert registry.supports_effort("openai-codex", "gpt-5.6-sol", "high") is True
    assert registry.supports_effort("openai-codex", "gpt-5.6-sol", "max") is True
    assert registry.supports_effort("openai-codex", "gpt-5.6-sol", "ultra") is False


def test_overlay_adds_a_new_provider(tmp_path: Path) -> None:
    _write_overlay(tmp_path, _MINIMAL_TOML)

    catalog = effective_catalog(home_dir=tmp_path)

    names = {provider.name for provider in catalog.providers}
    assert "acme" in names
    assert {"openai", "openai-codex", "fake"} <= names


def test_overlay_replaces_a_matching_provider_wholesale(tmp_path: Path) -> None:
    _write_overlay(
        tmp_path,
        """
        schema_version = 1

        [[providers]]
        name = "fake"
        display_name = "Replaced Fake"
        default_model = "only-model"
        docs_url = "https://example.com"
        models = ["only-model"]
        """,
    )

    catalog = effective_catalog(home_dir=tmp_path)

    fake_entries = [p for p in catalog.providers if p.name == "fake"]
    assert len(fake_entries) == 1
    assert fake_entries[0].display_name == "Replaced Fake"
    assert fake_entries[0].models == ("only-model",)


def test_malformed_overlay_falls_back_to_builtin(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    _write_overlay(tmp_path, "not valid [[[ toml")

    catalog = effective_catalog(home_dir=tmp_path)

    assert catalog == builtin_catalog()
    assert "invalid catalog file" in capsys.readouterr().err


def test_invalid_overlay_schema_falls_back_to_builtin(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    _write_overlay(
        tmp_path,
        """
        schema_version = 1

        [[providers]]
        name = "acme"
        display_name = "Acme"
        default_model = "missing"
        docs_url = "https://example.com"
        models = ["acme-1"]
        """,
    )

    catalog = effective_catalog(home_dir=tmp_path)

    assert catalog == builtin_catalog()
    assert "invalid catalog file" in capsys.readouterr().err


def test_user_catalog_path_is_under_wisp_home_dir(tmp_path: Path) -> None:
    path = user_catalog_path(home_dir=tmp_path)

    assert path == tmp_path / ".wisp" / "catalog.toml"


def test_catalog_error_raised_for_malformed_builtin_style_toml() -> None:
    from wisp.providers.catalog import _catalog_from_toml

    with pytest.raises(CatalogError, match="could not parse"):
        _catalog_from_toml("not valid [[[ toml", source="test source")
