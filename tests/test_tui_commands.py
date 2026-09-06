from __future__ import annotations

import pytest

from wisp.runtime.builtin_commands import builtin_command_descriptors
from wisp.tui.commands import (
    SLASH_COMMAND_SPECS,
    SlashCommandSpec,
    TuiSlashCommandError,
    TuiSlashCommandName,
    parse_tui_slash_command,
)


def test_parse_tui_slash_command_returns_none_for_prompt() -> None:
    assert parse_tui_slash_command("hello /help") is None
    assert parse_tui_slash_command("") is None


@pytest.mark.parametrize("text", ["/skill:demo", "/skill:demo review this"])
def test_parse_tui_slash_command_passes_skill_directives_to_core(text: str) -> None:
    assert parse_tui_slash_command(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "/help\nplease explain this",
        "/model\r\ngpt-test should be discussed",
        "/quit\n",
        "/compact\npreserve this as prompt text",
    ],
)
def test_parse_tui_slash_command_treats_multiline_input_as_prompt(text: str) -> None:
    assert parse_tui_slash_command(text) is None


def test_parse_tui_slash_command_parses_args_and_quotes() -> None:
    command = parse_tui_slash_command('/model "gpt test"')

    assert command is not None
    assert command.name is TuiSlashCommandName.model
    assert command.args == ("gpt test",)


@pytest.mark.parametrize(
    ("text", "args"),
    [
        ("/compact", ()),
        ("/compact preserve implementation details", ("preserve", "implementation", "details")),
        ('/compact "preserve implementation details"', ("preserve implementation details",)),
    ],
)
def test_parse_tui_compact_command(text: str, args: tuple[str, ...]) -> None:
    command = parse_tui_slash_command(text)

    assert command is not None
    assert command.name is TuiSlashCommandName.compact
    assert command.args == args


def test_compact_command_is_available_in_slash_menu() -> None:
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == "/compact")

    assert spec.takes_args is True
    assert "Compact" in spec.description


def test_resume_command_parses_and_is_available_in_slash_menu() -> None:
    bare = parse_tui_slash_command("/resume")
    selected = parse_tui_slash_command("/resume session-123")
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == "/resume")

    assert bare is not None
    assert bare.name is TuiSlashCommandName.resume
    assert bare.args == ()
    assert selected is not None
    assert selected.name is TuiSlashCommandName.resume
    assert selected.args == ("session-123",)
    assert spec.takes_args is True


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("/plan", TuiSlashCommandName.plan),
        ("/build", TuiSlashCommandName.build),
    ],
)
def test_agent_mode_commands_parse_and_are_available_in_slash_menu(
    text: str,
    name: TuiSlashCommandName,
) -> None:
    command = parse_tui_slash_command(text)
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == text)

    assert command is not None
    assert command.name is name
    assert command.args == ()
    assert spec.takes_args is False


def test_init_command_parses_and_is_available_in_slash_menu() -> None:
    command = parse_tui_slash_command("/init")
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == "/init")

    assert command is not None
    assert command.name is TuiSlashCommandName.init
    assert command.args == ()
    assert spec.takes_args is False


def test_new_command_parses_and_is_available_in_slash_menu() -> None:
    command = parse_tui_slash_command("/new")
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == "/new")

    assert command is not None
    assert command.name is TuiSlashCommandName.new
    assert command.args == ()
    assert spec.takes_args is False


def test_history_command_parses_and_is_available_in_slash_menu() -> None:
    command = parse_tui_slash_command("/history")
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == "/history")

    assert command is not None
    assert command.name is TuiSlashCommandName.history
    assert command.args == ()
    assert spec.takes_args is False


def test_mcp_command_parses_and_is_available_in_slash_menu() -> None:
    command = parse_tui_slash_command("/mcp")
    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == "/mcp")

    assert command is not None
    assert command.name is TuiSlashCommandName.mcp
    assert command.args == ()
    assert spec.takes_args is False


def test_slash_command_specs_project_shared_builtin_descriptors() -> None:
    descriptors = builtin_command_descriptors()

    assert tuple(spec.command for spec in SLASH_COMMAND_SPECS) == tuple(
        descriptor.slash_command for descriptor in descriptors
    )
    assert tuple(spec.description for spec in SLASH_COMMAND_SPECS) == tuple(
        descriptor.description for descriptor in descriptors
    )
    assert "/exit" not in {spec.command for spec in SLASH_COMMAND_SPECS}
    assert ":q" not in {spec.command for spec in SLASH_COMMAND_SPECS}


def test_parse_tui_slash_command_aliases_quit() -> None:
    assert parse_tui_slash_command("/exit") is not None
    assert parse_tui_slash_command("/exit").name is TuiSlashCommandName.quit  # type: ignore[union-attr]
    assert parse_tui_slash_command(":q") is not None
    assert parse_tui_slash_command(":q").name is TuiSlashCommandName.quit  # type: ignore[union-attr]
    assert parse_tui_slash_command("/:q") is None


def test_parse_tui_slash_command_aliases_logout_to_disconnect() -> None:
    command = parse_tui_slash_command("/logout openai-codex")

    assert command is not None
    assert command.name is TuiSlashCommandName.disconnect
    assert command.args == ("openai-codex",)


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


def test_parse_tui_slash_command_prose_with_lone_quote_is_a_prompt() -> None:
    # Prose is classified before shlex tokenizes, so slash-prose containing a lone
    # quote is a prompt — not a shlex "No closing quotation" error. A user's
    # message can legally contain an unmatched quote.
    assert parse_tui_slash_command('/todo remember "this') is None
    assert parse_tui_slash_command('/etc/hosts "broken') is None


def test_parse_tui_slash_command_rejects_bad_quotes_for_known_command() -> None:
    # A quote error is only a command error when a KNOWN command is being invoked;
    # then malformed quoting in its args is a real syntax error.
    with pytest.raises(TuiSlashCommandError):
        parse_tui_slash_command('/model "unterminated')


@pytest.mark.parametrize(
    ("typed", "command", "prefills"),
    [
        ("/co", "/compact", False),
        ("/compact", "/compact", False),
        ("/COMPACT", "/compact", False),
        ("/mo", "/model", False),
        ("/MODEL", "/model", False),
        ("/qu", "/quit", False),
    ],
)
def test_slash_enter_prefill_rules(typed: str, command: str, prefills: bool) -> None:
    from wisp.tui.textual_app import _slash_enter_prefills

    spec = next(spec for spec in SLASH_COMMAND_SPECS if spec.command == command)
    assert _slash_enter_prefills(typed, spec) is prefills


@pytest.mark.parametrize(
    ("typed", "expected"), [("/req", True), ("/required", False), ("/REQUIRED", False)]
)
def test_required_argument_command_prefills_only_partial_enter(typed: str, expected: bool) -> None:
    from wisp.tui.textual_app import _slash_enter_prefills

    spec = SlashCommandSpec(
        command="/required",
        description="Required argument",
        takes_args=True,
        prefill_on_partial_enter=True,
    )
    assert _slash_enter_prefills(typed, spec) is expected
