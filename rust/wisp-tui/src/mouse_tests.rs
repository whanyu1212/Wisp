//! Real input routing against hit regions emitted by the actual renderers.

use super::*;
use crossterm::event::{MouseButton, MouseEventKind};
use ratatui::{backend::TestBackend, buffer::Buffer, layout::Rect, style::Modifier};
use serde_json::Value;
use wisp_protocol::events::{ProjectFileEntry, ProjectFileKind, ProjectFileSnapshot};

fn ui(draft: &str) -> LiveUi {
    let mut ui = LiveUi {
        mouse_enabled: true,
        ..LiveUi::default()
    };
    ui.editor.insert_paste(draft);
    ui
}

fn event(kind: MouseEventKind, x: u16, y: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    }
}

fn click(x: u16, y: u16) -> Input {
    Input::Mouse(event(MouseEventKind::Down(MouseButton::Left), x, y))
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    terminal.backend().buffer().clone()
}

fn row_point(ui: &LiveUi, index: usize) -> (u16, u16) {
    let frame = ui.mouse_frame.as_ref().unwrap();
    let area = frame.popup.unwrap();
    for y in area.y..area.bottom() {
        let x = area.x + 2;
        if frame
            .rows
            .hit(event(MouseEventKind::Down(MouseButton::Left), x, y))
            == Some(index)
        {
            return (x, y);
        }
    }
    panic!("row {index} is not fully painted");
}

fn row_text(buffer: &Buffer, y: u16) -> String {
    (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect()
}

fn command(receiver: &mut mpsc::Receiver<WriterMessage>) -> Value {
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("expected command");
    };
    serde_json::from_slice(&payload).unwrap()
}

#[test]
fn capture_defaults_on_with_an_explicit_opt_out_and_filters_reports() {
    assert!(mouse::enabled(None));
    for value in [
        Some(""),
        Some("0"),
        Some("false"),
        Some("off"),
        Some("yes"),
        Some("typo"),
    ] {
        assert!(!mouse::enabled(value), "{value:?}");
    }
    for value in ["1", "true", "on", " TRUE "] {
        assert!(mouse::enabled(Some(value)));
    }
    for kind in [
        MouseEventKind::Down(MouseButton::Left),
        MouseEventKind::Drag(MouseButton::Left),
        MouseEventKind::Up(MouseButton::Left),
        MouseEventKind::ScrollUp,
        MouseEventKind::ScrollDown,
    ] {
        assert!(mouse::supported(event(kind, 1, 1)));
    }
    for kind in [
        MouseEventKind::Moved,
        MouseEventKind::Drag(MouseButton::Right),
        MouseEventKind::Up(MouseButton::Right),
        MouseEventKind::Down(MouseButton::Right),
        MouseEventKind::ScrollLeft,
        MouseEventKind::ScrollRight,
    ] {
        assert!(!mouse::supported(event(kind, 1, 1)));
    }
    let mut shifted = event(MouseEventKind::Down(MouseButton::Left), 1, 1);
    shifted.modifiers = KeyModifiers::SHIFT;
    assert!(!mouse::supported(shifted));
    // A release with a key held still ends the selection its press started.
    let mut released = event(MouseEventKind::Up(MouseButton::Left), 1, 1);
    released.modifiers = KeyModifiers::CONTROL;
    assert!(mouse::supported(released));
}

#[test]
fn hit_rows_exclude_borders_missing_items_and_partial_multiline_rows() {
    let rows = mouse::Rows::new(Rect::new(2, 4, 20, 5), 10, 15, 2);
    for (y, expected) in [
        (3, None),
        (4, Some(10)),
        (5, Some(10)),
        (6, Some(11)),
        (7, Some(11)),
        (8, None),
        (9, None),
    ] {
        assert_eq!(
            rows.hit(event(MouseEventKind::Down(MouseButton::Left), 2, y)),
            expected
        );
    }
    assert!(
        rows.hit(event(MouseEventKind::Down(MouseButton::Left), 22, 4))
            .is_none()
    );
    let short = mouse::Rows::new(Rect::new(2, 4, 20, 5), 10, 11, 1);
    assert!(
        short
            .hit(event(MouseEventKind::Down(MouseButton::Left), 2, 5))
            .is_none()
    );
}

#[tokio::test]
async fn disabled_mouse_and_unpainted_or_resized_coordinates_are_inert() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("keep draft");
    let before = ui.editor.clone();
    ui.handle_input(click(1, 21), &writer, 8192).await.unwrap();
    assert_eq!(ui.editor, before, "not yet painted");
    ui.mouse_enabled = false;
    draw(&mut ui, 80, 24);
    ui.handle_input(click(1, 21), &writer, 8192).await.unwrap();
    assert_eq!(ui.editor, before);
    ui.mouse_enabled = true;
    draw(&mut ui, 80, 24);
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    ui.handle_input(click(1, 21), &writer, 8192).await.unwrap();
    assert_eq!(ui.editor, before);
    draw(&mut ui, 29, 7);
    ui.handle_input(click(1, 1), &writer, 8192).await.unwrap();
    assert_eq!(ui.editor, before);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn composer_clicks_respect_unicode_tabs_scroll_and_source_revisions() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("first\nab\t界e\u{301}👩‍💻end");
    draw(&mut ui, 80, 24);
    let area = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap()
        .area;
    for (column, prefix) in [
        (2, "ab"),
        (3, "ab"),
        (4, "ab\t"),
        (5, "ab\t"),
        (6, "ab\t界"),
        (7, "ab\t界e\u{301}"),
        (8, "ab\t界e\u{301}"),
    ] {
        draw(&mut ui, 80, 24);
        ui.handle_input(click(area.x + column, area.y + 1), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.editor.cursor_offset(), "first\n".len() + prefix.len());
    }
    let before = draw(&mut ui, 80, 24);
    assert!(row_text(&before, area.y).contains("first"));
    ui.editor.restore_prompt("fresh\nreplacement");
    let restored = ui.editor.clone();
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.editor, restored,
        "old byte mapping must not target replacement text"
    );

    ui.editor.restore_prompt(&format!(
        "{}\n{}界end",
        "line\n".repeat(10),
        "x".repeat(100)
    ));
    draw(&mut ui, 30, 8);
    let mapping = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap();
    let area = mapping.area;
    let line = mapping.rows[0].logical_row;
    let column = mapping.rows[0].column_start;
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.cursor_row(), line);
    assert_eq!(ui.editor.cursor_column(), column);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn clicking_a_soft_wrapped_row_maps_back_to_the_logical_line() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&"x".repeat(100));
    draw(&mut ui, 80, 24);
    let mapping = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap();
    let area = mapping.area;
    let second = mapping.rows[1];

    ui.handle_input(click(area.x + 2, area.y + 1), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.cursor_row(), 0);
    assert_eq!(ui.editor.cursor_column(), second.column_start + 2);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn clicking_blank_cells_after_a_word_wrap_stays_at_that_rows_source_end() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui =
        ui("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron");
    draw(&mut ui, 30, 12);
    let mapping = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap();
    let area = mapping.area;
    let (visual_row, row) = mapping
        .rows
        .windows(2)
        .enumerate()
        .find(|(_, rows)| {
            rows[0].logical_row == rows[1].logical_row
                && rows[0].column_end.saturating_sub(rows[0].column_start) < usize::from(area.width)
        })
        .map(|(index, rows)| (index, rows[0]))
        .expect("word wrapping should leave a shortened non-final row");

    ui.handle_input(
        click(
            area.right() - 1,
            area.y + u16::try_from(visual_row).unwrap(),
        ),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.cursor_row(), row.logical_row);
    assert_eq!(ui.editor.cursor_column(), row.column_end);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn clicking_the_current_position_resets_vertical_column_intent() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("abcdefghijklmnop\nxy");
    for code in [KeyCode::Up, KeyCode::End, KeyCode::Down] {
        ui.handle_input(key(code), &writer, 8192).await.unwrap();
    }
    assert_eq!(ui.editor.cursor_column(), 2);
    draw(&mut ui, 80, 24);
    let area = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap()
        .area;
    ui.handle_input(click(area.x + 2, area.y + 1), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Up), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.editor.cursor_column(),
        2,
        "click overrides the previous long-line column"
    );
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn failed_cancellation_warning_is_not_replaced_by_mouse_history_requests() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.view_status = ViewStatus::Running;
    ui.state.current_command = Some(reducer::ActiveCommand {
        id: "prompt-1".into(),
        command_type: reducer::ActiveCommandType::Prompt,
    });
    ui.state.cancel_requested = true;
    ui.state.history.oldest_cursor = Some("c".repeat(100));
    ui.state.transcript.append_prompt("retained".into());
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
            call_id: "a".repeat(400),
            name: "bash".into(),
            arguments: serde_json::json!({}),
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        })),
        &writer,
        128,
    )
    .await
    .unwrap();
    assert!(ui.unsendable_current_response());
    let notice = ui.notice.clone();
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, 2, 5)),
        &writer,
        128,
    )
    .await
    .unwrap();
    assert_eq!(ui.notice, notice);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn coalesced_wheel_run_moves_the_transcript_atomically() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"long transcript line\n".repeat(120));
    draw(&mut ui, 80, 24);
    let before = ui
        .transcript_viewport
        .visible_rows(&ui.state.transcript, &mut ui.transcript_row_cache);
    let generation = ui.transcript_viewport.reader_generation();
    let editor = ui.editor.clone();
    let area = ui.mouse_frame.as_ref().unwrap().conversation.transcript;

    ui.handle_input(
        Input::Scroll(mouse::Scroll {
            event: event(MouseEventKind::ScrollUp, area.x + 2, area.y + 2),
            lines: -3,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();

    let after = ui
        .transcript_viewport
        .visible_rows(&ui.state.transcript, &mut ui.transcript_row_cache);
    assert_eq!(after[3..], before[..before.len() - 3]);
    assert_eq!(
        ui.transcript_viewport.reader_generation(),
        generation.wrapping_add(1),
        "one raw wheel run is one viewport action"
    );
    assert_eq!(ui.editor, editor);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn wheel_preserves_editor_focus_and_reading_position_during_streaming() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft 👩‍💻");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"long transcript line\n".repeat(120));
    draw(&mut ui, 80, 24);
    let editor = ui.editor.clone();
    let area = ui.mouse_frame.as_ref().unwrap().conversation.transcript;
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, area.x + 2, area.y + 2)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(!ui.transcript_viewport.follows_tail());
    let before = ui.transcript_viewport.clone();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageDelta {
            turn: 1,
            delta: "new output\n".into(),
            content_kind: reducer::MessageContentKind::Text,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(!ui.transcript_viewport.follows_tail());
    assert!(ui.transcript_viewport.has_unseen_output());
    assert_eq!(ui.editor, editor);
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollDown, area.x + 2, area.y + 2)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_ne!(ui.transcript_viewport, before);
    assert_eq!(ui.editor, editor);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn scrolling_near_the_start_repaints_when_no_older_history_remains() {
    for navigation in 0..4 {
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui("draft");
        ui.state.transcript.complete_message(
            1,
            (0..60)
                .map(|line| format!("line {line}"))
                .collect::<Vec<_>>()
                .join("\n"),
        );
        ui.state.transcript.mark_history_entries(0, "oldest-entry");
        draw(&mut ui, 80, 24);
        for action in [
            TranscriptViewAction::Home,
            TranscriptViewAction::ScrollLines(5),
        ] {
            ui.transcript_viewport.reduce(
                action,
                &ui.state.transcript,
                &mut ui.transcript_row_cache,
            );
        }
        let before = draw(&mut ui, 80, 24);
        let viewport = ui.transcript_viewport.clone();
        let editor = ui.editor.clone();
        let area = ui.mouse_frame.as_ref().unwrap().conversation.transcript;
        let input = match navigation {
            0 => Input::Mouse(event(MouseEventKind::ScrollUp, area.x + 2, area.y + 2)),
            1 => Input::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::CONTROL)),
            2 => key(KeyCode::PageUp),
            _ => Input::Key(KeyEvent::new(KeyCode::Home, KeyModifiers::CONTROL)),
        };
        assert!(!ui.render_pending);
        assert!(ui.state.history.oldest_cursor.is_none());

        ui.handle_input(input, &writer, 8192).await.unwrap();

        assert_ne!(ui.transcript_viewport, viewport, "navigation {navigation}");
        assert!(
            ui.render_pending,
            "navigation {navigation} moved without scheduling a repaint"
        );
        assert_ne!(draw(&mut ui, 80, 24), before);
        assert_eq!(ui.editor, editor);
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn first_wheel_scroll_uses_the_last_rendered_tail_after_unseen_output() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"long transcript line\n".repeat(120));
    draw(&mut ui, 80, 24);
    let before = ui
        .transcript_viewport
        .visible_rows(&ui.state.transcript, &mut ui.transcript_row_cache);
    ui.state
        .transcript
        .append_message_delta(1, "unseen output\n");

    let area = ui.mouse_frame.as_ref().unwrap().conversation.transcript;
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, area.x + 2, area.y + 2)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let after = ui
        .transcript_viewport
        .visible_rows(&ui.state.transcript, &mut ui.transcript_row_cache);

    assert!(!ui.transcript_viewport.follows_tail());
    assert_ne!(after[0].anchor, before[0].anchor);
    assert_eq!(after[1..], before[..before.len() - 1]);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn line_scroll_loads_each_history_direction_once_at_the_window_edge() {
    for older in [true, false] {
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui("draft");
        let session = reducer::SessionIdentity {
            session_id: "session".into(),
            session_path: "/session.jsonl".into(),
            session_name: None,
        };
        ui.state.selected_session = Some(session.clone());
        ui.state.history.session = Some(session);
        ui.state.history.oldest_cursor = older.then(|| "oldest".into());
        ui.state.history.newest_cursor = (!older).then(|| "newest".into());
        ui.state.history.tail_evicted = !older;
        ui.state.transcript.append_prompt("one retained row".into());
        draw(&mut ui, 80, 24);
        let area = ui.mouse_frame.as_ref().unwrap().conversation.transcript;
        let kind = if older {
            MouseEventKind::ScrollUp
        } else {
            MouseEventKind::ScrollDown
        };
        for _ in 0..2 {
            ui.handle_input(
                Input::Mouse(event(kind, area.x + 2, area.y + 2)),
                &writer,
                8192,
            )
            .await
            .unwrap();
        }
        let request = command(&mut receiver);
        assert_eq!(request["type"], "get_messages");
        if older {
            assert_eq!(request["before_entry_id"], "oldest");
        } else {
            // With live entries, the existing history policy reloads the latest
            // page instead of trying to join an old cursor to the live suffix.
            assert!(request["after_entry_id"].is_null());
            assert!(request["before_entry_id"].is_null());
        }
        assert!(
            receiver.try_recv().is_err(),
            "one in-flight history request"
        );
    }
}

#[tokio::test]
async fn popup_selection_is_not_activation_and_outside_click_never_passes_through() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("keep draft");
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    assert!(row_text(&buffer, y).contains("Glass"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme_picker.as_ref().unwrap().preview().slug, "glass");
    assert_eq!(ui.theme.active.slug, "vapor");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.theme.active.slug, "vapor",
        "new selection must be painted first"
    );
    draw(&mut ui, 80, 24);
    let editor = ui.editor.clone();
    let area = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap()
        .area;
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.theme_picker.is_none());
    assert_eq!(ui.theme.active.slug, "vapor");
    assert_eq!(ui.editor, editor);
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.editor, editor,
        "second click must wait for uncovered frame"
    );
    draw(&mut ui, 80, 24);
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.cursor_offset(), 0);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn wheel_targets_only_the_active_popup_and_can_repeat_between_frames() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"background\n".repeat(80));
    ui.command_help = Some(Help::default());
    draw(&mut ui, 80, 24);
    let viewport = ui.transcript_viewport.clone();
    let popup = ui.mouse_frame.as_ref().unwrap().popup.unwrap();
    for _ in 0..2 {
        ui.handle_input(
            Input::Mouse(event(MouseEventKind::ScrollDown, popup.x + 2, popup.y + 2)),
            &writer,
            8192,
        )
        .await
        .unwrap();
    }
    assert_eq!(ui.command_help.as_ref().unwrap().offset, 2);
    ui.handle_input(
        Input::Scroll(mouse::Scroll {
            event: event(MouseEventKind::ScrollDown, popup.x + 2, popup.y + 2),
            lines: 3,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.command_help.as_ref().unwrap().offset, 5);
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, 1, 4)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.command_help.as_ref().unwrap().offset, 5);
    assert_eq!(ui.transcript_viewport, viewport);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn history_and_skill_clicks_select_without_modifying_or_submitting_the_draft() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("request");
    ui.prompt_history.record("older prompt".into());
    ui.prompt_history.record("newer prompt".into());
    ui.open_prompt_history();
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    assert!(row_text(&buffer, y).contains("older prompt"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert_eq!(ui.editor.text(), "request");
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "older prompt");
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/rust_tui_discovery.json"
    ))
    .unwrap();
    ui.state.skills.snapshot = Some(Arc::new(
        serde_json::from_value(fixture["rpc.skills"]["catalog"].clone()).unwrap(),
    ));
    ui.discovery_view = Some(DiscoveryView::skills(ui.state.skills.snapshot.as_deref()));
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    assert!(row_text(&buffer, y).contains("/skill:build"));
    ui.handle_input(click(x, y + 1), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "older prompt");
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/skill:build older prompt");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn model_rows_are_selected_not_applied_and_refresh_invalidates_old_targets() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    let mut picker = ModelPicker::loading();
    picker.update_catalog(model_picker::tests::catalog());
    ui.model_picker = Some(picker);
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 2);
    assert!(row_text(&buffer, y).contains("two"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert!(receiver.try_recv().is_err());
    let action = ui
        .model_picker
        .as_mut()
        .unwrap()
        .handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE), false);
    assert!(
        matches!(action, ModelPickerAction::Apply(config) if config.model.as_deref() == Some("two"))
    );
    draw(&mut ui, 80, 24);
    ui.apply_effects(vec![UiEffect::InvalidateModelCatalog], &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert_eq!(ui.rendered_overlay, None);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn session_and_connection_clicks_only_select_visible_rows() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.session_picker = Some(SessionPicker::new(
        (0..3)
            .map(|index| reducer::SessionSummary {
                session_id: format!("session-{index}"),
                session_path: format!("/session-{index}.jsonl"),
                name: Some(format!("Session {index}")),
                updated_at: "now".into(),
                entry_count: 1,
            })
            .collect(),
        None,
    ));
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    assert!(row_text(&buffer, y).contains("Session 1"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert!(receiver.try_recv().is_err());
    assert_eq!(
        ui.session_picker
            .as_mut()
            .unwrap()
            .handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        SessionPickerAction::None,
    );
    draw(&mut ui, 80, 24);
    assert_eq!(
        ui.session_picker
            .as_mut()
            .unwrap()
            .handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        SessionPickerAction::Selected("session-1".into())
    );
    ui.session_picker = None;
    let catalog = wisp_protocol::events::ConnectionCatalogSnapshot {
        providers: ["alpha", "beta"]
            .into_iter()
            .map(
                |provider| wisp_protocol::events::ConnectionProviderSnapshot {
                    id: provider.into(),
                    label: provider.into(),
                    methods: vec![wisp_protocol::events::ConnectionMethodSnapshot {
                        provider: provider.into(),
                        label: "API key".into(),
                        kind: "api_key".into(),
                        source: "none".into(),
                        environment_variable: None,
                        oauth_expires_at: None,
                        has_stored_credential: false,
                    }],
                },
            )
            .collect(),
    };
    ui.connection_panel = Some(ConnectionPanel::new(catalog));
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    assert!(row_text(&buffer, y).contains("beta"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert_eq!(
        ui.connection_panel
            .as_mut()
            .unwrap()
            .handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        ConnectionPanelAction::EnterApiKey {
            provider: "beta".into()
        }
    );
    assert_eq!(ui.editor.text(), "draft");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn wheel_crosses_session_tree_page_boundary_without_jumping_ordinary_rows() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.selected_session = Some(reducer::SessionIdentity {
        session_id: "session".into(),
        session_path: "/session.jsonl".into(),
        session_name: None,
    });
    ui.session_tree_picker = Some(SessionTreePicker::new(reducer::SessionTreePage {
        session: ui.state.selected_session.clone(),
        active_leaf_id: None,
        total_node_count: 3,
        nodes: (0..2)
            .map(|index| reducer::SessionTreeNode {
                entry_id: format!("entry-{index}"),
                parent_id: None,
                created_at: "now".into(),
                kind: reducer::SessionTreeNodeKind::Message,
                role: Some("user".into()),
                preview: format!("Node {index}"),
                preview_truncated: false,
            })
            .collect(),
        truncated: true,
        next_after_entry_id: Some("entry-1".into()),
    }));
    draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 0);
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollDown, x, y)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.session_tree_picker.as_ref().unwrap().at_page_end());
    assert!(
        receiver.try_recv().is_err(),
        "first wheel moves only one row"
    );
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollDown, x, y)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let request = command(&mut receiver);
    assert_eq!(request["type"], "get_session_tree");
    assert_eq!(request["after_entry_id"], "entry-1");
    assert_eq!(ui.editor.text(), "draft");
}

#[test]
fn model_apply_lock_publishes_no_mouse_targets_and_rejects_selection() {
    let mut picker = ModelPicker::loading();
    picker.update_catalog(model_picker::tests::catalog());
    assert!(!picker.select_mouse(2, true));
    let action = picker.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE), false);
    assert!(
        matches!(action, ModelPickerAction::Apply(config) if config.model.as_deref() == Some("one"))
    );
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    let mut rows = mouse::Rows::default();
    terminal
        .draw(|frame| {
            rows =
                model_picker::render(frame, frame.area(), &picker, true, None, Palette::default());
        })
        .unwrap();
    for y in 0..24 {
        for x in 0..80 {
            assert!(
                rows.hit(event(MouseEventKind::Down(MouseButton::Left), x, y))
                    .is_none()
            );
        }
    }
    assert!(picker.select_mouse(2, false));
}

#[tokio::test]
async fn approval_clicks_cannot_approve_deny_or_cancel() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
            call_id: "approval".into(),
            name: "bash".into(),
            arguments: serde_json::json!({}),
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        })),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 80, 24);
    for y in 0..24 {
        ui.handle_input(click(2, y), &writer, 8192).await.unwrap();
    }
    assert!(ui.state.pending_approval.is_some());
    assert_eq!(ui.editor.text(), "draft");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn buffered_trust_preempts_clicks_and_decisions_remain_keyboard_only() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    let (sender, mut events) = mpsc::channel(16);
    sender
        .send(QueuedEvent {
            event: BackendEvent::TrustRequested {
                request_id: "trust".into(),
                project_path: "/project".into(),
            },
            _wire_bytes: Arc::new(Semaphore::new(1)).acquire_owned().await.unwrap(),
        })
        .await
        .unwrap();
    ui.handle_received_input(click(x, y), &mut events, &writer, 8192)
        .await
        .unwrap();
    assert!(ui.theme_picker.is_none());
    draw(&mut ui, 80, 24);
    for kind in [
        MouseEventKind::Down(MouseButton::Left),
        MouseEventKind::ScrollUp,
        MouseEventKind::ScrollDown,
    ] {
        ui.handle_input(Input::Mouse(event(kind, 2, 21)), &writer, 8192)
            .await
            .unwrap();
    }
    assert_eq!(ui.state.pending_trust_request_id.as_deref(), Some("trust"));
    assert_eq!(ui.editor.text(), "draft");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn file_popup_click_selects_a_reference_but_never_inserts_until_enter() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("@");
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = command(&mut receiver)["id"].as_str().unwrap().to_owned();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ProjectFilesReported {
            command_id: id.clone(),
            snapshot: Ok(Arc::new(ProjectFileSnapshot {
                generation: 1,
                truncated: false,
                entries: ["first.rs", "second file.rs"]
                    .into_iter()
                    .map(|path| ProjectFileEntry {
                        path: path.into(),
                        kind: ProjectFileKind::File,
                    })
                    .collect(),
            })),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: id,
            command_type: "get_project_files".into(),
            ok: true,
            error: None,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = row_point(&ui, 1);
    assert!(row_text(&buffer, y).contains("second file.rs"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert_eq!(ui.editor.text(), "@");
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@\"second file.rs\" ");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn clicking_unchanged_empty_composer_returns_focus_from_card_browse() {
    let mut ui = ui("");
    draw(&mut ui, 80, 24);
    let area = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap()
        .area;
    // Use any retained entry identity: the click must clear browse focus even
    // when placing the empty editor's cursor does not mutate it.
    ui.browse_selected = Some(BrowseTarget::Entry(
        ui.state.transcript.append_prompt("card".into()),
    ));
    let (writer, _) = mpsc::channel(8);
    ui.handle_input(click(area.x, area.y), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert!(ui.browse_selected.is_none());
    assert!(ui.editor.text().is_empty());
}

#[tokio::test]
async fn wheel_scrolls_the_transcript_after_a_resize_and_from_any_conversation_row() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"long transcript line\n".repeat(120));

    // Nothing is painted yet, so there is no viewport to scroll.
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, 2, 2)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.transcript_viewport.follows_tail());

    draw(&mut ui, 80, 24);
    let editor = ui.editor.clone();
    let composer_row = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .transcript
        .bottom()
        + 1;
    // A resize clears the painted frame until the next paint; a trackpad
    // gesture in that window still scrolls.
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    assert!(ui.mouse_frame.is_none());
    ui.handle_input(
        Input::Scroll(mouse::Scroll {
            event: event(MouseEventKind::ScrollUp, 2, 2),
            lines: -3,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(!ui.transcript_viewport.follows_tail());

    // With the pointer over the composer the gesture scrolls the transcript too,
    // and the draft and its focus are untouched.
    draw(&mut ui, 80, 24);
    let before = ui.transcript_viewport.clone();
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, 2, composer_row)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_ne!(ui.transcript_viewport, before);
    assert_eq!(ui.editor, editor);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn wheel_never_scrolls_the_transcript_behind_key_help() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"long transcript line\n".repeat(120));
    draw(&mut ui, 80, 24);
    ui.key_help = Some(key_help::KeyHelp::new(key_help::Owner::Composer));
    draw(&mut ui, 80, 24);
    let before = ui.transcript_viewport.clone();

    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, 2, 2)),
        &writer,
        8192,
    )
    .await
    .unwrap();

    assert_eq!(ui.transcript_viewport, before);
}

fn drag(kind: MouseEventKind, x: u16, y: u16) -> Input {
    Input::Mouse(event(kind, x, y))
}

/// The screen cell where `text` starts.
fn cell_of(buffer: &Buffer, text: &str) -> (u16, u16) {
    for y in 0..buffer.area.height {
        let row = row_text(buffer, y);
        if let Some(byte) = row.find(text) {
            return (u16::try_from(row[..byte].chars().count()).unwrap(), y);
        }
    }
    panic!("missing {text:?}");
}

fn transcript_ui() -> (
    LiveUi,
    std::sync::Arc<std::sync::Mutex<clipboard::TestClipboardState>>,
) {
    let mut ui = ui("");
    let (clipboard, copied) = clipboard::test_clipboard();
    ui.clipboard = clipboard;
    ui.state.transcript.append_prompt("show me".into());
    ui.state
        .transcript
        .complete_message(1, "alpha line\n\nbeta line".into());
    (ui, copied)
}

#[tokio::test]
async fn dragging_over_the_transcript_highlights_and_copies_the_painted_text() {
    let (writer, mut receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    let buffer = draw(&mut ui, 60, 20);
    let (x, y) = cell_of(&buffer, "alpha line");
    let (end_x, end_y) = cell_of(&buffer, "beta line");

    ui.handle_input(click(x + 6, y), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(
        drag(MouseEventKind::Drag(MouseButton::Left), end_x + 3, end_y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let highlighted = draw(&mut ui, 60, 20);
    assert!(
        highlighted[(x + 6, y)]
            .modifier
            .contains(Modifier::REVERSED)
    );
    assert!(
        !highlighted[(x + 5, y)]
            .modifier
            .contains(Modifier::REVERSED)
    );
    assert!(
        highlighted[(end_x + 3, end_y)]
            .modifier
            .contains(Modifier::REVERSED)
    );
    assert!(
        !highlighted[(end_x + 4, end_y)]
            .modifier
            .contains(Modifier::REVERSED)
    );

    // Releasing past the painted text still copies what the user saw, without a new paint.
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), end_x + 3, end_y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(copied.lock().unwrap().copied, ["line\nbeta"]);
    assert_eq!(ui.notice.as_deref(), Some("Copied 9 chars to clipboard."));
    let cleared = draw(&mut ui, 60, 20);
    assert!(!cleared[(x + 6, y)].modifier.contains(Modifier::REVERSED));
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn dragging_upward_selects_in_reading_order() {
    let (writer, _receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    let buffer = draw(&mut ui, 60, 20);
    let (x, y) = cell_of(&buffer, "alpha line");
    let (end_x, end_y) = cell_of(&buffer, "beta line");
    ui.handle_input(click(end_x + 3, end_y), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui, 60, 20);
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), x + 6, y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(copied.lock().unwrap().copied, ["line\nbeta"]);
}

#[tokio::test]
async fn plain_clicks_wheel_and_resize_copy_nothing() {
    let (writer, _receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    let buffer = draw(&mut ui, 60, 20);
    let (x, y) = cell_of(&buffer, "alpha line");

    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    draw(&mut ui, 60, 20);
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), x, y),
        &writer,
        8192,
    )
    .await
    .unwrap();

    for interrupt in [
        Input::Scroll(mouse::Scroll {
            event: event(MouseEventKind::ScrollUp, x, y),
            lines: -1,
        }),
        Input::Redraw,
    ] {
        draw(&mut ui, 60, 20);
        ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
        ui.handle_input(
            drag(MouseEventKind::Drag(MouseButton::Left), x + 4, y),
            &writer,
            8192,
        )
        .await
        .unwrap();
        draw(&mut ui, 60, 20);
        ui.handle_input(interrupt, &writer, 8192).await.unwrap();
        ui.handle_input(
            drag(MouseEventKind::Up(MouseButton::Left), x + 4, y),
            &writer,
            8192,
        )
        .await
        .unwrap();
    }
    assert!(copied.lock().unwrap().copied.is_empty());
    assert_eq!(ui.notice, None);
}

#[tokio::test]
async fn a_release_after_closing_a_popup_never_acts_as_a_second_click() {
    let (writer, _receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    ui.editor.insert_paste("keep draft");
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    let editor = ui.editor.clone();
    let area = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap()
        .area;
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.theme_picker.is_none());
    draw(&mut ui, 80, 24);
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), area.x, area.y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor, editor);
    assert!(copied.lock().unwrap().copied.is_empty());
}

#[tokio::test]
async fn an_approval_arriving_mid_drag_ends_the_selection_without_copying() {
    let (writer, _receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&buffer, "alpha line");
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    ui.handle_input(
        drag(MouseEventKind::Drag(MouseButton::Left), 70, 22),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 80, 24);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
            call_id: "approval".into(),
            name: "bash".into(),
            arguments: serde_json::json!({}),
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        })),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 80, 24);
    assert!(
        ui.transcript_selection.is_none(),
        "the stale selection must not be painted over the approval"
    );
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), 70, 22),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(copied.lock().unwrap().copied.is_empty());
    assert!(ui.state.pending_approval.is_some());
}

#[tokio::test]
async fn a_consumed_press_ends_a_selection_whose_release_was_lost() {
    let (writer, _receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&buffer, "alpha line");
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    ui.handle_input(
        drag(MouseEventKind::Drag(MouseButton::Left), x + 4, y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 80, 24);
    // The release never arrives; a popup then opens and consumes the next press.
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    let (row_x, row_y) = row_point(&ui, 1);
    ui.handle_input(click(row_x, row_y), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), row_x, row_y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(copied.lock().unwrap().copied.is_empty());
}

#[tokio::test]
async fn scrolling_during_a_decision_never_replaces_its_notice() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.state.view_status = ViewStatus::Running;
    ui.state.current_command = Some(reducer::ActiveCommand {
        id: "prompt-1".into(),
        command_type: reducer::ActiveCommandType::Prompt,
    });
    // The approval answer fits the frame limit; an older-history request does not.
    ui.state.history.oldest_cursor = Some("c".repeat(400));
    ui.state.transcript.append_prompt("retained".into());
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
            call_id: "approval".into(),
            name: "bash".into(),
            arguments: serde_json::json!({}),
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        })),
        &writer,
        300,
    )
    .await
    .unwrap();
    assert!(!ui.unsendable_current_response());
    let notice = ui.notice.clone();
    draw(&mut ui, 80, 24);
    for input in [
        Input::Mouse(event(MouseEventKind::ScrollUp, 2, 5)),
        key(KeyCode::PageUp),
    ] {
        ui.handle_input(input, &writer, 300).await.unwrap();
        assert_eq!(ui.notice, notice);
    }
    assert!(ui.state.pending_approval.is_some());
    assert!(receiver.try_recv().is_err());
}

fn scrolled_back_ui() -> LiveUi {
    let mut ui = ui("draft");
    ui.state.transcript.append_prompt("earlier".into());
    ui.state
        .transcript
        .complete_message(1, "earlier reply line\n".repeat(60));
    ui
}

async fn scroll_up(ui: &mut LiveUi, writer: &mpsc::Sender<WriterMessage>) {
    ui.handle_input(
        Input::Mouse(event(MouseEventKind::ScrollUp, 4, 4)),
        writer,
        8192,
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn clicking_the_jump_label_above_the_composer_returns_to_the_latest_output() {
    for (width, label) in [(100, " ↓ latest · Ctrl+End "), (30, " ↓ latest ")] {
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = scrolled_back_ui();
        let live = draw(&mut ui, width, 24);
        assert!((0..24).all(|y| !row_text(&live, y).contains('↓')));
        scroll_up(&mut ui, &writer).await;
        let buffer = draw(&mut ui, width, 24);
        let (x, y) = cell_of(&buffer, label.trim());
        // It sits on the blank row right above the composer, not in the footer.
        assert!(row_text(&buffer, y + 1).contains('╭'), "{width}");
        assert!(!row_text(&buffer, 23).contains("scrolled"));
        let frame = &ui.mouse_frame.as_ref().unwrap().conversation;
        assert!(
            frame.selectable.bottom() <= y,
            "the label is never selectable"
        );
        let editor = ui.editor.clone();

        ui.handle_input(click(x - 3, y), &writer, 8192)
            .await
            .unwrap();
        assert!(!ui.transcript_viewport.follows_tail(), "outside the label");
        ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
        assert!(ui.transcript_viewport.follows_tail(), "{width}");
        assert_eq!(ui.editor, editor);
        assert!(!row_text(&draw(&mut ui, width, 24), y).contains('↓'));
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn without_room_above_the_composer_the_footer_marks_the_scroll() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = scrolled_back_ui();
    draw(&mut ui, 60, 10);
    scroll_up(&mut ui, &writer).await;
    let buffer = draw(&mut ui, 60, 10);
    assert!((0..10).all(|y| !row_text(&buffer, y).contains('↓')));
    assert!(row_text(&buffer, 9).contains("scrolled"));
    assert!(
        ui.mouse_frame
            .as_ref()
            .unwrap()
            .conversation
            .jump_to_latest
            .is_none()
    );
}

#[tokio::test]
async fn the_jump_label_is_inert_while_a_decision_is_docked() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = scrolled_back_ui();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
            call_id: "approval".into(),
            name: "bash".into(),
            arguments: serde_json::json!({}),
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        })),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 100, 30);
    scroll_up(&mut ui, &writer).await;
    let buffer = draw(&mut ui, 100, 30);
    let (x, y) = cell_of(&buffer, "↓ latest");
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert!(!ui.transcript_viewport.follows_tail());
    assert!(ui.state.pending_approval.is_some());
}

#[tokio::test]
async fn a_release_keeps_the_recovery_notice_of_an_unsendable_response() {
    let (writer, mut receiver) = mpsc::channel(16);
    let (mut ui, copied) = transcript_ui();
    ui.state.view_status = ViewStatus::Running;
    ui.state.current_command = Some(reducer::ActiveCommand {
        id: "prompt-1".into(),
        command_type: reducer::ActiveCommandType::Prompt,
    });
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&buffer, "alpha line");
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    ui.handle_input(
        drag(MouseEventKind::Drag(MouseButton::Left), x + 4, y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 80, 24);

    // Mid-drag, after a cancellation was requested, an approval arrives whose answer exceeds
    // the frame limit: the response needs keyboard recovery before any repaint.
    ui.state.cancel_requested = true;
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
            call_id: "a".repeat(400),
            name: "bash".into(),
            arguments: serde_json::json!({}),
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
            grants_allowed: true,
            session_only_yolo: false,
        })),
        &writer,
        128,
    )
    .await
    .unwrap();
    assert!(ui.unsendable_current_response());
    let notice = ui.notice.clone();
    assert!(notice.is_some());
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), x + 4, y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.notice, notice);
    assert!(copied.lock().unwrap().copied.is_empty());
    assert!(ui.transcript_selection.is_none());
    assert!(receiver.try_recv().is_err());
}

fn finish_tool(ui: &mut LiveUi, call_id: &str, name: &str, arguments: Value, output: &str) {
    start_tool(ui, call_id, name, arguments);
    complete_tool(ui, call_id, name, output);
}

fn start_tool(ui: &mut LiveUi, call_id: &str, name: &str, arguments: Value) {
    ui.state
        .transcript
        .observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: call_id.into(),
            name: name.into(),
            arguments,
            detail_source: crate::tool_detail::ToolDetailSource::None,
        });
}

fn complete_tool(ui: &mut LiveUi, call_id: &str, name: &str, output: &str) {
    ui.state
        .transcript
        .observe_tool_result(crate::tool_cards::ToolResultInput {
            call_id: call_id.into(),
            name: name.into(),
            output: output.into(),
            output_tail: None,
            output_source_bytes: output.len() as u64,
            output_source_lines: 1,
            output_projection_cut_mid_line: false,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: Some(0),
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
            duration_ms: None,
        });
}

async fn plain_click(ui: &mut LiveUi, writer: &mpsc::Sender<WriterMessage>, x: u16, y: u16) {
    ui.handle_input(click(x, y), writer, 8192).await.unwrap();
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), x, y),
        writer,
        8192,
    )
    .await
    .unwrap();
}

#[tokio::test]
async fn click_toggles_a_card() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    ui.state.transcript.append_prompt("build".into());
    finish_tool(
        &mut ui,
        "b1",
        "bash",
        serde_json::json!({"command": "cargo build"}),
        "compiled ok",
    );
    let card = ui.state.transcript.entries().last().unwrap().id;
    let buffer = draw(&mut ui, 80, 24);
    assert!(!row_text(&buffer, cell_of(&buffer, "Bash").1).contains("expand"));
    let (x, y) = cell_of(&buffer, "Rust build");

    // A click folds the card without starting browse, so typing still reaches the prompt.
    plain_click(&mut ui, &writer, x, y).await;
    assert_eq!(ui.browse_selected, None);
    assert!(ui.transcript_row_cache.fold().is_expanded(card));
    let expanded = draw(&mut ui, 80, 24);
    let (detail_x, detail_y) = cell_of(&expanded, "compiled ok");

    // A detail row stays inert so people can press there to select its text.
    plain_click(&mut ui, &writer, detail_x + 2, detail_y).await;
    assert!(ui.transcript_row_cache.fold().is_expanded(card));

    // While browsing, the selection follows the click and the hint names it.
    ui.browse_selected = Some(BrowseTarget::Entry(card));
    let selected = draw(&mut ui, 80, 24);
    assert!(row_text(&selected, y).contains("(click to collapse)"));
    plain_click(&mut ui, &writer, x, y).await;
    assert!(!ui.transcript_row_cache.fold().is_expanded(card));
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Entry(card)));
    let collapsed = draw(&mut ui, 80, 24);
    assert!(row_text(&collapsed, y).contains("(click to expand)"));
}

#[tokio::test]
async fn click_opens_a_thought() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    ui.state.transcript.append_prompt("why".into());
    let thought = ui
        .state
        .transcript
        .append_thinking_delta(1, "secret plan\nmore");
    ui.state
        .transcript
        .complete_message(1, "visible answer".into());
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&buffer, "Thought");
    plain_click(&mut ui, &writer, x, y).await;
    assert!(ui.transcript_row_cache.fold().is_expanded(thought));
    let opened = draw(&mut ui, 80, 24);
    cell_of(&opened, "secret plan");
}

#[tokio::test]
async fn click_toggles_explored_group() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    ui.state.transcript.append_prompt("look".into());
    for (call_id, path) in [("r1", "a.rs"), ("r2", "b.rs")] {
        finish_tool(
            &mut ui,
            call_id,
            "read",
            serde_json::json!({"path": path}),
            "text",
        );
    }
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&buffer, "Explored ▸ 2 actions");
    plain_click(&mut ui, &writer, x, y).await;
    let start = ui.state.transcript.entries()[1].id;
    assert!(ui.transcript_row_cache.fold().is_group_expanded(start));
    let opened = draw(&mut ui, 80, 24);
    assert!(row_text(&opened, cell_of(&opened, "a.rs").1).contains("Read"));
    let (x, y) = cell_of(&opened, "Explored ▾ 2 actions");
    plain_click(&mut ui, &writer, x, y).await;
    assert!(!ui.transcript_row_cache.fold().is_group_expanded(start));
    cell_of(&draw(&mut ui, 80, 24), "Explored ▸ 2 actions");
}

#[tokio::test]
async fn click_toggles_consecutive_bash_group_and_member_details() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    ui.state.transcript.append_prompt("check".into());
    finish_tool(
        &mut ui,
        "first-shell",
        "bash",
        serde_json::json!({"command": "git diff --stat"}),
        "text",
    );
    finish_tool(
        &mut ui,
        "shell",
        "bash",
        serde_json::json!({"command": "git status"}),
        "clean",
    );
    let buffer = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&buffer, "Bash    ▸ 2 commands");
    plain_click(&mut ui, &writer, x, y).await;
    let start = ui.state.transcript.entries()[1].id;
    assert!(ui.transcript_row_cache.fold().is_group_expanded(start));
    let opened = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&opened, "git diff --stat");
    cell_of(&opened, "git status");
    // The first command and the persistent group header have separate click targets.
    plain_click(&mut ui, &writer, x, y).await;
    assert!(ui.transcript_row_cache.fold().is_expanded(start));
    assert!(ui.transcript_row_cache.fold().is_group_expanded(start));
    let detailed = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&detailed, "Bash    ▾ 2 commands");
    plain_click(&mut ui, &writer, x, y).await;
    assert!(!ui.transcript_row_cache.fold().is_group_expanded(start));
    let collapsed = draw(&mut ui, 80, 24);
    let (x, y) = cell_of(&collapsed, "Bash    ▸ 2 commands");
    // Only the closed header's preview names the commands; their rows are folded away.
    assert!(
        (0..collapsed.area.height)
            .map(|y| row_text(&collapsed, y))
            .filter(|row| row.contains("git status"))
            .all(|row| row.contains("▸ 2 commands"))
    );
    plain_click(&mut ui, &writer, x, y).await;
    let reopened = draw(&mut ui, 80, 24);
    cell_of(&reopened, "git diff --stat");
    cell_of(&reopened, "git status");
    assert!(ui.transcript_row_cache.fold().is_expanded(start));
}

#[tokio::test]
async fn keyboard_browse_distinguishes_group_header_from_first_member() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    for (call_id, command) in [("one", "echo one"), ("two", "echo two")] {
        finish_tool(
            &mut ui,
            call_id,
            "bash",
            serde_json::json!({"command": command}),
            "result",
        );
    }
    let start = ui.state.transcript.entries()[0].id;
    draw(&mut ui, 80, 24);
    for (key, selected, group_open, member_open) in [
        (KeyCode::F(6), BrowseTarget::Group(start), false, false),
        (KeyCode::Right, BrowseTarget::Group(start), true, false),
        (KeyCode::Right, BrowseTarget::Group(start), true, false),
        (KeyCode::Tab, BrowseTarget::Entry(start), true, false),
        (KeyCode::Right, BrowseTarget::Entry(start), true, true),
        (KeyCode::Left, BrowseTarget::Entry(start), true, false),
        (KeyCode::Right, BrowseTarget::Entry(start), true, true),
        (KeyCode::BackTab, BrowseTarget::Group(start), true, true),
        (KeyCode::Left, BrowseTarget::Group(start), false, true),
        (KeyCode::Enter, BrowseTarget::Group(start), true, true),
        (KeyCode::Enter, BrowseTarget::Group(start), false, true),
    ] {
        ui.handle_input(
            Input::Key(KeyEvent::new(key, KeyModifiers::NONE)),
            &writer,
            8192,
        )
        .await
        .unwrap();
        let buffer = draw(&mut ui, 80, 24);
        assert_eq!(ui.browse_selected, Some(selected), "{key:?}");
        assert_eq!(
            ui.transcript_row_cache.fold().is_group_expanded(start),
            group_open
        );
        assert_eq!(
            ui.transcript_row_cache.fold().is_expanded(start),
            member_open
        );
        let text = match selected {
            BrowseTarget::Group(_) => {
                if group_open {
                    "▾ 2 commands"
                } else {
                    "▸ 2 commands"
                }
            }
            BrowseTarget::Entry(_) => "echo one",
        };
        let selected_line = row_text(&buffer, cell_of(&buffer, text).1);
        assert!(selected_line.contains("(click to"), "{selected_line}");
    }
}

#[tokio::test]
async fn group_selection_follows_new_start() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    start_tool(
        &mut ui,
        "zero",
        "bash",
        serde_json::json!({"command": "echo zero"}),
    );
    for (call_id, command) in [("one", "echo one"), ("two", "echo two")] {
        finish_tool(
            &mut ui,
            call_id,
            "bash",
            serde_json::json!({"command": command}),
            "result",
        );
    }
    let entries = ui.state.transcript.entries();
    let (late, first) = (entries[0].id, entries[1].id);
    draw(&mut ui, 80, 24);
    for key in [KeyCode::F(6), KeyCode::Right] {
        ui.handle_input(
            Input::Key(KeyEvent::new(key, KeyModifiers::NONE)),
            &writer,
            8192,
        )
        .await
        .unwrap();
    }
    draw(&mut ui, 80, 24);
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Group(first)));

    complete_tool(&mut ui, "zero", "bash", "result");
    draw(&mut ui, 80, 24);
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Group(late)));
    let buffer = draw(&mut ui, 80, 24);
    let header = row_text(&buffer, cell_of(&buffer, "▾ 3 commands").1);
    assert!(header.contains("(click to"), "{header}");

    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui, 80, 24);
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Group(late)));
    assert!(!ui.transcript_row_cache.fold().is_group_expanded(late));
    assert!(!ui.transcript_row_cache.fold().is_expanded(late));
}

#[tokio::test]
async fn scrolled_group_header_stays_on_top() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    start_tool(
        &mut ui,
        "zero",
        "bash",
        serde_json::json!({"command": "echo zero"}),
    );
    for (call_id, command) in [("one", "echo one"), ("two", "echo two")] {
        finish_tool(
            &mut ui,
            call_id,
            "bash",
            serde_json::json!({"command": command}),
            "result",
        );
    }
    ui.state.transcript.append_prompt("next".into());
    ui.state.transcript.complete_message(1, "line\n".repeat(40));
    let entries = ui.state.transcript.entries();
    let (late, first) = (entries[0].id, entries[1].id);
    let header = |entry_id| crate::transcript_view::RowAnchor {
        entry_id,
        position: crate::transcript_view::RowPosition::Header,
    };
    draw(&mut ui, 80, 12);
    ui.transcript_viewport.reduce(
        TranscriptViewAction::Home,
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    while ui.transcript_viewport.scrolled_top() != Some(header(first)) {
        assert_eq!(
            ui.transcript_viewport
                .reduce(
                    TranscriptViewAction::ScrollLines(1),
                    &ui.state.transcript,
                    &mut ui.transcript_row_cache,
                )
                .consumed_rows,
            1
        );
    }
    draw(&mut ui, 80, 12);
    for key in [KeyCode::F(6), KeyCode::Right] {
        ui.handle_input(
            Input::Key(KeyEvent::new(key, KeyModifiers::NONE)),
            &writer,
            8192,
        )
        .await
        .unwrap();
    }
    draw(&mut ui, 80, 12);
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Group(first)));
    assert_eq!(ui.transcript_viewport.scrolled_top(), Some(header(first)));

    complete_tool(&mut ui, "zero", "bash", "result");
    ui.transcript_viewport.reduce(
        TranscriptViewAction::OutputChanged,
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    draw(&mut ui, 80, 12);
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Group(late)));
    assert_eq!(ui.transcript_viewport.scrolled_top(), Some(header(late)));
    let buffer = draw(&mut ui, 80, 12);
    let selected = row_text(&buffer, cell_of(&buffer, "▾ 3 commands").1);
    assert!(selected.contains("(click to"), "{selected}");
}

#[tokio::test]
async fn click_keeps_its_pressed_card() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui("");
    ui.state.transcript.append_prompt("build".into());
    finish_tool(
        &mut ui,
        "b1",
        "bash",
        serde_json::json!({"command": "cargo build"}),
        "compiled ok",
    );
    let card = ui.state.transcript.entries().last().unwrap().id;
    let buffer = draw(&mut ui, 80, 12);
    let (x, y) = cell_of(&buffer, "Rust build");

    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    // Output arriving before the release scrolls the card away from the pressed row.
    ui.state
        .transcript
        .complete_message(1, "one\ntwo\nthree\nfour\nfive".into());
    let shifted = draw(&mut ui, 80, 12);
    assert!(!row_text(&shifted, y).contains("Rust build"));
    ui.handle_input(
        drag(MouseEventKind::Up(MouseButton::Left), x, y),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.transcript_row_cache.fold().is_expanded(card));
}
