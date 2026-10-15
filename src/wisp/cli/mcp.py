"""`wisp mcp`: list, add, remove, enable, and disable MCP servers.

Servers are saved to the user settings file and connect the next time Wisp starts.
Adding or enabling a server that runs a command always shows that exact command
and asks for confirmation unless ``--yes`` is passed, because Wisp will run it on
every launch. Environment values are only ever entered through a masked prompt
and are never printed.
"""

from __future__ import annotations

import re
import shlex
import sys
import unicodedata
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, NoReturn
from urllib.parse import urlsplit

import anyio
import typer
from typer.core import TyperCommand

from wisp.auth.storage import AuthStorageError
from wisp.config.mcp_servers import (
    McpServerExistsError,
    McpSettingsError,
    add_user_mcp_servers,
    load_user_mcp_servers,
    parse_mcp_snippet,
    remove_user_mcp_server,
    same_launch,
    set_user_mcp_server_enabled,
    validated_mcp_server,
)
from wisp.config.settings import user_settings_path
from wisp.mcp.config import HEADER_TEMPLATE_PATTERN, McpServerConfig, header_template_variable
from wisp.mcp.credentials import McpTokenStore

mcp_app = typer.Typer(
    help="Manage MCP servers. Changes apply the next time Wisp starts.",
    no_args_is_help=True,
)

_RESTART_NOTE = "Restart Wisp to apply the change."
_SERVER_COMMAND = "wisp_mcp_server_command"


class _ServerCommandAfterDoubleDash(TyperCommand):
    """Take everything after the first ``--`` as the server command, unparsed.

    Without this, Click would read the server's own flags as Wisp options: in
    ``wisp mcp add github npx -y pkg`` the ``-y`` meant for npx would become
    ``--yes`` and skip the confirmation. Tokens before ``--`` are parsed as usual,
    so a command given without ``--`` is rejected as unexpected arguments.
    """

    def parse_args(self, ctx: typer.Context, args: list[str]) -> list[str]:  # type: ignore[override]
        if "--" in args:
            split = args.index("--")
            ctx.meta[_SERVER_COMMAND] = args[split + 1 :]
            args = args[:split]
        return super().parse_args(ctx, args)


@mcp_app.command("list")
def list_command() -> None:
    """List configured MCP servers without showing environment values."""

    servers = _load_or_exit()
    if not servers:
        typer.echo(f"No MCP servers configured in {user_settings_path()}.")
        return
    for server in servers:
        state = "enabled" if server.enabled else "disabled"
        typer.echo(f"{_display(server.name)}  ({state})")
        for line in _describe(server):
            typer.echo(f"  {line}")


@mcp_app.command("add", cls=_ServerCommandAfterDoubleDash)
def add_command(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Server name: lowercase letters, digits, '-'.")],
    url: Annotated[
        str | None, typer.Option("--url", help="Connect to an HTTP MCP endpoint instead.")
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Variable to set; its value is prompted for, masked."),
    ] = None,
    env_from: Annotated[
        list[str] | None,
        typer.Option("--env-from", help="Variable to forward from Wisp's environment."),
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option("--header", help="HTTP header to send; its value is prompted for, masked."),
    ] = None,
    header_from: Annotated[
        list[str] | None,
        typer.Option(
            "--header-from",
            help="HTTP header built from a variable: NAME=VAR, or 'NAME=Bearer VAR'.",
        ),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace an existing server with this name.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Save without asking.")] = False,
) -> None:
    """Add a server that runs the command given after `--`, or connects to `--url`.

    Everything after `--` is passed to the server unchanged.

    Example: `wisp mcp add github -- npx -y @modelcontextprotocol/server-github`

    Example: `wisp mcp add linear --url https://mcp.linear.app/mcp
    --header-from "Authorization=Bearer LINEAR_API_KEY"`
    """

    command: list[str] = ctx.meta.get(_SERVER_COMMAND, [])
    if (url is None) == (not command):
        _fail("pass either a command after `--`, or --url")
    entry: dict[str, object] = {}
    if url is not None:
        entry["url"] = url
    else:
        entry["command"] = command[0]
        entry["args"] = command[1:]
    if env_from:
        entry["env_from"] = list(env_from)
    if header_from:
        entry["headers_from"] = dict(_header_from(value) for value in header_from)
    if env and not _interactive():
        _fail("--env values are prompted for; run in a terminal, or use --env-from")
    if header and not _interactive():
        _fail("--header values are prompted for; run in a terminal, or use --header-from")
    # Show what will run or receive credentials before asking for any secret value.
    _show(
        _validated(
            name,
            {
                **entry,
                "env": dict.fromkeys(env or (), ""),
                # A placeholder: header values must be non-empty to validate.
                "headers": dict.fromkeys(header or (), "hidden"),
            },
        ),
        action="Add",
    )
    if env:
        entry["env"] = {key: typer.prompt(f"Value for {key}", hide_input=True) for key in env}
    if header:
        entry["headers"] = {
            key: typer.prompt(f"Value for header {key}", hide_input=True) for key in header
        }
    server = _validated(name, entry)
    _confirm(yes=yes)
    _save((server,), replace=replace)


@mcp_app.command("import")
def import_command(
    source: Annotated[
        Path,
        typer.Argument(help='File holding an {"mcpServers": {...}} snippet, or - for stdin.'),
    ],
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace existing servers with the same names.")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Save without asking; keeps the snippet's env and header values."
        ),
    ] = False,
) -> None:
    """Import servers from a config snippet copied from a server's README.

    Every server in the snippet is saved, or none is.
    """

    reading_stdin = str(source) == "-"
    if reading_stdin and not yes:
        _fail("reading the snippet from stdin leaves no way to confirm; pass a file, or --yes")
    try:
        text = sys.stdin.read() if reading_stdin else source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _fail(f"could not read {source}: {exc}")
    try:
        servers = parse_mcp_snippet(text)
    except McpSettingsError as exc:
        _fail(str(exc))
    if not replace:
        # Checked again under the settings lock when saving; checking here avoids
        # asking for secrets that could not be saved.
        existing = {server.name for server in _load_or_exit()}
        taken = [server.name for server in servers if server.name in existing]
        if taken:
            names = ", ".join(_display(name) for name in taken)
            _fail(f"MCP servers already exist: {names}; pass --replace to overwrite")
    ready: list[McpServerConfig] = []
    for server in servers:
        _show(server, action="Import")
        if (server.env or server.headers) and not yes:
            server = _ask_for_values(server)
        ready.append(server)
    _confirm(yes=yes, question="Save it?" if len(ready) == 1 else f"Save all {len(ready)}?")
    _save(ready, replace=replace)


@mcp_app.command("remove")
def remove_command(
    name: Annotated[str, typer.Argument(help="Server to remove.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Remove without asking.")] = False,
) -> None:
    """Remove a server and its saved settings."""

    if not yes:
        _require_terminal()
        if not typer.confirm(f"Remove MCP server {_display(name)}?", default=False):
            raise typer.Exit(1)
    try:
        remove_user_mcp_server(name)
    except McpSettingsError as exc:
        _fail(str(exc))
    typer.echo(f"Removed {_display(name)}. {_RESTART_NOTE}")


@mcp_app.command("enable")
def enable_command(
    name: Annotated[str, typer.Argument(help="Server to enable.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Enable without asking.")] = False,
) -> None:
    """Enable a disabled server; Wisp starts it on its next launch."""

    server = next((server for server in _load_or_exit() if server.name == name), None)
    if server is None:
        _fail(f"no MCP server named {name!r} is configured")
    _show(server, action="Enable")
    _confirm(yes=yes)
    try:
        set_user_mcp_server_enabled(name, True, confirmed=lambda saved: same_launch(saved, server))
    except McpSettingsError as exc:
        _fail(str(exc))
    typer.echo(f"Enabled {_display(name)}. {_RESTART_NOTE}")


@mcp_app.command("disable")
def disable_command(name: Annotated[str, typer.Argument(help="Server to disable.")]) -> None:
    """Disable a server without deleting its settings."""

    try:
        set_user_mcp_server_enabled(name, False)
    except McpSettingsError as exc:
        _fail(str(exc))
    typer.echo(f"Disabled {_display(name)}. {_RESTART_NOTE}")


@mcp_app.command("login")
def login_command(name: Annotated[str, typer.Argument(help="HTTP server to sign in to.")]) -> None:
    """Sign in to an HTTP server with OAuth in your browser.

    Wisp opens the server's sign-in page and waits up to five minutes for the
    browser to return. The tokens are saved in ~/.wisp/mcp-auth.json, readable
    only by you, and refreshed automatically.
    """

    # Imported here: the MCP SDK is loaded only for commands that connect.
    from wisp.mcp.oauth import McpLoginError, login_mcp_server

    server = next((server for server in _load_or_exit() if server.name == name), None)
    if server is None:
        _fail(f"no MCP server named {name!r} is configured")

    async def show(url: str) -> None:
        typer.echo(f"Opening the sign-in page for {_display(name)}. If no browser opens, visit:")
        typer.echo(f"  {_display(url)}")
        await anyio.to_thread.run_sync(webbrowser.open, url)

    async def sign_in() -> None:
        await login_mcp_server(server, store=McpTokenStore(), open_url=show)

    try:
        anyio.run(sign_in)
    except (McpLoginError, AuthStorageError) as exc:
        _fail(str(exc))
    typer.echo(f"Signed in to {_display(name)}. {_RESTART_NOTE}")


@mcp_app.command("logout")
def logout_command(name: Annotated[str, typer.Argument(help="Server to sign out of.")]) -> None:
    """Forget a server's saved OAuth sign-in."""

    try:
        forgotten = McpTokenStore().delete(name)
    except AuthStorageError as exc:
        _fail(str(exc))
    if not forgotten:
        _fail(f"MCP server {name!r} is not signed in")
    typer.echo(f"Signed out of {_display(name)}. {_RESTART_NOTE}")


def _describe(server: McpServerConfig) -> list[str]:
    """Describe a server for display, never including environment values."""

    if server.url is not None:
        lines = [f"connects to: {_display(server.url)}"]
    else:
        assert server.command is not None
        lines = [
            f"runs: {_display(shlex.join([server.command, *server.args]))}",
            "from: your home directory",
        ]
    if server.env:
        names = ", ".join(_display(name) for name, _ in server.env)
        lines.append(f"sets: {names} (values hidden)")
    if server.env_from:
        lines.append(f"forwards: {', '.join(_display(name) for name in server.env_from)}")
    if server.headers:
        names = ", ".join(_display(name) for name, _ in server.headers)
        lines.append(f"sends headers: {names} (values hidden)")
    if server.headers_from:
        # Name the variable and where its value goes on one line: this is what
        # leaves the machine.
        host = _display(urlsplit(server.url or "").netloc)
        for header_name, template in server.headers_from:
            _scheme, variable = header_template_variable(template)
            lines.append(f"sends ${_display(variable)} to {host} in header {_display(header_name)}")
    return lines


def _header_from(option: str) -> tuple[str, str]:
    """Parse ``NAME=VAR``, ``NAME=Scheme VAR``, or ``NAME=Scheme ${VAR}``."""

    header_name, separator, value = option.partition("=")
    if not separator or not header_name:
        _fail("--header-from takes NAME=VAR, for example 'Authorization=Bearer LINEAR_API_KEY'")
    if HEADER_TEMPLATE_PATTERN.fullmatch(value):
        return header_name, value
    match = re.fullmatch(r"(?:([A-Za-z]{1,20}) )?([A-Za-z_][A-Za-z0-9_]*)", value)
    if match is None:
        _fail(
            "--header-from takes a variable name, optionally after a scheme word, "
            "for example 'Authorization=Bearer LINEAR_API_KEY'"
        )
    scheme, variable = match.groups()
    return header_name, f"{scheme} ${{{variable}}}" if scheme else f"${{{variable}}}"


def _show(server: McpServerConfig, *, action: str) -> None:
    """Show exactly what the server runs, before any confirmation or secret prompt."""

    typer.echo(f"{action} MCP server {_display(server.name)}:")
    for line in _describe(server):
        typer.echo(f"  {line}")
    if server.command is not None:
        typer.echo("Wisp runs this command every time it starts.")


def _confirm(*, yes: bool, question: str = "Save it?") -> None:
    """Ask ``question`` unless ``yes``; declining exits without saving."""

    if yes:
        return
    _require_terminal()
    if not typer.confirm(question, default=False):
        raise typer.Exit(1)


def _ask_for_values(server: McpServerConfig) -> McpServerConfig:
    """Replace a snippet's env and header values, usually placeholders, by asking.

    Each value is entered masked. Leaving an env value empty forwards the
    variable from Wisp's environment instead; a header value is required.
    """

    _require_terminal()
    env: dict[str, str] = {}
    env_from = list(server.env_from)
    for name, _ in server.env:
        value = typer.prompt(
            f"Value for {name} (leave empty to forward ${name} from your environment)",
            default="",
            hide_input=True,
            show_default=False,
        )
        if value:
            env[name] = value
        else:
            env_from.append(name)
    headers = {
        name: typer.prompt(f"Value for header {name}", hide_input=True)
        for name, _ in server.headers
    }
    data = server.model_dump(exclude={"name", "env", "env_from", "headers"})
    return _validated(server.name, {**data, "env": env, "env_from": env_from, "headers": headers})


def _save(servers: Sequence[McpServerConfig], *, replace: bool) -> None:
    try:
        add_user_mcp_servers(servers, replace=replace)
    except McpServerExistsError as exc:
        _fail(f"{exc}; pass --replace to overwrite")
    except McpSettingsError as exc:
        _fail(str(exc))
    names = ", ".join(_display(server.name) for server in servers)
    typer.echo(f"Saved {names} to {user_settings_path()}. {_RESTART_NOTE}")


def _validated(name: str, entry: dict[str, object]) -> McpServerConfig:
    try:
        return validated_mcp_server(name, entry)
    except McpSettingsError as exc:
        _fail(str(exc))


def _load_or_exit() -> tuple[McpServerConfig, ...]:
    try:
        return load_user_mcp_servers()
    except McpSettingsError as exc:
        _fail(str(exc))


def _interactive() -> bool:
    return sys.stdin.isatty()


def _require_terminal() -> None:
    if not _interactive():
        _fail("confirmation needs a terminal; pass --yes to skip it")


def _display(text: str) -> str:
    """Neutralize control and bidi-override characters before showing ``text``.

    Server names, commands, and arguments may come from a pasted snippet; escape
    sequences or direction overrides could otherwise make the terminal show a
    different command than the one saved.
    """

    return "".join(
        "\N{REPLACEMENT CHARACTER}"
        if unicodedata.category(character) in {"Cc", "Cf"} and character not in "\t"
        else character
        for character in text
    )


def _fail(message: str) -> NoReturn:
    typer.echo(f"wisp mcp: {_display(message)}", err=True)
    raise typer.Exit(1)
