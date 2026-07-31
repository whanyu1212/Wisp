from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal
from unittest import mock

import anyio
import pytest
from textual import events
from textual.app import App
from textual.widgets import OptionList, Static

from wisp.events import ToolApprovalRequested, TrustRequested
from wisp.tui import TuiViewSnapshot
from wisp.tui.decision_content import (
    _approval_content,
    _bounded_decision_preview,
    _bounded_tool_session_option_name,
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


def test_bounded_tool_session_option_name_truncates_long_names() -> None:
    assert _bounded_tool_session_option_name("bash") == "bash"
    long_name = "a" * 71
    truncated = _bounded_tool_session_option_name(long_name)
    assert len(truncated) == 40
    assert truncated.endswith("…")
    assert truncated[:-1] == long_name[:39]


def test_bounded_tool_session_option_name_collapses_embedded_newlines() -> None:
    # A character-count cap alone doesn't stop an embedded newline from still
    # splitting the option onto a second rendered line — collapse first.
    assert _bounded_tool_session_option_name("bash\ntool") == "bash tool"
    assert _bounded_tool_session_option_name("a\r\nb\rc\nd") == "a b c d"
    assert "\n" not in _bounded_tool_session_option_name("x\n" * 30)
    assert "\r" not in _bounded_tool_session_option_name("x\r" * 30)


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


def test_bash_approval_content_summarizes_resumable_operations() -> None:
    start = _approval_content(
        _approval(
            "bash",
            {
                "operation": "start",
                "command": "uv run pytest",
                "lifetime_seconds": 300,
                "yield_seconds": 1,
            },
            safety="command",
        ),
        cwd="/work/project",
    )
    poll = _approval_content(
        _approval(
            "bash",
            {"operation": "poll", "process_id": "proc-1", "wait_seconds": 2},
            safety="command",
        ),
        cwd="/work/project",
    )
    cancel = _approval_content(
        _approval(
            "bash",
            {"operation": "cancel", "process_id": "proc-1"},
            safety="command",
        ),
        cwd="/work/project",
    )

    assert start.title == "Start command?"
    assert start.detail == "$ uv run pytest\nlifetime_seconds: 300s\nyield_seconds: 1s"
    assert poll.title == "Poll process?"
    assert poll.detail == "operation: poll\nprocess_id: proc-1\nwait_seconds: 2s"
    assert cancel.title == "Cancel process?"
    assert cancel.detail == "operation: cancel\nprocess_id: proc-1"


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


def test_approval_panel_defaults_to_approve_once_and_preserves_composer_draft() -> None:
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

            await pilot.press("1")
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
    assert highlighted == 0
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
        "1  Approve once (default)",
        "2  Allow bash for this session",
        "3  YOLO: allow all tools for this session",
        "4  Deny",
    ]
    assert highlighted == 0


@pytest.mark.parametrize("cancel_key", ["enter", "2", "escape"])
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
            await pilot.press("3")
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


@pytest.mark.parametrize(("key", "expected"), [("4", "n"), ("escape", "n")])
def test_approval_panel_explicit_deny_paths_are_fail_closed(key: str, expected: str) -> None:
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


def test_approval_panel_enter_follows_approve_once_default() -> None:
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
            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return answer

    assert anyio.run(scenario) == "y"


def test_approval_panel_drops_key_queued_before_panel_opened() -> None:
    # Widget-level defense in depth: even if a stale Key/OptionSelected message
    # reaches DecisionPanel directly (e.g. posted by code other than the normal
    # driver pipeline — see the app-level barrier test below for the real
    # end-to-end race), both the on_key digit/Escape path and the OptionList
    # Enter-selects-highlighted path must ignore events timestamped before the
    # panel opened rather than treat them as an intentional approval. A
    # genuinely fresh key afterward must still work — the guard must not wedge
    # the panel shut.
    async def scenario() -> tuple[bool, str]:
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

            panel = app.query_one("#decision-panel", DecisionPanel)
            options = app.query_one("#decision-options", OptionList)

            stale_key = events.Key("enter", None)
            stale_key.set_sender(app)
            stale_key.time = panel._opened_at - 1.0
            panel.on_key(stale_key)

            option = options.get_option_at_index(0)
            assert option.id is not None
            stale_selected = OptionList.OptionSelected(options, option, 0)
            stale_selected.time = panel._opened_at - 1.0
            panel.on_option_list_option_selected(stale_selected)

            await pilot.pause()
            rejected = not panel._submitted

            await pilot.press("4")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return rejected, answer

    rejected, answer = anyio.run(scenario)
    assert rejected
    assert answer == "n"


def test_app_on_event_drops_key_queued_before_decision_panel_opened() -> None:
    # The actual race (see test_approval_panel_drops_key_queued_before_panel_
    # opened's docstring): a key already read by the driver — e.g. a stray
    # Enter queued for the composer — can still be delivered after a decision
    # panel opens. OptionList's own native enter->select binding builds a
    # *fresh*-timestamped OptionSelected once it finally runs, so a
    # DecisionPanel-level guard comparing that message's .time can never see
    # it as stale; and the composer itself may still be focused (only hidden,
    # not blurred) in the same window, so a per-widget guard can't cover every
    # place the key might land either. The barrier has to sit earlier, before
    # Textual forwards the key to whatever currently has focus — App.on_event,
    # which every InputEvent passes through first (see TextualTui.on_event).
    # Reproduced here by calling on_event directly with a controlled timestamp
    # rather than racing Textual's real focus-change scheduling, which is
    # nondeterministic from a test (whether the deferred call_later that moves
    # focus to the OptionList has already run by the time a posted message is
    # processed varies with the event loop's own scheduling).
    async def scenario() -> tuple[bool, bool]:
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

            forwarded: list[events.Key] = []

            # Patch the exact call TextualTui.on_event delegates a non-stale
            # event to (App.on_event), so this asserts the barrier's own
            # control flow — whether it forwards the event at all — rather
            # than depending on how far downstream dispatch (itself
            # timing-sensitive, per this test's docstring) happens to go.
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


@pytest.mark.parametrize(
    "make_event",
    [
        lambda: events.MouseDown(None, 0, 0, 0, 0, 1, False, False, False),
        lambda: events.MouseUp(None, 0, 0, 0, 0, 1, False, False, False),
        lambda: events.MouseScrollDown(None, 0, 0, 0, 0, 1, False, False, False),
        lambda: events.MouseScrollUp(None, 0, 0, 0, 0, 1, False, False, False),
        lambda: events.Paste("y"),
    ],
    ids=["MouseDown", "MouseUp", "MouseScrollDown", "MouseScrollUp", "Paste"],
)
def test_app_on_event_drops_stale_mouse_and_paste_events_too(
    make_event: Callable[[], events.Event],
) -> None:
    # The barrier gates events.MouseEvent as a whole family, not each
    # subclass individually — this covers MouseScrollDown/Up too, which a
    # narrower per-subclass gate previously missed: a stale scroll event
    # could otherwise scroll the highlighted option out of the fixed-height
    # viewport (without changing which option is logically selected) right
    # before a legitimate Enter approves whatever is still highlighted, the
    # same class of bug this barrier exists to prevent via a different input
    # channel. Click is synthesized from an already-forwarded MouseUp inside
    # App.on_event's own body (see textual.app.App.on_event), not delivered
    # independently, so gating MouseUp transitively covers a stale Click too.
    # Paste is gated separately (not a MouseEvent or Key) because a stale
    # paste could otherwise still reach a focused-but-hidden PromptEditor or
    # an OptionList, same as a stale Key.
    async def scenario() -> tuple[bool, bool]:
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

            forwarded: list[events.Event] = []

            async def recording_app_on_event(_self: object, event: events.Event) -> None:
                forwarded.append(event)

            with mock.patch.object(App, "on_event", recording_app_on_event):
                stale_event = make_event()
                stale_event.set_sender(app)
                assert app._overlay_controller is not None
                barrier = app._overlay_controller.stale_event_barrier
                stale_event.time = barrier - 1.0
                await app.on_event(stale_event)

                fresh_event = make_event()
                fresh_event.set_sender(app)
                fresh_event.time = barrier + 1.0
                await app.on_event(fresh_event)

            return stale_event not in forwarded, fresh_event in forwarded

    stale_rejected, fresh_forwarded = anyio.run(scenario)
    assert stale_rejected
    assert fresh_forwarded


def test_trust_panel_stale_home_does_not_move_deny_first_highlight() -> None:
    # The same overlay-controller barrier protects Home/PageUp/PageDown/End too, not just
    # Enter/digits: those are app-level priority bindings (see BINDINGS),
    # dispatched via App._check_bindings before DecisionPanel.on_key ever sees
    # them, so a per-widget guard couldn't catch a stale one regardless. The
    # trust and YOLO-confirmation panels are deny-first (highlighted defaults
    # to the safe option) — a stale Home/PageUp must not flip that highlight
    # to the affirmative option before a legitimate Enter lands.
    async def scenario() -> tuple[int | None, int | None]:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for trust", input_hint="trust> ", input_mode="trust"
                )
            )
            renderer.trust_request(
                TrustRequested(request_id="trust-1", project_path=Path("/work/project"))
            )
            await pilot.pause()
            options = app.query_one("#decision-options", OptionList)
            initial_highlighted = options.highlighted

            stale_key = events.Key("home", None)
            stale_key.set_sender(app)
            assert app._overlay_controller is not None
            stale_key.time = app._overlay_controller.stale_event_barrier - 1.0
            await app.on_event(stale_key)
            await pilot.pause()
            after_stale = options.highlighted

            return initial_highlighted, after_stale

    initial_highlighted, after_stale = anyio.run(scenario)
    assert initial_highlighted == 1
    assert after_stale == 1


def test_approval_panel_end_key_moves_highlight_not_transcript() -> None:
    # `end`/`pagedown` are priority app bindings (needed so they reach the
    # transcript through a focused TextArea composer) that would otherwise
    # always intercept the key before OptionList's own End/PageDown handling
    # while the decision panel is focused — scrolling the transcript instead
    # of moving the highlight, stranding it on "Approve once" and turning a
    # follow-up Enter into an unintended approval. Assert End instead reaches
    # the panel and moves the highlight to the last option ("Deny").
    async def scenario() -> tuple[int | None, str]:
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
            assert options.highlighted == 0

            await pilot.press("end")
            await pilot.pause()
            highlighted = options.highlighted

            await pilot.press("enter")
            with anyio.fail_after(1):
                answer = await app._prompt_receive.receive()
            assert isinstance(answer, str)
            return highlighted, answer

    highlighted, answer = anyio.run(scenario)
    assert highlighted == 3
    assert answer == "n"


def test_textual_help_describes_approve_once_default_not_deny_default() -> None:
    # The shared _tui_help_text() default ("approve? [y/N]") is still accurate
    # for the line/fullscreen renderers' free-text approval prompt, but the
    # Textual decision panel now defaults its highlight to "Approve once" —
    # the help text seen through the Textual renderer must say so, not repeat
    # the now-backwards y/N (deny-on-Enter) convention.
    async def scenario() -> str:
        app, renderer = create_textual_tui()
        async with app.run_test() as pilot:
            renderer.help()
            await pilot.pause()
            lines = [str(widget.render()) for widget in app.query(Static)]
            return "\n".join(lines)

    rendered = anyio.run(scenario)
    assert "approve? [y/N]" not in rendered
    assert "1 (Approve once)" in rendered


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


def test_approval_panel_options_stay_unwrapped_with_long_tool_name() -> None:
    # A long tool name in the "Allow <name> for this session" option can wrap
    # the fixed-height #decision-options viewport, scrolling "4 Deny" out of
    # view with nothing to auto-scroll it back (the default highlight is no
    # longer the last option). At the project's supported narrow-terminal
    # width (see test_decision_panel_fits_above_footer_in_narrow_terminal),
    # the truncated label must keep the option list's virtual size equal to
    # its viewport size — i.e. no wrapping, all four options visible.
    async def scenario() -> tuple[int, int]:
        app, renderer = create_textual_tui()
        async with app.run_test(size=(72, 20)) as pilot:
            renderer.view_updated(
                TuiViewSnapshot(
                    status="waiting for approval",
                    input_hint="approve> ",
                    input_mode="approval",
                    cwd="/work/project",
                )
            )
            renderer.approval_request(_approval("a" * 71, {}, safety="command"))
            await pilot.pause()
            options = app.query_one("#decision-options", OptionList)
            return options.virtual_size.height, options.size.height

    virtual_height, viewport_height = anyio.run(scenario)
    assert virtual_height == viewport_height == 4
