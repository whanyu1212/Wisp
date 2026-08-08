from __future__ import annotations

import anyio
import pytest

from wisp.events import (
    RpcCommandArgument,
    RpcCommandDescriptor,
    RpcSkillCatalogEntry,
    RpcSkillCatalogSnapshot,
)
from wisp.runtime.commands import CommandArgument, CommandCategory, CommandDescriptor
from wisp.tui.commands import TuiCommandCatalog
from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import PromptEditor, SlashSuggest

pytestmark = pytest.mark.tui


def _descriptor(
    name: str,
    *,
    title: str | None = None,
    description: str = "Run a command",
    category: CommandCategory = CommandCategory.general,
    aliases: tuple[str, ...] = (),
    order: int = 100,
) -> CommandDescriptor:
    return CommandDescriptor(
        name=name,
        title=title or name.title(),
        description=description,
        category=category,
        aliases=aliases,
        order=order,
    )


def test_rpc_catalog_preserves_metadata_and_excludes_commands_without_tui_handlers() -> None:
    catalog = TuiCommandCatalog.from_rpc(
        (
            RpcCommandDescriptor(
                name="model",
                title="Choose model",
                description="Switch the active model",
                category="configuration",
                aliases=("models",),
                slash_command="/model",
                slash_aliases=("/models",),
                arguments=(
                    RpcCommandArgument(
                        name="model",
                        description="Model id",
                        required=True,
                    ),
                ),
                accepts_arguments=True,
                prefill_on_partial_enter=True,
                order=7,
            ),
            RpcCommandDescriptor(
                name="extension-action",
                title="Extension action",
                description="Not executable by TuiShell yet",
                category="general",
                slash_command="/extension-action",
                order=8,
            ),
        )
    )

    assert tuple(descriptor.name for descriptor in catalog.descriptors) == ("model",)
    descriptor = catalog.descriptors[0]
    assert descriptor.title == "Choose model"
    assert descriptor.aliases == ("models",)
    assert descriptor.arguments == (
        CommandArgument(name="model", description="Model id", required=True),
    )
    assert descriptor.prefill_on_partial_enter is True
    assert descriptor.order == 7
    assert catalog.get("/models") is descriptor


def test_ctrl_o_palette_binding_and_widget_are_removed() -> None:
    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test():
            return bool(list(app.query("#command-palette")))

    assert all(binding.key != "ctrl+o" for binding in TextualTui.BINDINGS)
    assert anyio.run(scenario) is False


def test_slash_menu_matches_screen_background_and_highlights_with_text() -> None:
    async def scenario() -> list[tuple[object, object, object]]:
        styles: list[tuple[object, object, object]] = []
        for theme in ("wisp", "wisp-light"):
            app = TextualTui()
            async with app.run_test(size=(80, 24)) as pilot:
                app.theme = theme
                editor = app.query_one("#input", PromptEditor)
                editor.focus()
                await pilot.press("/")
                await pilot.pause()
                suggest = app.query_one("#suggest", SlashSuggest)
                highlight = suggest.get_component_styles("option-list--option-highlighted")
                styles.append(
                    (
                        suggest.styles.background,
                        highlight.background,
                        highlight.color,
                    )
                )
                assert suggest.styles.background == app.styles.background
        return styles

    for menu_background, highlight_background, highlight_color in anyio.run(scenario):
        assert menu_background.a == 1
        assert highlight_background.a == 0
        assert highlight_color.a == 1


def test_catalog_update_keeps_freshly_opened_slash_suggestions_visible() -> None:
    catalog = TuiCommandCatalog((_descriptor("model"),))

    async def scenario() -> tuple[str, bool, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            await pilot.press("/")
            await pilot.pause()
            suggest = app.query_one("#suggest", SlashSuggest)
            assert suggest.is_open

            # Runtime command discovery may finish immediately after startup.
            app.set_command_catalog(catalog)
            await pilot.pause()
            return editor.value, suggest.is_open, suggest.option_count

    assert anyio.run(scenario) == ("/", True, 1)


def test_loaded_catalog_updates_inline_slash_suggestions() -> None:
    catalog = TuiCommandCatalog(
        (
            CommandDescriptor(
                name="model",
                title="[Model]",
                description="[Choose] the model",
                category=CommandCategory.configuration,
            ),
        )
    )

    async def scenario() -> tuple[int, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            app.set_command_catalog(catalog)
            editor.value = "/"
            await pilot.pause()
            suggest = app.query_one("#suggest", SlashSuggest)
            prompt = suggest.get_option_at_index(0).prompt
            return suggest.option_count, str(prompt)

    count, prompt = anyio.run(scenario)
    assert count == 1
    assert "/model" in prompt
    assert "[Choose] the model" in prompt


def test_skill_suggestions_require_skill_prefix_and_use_literal_descriptions() -> None:
    suggest = SlashSuggest()
    suggest.set_skill_catalog(
        RpcSkillCatalogSnapshot(
            entries=(
                RpcSkillCatalogEntry(
                    name="review",
                    description="Review [b]literal[/b] output",
                    source="user:wisp",
                ),
            )
        )
    )

    assert tuple(spec.command for spec in suggest.matches("/skill")) == ("/skills",)
    matches = suggest.matches("/skill:r")
    assert tuple(spec.command for spec in matches) == ("/skill:review",)
    assert matches[0].description == "Review [b]literal[/b] output"
