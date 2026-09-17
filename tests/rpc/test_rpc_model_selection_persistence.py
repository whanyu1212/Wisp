from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from tests.rpc_support import RpcExecutorFixture, build_rpc_executor_fixture
from wisp import settings
from wisp.events import ErrorEvent, RpcCommandFinished
from wisp.rpc import configure as configure_module
from wisp.rpc.commands import ConfigureCommand


def _configure(fixture: RpcExecutorFixture, **values: object) -> None:
    command = ConfigureCommand.model_validate({"id": "selection", **values})
    configure_module.handle_rpc_configure_command(
        command,
        command_id="selection",
        provided_fields=command.model_fields_set,
        agent=fixture.agent,
        runtime=fixture.runtime,
        configure_overrides=fixture.configure_overrides,
        write_event=fixture.events.append,
    )


@pytest.mark.parametrize("mutation", [{}, {"mode": "plan"}, {"auto_compaction_enabled": False}])
def test_persistence_requires_a_selection_mutation(mutation: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ConfigureCommand.model_validate({**mutation, "persist_model_selection": True})


@pytest.mark.parametrize("persist", [False, True])
def test_configure_only_saves_defaults_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persist: bool
) -> None:
    path = tmp_path / "settings.json"
    original = '{"future_setting": {"enabled": true}, "effort": "old"}\n'
    path.write_text(original)
    monkeypatch.setattr(settings, "user_settings_path", lambda **_: path)
    fixture = anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")

    _configure(
        fixture,
        model="custom-model",
        effort="custom-effort",
        persist_model_selection=persist,
    )

    assert fixture.agent.model == "custom-model"
    assert fixture.agent.effort == "custom-effort"
    finished = fixture.events[-1]
    assert isinstance(finished, RpcCommandFinished) and finished.ok
    if persist:
        assert json.loads(path.read_text()) == {
            "provider": "fake",
            "model": "custom-model",
            "effort": "custom-effort",
            "future_setting": {"enabled": True},
        }
    else:
        assert path.read_text() == original


def test_provider_default_selection_removes_saved_model_and_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"provider":"fake","model":"custom","effort":"custom"}')
    monkeypatch.setattr(settings, "user_settings_path", lambda **_: path)
    fixture = anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")
    _configure(fixture, provider="fake", persist_model_selection=True)
    assert fixture.agent.model is None
    assert fixture.agent.effort is None
    assert json.loads(path.read_text()) == {"provider": "fake"}


def test_failed_configuration_never_saves_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_save(*args: object, **kwargs: object) -> bool:
        raise AssertionError("failed configuration must not save preferences")

    monkeypatch.setattr(configure_module, "try_persist_user_model_selection", unexpected_save)
    fixture = anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")
    _configure(fixture, provider="unregistered", persist_model_selection=True)
    finished = fixture.events[-1]
    assert isinstance(finished, RpcCommandFinished) and not finished.ok
    assert fixture.agent.provider.name == "fake"


@pytest.mark.parametrize("failure", ["invalid-utf8", "replace"])
def test_save_failure_preserves_live_configuration_and_existing_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "settings.json"
    original = b"\xff\xfe" if failure == "invalid-utf8" else b'{"model":"previous"}'
    path.write_bytes(original)
    monkeypatch.setattr(settings, "user_settings_path", lambda **_: path)
    if failure == "replace":

        def denied_replace(*args: object, **kwargs: object) -> None:
            raise PermissionError("settings are read-only")

        monkeypatch.setattr(Path, "replace", denied_replace)
    fixture = anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")
    _configure(fixture, model="custom-model", persist_model_selection=True)
    assert fixture.agent.model == "custom-model"
    assert path.read_bytes() == original
    assert any(
        isinstance(event, ErrorEvent) and "could not save user defaults" in event.message
        for event in fixture.events
    )
    finished = fixture.events[-1]
    assert isinstance(finished, RpcCommandFinished) and finished.ok


def test_catalog_failure_does_not_prevent_saving_applied_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "user_settings_path", lambda **_: path)
    fixture = anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")

    def unavailable_catalog(**kwargs: object) -> None:
        raise ValueError("catalog unavailable")

    monkeypatch.setattr(configure_module, "rpc_model_catalog_snapshot", unavailable_catalog)
    _configure(fixture, model="custom-model", persist_model_selection=True)
    assert fixture.agent.model == "custom-model"
    assert json.loads(path.read_text()) == {"provider": "fake", "model": "custom-model"}
    finished = fixture.events[-1]
    assert isinstance(finished, RpcCommandFinished) and finished.ok
