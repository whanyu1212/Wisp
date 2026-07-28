from __future__ import annotations

import anyio
import pytest
from textual.content import Content
from textual.widgets import OptionList

from wisp.events import RpcCommandArgument, RpcCommandDescriptor
from wisp.runtime.commands import CommandArgument, CommandCategory, CommandDescriptor
from wisp.tui.command_palette import search_command_catalog
from wisp.tui.commands import TuiCommandCatalog
from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import CommandPalette, PromptEditor, SlashSuggest, Transcript


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


def test_command_palette_search_ranks_exact_prefix_and_description_matches() -> None:
    catalog = TuiCommandCatalog(
        (
            _descriptor("help", description="Show command documentation", order=10),
            _descriptor("model", description="Configure the provider model", order=20),
            _descriptor("resume", description="Browse model sessions", order=30),
        )
    )

    assert tuple(match.descriptor.name for match in search_command_catalog(catalog, "model")) == (
        "model",
        "resume",
    )
    assert search_command_catalog(catalog, "/model")[0].descriptor.name == "model"
    assert tuple(match.descriptor.name for match in search_command_catalog(catalog, "mod")) == (
        "model",
        "resume",
    )


def test_command_palette_searches_alias_title_category_and_multiple_tokens() -> None:
    catalog = TuiCommandCatalog(
        (
            _descriptor("quit", aliases=("exit", ":q"), order=40),
            _descriptor(
                "resume",
                title="Session browser",
                description="Open persisted work",
                category=CommandCategory.session,
                order=20,
            ),
            _descriptor(
                "model",
                title="Model picker",
                description="Choose a provider model",
                category=CommandCategory.configuration,
                order=10,
            ),
        )
    )

    assert search_command_catalog(catalog, "exit")[0].descriptor.name == "quit"
    assert search_command_catalog(catalog, "browser")[0].descriptor.name == "resume"
    assert search_command_catalog(catalog, "configuration provider")[0].descriptor.name == "model"


def test_command_palette_search_is_casefolded_fuzzy_and_deterministic() -> None:
    catalog = TuiCommandCatalog(
        (
            _descriptor("provider", title="Provider", order=20),
            _descriptor("compact", title="Compact", order=10),
        )
    )

    assert search_command_catalog(catalog, "  PVD  ")[0].descriptor.name == "provider"
    assert tuple(match.descriptor.name for match in search_command_catalog(catalog, "")) == (
        "compact",
        "provider",
    )


def test_ctrl_o_palette_cancel_preserves_editor_state_and_focus() -> None:
    async def scenario() -> tuple[str, object, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft in progress"
            editor.selection = type(editor.selection)((0, 2), (0, 8))
            await pilot.press("ctrl+o")
            await pilot.pause()
            palette = app.query_one("#command-palette", CommandPalette)
            opened = palette.is_open
            await pilot.press("escape")
            await pilot.pause()
            return editor.value, editor.selection, opened, editor.has_focus

    draft, selection, opened, focused = anyio.run(scenario)
    assert draft == "draft in progress"
    assert selection.start == (0, 2)
    assert selection.end == (0, 8)
    assert opened is True
    assert focused is True


def test_ctrl_o_palette_preserves_transcript_scroll_position() -> None:
    async def scenario() -> tuple[float, float, float, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            for index in range(40):
                app.write_notice(f"historical line {index}")
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.return_to_latest()
            await pilot.pause()
            transcript.scroll_home(animate=False)
            await pilot.pause()
            before = transcript.scroll_y
            following_before = transcript.is_following
            await pilot.press("ctrl+o")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return before, transcript.scroll_y, transcript.max_scroll_y, following_before

    before, after, max_scroll_y, following_before = anyio.run(scenario)
    assert max_scroll_y > 0
    assert following_before is False
    assert after == before


def test_ctrl_o_palette_preserves_near_tail_scroll_with_multiline_draft() -> None:
    async def scenario() -> tuple[float, float, float, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft line 1\ndraft line 2\ndraft line 3\ndraft line 4"
            for index in range(80):
                app.write_notice(f"historical line {index}")
            await pilot.pause()
            transcript = app.query_one("#transcript", Transcript)
            transcript.return_to_latest()
            await pilot.pause()
            target_y = max(0.0, transcript.max_scroll_y - 2.0)
            transcript.scroll_to(y=target_y, animate=False)
            await pilot.pause()
            before = transcript.scroll_y
            following_before = transcript.is_following
            max_scroll_y = transcript.max_scroll_y
            await pilot.press("ctrl+o")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            return (
                max_scroll_y,
                before,
                transcript.scroll_y,
                following_before,
                transcript.is_following,
            )

    max_scroll_y, before, after, following_before, following_after = anyio.run(scenario)
    assert max_scroll_y > 2
    assert following_before is False
    assert after == before
    assert following_after is False


def test_palette_selection_submits_existing_command_path_without_clearing_draft() -> None:
    async def scenario() -> tuple[str, str]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "draft stays here"
            await pilot.press("ctrl+o")
            await pilot.pause()
            await pilot.press("enter")
            answer = await app.read_prompt("wisp> ")
            return answer, editor.value

    answer, draft = anyio.run(scenario)
    assert answer == "/help"
    assert draft == "draft stays here"


def test_palette_keyboard_navigation_selects_highlighted_command() -> None:
    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause()
            await pilot.press("down", "enter")
            return await app.read_prompt("wisp> ")

    assert anyio.run(scenario) == "/compact"


def test_palette_required_argument_prefills_instead_of_submitting() -> None:
    catalog = TuiCommandCatalog(
        (
            CommandDescriptor(
                name="model",
                title="Model",
                description="Choose a model",
                category=CommandCategory.configuration,
                arguments=(CommandArgument(name="model", required=True),),
                accepts_arguments=True,
            ),
        )
    )

    async def scenario() -> tuple[str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            app.set_command_catalog(catalog)
            await pilot.press("ctrl+o")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return editor.value, editor.has_focus

    value, focused = anyio.run(scenario)
    assert value == "/model "
    assert focused is True


def test_loaded_catalog_updates_palette_and_inline_slash_suggestions() -> None:
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

    async def scenario() -> tuple[int, int, Content]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            app.set_command_catalog(catalog)
            await pilot.press("ctrl+o")
            await pilot.pause()
            palette_options = app.query_one("#command-palette-options", OptionList)
            palette_count = palette_options.option_count
            prompt = palette_options.get_option_at_index(0).prompt
            await pilot.press("escape")
            editor.value = "/"
            await pilot.pause()
            suggest = app.query_one("#suggest", SlashSuggest)
            return palette_count, suggest.option_count, prompt

    palette_count, suggest_count, prompt = anyio.run(scenario)
    assert palette_count == 1
    assert suggest_count == 1
    assert isinstance(prompt, Content)
    assert "[Model]" in prompt.plain
    assert "[Choose]" in prompt.plain


@pytest.mark.parametrize("size", [(40, 16), (72, 20), (80, 24), (120, 40)])
def test_palette_is_bounded_above_composer(size: tuple[int, int]) -> None:
    async def scenario() -> tuple[int, int, int, int, int, int]:
        app = TextualTui()
        async with app.run_test(size=size) as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause()
            palette = app.query_one("#command-palette", CommandPalette)
            composer = app.query_one("#composer")
            return (
                palette.region.x,
                palette.region.right,
                palette.region.y,
                palette.region.bottom,
                composer.region.y,
                composer.region.bottom,
            )

    left, right, palette_top, palette_bottom, composer_top, composer_bottom = anyio.run(scenario)
    assert left >= 0
    assert right <= size[0]
    assert palette_bottom <= composer_top, (
        palette_top,
        palette_bottom,
        composer_top,
        composer_bottom,
    )


def test_palette_rejects_selection_event_timestamped_before_open() -> None:
    async def scenario() -> bool:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause()
            palette = app.query_one("#command-palette", CommandPalette)
            options = app.query_one("#command-palette-options", OptionList)
            selected = OptionList.OptionSelected(
                options,
                options.get_option_at_index(0),
                0,
            )
            selected.time = palette._opened_at - 1.0
            palette.on_option_list_option_selected(selected)
            with anyio.move_on_after(0.01) as scope:
                await app.read_prompt("wisp> ")
            return scope.cancel_called

    assert anyio.run(scenario) is True


def test_palette_accepts_fresh_option_selection() -> None:
    async def scenario() -> str:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+o")
            await pilot.pause()
            palette = app.query_one("#command-palette", CommandPalette)
            options = app.query_one("#command-palette-options", OptionList)
            selected = OptionList.OptionSelected(
                options,
                options.get_option_at_index(0),
                0,
            )
            selected.time = palette._opened_at + 1.0
            palette.on_option_list_option_selected(selected)
            await pilot.pause()
            return await app.read_prompt("wisp> ")

    assert anyio.run(scenario) == "/help"
