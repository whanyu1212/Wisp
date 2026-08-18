from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import anyio
import pytest
from rich.style import Style

from wisp.runtime.commands import CommandDescriptor
from wisp.tui.commands import TuiCommandCatalog
from wisp.tui.file_index import (
    FileIndexRequest,
    ProjectFile,
    ProjectSnapshot,
    SnapshotTruncation,
)
from wisp.tui.file_suggest import FileSuggest
from wisp.tui.prompt_highlighting import (
    MAX_PROMPT_HIGHLIGHT_DOCUMENT_CHARACTERS,
    MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS,
    MAX_PROMPT_HIGHLIGHT_LINES,
    MAX_PROMPT_HIGHLIGHTS_PER_DOCUMENT,
    MAX_PROMPT_HIGHLIGHTS_PER_LINE,
    PromptHighlight,
    prompt_document_highlights,
    prompt_line_highlights,
)
from wisp.tui.textual_app import TextualTui
from wisp.tui.widgets import PromptEditor

pytestmark = pytest.mark.tui


def _highlights(
    line: str,
    *,
    line_index: int = 0,
    line_count: int = 1,
    command_tokens: frozenset[str] = frozenset(),
    project_paths: frozenset[str] | None = None,
    unresolved_paths_known: bool = True,
) -> tuple[PromptHighlight, ...]:
    return prompt_line_highlights(
        line,
        line_index=line_index,
        line_count=line_count,
        command_tokens=command_tokens,
        project_paths=project_paths,
        unresolved_paths_known=unresolved_paths_known,
    )


def _document_highlights(
    text: str,
    *,
    command_tokens: frozenset[str] = frozenset(),
    project_paths: frozenset[str] | None = None,
    unresolved_paths_known: bool = True,
) -> tuple[tuple[PromptHighlight, ...], ...]:
    return prompt_document_highlights(
        text.split("\n"),
        command_tokens=command_tokens,
        project_paths=project_paths,
        unresolved_paths_known=unresolved_paths_known,
    )


@pytest.mark.parametrize("token", ["/model", "/models"])
def test_recognized_command_token_is_highlighted(token: str) -> None:
    assert _highlights(
        f"  {token} openai/gpt-5",
        command_tokens=frozenset({"/model", "/models"}),
    ) == (PromptHighlight(2, 2 + len(token), "command"),)


def test_unknown_and_inline_commands_remain_neutral() -> None:
    command_tokens = frozenset({"/model"})

    assert _highlights("/unknown", command_tokens=command_tokens) == ()
    assert _highlights("please run /model", command_tokens=command_tokens) == ()


def test_multiline_runtime_command_remains_neutral_like_command_parser() -> None:
    assert (
        _highlights(
            "/model openai/gpt-5",
            line_count=2,
            command_tokens=frozenset({"/model"}),
        )
        == ()
    )


def test_resolved_and_unresolved_bare_paths_are_distinguished() -> None:
    line = "Compare @src/app.py with @missing.py"

    assert _highlights(
        line,
        project_paths=frozenset({"src/app.py"}),
    ) == (
        PromptHighlight(8, 19, "resolved_path"),
        PromptHighlight(25, 36, "unresolved_path"),
    )


def test_paths_remain_neutral_until_snapshot_is_available() -> None:
    assert _highlights("Read @src/app.py", project_paths=None) == ()


def test_json_quoted_unicode_path_uses_decoded_snapshot_value() -> None:
    reference = '@"docs/设计 notes.md"'
    line = f"Read {reference} now"

    assert _highlights(
        line,
        project_paths=frozenset({"docs/设计 notes.md"}),
    ) == (PromptHighlight(5, 5 + len(reference), "resolved_path"),)


def test_json_quoted_path_decodes_escapes_before_snapshot_lookup() -> None:
    reference = '@"docs/a\\\\b\\"c.md"'

    assert _highlights(
        f"Read {reference}",
        project_paths=frozenset({'docs/a\\b"c.md'}),
    ) == (PromptHighlight(5, 5 + len(reference), "resolved_path"),)


def test_malformed_quoted_path_is_subtly_unresolved() -> None:
    line = 'Read @"unterminated path'

    assert _highlights(line, project_paths=frozenset()) == (
        PromptHighlight(5, len(line), "unresolved_path"),
    )


def test_text_attached_to_closing_quote_keeps_whole_token_unresolved() -> None:
    line = 'Read @"docs/a.md"suffix'

    assert _highlights(line, project_paths=frozenset({"docs/a.md"})) == (
        PromptHighlight(5, len(line), "unresolved_path"),
    )


def test_embedded_at_signs_and_lone_trigger_are_not_references() -> None:
    assert (
        _highlights(
            "mail dev@example.com or type @",
            project_paths=frozenset({"example.com"}),
        )
        == ()
    )


def test_directory_reference_requires_snapshot_display_spelling() -> None:
    assert _highlights(
        "Inspect @src/ then @src",
        project_paths=frozenset({"src/"}),
    ) == (
        PromptHighlight(8, 13, "resolved_path"),
        PromptHighlight(19, 23, "unresolved_path"),
    )


def test_scanning_is_bounded_by_line_and_span_limits() -> None:
    repeated = " ".join("@missing.py" for _ in range(MAX_PROMPT_HIGHLIGHTS_PER_LINE + 20))
    line = repeated + ("x" * MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS)

    highlights = _highlights(line, project_paths=frozenset())

    assert len(highlights) == MAX_PROMPT_HIGHLIGHTS_PER_LINE
    assert all(highlight.end <= MAX_PROMPT_HIGHLIGHT_LINE_CHARACTERS for highlight in highlights)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("# Heading", PromptHighlight(0, 9, "markdown_heading")),
        ("   ###### Heading", PromptHighlight(3, 17, "markdown_heading")),
        ("- item", PromptHighlight(0, 1, "markdown_list_marker")),
        ("  * item", PromptHighlight(2, 3, "markdown_list_marker")),
        ("9) item", PromptHighlight(0, 2, "markdown_list_marker")),
        ("123456789. item", PromptHighlight(0, 10, "markdown_list_marker")),
    ],
)
def test_markdown_headings_and_list_markers_are_highlighted(
    line: str,
    expected: PromptHighlight,
) -> None:
    assert _document_highlights(line) == ((expected,),)


@pytest.mark.parametrize(
    "line",
    [
        "#attached",
        "####### too deep",
        "    # code-indented",
        "-attached",
        "1234567890. too many digits",
    ],
)
def test_invalid_or_ambiguous_block_markers_remain_neutral(line: str) -> None:
    assert _document_highlights(line) == ((),)


def test_inline_code_supports_matching_single_and_multi_backtick_runs() -> None:
    line = "Use `value` and ``a ` b`` now"

    assert _document_highlights(line) == (
        (
            PromptHighlight(4, 5, "markdown_inline_code_delimiter"),
            PromptHighlight(5, 10, "markdown_inline_code"),
            PromptHighlight(10, 11, "markdown_inline_code_delimiter"),
            PromptHighlight(16, 18, "markdown_inline_code_delimiter"),
            PromptHighlight(18, 23, "markdown_inline_code"),
            PromptHighlight(23, 25, "markdown_inline_code_delimiter"),
        ),
    )


def test_unmatched_inline_code_and_mismatched_runs_remain_conservative() -> None:
    assert _document_highlights("Use `unfinished") == ((),)
    assert _document_highlights("Use ``value`") == ((),)


def test_nested_looking_backtick_runs_are_content_not_nested_spans() -> None:
    line = "`outer ``inner`` text`"

    assert _document_highlights(line) == (
        (
            PromptHighlight(0, 1, "markdown_inline_code_delimiter"),
            PromptHighlight(1, 21, "markdown_inline_code"),
            PromptHighlight(21, 22, "markdown_inline_code_delimiter"),
        ),
    )


def test_adversarial_inline_delimiter_count_fails_neutral() -> None:
    line = " ".join("`" for _ in range(2_050))

    assert _document_highlights(line) == ((),)


def test_backtick_fence_tracks_language_body_and_closing_delimiter() -> None:
    source = "```python extra\nprint('hello')\n```"

    assert _document_highlights(source) == (
        (
            PromptHighlight(0, 3, "markdown_fence_delimiter"),
            PromptHighlight(3, 9, "markdown_fence_info"),
        ),
        (PromptHighlight(0, 14, "markdown_fence_body"),),
        (PromptHighlight(0, 3, "markdown_fence_delimiter"),),
    )


def test_tilde_fence_requires_a_compatible_complete_closer() -> None:
    source = "~~~~ text\n# body\n```\n~~~\n~~~~   \n# heading"

    assert _document_highlights(source) == (
        (
            PromptHighlight(0, 4, "markdown_fence_delimiter"),
            PromptHighlight(5, 9, "markdown_fence_info"),
        ),
        (PromptHighlight(0, 6, "markdown_fence_body"),),
        (PromptHighlight(0, 3, "markdown_fence_body"),),
        (PromptHighlight(0, 3, "markdown_fence_body"),),
        (PromptHighlight(0, 4, "markdown_fence_delimiter"),),
        (PromptHighlight(0, 9, "markdown_heading"),),
    )


def test_incomplete_fence_styles_remaining_lines_without_raising() -> None:
    source = "```python\ndef value() -> str:\n    return '✓'"

    highlights = _document_highlights(source)

    assert highlights[1] == (PromptHighlight(0, 19, "markdown_fence_body"),)
    assert highlights[2] == (PromptHighlight(0, 14, "markdown_fence_body"),)


def test_large_paste_placeholder_remains_literal_and_neutral() -> None:
    placeholder = "[Pasted content #1: 10,000 characters, 200 lines, 9.8 KB]"

    assert _document_highlights(placeholder) == ((),)


def test_paths_override_broad_markdown_styles_by_application_order() -> None:
    source = "# Inspect @src/app.py\n`see @src/app.py now`\n```text\n@src/app.py\n```"

    highlights = _document_highlights(
        source,
        project_paths=frozenset({"src/app.py"}),
    )

    assert highlights[0][-1] == PromptHighlight(10, 21, "resolved_path")
    assert highlights[1][-1] == PromptHighlight(5, 16, "resolved_path")
    assert highlights[3][-1] == PromptHighlight(0, 11, "resolved_path")


def test_document_scanning_is_bounded_by_char_line_and_span_limits() -> None:
    dense_line = " ".join("`x`" for _ in range(MAX_PROMPT_HIGHLIGHTS_PER_LINE))
    dense_lines = [dense_line] * (MAX_PROMPT_HIGHLIGHT_LINES + 10)

    dense_highlights = prompt_document_highlights(
        dense_lines,
        command_tokens=frozenset(),
        project_paths=None,
        unresolved_paths_known=False,
    )
    character_lines = ["x" * 1_000] * 500
    character_highlights = prompt_document_highlights(
        character_lines,
        command_tokens=frozenset(),
        project_paths=None,
        unresolved_paths_known=False,
    )
    line_highlights = prompt_document_highlights(
        ["plain"] * (MAX_PROMPT_HIGHLIGHT_LINES + 10),
        command_tokens=frozenset(),
        project_paths=None,
        unresolved_paths_known=False,
    )

    assert sum(len(line) for line in dense_highlights) == MAX_PROMPT_HIGHLIGHTS_PER_DOCUMENT
    assert len(character_highlights) < len(character_lines)
    assert (
        len(character_highlights) * 1_000 + len(character_highlights) - 1
        >= MAX_PROMPT_HIGHLIGHT_DOCUMENT_CHARACTERS
    )
    assert len(line_highlights) == MAX_PROMPT_HIGHLIGHT_LINES


def _catalog() -> TuiCommandCatalog:
    return TuiCommandCatalog(
        (
            CommandDescriptor(
                name="model",
                title="Choose model",
                description="Choose the active model",
                aliases=("models",),
            ),
        )
    )


def _snapshot(*paths: str) -> ProjectSnapshot:
    return ProjectSnapshot(
        root=Path("/work"),
        entries=tuple(ProjectFile(path) for path in paths),
    )


def _style_for_span(
    editor: PromptEditor,
    start: int,
    end: int,
    *,
    line_index: int = 0,
) -> Style | None:
    for span in editor.get_line(line_index).spans:
        if (span.start, span.end) == (start, end) and isinstance(span.style, Style):
            return span.style
    return None


def test_prompt_editor_applies_semantic_component_styles_without_changing_text() -> None:
    async def scenario() -> tuple[str, bool, bool, bool, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.set_command_catalog(_catalog())
            editor.set_project_snapshot(_snapshot("src/app.py"))
            editor.value = "/models @src/app.py @missing.py"
            await pilot.pause()

            command = _style_for_span(editor, 0, 7)
            resolved = _style_for_span(editor, 8, 19)
            unresolved = _style_for_span(editor, 20, 31)
            return (
                editor.text,
                command == editor.get_component_rich_style("prompt-editor--command", partial=True),
                resolved
                == editor.get_component_rich_style("prompt-editor--resolved-path", partial=True),
                unresolved
                == editor.get_component_rich_style("prompt-editor--unresolved-path", partial=True),
                bool(resolved and resolved.underline),
                bool(unresolved and unresolved.dim and unresolved.underline),
            )

    assert anyio.run(scenario) == (
        "/models @src/app.py @missing.py",
        True,
        True,
        True,
        True,
        True,
    )


def test_prompt_editor_restyles_live_catalog_and_snapshot_updates() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "/highlight-demo @src/app.py"
            before = editor.get_line(0).spans

            editor.set_command_catalog(
                TuiCommandCatalog(
                    (
                        CommandDescriptor(
                            name="highlight-demo",
                            title="Highlight demo",
                            description="Exercise a live catalog replacement",
                        ),
                    )
                )
            )
            editor.set_project_snapshot(_snapshot("src/app.py"))
            await pilot.pause()
            after = editor.get_line(0).spans

            editor.set_project_snapshot(None)
            editor.value = "Read @src/app.py"
            await pilot.pause()
            unavailable = editor.get_line(0).spans
            return not before, len(after) == 2, not unavailable

    assert anyio.run(scenario) == (True, True, True)


def test_incomplete_snapshots_resolve_known_paths_without_warning_for_absent_paths() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "Read @known.py and @possibly-omitted.py"
            editor.set_project_snapshot(
                ProjectSnapshot(
                    root=Path("/work"),
                    entries=(ProjectFile("known.py"),),
                    truncation=SnapshotTruncation(entry_limit_reached=True),
                )
            )
            await pilot.pause()
            known = _style_for_span(editor, 5, 14)
            omitted = _style_for_span(editor, 19, 39)

            editor.set_project_snapshot(ProjectSnapshot(root=Path("/work")))
            await pilot.pause()
            empty_snapshot_spans = editor.get_line(0).spans
            return (
                known
                == editor.get_component_rich_style("prompt-editor--resolved-path", partial=True),
                omitted is None,
                not empty_snapshot_spans,
            )

    assert anyio.run(scenario) == (True, True, True)


def test_semantic_state_updates_invalidate_cache_and_schedule_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[tuple[int, int], tuple[int, int]]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)):
            editor = app.query_one("#input", PromptEditor)
            notify_style_update = Mock(wraps=editor.notify_style_update)
            refresh = Mock(wraps=editor.refresh)
            monkeypatch.setattr(editor, "notify_style_update", notify_style_update)
            monkeypatch.setattr(editor, "refresh", refresh)

            editor.set_command_catalog(
                TuiCommandCatalog(
                    (
                        CommandDescriptor(
                            name="highlight-demo",
                            title="Highlight demo",
                            description="Exercise repaint behavior",
                        ),
                    )
                )
            )
            after_command = notify_style_update.call_count, refresh.call_count

            editor.set_project_snapshot(_snapshot("src/app.py"))
            after_snapshot = notify_style_update.call_count, refresh.call_count
            return after_command, after_snapshot

    assert anyio.run(scenario) == ((1, 1), (2, 2))


def test_prompt_editor_analyzes_a_document_once_per_content_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = Mock(wraps=prompt_document_highlights)
    monkeypatch.setattr("wisp.tui.widgets.prompt_document_highlights", scanner)

    async def scenario() -> tuple[int, int, int]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "# heading\n`code`"
            await pilot.pause()
            after_change = scanner.call_count

            editor.get_line(0)
            editor.get_line(1)
            editor.get_line(0)
            after_repeated_reads = scanner.call_count

            editor.insert("!", location=(1, 6))
            await pilot.pause()
            after_edit = scanner.call_count
            return after_change, after_repeated_reads, after_edit

    after_change, after_repeated_reads, after_edit = anyio.run(scenario)
    assert after_change >= 1
    assert after_repeated_reads == after_change
    assert after_edit == after_change + 1


def test_textual_app_only_publishes_latest_project_snapshot_to_editor() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            picker = app.query_one("#file-suggest", FileSuggest)
            app.set_command_catalog(
                TuiCommandCatalog(
                    (
                        CommandDescriptor(
                            name="highlight-demo",
                            title="Highlight demo",
                            description="Exercise application catalog wiring",
                        ),
                    )
                )
            )
            editor.value = "/highlight-demo @new.py @stale.py"

            app._file_index_generation = 2  # noqa: SLF001 - lifecycle test seam
            current = FileIndexRequest(generation=2, cwd="/work")
            stale = FileIndexRequest(generation=1, cwd="/work")
            app._file_index_request = current  # noqa: SLF001
            app._install_file_suggestions(  # noqa: SLF001 - simulated worker callback
                current, picker, _snapshot("new.py")
            )
            app._install_file_suggestions(  # noqa: SLF001 - stale worker callback
                stale, picker, _snapshot("stale.py")
            )
            await pilot.pause()
            return (
                _style_for_span(editor, 0, len("/highlight-demo"))
                == editor.get_component_rich_style("prompt-editor--command", partial=True),
                _style_for_span(editor, 16, 23)
                == editor.get_component_rich_style("prompt-editor--resolved-path", partial=True),
                _style_for_span(editor, 24, 33)
                == editor.get_component_rich_style("prompt-editor--unresolved-path", partial=True),
            )

    assert anyio.run(scenario) == (True, True, True)


def test_semantic_styles_rederive_across_dark_and_light_themes() -> None:
    async def scenario() -> tuple[str, str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.set_project_snapshot(_snapshot("src/app.py"))
            editor.value = "Read @src/app.py"
            await pilot.pause()
            dark = _style_for_span(editor, 5, 16)

            app.theme = "wisp-light"
            await pilot.pause()
            light = _style_for_span(editor, 5, 16)
            assert dark is not None and dark.color is not None
            assert light is not None and light.color is not None
            return (
                dark.color.get_truecolor().hex,
                light.color.get_truecolor().hex,
                bool(light.underline),
            )

    dark, light, underline = anyio.run(scenario)
    assert dark != light
    assert underline is True


def test_prompt_editor_applies_markdown_styles_without_changing_source_or_selection() -> None:
    source = "## Fix authentication\n- Inspect `TokenStore`\n```python\nreturn '✓'\n```"

    async def scenario() -> tuple[str, object, bool, bool, bool, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(32, 16)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = source
            editor.selection = type(editor.selection)((1, 2), (1, 9))
            await pilot.pause()

            heading = _style_for_span(editor, 0, 21, line_index=0)
            marker = _style_for_span(editor, 0, 1, line_index=1)
            inline = _style_for_span(editor, 11, 21, line_index=1)
            delimiter = _style_for_span(editor, 0, 3, line_index=2)
            body = _style_for_span(editor, 0, 10, line_index=3)
            return (
                editor.text_for_submission(),
                editor.selection,
                heading
                == editor.get_component_rich_style("prompt-editor--markdown-heading", partial=True),
                marker
                == editor.get_component_rich_style(
                    "prompt-editor--markdown-list-marker", partial=True
                ),
                inline
                == editor.get_component_rich_style(
                    "prompt-editor--markdown-inline-code", partial=True
                ),
                delimiter
                == editor.get_component_rich_style(
                    "prompt-editor--markdown-fence-delimiter", partial=True
                ),
                body
                == editor.get_component_rich_style(
                    "prompt-editor--markdown-fence-body", partial=True
                ),
            )

    submitted, selection, *styled = anyio.run(scenario)
    assert submitted == source
    assert selection == type(selection)((1, 2), (1, 9))
    assert all(styled)


def test_prompt_editor_recomputes_fence_state_after_edit_undo_and_redo() -> None:
    async def scenario() -> tuple[bool, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "```py\nbody\n# heading"
            await pilot.pause()
            initially_body = _style_for_span(editor, 0, 9, line_index=2)

            editor.insert("\n```", location=(1, 4))
            await pilot.pause()
            after_close = _style_for_span(editor, 0, 9, line_index=3)

            editor.undo()
            await pilot.pause()
            after_undo = _style_for_span(editor, 0, 9, line_index=2)

            editor.redo()
            await pilot.pause()
            after_redo = _style_for_span(editor, 0, 9, line_index=3)
            body_style = editor.get_component_rich_style(
                "prompt-editor--markdown-fence-body", partial=True
            )
            heading_style = editor.get_component_rich_style(
                "prompt-editor--markdown-heading", partial=True
            )
            return (
                initially_body == body_style and after_undo == body_style,
                after_close == heading_style,
                after_redo == heading_style,
            )

    assert anyio.run(scenario) == (True, True, True)


def test_markdown_styles_rederive_across_dark_and_light_themes() -> None:
    async def scenario() -> tuple[str, str, bool]:
        app = TextualTui()
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = "## Heading"
            await pilot.pause()
            dark = _style_for_span(editor, 0, 10)

            app.theme = "wisp-light"
            await pilot.pause()
            light = _style_for_span(editor, 0, 10)
            assert dark is not None and dark.color is not None
            assert light is not None and light.color is not None
            return (
                dark.color.get_truecolor().hex,
                light.color.get_truecolor().hex,
                bool(light.bold),
            )

    dark, light, bold = anyio.run(scenario)
    assert dark != light
    assert bold is True


def test_markdown_highlighting_keeps_literal_cues_with_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    source = "# Heading\n- `code`\n```python\nvalue\n```"

    async def scenario() -> tuple[bool, str, bool, bool]:
        app = TextualTui()
        async with app.run_test(size=(32, 14)) as pilot:
            editor = app.query_one("#input", PromptEditor)
            editor.value = source
            await pilot.pause()
            heading = _style_for_span(editor, 0, 9, line_index=0)
            fence = _style_for_span(editor, 0, 3, line_index=2)
            rendered_source = "\n".join(
                editor.get_line(index).plain for index in range(editor.document.line_count)
            )
            return (
                app.no_color,
                rendered_source,
                bool(heading and heading.bold),
                bool(fence and fence.bold),
            )

    assert anyio.run(scenario) == (True, source, True, True)
