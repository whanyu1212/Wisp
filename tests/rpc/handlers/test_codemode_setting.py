from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from tests.support.rpc import RpcExecutorFixture, build_rpc_executor_fixture
from wisp.coding.session import codemode_available
from wisp.config import settings
from wisp.events import (
    CodemodePresentation,
    CodemodeSetting,
    CodemodeState,
    ErrorEvent,
    RpcCodemodeReported,
    RpcCommandFinished,
)
from wisp.rpc.commands import GetCodemodeCommand, SetCodemodeCommand
from wisp.rpc.configuration import _RpcConfigureOverrides
from wisp.rpc.handlers.codemode import handle_rpc_codemode_command


@pytest.fixture
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "user_settings_path", lambda **_: path)
    return path


@pytest.fixture
def fixture(tmp_path: Path) -> RpcExecutorFixture:
    return anyio.run(build_rpc_executor_fixture, tmp_path / "sessions")


def _send(
    fixture: RpcExecutorFixture, command: GetCodemodeCommand | SetCodemodeCommand
) -> CodemodeState:
    handle_rpc_codemode_command(command, agent=fixture.agent, write_event=fixture.events.append)
    finished = fixture.events[-1]
    assert isinstance(finished, RpcCommandFinished) and finished.ok
    (report,) = [event for event in fixture.events if isinstance(event, RpcCodemodeReported)]
    assert report.command_id == command.id
    return report.codemode


@pytest.mark.parametrize(
    ("setting", "codemode", "saved"),
    [
        ("always", True, {"codemode": True}),
        ("never", False, {"codemode": False}),
        ("automatic", None, {}),
    ],
)
def test_set_applies_and_saves(
    fixture: RpcExecutorFixture,
    settings_path: Path,
    setting: CodemodeSetting,
    codemode: bool | None,
    saved: dict[str, object],
) -> None:
    """The session takes the setting; the file keeps its other settings, automatic unset."""

    settings_path.write_text('{"codemode": true, "effort": "high"}')

    state = _send(fixture, SetCodemodeCommand(id="codemode-1", setting=setting))

    assert fixture.agent.codemode is codemode
    assert state.setting == setting
    assert json.loads(settings_path.read_text()) == {"effort": "high", **saved}


def test_get_reports_without_saving(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    fixture.agent.codemode = False

    state = _send(fixture, GetCodemodeCommand(id="codemode-1"))

    assert state == CodemodeState(
        setting="never",
        available=codemode_available(),
        offered=False,
        mcp_tools=0,
        scriptable_mcp_tools=0,
    )
    assert not settings_path.exists()


def test_reports_scriptable_mcp_tools(
    fixture: RpcExecutorFixture, settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot says how many of the run's MCP tools a script may call."""

    monkeypatch.setattr(fixture.agent, "scriptable_mcp_tools", lambda: (3, 12))

    state = _send(fixture, GetCodemodeCommand(id="codemode-1"))

    assert (state.scriptable_mcp_tools, state.mcp_tools) == (3, 12)


def test_env_setting_is_reported(
    fixture: RpcExecutorFixture, settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`WISP_CODEMODE` sets codemode at startup, so the snapshot says what it gives."""

    monkeypatch.setenv("WISP_CODEMODE", "off")

    state = _send(fixture, SetCodemodeCommand(id="codemode-1", setting="always"))

    assert (state.setting, state.env_setting) == ("always", "never")
    assert json.loads(settings_path.read_text()) == {"codemode": True}


def test_failed_save_still_applies(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    """A settings file that is not a JSON object is kept, and the change lasts the session."""

    settings_path.write_text("[]")

    state = _send(fixture, SetCodemodeCommand(id="codemode-1", setting="always"))

    assert fixture.agent.codemode is True and state.setting == "always"
    assert settings_path.read_text() == "[]"
    assert any(isinstance(event, ErrorEvent) for event in fixture.events)


@pytest.mark.parametrize(
    ("offered", "effective"),
    [(False, "direct_and_codemode"), (True, "codemode_only"), (True, "direct_and_codemode")],
)
def test_presentation_applies_and_saves(
    fixture: RpcExecutorFixture,
    settings_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offered: bool,
    effective: str,
) -> None:
    settings_path.write_text('{"effort": "high"}')
    monkeypatch.setattr(fixture.agent, "codemode_offered", lambda: offered)
    monkeypatch.setattr(fixture.agent, "codemode_presentation_in_use", lambda: effective)

    state = _send(
        fixture,
        SetCodemodeCommand(id="codemode-1", setting="always", presentation="codemode_only"),
    )

    assert fixture.agent.codemode_presentation == state.presentation == "codemode_only"
    assert state.effective_presentation == effective
    assert state.offered is offered
    assert json.loads(settings_path.read_text()) == {
        "effort": "high",
        "codemode": True,
        "codemode_presentation": "codemode_only",
    }


def test_omitted_presentation_is_preserved(
    fixture: RpcExecutorFixture, settings_path: Path
) -> None:
    fixture.agent.codemode_presentation = "codemode_only"
    settings_path.write_text('{"codemode_presentation": "codemode_only"}')

    state = _send(fixture, SetCodemodeCommand(id="codemode-1", setting="never"))

    assert state.presentation == "codemode_only"
    assert state.effective_presentation == "direct_and_codemode"
    assert json.loads(settings_path.read_text()) == {
        "codemode": False,
        "codemode_presentation": "codemode_only",
    }


@pytest.mark.parametrize("presentation", [None, "codemode_only"])
def test_presentation_override_is_retained(
    fixture: RpcExecutorFixture,
    settings_path: Path,
    presentation: CodemodePresentation | None,
) -> None:
    choices = _RpcConfigureOverrides()
    handle_rpc_codemode_command(
        SetCodemodeCommand(id="codemode-1", setting="automatic", presentation=presentation),
        agent=fixture.agent,
        write_event=fixture.events.append,
        configure_overrides=choices,
    )

    assert choices.has_codemode and choices.codemode is None
    assert choices.codemode_presentation == presentation


def test_failed_presentation_save_applies(fixture: RpcExecutorFixture, settings_path: Path) -> None:
    settings_path.write_text("[]")

    state = _send(
        fixture,
        SetCodemodeCommand(id="codemode-1", setting="never", presentation="codemode_only"),
    )

    assert fixture.agent.codemode_presentation == state.presentation == "codemode_only"
    assert settings_path.read_text() == "[]"
    assert any(isinstance(event, ErrorEvent) for event in fixture.events)
