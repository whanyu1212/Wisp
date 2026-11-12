from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from tests.support.rpc import RpcExecutorFixture, build_rpc_executor_fixture
from wisp.config import settings
from wisp.events import ErrorEvent, RpcCommandFinished, RpcSubagentsReported, SubagentsState
from wisp.rpc.commands import GetSubagentsCommand, SetSubagentsCommand
from wisp.rpc.handlers.subagents import handle_rpc_subagents_command


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "user_settings_path", lambda **_: path)
    return path


@pytest.fixture
def fixture(tmp_path: Path) -> RpcExecutorFixture:
    return anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")


def _send(
    fixture: RpcExecutorFixture, command: GetSubagentsCommand | SetSubagentsCommand
) -> SubagentsState:
    handle_rpc_subagents_command(command, agent=fixture.agent, write_event=fixture.events.append)
    finished = fixture.events[-1]
    assert isinstance(finished, RpcCommandFinished) and finished.ok
    (report,) = [event for event in fixture.events if isinstance(event, RpcSubagentsReported)]
    assert report.command_id == command.id
    return report.subagents


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
def test_set_applies_and_saves(
    fixture: RpcExecutorFixture, settings_path: Path, enabled: bool
) -> None:
    """The session takes the setting, and the file keeps its other settings."""

    settings_path.write_text(json.dumps({"subagents": not enabled, "effort": "high"}))

    state = _send(fixture, SetSubagentsCommand(id="subagents-1", enabled=enabled))

    assert fixture.agent.subagents is enabled
    assert state.enabled is enabled
    assert json.loads(settings_path.read_text()) == {"effort": "high", "subagents": enabled}


def test_get_reports_without_saving(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    state = _send(fixture, GetSubagentsCommand(id="subagents-1"))

    assert (state.enabled, state.offered, state.env_enabled) == (False, False, None)
    assert not settings_path.exists()


def test_offered_follows_the_setting(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    """The snapshot says what the next run would do, not only what the setting is."""

    state = _send(fixture, SetSubagentsCommand(id="subagents-1", enabled=True))

    assert state.offered is fixture.agent.subagents_offered()


def test_env_setting_is_reported(
    fixture: RpcExecutorFixture, settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`WISP_SUBAGENTS` sets subagents at startup, so the snapshot says what it gives."""

    monkeypatch.setenv("WISP_SUBAGENTS", "off")

    state = _send(fixture, SetSubagentsCommand(id="subagents-1", enabled=True))

    assert (state.enabled, state.env_enabled) == (True, False)
    assert json.loads(settings_path.read_text()) == {"subagents": True}


def test_failed_save_still_applies(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    """A settings file that is not a JSON object is kept, and the change lasts the session."""

    settings_path.write_text("[]")

    state = _send(fixture, SetSubagentsCommand(id="subagents-1", enabled=True))

    assert fixture.agent.subagents is True and state.enabled
    assert settings_path.read_text() == "[]"
    assert any(isinstance(event, ErrorEvent) for event in fixture.events)


def test_model_is_reported(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    """The snapshot names the configured subagent model and the one that would be used."""

    fixture.agent.subagent_model = "small-model"

    state = _send(fixture, GetSubagentsCommand(id="subagents-1"))

    assert state.model == "small-model"
    assert state.model_in_use == fixture.agent.subagent_model_in_use()
