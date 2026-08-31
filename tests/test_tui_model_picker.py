from __future__ import annotations

from typing import Any, cast
from unittest import mock

import anyio
import pytest
from textual import events
from textual.app import App
from textual.widgets import OptionList, RadioButton, RadioSet

from wisp.events import (
    RpcModelCatalogEntry,
    RpcModelCatalogSnapshot,
    RpcModelProviderSnapshot,
    RpcModelSelectionSnapshot,
)
from wisp.providers.catalog import ModelCatalogProviderEntry, builtin_catalog
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import ModelPicker
from wisp.tui.widgets import PromptEditor as Input

pytestmark = pytest.mark.tui


def _entry(
    name: str,
    *,
    display_name: str | None = None,
    default_model: str,
    models: tuple[str, ...],
    effort_levels: dict[str, tuple[str, ...]] | None = None,
    model_aliases: dict[str, str] | None = None,
    model_lifecycle: dict[str, str] | None = None,
) -> ModelCatalogProviderEntry:
    return ModelCatalogProviderEntry(
        name=name,
        display_name=display_name or name.title(),
        default_model=default_model,
        docs_url=f"https://example.test/{name}",
        models=models,
        effort_levels=effort_levels or {},
        model_aliases=model_aliases or {},
        model_lifecycle=model_lifecycle or {},
    )


_ANTHROPIC = _entry(
    "anthropic",
    default_model="claude-opus-4-8",
    models=("claude-opus-4-8", "claude-haiku-4-5"),
    effort_levels={"claude-opus-4-8": ("low", "medium", "high")},
)
_OPENAI = _entry(
    "openai",
    display_name="OpenAI",
    default_model="gpt-5.5",
    models=("gpt-5.5",),
)
_ENTRIES = (_ANTHROPIC, _OPENAI)


def _snapshot(
    entries: tuple[ModelCatalogProviderEntry, ...],
    *,
    current_provider: str,
    current_model: str | None,
    current_effort: str | None,
    unavailable: frozenset[str] = frozenset(),
) -> RpcModelCatalogSnapshot:
    provider = next((entry for entry in entries if entry.name == current_provider), None)
    effective_model = current_model or (provider.default_model if provider is not None else None)
    return RpcModelCatalogSnapshot(
        selection=RpcModelSelectionSnapshot(
            provider=current_provider,
            model=current_model,
            effective_model=effective_model,
            catalog_model=(
                provider.canonical_model(effective_model)
                if provider is not None and effective_model is not None
                else None
            ),
            effort=current_effort,
        ),
        providers=tuple(
            RpcModelProviderSnapshot(
                name=entry.name,
                display_name=entry.display_name,
                default_model=entry.default_model,
                available=entry.name not in unavailable,
                models=tuple(
                    RpcModelCatalogEntry(
                        id=model_id,
                        lifecycle=cast(Any, entry.model_lifecycle.get(model_id)),
                        effort_levels=entry.effort_levels.get(model_id, ()),
                    )
                    for model_id in entry.models
                ),
            )
            for entry in entries
        ),
    )


def _show(
    renderer: Any,
    entries: tuple[ModelCatalogProviderEntry, ...],
    *,
    current_provider: str,
    current_model: str | None,
    current_effort: str | None,
    unavailable: frozenset[str] = frozenset(),
) -> None:
    renderer.model_picker_request(
        _snapshot(
            entries,
            current_provider=current_provider,
            current_model=current_model,
            current_effort=current_effort,
            unavailable=unavailable,
        )
    )


def test_model_picker_lists_providers_and_models_with_current_marked() -> None:
    async def scenario() -> tuple[list[str], list[bool], int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
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
        "Anthropic",
        "  claude-opus-4-8",
        "  claude-haiku-4-5 (current)",
        "OpenAI",
        "  gpt-5.5",
    ]
    assert disabled == [True, False, False, True, False]
    assert highlighted == 2


def test_model_picker_distinguishes_openai_api_and_codex_subscription() -> None:
    entries = tuple(
        entry for entry in builtin_catalog().providers if entry.name in {"openai", "openai-codex"}
    )

    async def scenario() -> list[str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(100, 30)) as pilot:
            _show(
                renderer,
                entries,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            return [
                str(options.get_option_at_index(index).prompt)
                for index in range(options.option_count)
                if options.get_option_at_index(index).disabled
            ]

    assert anyio.run(scenario) == [
        "OpenAI API",
        "OpenAI Codex (ChatGPT subscription)",
    ]


def test_model_picker_disables_unavailable_providers() -> None:
    async def scenario() -> tuple[list[str], list[bool], int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="anthropic",
                current_model=None,
                current_effort=None,
                unavailable=frozenset({"anthropic"}),
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            return (
                [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)],
                [options.get_option_at_index(i).disabled for i in range(options.option_count)],
                options.highlighted,
            )

    labels, disabled, highlighted = anyio.run(scenario)
    assert labels[0] == "Anthropic (unavailable)"
    assert disabled == [True, True, True, True, False]
    assert highlighted == 4


def test_model_picker_marks_provider_default_current_when_model_unset() -> None:
    async def scenario() -> int | None:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
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


def test_model_picker_defaults_to_first_selectable_row_when_current_is_uncataloged() -> None:
    # Regression test (Codex review on #125): a permissive "/model
    # brand-new-model" (or a custom provider) leaves current_provider/
    # current_model with no matching row at all -- default_index must not
    # silently stay on index 0, the first (disabled) provider header, which
    # would open the picker on a non-interactive row where Enter does nothing
    # until the user manually navigates.
    async def scenario() -> tuple[int | None, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="uncataloged-provider",
                current_model="uncataloged-model",
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            highlighted = options.highlighted
            is_disabled = (
                options.get_option_at_index(highlighted).disabled
                if highlighted is not None
                else True
            )
            return highlighted, is_disabled

    highlighted, is_disabled = anyio.run(scenario)
    # index 1 == the first entry's first model row (anthropic::claude-opus-4-8).
    assert highlighted == 1
    assert is_disabled is False


def test_model_picker_marks_an_alias_current_and_labels_nonstable_models() -> None:
    entry = _entry(
        "acme",
        default_model="acme-1",
        models=("acme-1", "acme-preview"),
        model_aliases={"acme-latest": "acme-1"},
        model_lifecycle={"acme-1": "stable", "acme-preview": "preview"},
    )

    async def scenario() -> list[str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                (entry,),
                current_provider="acme",
                current_model="acme-latest",
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]

    assert anyio.run(scenario) == [
        "Acme",
        "  acme-1 (current)",
        "  acme-preview (preview)",
    ]


def test_model_picker_hides_composer_and_focuses_options() -> None:
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            _show(
                renderer,
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
            _show(
                renderer,
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._input_controller.receive_stream.receive()
            assert isinstance(answer, str)
            return answer

    assert anyio.run(scenario) == "/model openai::gpt-5.5"


def test_model_picker_uses_radio_set_and_arrow_keys_cycle_effort() -> None:
    async def scenario() -> tuple[str, str, str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-opus-4-8",
                current_effort=None,
            )
            await pilot.pause()
            radio = app.query_one("#model-picker-effort RadioSet", RadioSet)
            before = next(str(button.label) for button in radio.query(RadioButton) if button.value)

            await pilot.press("right")
            await pilot.pause()
            after_one_right = next(
                str(button.label) for button in radio.query(RadioButton) if button.value
            )

            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._input_controller.receive_stream.receive()
            assert isinstance(answer, str)
            return before, after_one_right, answer

    before, after_one_right, answer = anyio.run(scenario)
    assert before == "Default"
    assert after_one_right == "low"
    assert answer == "/model anthropic::claude-opus-4-8 low"


def test_model_picker_displays_effort_for_every_builtin_codex_model() -> None:
    codex = next(entry for entry in builtin_catalog().providers if entry.name == "openai-codex")

    async def scenario() -> dict[str, tuple[str, ...]]:
        app, renderer = create_textual_tui()
        rendered: dict[str, tuple[str, ...]] = {}
        async with app.run_test(size=(80, 24)) as pilot:
            for model in codex.models:
                _show(
                    renderer,
                    (codex,),
                    current_provider="openai-codex",
                    current_model=model,
                    current_effort=None,
                )
                await pilot.pause()
                radio = app.query_one("#model-picker-effort RadioSet", RadioSet)
                rendered[model] = tuple(str(button.label) for button in radio.query(RadioButton))
        return rendered

    rendered = anyio.run(scenario)
    assert set(rendered) == set(codex.models)
    assert all(labels[0] == "Default" for labels in rendered.values())
    assert "max" in rendered["gpt-5.6-terra"]
    assert "max" not in rendered["gpt-5.4"]


def test_model_picker_mouse_selects_effort_without_taking_model_list_focus() -> None:
    async def scenario() -> tuple[str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-opus-4-8",
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            radio = app.query_one("#model-picker-effort RadioSet", RadioSet)
            high = next(
                button for button in radio.query(RadioButton) if str(button.label) == "high"
            )

            assert await pilot.click(high)
            await pilot.pause()
            model_list_focused = app.focused is options
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._input_controller.receive_stream.receive()
            assert isinstance(answer, str)
            return answer, model_list_focused

    answer, model_list_focused = anyio.run(scenario)
    assert answer == "/model anthropic::claude-opus-4-8 high"
    assert model_list_focused


def test_model_picker_left_right_ignored_for_model_without_effort_levels() -> None:
    async def scenario() -> tuple[str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="openai",
                current_model="gpt-5.5",
                current_effort=None,
            )
            await pilot.pause()
            effort_hidden = not app.query_one("#model-picker-effort").display
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._input_controller.receive_stream.receive()
            assert isinstance(answer, str)
            return answer, effort_hidden

    answer, effort_hidden = anyio.run(scenario)
    assert answer == "/model openai::gpt-5.5"
    assert effort_hidden


def test_model_picker_escape_cancels_without_submitting_and_restores_composer() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            _show(
                renderer,
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
            _show(
                renderer,
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
                answer = await app._input_controller.receive_stream.receive()
            assert isinstance(answer, str)
            return rejected, answer

    rejected, answer = anyio.run(scenario)
    assert rejected
    assert answer == "/model openai::gpt-5.5"


def test_model_picker_preserves_composer_draft_across_selection() -> None:
    async def scenario() -> tuple[str, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "draft follow-up"
            _show(
                renderer,
                _ENTRIES,
                current_provider="openai",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._input_controller.receive_stream.receive()
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
    # overlay-controller barrier before hiding the composer/moving focus, so a key
    # already queued for the composer can't land on it (still focused, only
    # hidden) or on the picker's OptionList once focus lands there.
    async def scenario() -> tuple[bool, bool]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
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
                assert app._overlay_controller is not None
                barrier = app._overlay_controller.stale_event_barrier
                stale_key.time = barrier - 1.0
                await app.on_event(stale_key)

                fresh_key = events.Key("enter", None)
                fresh_key.set_sender(app)
                fresh_key.time = barrier + 1.0
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
            _show(
                renderer,
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


# "gpt-5.5" is deliberately claimed by two providers, mirroring the real
# built-in catalog (openai and openai-codex both accept the same OpenAI model
# names) -- regression fixture for the picker-must-qualify-provider finding.
_OPENAI_SHARED = _entry(
    "openai",
    default_model="gpt-5.5",
    models=("gpt-5.5",),
)
_OPENAI_CODEX_SHARED = _entry(
    "openai-codex",
    default_model="gpt-5.5",
    models=("gpt-5.5",),
)
_SHARED_ID_ENTRIES = (_OPENAI_SHARED, _OPENAI_CODEX_SHARED)


def test_model_picker_qualifies_selection_with_provider_for_a_shared_model_id() -> None:
    # Regression test: a bare "/model gpt-5.5" is ambiguous between the two
    # providers below -- ModelRegistry.resolve() can't disambiguate it without
    # a `prefer` hint that matches, and the shell's active provider being
    # neither (or the "wrong" one of the two) would silently fail to switch to
    # the row the user actually picked. The picker must send an unambiguous,
    # provider-qualified answer regardless of which row is highlighted.
    async def scenario() -> tuple[str, str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _SHARED_ID_ENTRIES,
                current_provider="anthropic",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            # Row order: [0]=openai header, [1]=openai::gpt-5.5,
            # [2]=openai-codex header, [3]=openai-codex::gpt-5.5.
            options.highlighted = 1
            await pilot.press("enter")
            with anyio.fail_after(1):
                first_answer = await app._input_controller.receive_stream.receive()
            assert isinstance(first_answer, str)

            _show(
                renderer,
                _SHARED_ID_ENTRIES,
                current_provider="anthropic",
                current_model=None,
                current_effort=None,
            )
            await pilot.pause()
            options = app.query_one("#model-picker-options", OptionList)
            options.highlighted = 3
            await pilot.press("enter")
            with anyio.fail_after(1):
                second_answer = await app._input_controller.receive_stream.receive()
            assert isinstance(second_answer, str)
            return first_answer, second_answer

    first_answer, second_answer = anyio.run(scenario)
    assert first_answer == "/model openai::gpt-5.5"
    assert second_answer == "/model openai-codex::gpt-5.5"


def test_model_picker_cycling_back_to_default_sends_explicit_clear_token() -> None:
    # Regression test: cycling effort left back past the lowest tier lands on
    # None/"(default)" -- indistinguishable, by value alone, from a row whose
    # effort was never touched at all. An untouched row must omit the effort
    # argument entirely (so the shell leaves any already-configured effort
    # alone); an explicitly-cleared row must send a token the shell recognizes
    # as "clear it," or a previously-persisted tier would never be clearable
    # through the picker.
    async def scenario() -> tuple[str, str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-opus-4-8",
                current_effort="high",
            )
            await pilot.pause()

            # Untouched: submit immediately without cycling effort at all.
            await pilot.press("enter")
            with anyio.fail_after(1):
                untouched_answer = await app._input_controller.receive_stream.receive()
            assert isinstance(untouched_answer, str)

            _show(
                renderer,
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-opus-4-8",
                current_effort="high",
            )
            await pilot.pause()
            # "high" is the seeded/current tier -- cycle left 3 times (high ->
            # medium -> low -> default) to explicitly land back on default.
            await pilot.press("left")
            await pilot.press("left")
            await pilot.press("left")
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                cleared_answer = await app._input_controller.receive_stream.receive()
            assert isinstance(cleared_answer, str)
            return untouched_answer, cleared_answer

    untouched_answer, cleared_answer = anyio.run(scenario)
    assert untouched_answer == "/model anthropic::claude-opus-4-8 high"
    assert cleared_answer == "/model anthropic::claude-opus-4-8 -"


def test_model_picker_does_not_seed_a_tier_the_current_row_does_not_list() -> None:
    # Regression test (Codex review on #125): current_effort is a
    # caller-supplied value (e.g. a stale global setting.json string from a
    # different provider's vocabulary, like Google's uppercase "HIGH") -- not
    # guaranteed to be one of this exact model's catalog-listed tiers. Seeding
    # it onto the "current" row anyway would let an untouched Enter resubmit
    # an incompatible tier the caller never actually validated.
    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            _show(
                renderer,
                _ENTRIES,
                current_provider="anthropic",
                current_model="claude-opus-4-8",
                current_effort="HIGH",  # not in _ANTHROPIC's ("low","medium","high")
            )
            await pilot.pause()
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._input_controller.receive_stream.receive()
            assert isinstance(answer, str)
            return answer

    assert anyio.run(scenario) == "/model anthropic::claude-opus-4-8"
