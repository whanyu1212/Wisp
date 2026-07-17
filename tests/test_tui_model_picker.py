from __future__ import annotations

from unittest import mock

import anyio
from textual import events
from textual.app import App
from textual.widgets import OptionList, Static

from wisp.providers.catalog import ModelCatalogProviderEntry
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import ModelPicker
from wisp.tui.widgets import PromptEditor as Input


def _entry(
    name: str,
    *,
    default_model: str,
    models: tuple[str, ...],
    effort_levels: dict[str, tuple[str, ...]] | None = None,
) -> ModelCatalogProviderEntry:
    return ModelCatalogProviderEntry(
        name=name,
        display_name=name.title(),
        default_model=default_model,
        docs_url=f"https://example.test/{name}",
        models=models,
        effort_levels=effort_levels or {},
    )


_ANTHROPIC = _entry(
    "anthropic",
    default_model="claude-opus-4-8",
    models=("claude-opus-4-8", "claude-haiku-4-5"),
    effort_levels={"claude-opus-4-8": ("low", "medium", "high")},
)
_OPENAI = _entry(
    "openai",
    default_model="gpt-5.5",
    models=("gpt-5.5",),
)
_ENTRIES = (_ANTHROPIC, _OPENAI)


def test_model_picker_lists_providers_and_models_with_current_marked() -> None:
    async def scenario() -> tuple[list[str], list[bool], int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-haiku-4-5",
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            labels = [
                str(options.get_option_at_index(i).prompt) for i in range(options.option_count)
            ]
            disabled = [
                options.get_option_at_index(i).disabled for i in range(options.option_count)
            ]
            return labels, disabled, options.highlighted

    labels, disabled, highlighted = anyio.run(scenario)
    assert labels == [
        "anthropic",
        "  claude-opus-4-8",
        "  claude-haiku-4-5 (current)",
        "openai",
        "  gpt-5.5",
    ]
    assert disabled == [True, False, False, True, False]
    assert highlighted == 2


def test_model_picker_marks_provider_default_current_when_model_unset() -> None:
    async def scenario() -> int | None:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="anthropic",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            return options.highlighted

    # index 1 == claude-opus-4-8, anthropic's default_model.
    assert anyio.run(scenario) == 1


def test_model_picker_hides_composer_and_focuses_options() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            return (not input_widget.display, app.focused is options)

    hidden, focused = anyio.run(scenario)
    assert hidden
    assert focused


def test_model_picker_enter_selects_highlighted_model_without_effort() -> None:
    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return answer

    assert anyio.run(scenario) == "/model gpt-5.5"


def test_model_picker_arrow_keys_cycle_effort_and_submit_it() -> None:
    async def scenario() -> tuple[str, str, str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-opus-4-8",
                current_effort=None,
            )
            await pilot.pause()
            effort_line = app.query_one("#model-picker-effort", Static)
            before = str(effort_line.render())

            await pilot.press("right")
            await pilot.pause()
            after_one_right = str(effort_line.render())

            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return before, after_one_right, answer

    before, after_one_right, answer = anyio.run(scenario)
    assert "(default)" in before
    assert "[low]" in after_one_right
    assert answer == "/model claude-opus-4-8 low"


def test_model_picker_left_right_ignored_for_model_without_effort_levels() -> None:
    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model="gpt-5.5",
                current_effort=None,
            )
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return answer

    assert anyio.run(scenario) == "/model gpt-5.5"


def test_model_picker_escape_cancels_without_submitting_and_restores_composer() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            picker = app.query_one("#model-picker", ModelPicker)

            await pilot.press("escape")
            await pilot.pause()

            return (
                not picker.is_open,
                input_widget.display,
                app.focused is input_widget,
            )

    hidden, input_visible, input_focused = anyio.run(scenario)
    assert hidden
    assert input_visible
    assert input_focused


def test_model_picker_drops_stale_key_and_selection_queued_before_open() -> None:
    async def scenario() -> tuple[bool, str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()

            picker = app.query_one("#model-picker", ModelPicker)
            options = app.query_one("#model-picker-options", OptionList)

            stale_key = events.Key("right", None)
            stale_key.set_sender(app)
            stale_key.time = picker._opened_at - 1.0
            picker.on_key(stale_key)

            option = options.get_option_at_index(1)
            assert option.id is not None
            stale_selected = OptionList.OptionSelected(options, option, 1)
            stale_selected.time = picker._opened_at - 1.0
            picker.on_option_list_option_selected(stale_selected)

            await pilot.pause()
            rejected = not picker._submitted

            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return rejected, answer

    rejected, answer = anyio.run(scenario)
    assert rejected
    assert answer == "/model gpt-5.5"


def test_model_picker_preserves_composer_draft_across_selection() -> None:
    async def scenario() -> tuple[str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "draft follow-up"
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            await pilot.pause()
            restored = input_widget.display and app.focused is input_widget
            return input_widget.value, restored

    draft, restored = anyio.run(scenario)
    assert draft == "draft follow-up"
    assert restored


def test_app_on_event_drops_stale_key_queued_before_model_picker_opened() -> None:
    # Same app-level barrier DecisionPanel relies on (see
    # test_app_on_event_drops_key_queued_before_decision_panel_opened in
    # test_tui_decision_panel.py) -- show_model_picker must also raise
    # _stale_event_barrier before hiding the composer/moving focus, so a key
    # already queued for the composer can't land on it (still focused, only
    # hidden) or on the picker's OptionList once focus lands there.
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()

            forwarded: list[events.Key] = []

            async def recording_app_on_event(_self: object, event: events.Event) -> None:
                if isinstance(event, events.Key):
                    forwarded.append(event)

            with mock.patch.object(App, "on_event", recording_app_on_event):
                stale_key = events.Key("enter", None)
                stale_key.set_sender(app)
                stale_key.time = app._stale_event_barrier - 1.0
                await app.on_event(stale_key)

                fresh_key = events.Key("enter", None)
                fresh_key.set_sender(app)
                fresh_key.time = app._stale_event_barrier + 1.0
                await app.on_event(fresh_key)

            return stale_key not in forwarded, fresh_key in forwarded

    stale_rejected, fresh_forwarded = anyio.run(scenario)
    assert stale_rejected
    assert fresh_forwarded


def test_model_picker_cancel_restores_composer_draft() -> None:
    async def scenario() -> tuple[str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "draft follow-up"
            renderer.model_picker_request(
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            restored = input_widget.display and app.focused is input_widget
            return input_widget.value, restored

    draft, restored = anyio.run(scenario)
    assert draft == "draft follow-up"
    assert restored
