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
    # A bare slash-word that isn't known is a mistyped command → error (helpful).
    with pytest.raises(TuiSlashCommandError, match="Unknown command"):
        parse_tui_slash_command("/missing")


def test_parse_tui_slash_command_treats_slash_prose_as_prompt() -> None:
    # Slash-prefixed prose is a literal message, not a command attempt, and passes
    # through as a prompt (None) instead of raising "Unknown command". The error is
    # gated on the WHOLE input being a lone `/word`, so a slash word followed by
    # anything — more words, an inner slash, a space — is prose.
    assert parse_tui_slash_command("/todo remember this") is None  # word + prose
    assert parse_tui_slash_command("/note to self") is None
    assert parse_tui_slash_command("/etc/hosts is broken") is None  # inner slash
    assert parse_tui_slash_command("/ note to self") is None  # space after slash
    assert parse_tui_slash_command("/some/path") is None
    assert parse_tui_slash_command("/usr/local/bin") is None
    # A lone unknown slash-word is still a mistyped command, so it still errors.
    with pytest.raises(TuiSlashCommandError, match="Unknown command"):
        parse_tui_slash_command("/todo")


def test_parse_tui_slash_command_rejects_bad_quotes() -> None:
    with pytest.raises(TuiSlashCommandError):
        parse_tui_slash_command('/model "unterminated')
