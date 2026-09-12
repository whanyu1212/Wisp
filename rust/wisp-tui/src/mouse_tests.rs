//! Real input routing against hit regions emitted by the actual renderers.

use super::*;
use crossterm::event::{MouseButton, MouseEventKind};
use ratatui::{backend::TestBackend, buffer::Buffer, layout::Rect};
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
            protocol_version: 6,
            event_schema_version: 37,
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
fn capture_requires_explicit_opt_in_and_filters_non_navigation_reports() {
    for value in [
        None,
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
        MouseEventKind::Moved,
        MouseEventKind::Drag(MouseButton::Left),
        MouseEventKind::Up(MouseButton::Left),
        MouseEventKind::Down(MouseButton::Right),
        MouseEventKind::ScrollLeft,
        MouseEventKind::ScrollRight,
    ] {
        assert!(!mouse::supported(event(kind, 1, 1)));
    }
    let mut shifted = event(MouseEventKind::Down(MouseButton::Left), 1, 1);
    shifted.modifiers = KeyModifiers::SHIFT;
    assert!(!mouse::supported(shifted));
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
    let line = mapping.first_line;
    let column = mapping.column_starts[0];
    ui.handle_input(click(area.x, area.y), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.cursor_row(), line);
    assert_eq!(ui.editor.cursor_column(), column);
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
    assert!(row_text(&buffer, y).contains("Orchid"));
    ui.handle_input(click(x, y), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme_picker.as_ref().unwrap().preview().slug, "orchid");
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
        Input::Mouse(event(MouseEventKind::ScrollUp, 1, 4)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.command_help.as_ref().unwrap().offset, 2);
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
    ui.browse_selected = Some(ui.state.transcript.append_prompt("card".into()));
    let (writer, _) = mpsc::channel(8);
    ui.handle_input(click(area.x, area.y), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert!(ui.browse_selected.is_none());
    assert!(ui.editor.text().is_empty());
}
