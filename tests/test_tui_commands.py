from __future__ import annotations

import pytest

from wisp.tui.commands import TuiSlashCommandError, TuiSlashCommandName, parse_tui_slash_command


def test_parse_tui_slash_command_returns_none_for_prompt() -> None:
    assert parse_tui_slash_command("hello /help") is None
    assert parse_tui_slash_command("") is None


def test_parse_tui_slash_command_parses_args_and_quotes() -> None:
    command = parse_tui_slash_command('/model "gpt test"')

    assert command is not None
    assert command.name is TuiSlashCommandName.model
    assert command.args == ("gpt test",)


def test_parse_tui_slash_command_aliases_quit() -> None:
    assert parse_tui_slash_command("/exit") is not None
    assert parse_tui_slash_command("/exit").name is TuiSlashCommandName.quit  # type: ignore[union-attr]
    assert parse_tui_slash_command(":q") is not None
    assert parse_tui_slash_command(":q").name is TuiSlashCommandName.quit  # type: ignore[union-attr]


def test_parse_tui_slash_command_rejects_unknown_command() -> None:
    with pytest.raises(TuiSlashCommandError, match="Unknown command"):
        parse_tui_slash_command("/missing")


def test_parse_tui_slash_command_rejects_bad_quotes() -> None:
    with pytest.raises(TuiSlashCommandError):
        parse_tui_slash_command('/model "unterminated')
