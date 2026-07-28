from __future__ import annotations

import pytest

from wisp.runtime import (
    CommandArgument,
    CommandCategory,
    CommandDescriptor,
    CommandRegistry,
    DuplicateCommandError,
    UnknownCommandError,
)
from wisp.runtime.builtin_commands import builtin_command_descriptors


def _command(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    order: int = 1000,
) -> CommandDescriptor:
    return CommandDescriptor(
        name=name,
        title=name.title(),
        description=f"{name} command",
        aliases=aliases,
        order=order,
    )


def test_command_descriptor_validates_and_normalizes_fields() -> None:
    descriptor = CommandDescriptor(
        name="model",
        title="Model",
        description="Show or switch the active model",
        category="configuration",
        aliases=("models",),
        arguments=(
            CommandArgument(
                name="model-id",
                description="Optional model id",
            ),
        ),
        accepts_arguments=True,
        prefill_on_partial_enter=True,
        order=30,
    )

    assert descriptor.category is CommandCategory.configuration
    assert descriptor.slash_command == "/model"
    assert descriptor.slash_aliases == ("/models",)


@pytest.mark.parametrize(
    "name",
    ["", "Model", "model_name", "-model", "model/name"],
)
def test_command_descriptor_rejects_invalid_names(name: str) -> None:
    with pytest.raises(ValueError, match="Command name is invalid"):
        _command(name)


@pytest.mark.parametrize(
    "alias",
    ["", "/exit", "Exit", "exit_now", ":Q", "exit/name"],
)
def test_command_descriptor_rejects_invalid_aliases(alias: str) -> None:
    with pytest.raises(ValueError, match="Command alias"):
        _command("quit", aliases=(alias,))


def test_command_descriptor_rejects_argument_metadata_without_argument_support() -> None:
    with pytest.raises(ValueError, match="arguments require accepts_arguments"):
        CommandDescriptor(
            name="model",
            title="Model",
            description="Show or switch the active model",
            arguments=(CommandArgument(name="model-id"),),
        )


def test_command_registry_resolves_names_slash_names_and_aliases() -> None:
    registry = CommandRegistry((_command("quit", aliases=("exit", ":q")),))

    assert registry.get("quit").name == "quit"
    assert registry.get("/quit").name == "quit"
    assert registry.get("exit").name == "quit"
    assert registry.get("/exit").name == "quit"
    assert registry.get(":q").name == "quit"


def test_command_registry_does_not_slash_normalize_special_aliases() -> None:
    registry = CommandRegistry((_command("quit", aliases=("exit", ":q")),))

    with pytest.raises(UnknownCommandError):
        registry.get("/:q")


def test_command_registry_returns_deterministic_order() -> None:
    registry = CommandRegistry(
        (
            _command("zzz", order=20),
            _command("aaa", order=20),
            _command("help", order=10),
        )
    )

    assert registry.names() == ("help", "aaa", "zzz")


def test_command_registry_rejects_duplicate_names() -> None:
    registry = CommandRegistry((_command("model"),))

    with pytest.raises(DuplicateCommandError, match="Command already registered: model"):
        registry.register(_command("model"))


def test_command_registry_rejects_alias_conflicts() -> None:
    registry = CommandRegistry((_command("quit", aliases=("exit",)),))

    with pytest.raises(DuplicateCommandError, match="Command alias already registered: exit"):
        registry.register(_command("close", aliases=("exit",)))

    with pytest.raises(DuplicateCommandError, match="Command alias conflicts with name: quit"):
        registry.register(_command("close", aliases=("quit",)))

    with pytest.raises(DuplicateCommandError, match="Command name conflicts with alias"):
        registry.register(_command("exit"))


def test_command_registry_replace_removes_stale_aliases() -> None:
    registry = CommandRegistry((_command("quit", aliases=("exit",)),))

    registry.register(_command("quit", aliases=("close",)), replace=True)

    assert registry.get("/close").name == "quit"
    with pytest.raises(UnknownCommandError):
        registry.get("/exit")


def test_command_registry_failed_replace_preserves_existing_aliases() -> None:
    registry = CommandRegistry(
        (
            _command("quit", aliases=("exit",)),
            _command("close", aliases=("bye",)),
        )
    )

    with pytest.raises(DuplicateCommandError, match="Command alias already registered: bye"):
        registry.register(_command("quit", aliases=("bye",)), replace=True)

    assert registry.get("/exit").name == "quit"
    assert registry.get("/bye").name == "close"


def test_command_registry_raises_for_unknown_command() -> None:
    registry = CommandRegistry()

    with pytest.raises(UnknownCommandError, match="Unknown command: /missing"):
        registry.get("/missing")


def test_builtin_command_descriptors_capture_supported_arguments() -> None:
    descriptors = {descriptor.name: descriptor for descriptor in builtin_command_descriptors()}

    assert tuple(argument.name for argument in descriptors["model"].arguments) == (
        "model",
        "effort",
    )
    assert tuple(argument.name for argument in descriptors["login"].arguments) == (
        "provider",
        "method",
    )
