"""Built-in command descriptors shared by Wisp frontends and runtime setup."""

from __future__ import annotations

from wisp.runtime.commands import CommandArgument, CommandCategory, CommandDescriptor


def builtin_command_descriptors() -> tuple[CommandDescriptor, ...]:
    """Return Wisp's built-in command descriptors in display order."""

    return (
        CommandDescriptor(
            name="help",
            title="Help",
            description="Show the TUI commands",
            category=CommandCategory.general,
            order=10,
        ),
        CommandDescriptor(
            name="compact",
            title="Compact",
            description="Compact the session context",
            category=CommandCategory.session,
            arguments=(
                CommandArgument(
                    name="instructions",
                    description="Optional compaction guidance",
                ),
            ),
            accepts_arguments=True,
            order=20,
        ),
        CommandDescriptor(
            name="context",
            title="Context",
            description="Show context and automatic-compaction status",
            category=CommandCategory.configuration,
            arguments=(
                CommandArgument(
                    name="auto",
                    description="Optional 'on' or 'off' automatic-compaction toggle",
                ),
                CommandArgument(
                    name="state",
                    description="Automatic-compaction state",
                ),
            ),
            accepts_arguments=True,
            order=22,
        ),
        CommandDescriptor(
            name="history",
            title="Prompt history",
            description="Search prompts submitted in this TUI run",
            category=CommandCategory.general,
            order=25,
        ),
        CommandDescriptor(
            name="skills",
            title="Agent Skills",
            description="Show loaded skills and discovery diagnostics",
            category=CommandCategory.general,
            order=26,
        ),
        CommandDescriptor(
            name="plan",
            title="Plan mode",
            description="Switch to read-only planning mode",
            category=CommandCategory.configuration,
            order=27,
        ),
        CommandDescriptor(
            name="build",
            title="Build mode",
            description="Switch to normal build mode",
            category=CommandCategory.configuration,
            order=28,
        ),
        CommandDescriptor(
            name="model",
            title="Model",
            description="Show or switch the active model",
            category=CommandCategory.configuration,
            arguments=(
                CommandArgument(
                    name="model",
                    description="Optional model id",
                ),
                CommandArgument(
                    name="effort",
                    description="Optional effort tier or '-' to clear",
                ),
            ),
            accepts_arguments=True,
            order=30,
        ),
        CommandDescriptor(
            name="new",
            title="New session",
            description="Start a fresh session",
            category=CommandCategory.session,
            order=35,
        ),
        CommandDescriptor(
            name="resume",
            title="Resume",
            description="Browse or resume a previous session",
            category=CommandCategory.session,
            arguments=(
                CommandArgument(
                    name="session-id",
                    description="Optional session id, path, or prefix",
                ),
            ),
            accepts_arguments=True,
            order=40,
        ),
        CommandDescriptor(
            name="provider",
            title="Provider",
            description="Show or switch the active provider",
            category=CommandCategory.configuration,
            arguments=(
                CommandArgument(
                    name="provider",
                    description="Optional provider name",
                ),
            ),
            accepts_arguments=True,
            order=50,
        ),
        CommandDescriptor(
            name="auth",
            title="Auth",
            description="Show credential status",
            category=CommandCategory.auth,
            arguments=(
                CommandArgument(
                    name="provider",
                    description="Optional provider name",
                ),
            ),
            accepts_arguments=True,
            order=60,
        ),
        CommandDescriptor(
            name="connect",
            title="Connect",
            description="Connect a model provider",
            category=CommandCategory.auth,
            arguments=(
                CommandArgument(
                    name="provider",
                    description="Optional provider name",
                ),
            ),
            accepts_arguments=True,
            order=70,
        ),
        CommandDescriptor(
            name="disconnect",
            title="Disconnect",
            description="Remove stored credentials",
            category=CommandCategory.auth,
            arguments=(
                CommandArgument(
                    name="provider",
                    description="Optional provider name",
                ),
            ),
            accepts_arguments=True,
            aliases=("logout",),
            order=80,
        ),
        CommandDescriptor(
            name="quit",
            title="Quit",
            description="Quit the TUI",
            category=CommandCategory.general,
            aliases=("exit", ":q"),
            order=90,
        ),
    )
