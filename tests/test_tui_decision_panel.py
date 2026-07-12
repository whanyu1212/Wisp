from __future__ import annotations

from pathlib import Path
from typing import Literal

import anyio
import pytest
from textual.widgets import OptionList, Static

from wisp.events import ToolApprovalRequested, TrustRequested
from wisp.tui import TuiViewSnapshot
from wisp.tui.decision_content import (
    _approval_content,
    _bounded_decision_preview,
    _trust_content,
)
from wisp.tui.textual_app import create_textual_tui
from wisp.tui.widgets import (
    DecisionPanel,
)
from wisp.tui.widgets import (
    PromptEditor as Input,
)


def _approval(
    name: str,
    arguments: dict[str, object],
    *,
    safety: Literal["read", "mutating", "command"] = "mutating",
) -> ToolApprovalRequested:
    return ToolApprovalRequested(
        call_id="call-1",
        name=name,
        arguments=arguments,
        safety=safety,
    )


def _static_plain(widget: Static) -> str:
    return str(widget.render())


def test_bounded_decision_preview_marks_line_and_character_truncation() -> None:
    assert _bounded_decision_preview(["one", "two", "three"], max_lines=2) == (
        "one\n... preview truncated"
    )
    assert _bounded_decision_preview(["abcdefgh"], max_chars=4) == ("abcd\n... preview truncated")


def test_bash_approval_content_summarizes_command_context() -> None:
    content = _approval_content(
        _approval(
            "bash",
            {"command": "uv run pytest\necho done", "timeout": 30},
            safety="command",
        ),
        cwd="/work/project",
    )

    assert content.title == "Run command?"
    assert content.meta == "bash - command execution\ncwd: /work/project"
    assert content.detail == "$ uv run pytest\n  echo done\ntimeout: 30s"


def test_write_and_edit_approval_content_are_bounded_and_structured() -> None:
    write = _approval_content(
        _approval("write", {"path": "notes.txt", "content": "a\nb\nc\nd\ne\nf"}),
        cwd="/work/project",
    )
    edit = _approval_content(
        _approval(
            "edit",
            {
                "path": "app.py",
                "edits": [
                    {"oldText": "old one", "newText": "new one"},
                    {"oldText": "old two", "newText": "new two"},
                    {"oldText": "old three", "newText": "new three"},
                ],
            },
        ),
        cwd="/work/project",
    )

    assert write.title == "Write file?"
    assert write.meta == "notes.txt\nfile mutation - cwd: /work/project"
    assert write.detail.startswith("content: 6 lines, 11 bytes\na\nb")
    assert write.detail.endswith("... preview truncated")
    assert edit.title == "Edit file?"
    assert edit.meta == "app.py\nfile mutation - cwd: /work/project"
    assert "replacements: 3" in edit.detail
    assert "- old one\n+ new one" in edit.detail
    assert "- old two" in edit.detail
    assert edit.detail.endswith("... preview truncated")


def test_custom_approval_content_uses_sorted_json_instead_of_repr() -> None:
    content = _approval_content(
        _approval("deploy", {"z": "last", "a": True}),
        cwd="/work/project",
    )

    assert content.title == "Allow deploy?"
    assert content.detail.index('"a"') < content.detail.index('"z"')
    assert '"a": true' in content.detail
    assert "{'a': True}" not in content.detail


def test_trust_content_explains_scope_without_implying_persistence() -> None:
    content = _trust_content(
        TrustRequested(request_id="trust-1", project_path=Path("/work/project"))
    )

    assert content.title == "Trust this project?"
    assert content.meta == "/work/project"
    assert content.detail == ("Trusting allows project-local settings and instructions to load.")


def test_approval_panel_defaults_to_deny_and_preserves_composer_draft() -> None:
    async def scenario() -> tuple[str, str, bool, bool, bool, int | None, str]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "draft follow-up"
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(
                _approval("bash", {"command": "rm output.txt"}, safety="command")
            )
            await pilot.pause()

            panel = app.query_one("#decision-panel", DecisionPanel)
            options = app.query_one("#decision-options", OptionList)
            title = _static_plain(app.query_one("#decision-title", Static))
            detail = _static_plain(app.query_one("#decision-detail", Static))
            visible = panel.is_open and not input_widget.display
            focused = app.focused is options
            highlighted = options.highlighted

            await pilot.press("y")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            renderer.view_updated(TuiViewSnapshot(status="idle", input_hint="wisp> "))
            await pilot.pause()
            return (
                answer,
                input_widget.value,
                visible,
                focused,
                input_widget.display and app.focused is input_widget,
                highlighted,
                f"{title}\n{detail}",
            )

    answer, draft, visible, focused, restored, highlighted, rendered = anyio.run(scenario)
    assert answer == "y"
    assert draft == "draft follow-up"
    assert visible
    assert focused
    assert restored
    assert highlighted == 3
    assert "Run command?" in rendered
    assert "$ rm output.txt" in rendered


def test_approval_panel_exposes_once_tool_session_and_yolo_choices() -> None:
    async def scenario() -> tuple[list[str], int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(_approval("bash", {"command": "echo hi"}, safety="command"))
            await pilot.pause()
            options = app.query_one("#decision-options", OptionList)
            return (
                [str(options.get_option_at_index(index).prompt) for index in range(4)],
                options.highlighted,
            )

    options, highlighted = anyio.run(scenario)
    assert options == [
        "Y  Approve once",
        "T  Allow bash for this session",
        "A  YOLO: allow all tools for this session",
        "N  Deny (default)",
    ]
    assert highlighted == 3


@pytest.mark.parametrize("cancel_key", ["enter", "n", "escape"])
def test_approval_panel_yolo_confirmation_defaults_back(cancel_key: str) -> None:
    async def scenario() -> tuple[str, str, int | None, str]:
        app, renderer = create_textual_tui()
        approval = _approval("bash", {"command": "echo hi"}, safety="command")
        async with app.run_test(size=(80, 24)) as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(approval)
            await pilot.pause()
            await pilot.press("a")
            with anyio.fail_after(1):
                first = await app._prompt_receive.receive()
            assert isinstance(first, str)

            renderer.approval_all_confirmation(approval)
            await pilot.pause()
            options = app.query_one("#decision-options", OptionList)
            highlighted = options.highlighted
            title = _static_plain(app.query_one("#decision-title", Static))
            await pilot.press(cancel_key)
            with anyio.fail_after(1):
                second = await app._prompt_receive.receive()
            assert isinstance(second, str)
            return first, second, highlighted, title

    first, second, highlighted, title = anyio.run(scenario)
    assert first == "a"
    assert second == "cancel-all"
    assert highlighted == 1
    assert title == "Enable YOLO for this TUI run?"


def test_approval_panel_yolo_confirmation_hides_composer_and_preserves_draft() -> None:
    async def scenario() -> tuple[bool, str, bool]:
        app, renderer = create_textual_tui()
        approval = _approval("bash", {"command": "echo hi"}, safety="command")
        async with app.run_test(size=(80, 24)) as pilot:
            input_widget = app.query_one("#input", Input)
            input_widget.value = "draft follow-up"
            renderer.approval_request(approval)
            await pilot.pause()
            renderer.approval_all_confirmation(approval)
            await pilot.pause()
            hidden = not input_widget.display
            draft = input_widget.value
            renderer.view_updated(TuiViewSnapshot(status="idle", input_hint="wisp> "))
            await pilot.pause()
            restored = input_widget.display and app.focused is input_widget
            return hidden, draft, restored

    hidden, draft, restored = anyio.run(scenario)
    assert hidden
    assert draft == "draft follow-up"
    assert restored


@pytest.mark.parametrize(("key", "expected"), [("enter", "n"), ("n", "n"), ("escape", "n")])
def test_approval_panel_deny_paths_are_fail_closed(key: str, expected: str) -> None:
    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(
                _approval("write", {"path": "file.txt", "content": "content"})
            )
            await pilot.pause()
            await pilot.press(key)
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return answer

    assert anyio.run(scenario) == expected


def test_trust_panel_uses_deny_first_project_wording() -> None:
    async def scenario() -> tuple[str, str, str, int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for trust",
                    input_hint="trust> ",
                    input_mode="trust",
                )
            )
            renderer.trust_request(
                TrustRequested(request_id="trust-1", project_path=Path("/work/project"))
            )
            await pilot.pause()
            options = app.query_one("#decision-options", OptionList)
            return (
                _static_plain(app.query_one("#decision-title", Static)),
                str(options.get_option_at_index(0).prompt),
                str(options.get_option_at_index(1).prompt),
                options.highlighted,
            )

    title, approve, deny, highlighted = anyio.run(scenario)
    assert title == "Trust this project?"
    assert "Trust project" in approve
    assert "Keep untrusted (default)" in deny
    assert highlighted == 1


@pytest.mark.parametrize("theme", ["wisp", "wisp-light"])
def test_decision_panel_fits_above_footer_in_narrow_terminal(theme: str) -> None:
    async def scenario() -> tuple[int, int, int, int]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(72, 20)) as pilot:
            app.theme = theme
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(
                _approval(
                    "write",
                    {
                        "path": "a/long/path/that/must/still/fit/settings.json",
                        "content": "\n".join(f"line {index}" for index in range(20)),
                    },
                )
            )
            await pilot.pause()
            panel = app.query_one("#decision-panel", DecisionPanel)
            footer = app.query_one("#status", Static)
            transcript = app.query_one("#transcript")
            return (
                panel.region.bottom,
                footer.region.y,
                panel.region.height,
                transcript.region.height,
            )

    panel_bottom, footer_top, panel_height, transcript_height = anyio.run(scenario)
    assert panel_bottom <= footer_top
    assert panel_height <= 12
    assert transcript_height > 0
