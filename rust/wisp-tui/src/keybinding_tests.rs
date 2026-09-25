use super::*;

fn configured(json: &str) -> LiveUi {
    LiveUi {
        bindings: Bindings::from_json(json).unwrap(),
        ..LiveUi::default()
    }
}

#[tokio::test]
async fn rebound_submit_replaces_enter_and_preserves_raw_prompt() {
    let mut ui = configured(r#"{"prompt.submit":["ctrl+enter"]}"#);
    ui.editor.insert_paste("exact\n🙂 prompt");
    let (writer, mut received) = mpsc::channel(8);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.text(), "exact\n🙂 prompt");
    assert!(received.try_recv().is_err());
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let WriterMessage::Frame { payload, .. } = received.try_recv().unwrap() else {
        panic!("frame")
    };
    let value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(value["prompt"], "exact\n🙂 prompt");
    assert!(ui.editor.text().is_empty());
}

#[tokio::test]
async fn rebound_newline_does_not_leave_ctrl_j_active() {
    let mut ui = configured(r#"{"prompt.newline":["f3"]}"#);
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("before");
    for key in [
        KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL),
        KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT),
    ] {
        ui.handle_input(Input::Key(key), &writer, MAX_APPLICATION_FRAME_BYTES)
            .await
            .unwrap();
    }
    assert_eq!(ui.editor.text(), "before");
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(3), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.text(), "before\n");
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn rebound_theme_and_history_do_not_capture_old_keys() {
    let mut ui = configured(r#"{"theme.toggle":["f3"],"history.open":["f4"]}"#);
    let original = ui.theme.active.name.clone();
    let (writer, _) = mpsc::channel(8);
    for character in ['t', 'r'] {
        ui.handle_input(
            Input::Key(KeyEvent::new(
                KeyCode::Char(character),
                KeyModifiers::CONTROL,
            )),
            &writer,
            MAX_APPLICATION_FRAME_BYTES,
        )
        .await
        .unwrap();
    }
    assert_eq!(ui.theme.active.name, original);
    assert!(ui.prompt_history_view.is_none());
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(3), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_ne!(ui.theme.active.name, original);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert!(ui.prompt_history_view.is_some());
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>()
}

fn ctrl(character: char) -> Input {
    Input::Key(KeyEvent::new(
        KeyCode::Char(character),
        KeyModifiers::CONTROL,
    ))
}

#[tokio::test]
async fn composer_selection_bypasses_completion_and_rebound_newline_replaces_it() {
    let mut ui = configured(r#"{"prompt.newline":["f3"]}"#);
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("/the");
    draw(&mut ui, 80, 24);
    assert!(ui.completion.view(None, None).is_some());
    ui.handle_input(
        Input::Key(KeyEvent::new(
            KeyCode::Home,
            KeyModifiers::CONTROL | KeyModifiers::SHIFT,
        )),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.selection_range(), Some(0..4));
    assert!(ui.completion.view(None, None).is_none());
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(3), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.text(), "\n");
    assert!(ui.editor.selection_range().is_none());
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn composer_undo_bypasses_and_restores_visible_completion() {
    let mut ui = LiveUi::default();
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("/the");
    draw(&mut ui, 80, 24);
    assert!(ui.completion.view(None, None).is_some());

    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_ne!(ui.editor.text(), "/the");
    let completed = ui.editor.text().to_owned();

    ui.handle_input(ctrl('z'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/the");
    assert!(ui.completion.view(None, None).is_some());
    ui.handle_input(ctrl('y'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), completed);
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn selected_draft_submits_in_full() {
    let mut ui = LiveUi::default();
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("whole draft");
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::SHIFT)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.selection_range(), Some(10..11));
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let WriterMessage::Frame { payload, .. } = received.try_recv().unwrap() else {
        panic!("prompt frame")
    };
    let value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(value["prompt"], "whole draft");
    assert!(ui.editor.selection_range().is_none());
}

#[tokio::test]
async fn ctrl_c_copies_a_composer_selection_and_otherwise_keeps_interrupt_behavior() {
    let (clipboard, state) = crate::clipboard::test_clipboard();
    let mut ui = LiveUi {
        clipboard,
        ..LiveUi::default()
    };
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("copy 🙂");
    ui.editor.handle_key(KeyEvent::new(
        KeyCode::Home,
        KeyModifiers::CONTROL | KeyModifiers::SHIFT,
    ));

    let control = ui
        .handle_input(ctrl('c'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(control, LoopControl::Continue);
    assert_eq!(ui.editor.text(), "copy 🙂");
    assert_eq!(ui.editor.selection_range(), Some(0..9));
    assert_eq!(state.lock().unwrap().copied, ["copy 🙂"]);
    assert!(received.try_recv().is_err());

    ui.editor
        .handle_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
    let control = ui
        .handle_input(ctrl('c'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(control, LoopControl::Exit);
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn focused_composer_popups_keep_ctrl_c_interrupt_precedence_over_hidden_selection() {
    let selected_ui = || {
        let (clipboard, state) = crate::clipboard::test_clipboard();
        let mut ui = LiveUi {
            clipboard,
            ..LiveUi::default()
        };
        ui.editor.insert_paste("@selected");
        (ui, state)
    };

    let (mut help_ui, help_clipboard) = selected_ui();
    help_ui
        .editor
        .handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::ALT));
    help_ui.state.view_status = ViewStatus::Running;
    help_ui.state.interaction_status = InteractionStatus::Running;
    help_ui.state.current_command = Some(crate::reducer::ActiveCommand {
        id: "prompt-help".into(),
        command_type: crate::reducer::ActiveCommandType::Prompt,
    });
    let (help_writer, mut help_received) = mpsc::channel(8);
    help_ui
        .handle_input(ctrl('g'), &help_writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert!(help_ui.key_help.is_some());
    let control = help_ui
        .handle_input(ctrl('c'), &help_writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(control, LoopControl::Continue);
    assert!(help_ui.key_help.is_none());
    assert!(help_clipboard.lock().unwrap().copied.is_empty());
    let WriterMessage::Frame { payload, .. } = help_received.try_recv().unwrap() else {
        panic!("cancel frame")
    };
    let cancel: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(cancel["type"], "cancel");
    assert_eq!(cancel["target_id"], "prompt-help");

    let (mut file_ui, file_clipboard) = selected_ui();
    file_ui.file_picker.sync_editor(&file_ui.editor);
    // Build the selection after opening file completion to exercise a stale
    // hidden editor selection behind the focused popup.
    file_ui
        .editor
        .handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::ALT));
    assert!(file_ui.file_picker.is_open());
    let (file_writer, mut file_received) = mpsc::channel(8);
    let control = file_ui
        .handle_input(ctrl('c'), &file_writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(control, LoopControl::Exit);
    assert!(file_clipboard.lock().unwrap().copied.is_empty());
    assert!(file_received.try_recv().is_err());
}

#[tokio::test]
async fn clipboard_cut_and_paste_are_selection_aware_and_undoable() {
    let (clipboard, state) = crate::clipboard::test_clipboard();
    let mut ui = LiveUi {
        clipboard,
        ..LiveUi::default()
    };
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("keep CUT");
    for _ in 0..3 {
        ui.editor
            .handle_key(KeyEvent::new(KeyCode::Left, KeyModifiers::SHIFT));
    }

    ui.handle_input(ctrl('x'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep ");
    assert_eq!(state.lock().unwrap().copied, ["CUT"]);
    ui.handle_input(ctrl('z'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep CUT");

    state
        .lock()
        .unwrap()
        .pastes
        .push_back(Ok("paste\u{1b}\ntext".into()));
    ui.handle_input(ctrl('v'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep paste\ntext");
    assert!(
        ui.notice
            .as_deref()
            .is_some_and(|notice| notice.contains("Ignored 1 unsafe"))
    );
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn failed_cut_preserves_the_selected_draft() {
    let (clipboard, state) = crate::clipboard::test_clipboard();
    state.lock().unwrap().copy_error = Some(crate::clipboard::ClipboardError::CopyFailed);
    let mut ui = LiveUi {
        clipboard,
        ..LiveUi::default()
    };
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("preserve");
    ui.editor
        .handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::ALT));

    ui.handle_input(ctrl('x'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "preserve");
    assert_eq!(ui.editor.selection_range(), Some(0..8));
    let control = ui
        .handle_input(ctrl('c'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(control, LoopControl::Continue);
    assert_eq!(ui.editor.text(), "preserve");
    assert_eq!(ui.editor.selection_range(), Some(0..8));
    assert!(
        ui.notice
            .as_deref()
            .is_some_and(|notice| notice.contains("Could not copy selection"))
    );
    assert!(received.try_recv().is_err());
}

#[test]
fn application_bindings_cannot_shadow_fixed_composer_editing() {
    for chord in [
        "Shift+Left",
        "Ctrl+Shift+Home",
        "Alt+Left",
        "Alt+A",
        "Ctrl+W",
        "Ctrl+U",
        "Ctrl+K",
        "Ctrl+Z",
        "Ctrl+Y",
        "Ctrl+Shift+Z",
        "Ctrl+X",
        "Ctrl+V",
        "Shift+Delete",
    ] {
        let json = serde_json::json!({"history.open": [chord]}).to_string();
        assert!(Bindings::from_json(&json).is_err(), "{chord}");
    }
}

#[tokio::test]
async fn contextual_help_preserves_theme_preview_and_routes_ctrl_c_to_its_owner() {
    let mut ui = LiveUi::default();
    ui.editor.insert_paste("preserved draft");
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    let (writer, mut received) = mpsc::channel(8);
    let committed = ui.theme.active.name.clone();
    ui.handle_input(ctrl('g'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert!(ui.key_help.is_some());
    for (width, height) in [(30, 8), (80, 24), (160, 40)] {
        let text = draw(&mut ui, width, height);
        assert!(text.contains("Keys"));
    }
    let control = ui
        .handle_input(ctrl('c'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert_eq!(control, LoopControl::Continue);
    assert!(ui.key_help.is_none());
    assert!(ui.theme_picker.is_none());
    assert_eq!(ui.theme.active.name, committed);
    assert_eq!(ui.editor.text(), "preserved draft");
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn help_cannot_approve_obscured_trust_but_can_deny_it() {
    let mut ui = LiveUi::default();
    ui.state.view_status = ViewStatus::WaitingForTrust;
    ui.state.pending_trust_request_id = Some("trust-one".into());
    ui.state.pending_trust_project_path = Some("/project".into());
    let (writer, mut received) = mpsc::channel(8);
    draw(&mut ui, 80, 24);
    ui.handle_input(ctrl('g'), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert!(received.try_recv().is_err());
    assert_eq!(
        ui.state.pending_trust_request_id.as_deref(),
        Some("trust-one")
    );
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let WriterMessage::Frame { payload, .. } = received.try_recv().unwrap() else {
        panic!("frame")
    };
    let value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(value["trusted"], false);
    assert!(ui.key_help.is_none());
}

#[tokio::test]
async fn large_paste_submits_raw_text_and_keeps_only_local_compact_echo() {
    let mut ui = configured(r#"{"prompt.submit":["f3"]}"#);
    let raw = format!("{}\nexact end", "界🙂".repeat(1100));
    ui.editor.insert_paste(&raw);
    assert!(ui.editor.compact_text().contains("Pasted content"));
    let (writer, mut received) = mpsc::channel(8);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(3), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let WriterMessage::Frame { payload, .. } = received.try_recv().unwrap() else {
        panic!("frame")
    };
    let value: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(value["prompt"], raw);
    assert_eq!(ui.state.transcript.latest_user_text(), Some(raw.as_str()));
    assert!(
        ui.state.transcript.entries()[0]
            .display_content()
            .contains("Pasted content")
    );
    assert!(!ui.editor.has_folds());
}

#[tokio::test]
async fn update_guidance_preserves_draft_and_performs_no_rpc() {
    let mut ui = LiveUi::default();
    let (writer, mut received) = mpsc::channel(8);
    for command in ["/update", "/update check", "/update install"] {
        ui.key_help = None;
        ui.editor.restore_prompt(command);
        let control = ui
            .handle_input(
                Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
                &writer,
                MAX_APPLICATION_FRAME_BYTES,
            )
            .await
            .unwrap();
        assert_eq!(control, LoopControl::Continue);
        assert_eq!(ui.editor.text(), command);
        assert!(
            ui.notice
                .as_deref()
                .is_some_and(|text| text.contains("wisp update --check"))
        );
        assert!(received.try_recv().is_err());
        let text = draw(&mut ui, 80, 24);
        assert!(text.contains("Update instructions"));
        let text = all_help_text(&mut ui, 80, 24);
        assert!(text.contains("matching the updated Python"));
        assert!(text.contains("relaunching"));
    }
}

#[tokio::test]
async fn remapped_submit_completes_partial_command_before_execution() {
    let mut ui = configured(r#"{"prompt.submit":["f3"]}"#);
    ui.editor.insert_paste("/the");
    let (writer, mut received) = mpsc::channel(8);
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(3), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.text(), "/theme ");
    assert!(ui.theme_picker.is_none());
    assert!(received.try_recv().is_err());
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::F(3), KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert!(ui.theme_picker.is_some());
    assert!(received.try_recv().is_err());
}

#[tokio::test]
async fn custom_theme_enter_does_not_steal_completion_activation() {
    let mut ui = configured(r#"{"theme.toggle":["enter"],"prompt.submit":["f3"]}"#);
    let original = ui.theme.active.name.clone();
    let (writer, mut received) = mpsc::channel(8);
    ui.editor.insert_paste("/the");
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.editor.text(), "/theme ");
    assert_eq!(ui.theme.active.name, original);
    // Exact command completion also retains its local Enter action.
    ui.editor.restore_prompt("/theme");
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert!(ui.theme_picker.is_some());
    assert_eq!(ui.theme.active.name, original);
    assert!(received.try_recv().is_err());
}

#[test]
fn minimum_size_help_scrolls_all_rows_of_long_alias_lists() {
    let mut ui = configured(
        r#"{"history.open":["ctrl+alt+shift+f1","ctrl+alt+shift+f2","ctrl+alt+shift+f3","ctrl+alt+shift+f4","ctrl+alt+shift+f5","ctrl+alt+shift+f6","ctrl+alt+shift+f7","ctrl+alt+shift+f8"]}"#,
    );
    ui.key_help = Some(key_help::KeyHelp::new(key_help::Owner::Composer));
    draw(&mut ui, 30, 8);
    let rows = ui.key_help.as_ref().unwrap().rendered_rows;
    assert!(rows > ui.key_help.as_ref().unwrap().rows(&ui.bindings).len());
    let seen = all_help_text(&mut ui, 30, 8);
    assert!(seen.contains("Ctrl+Alt+Shift+F8"));
    assert!(seen.contains("Open prompt history"));
    assert!(seen.contains("Backend test"));
}

// Read each painted visual row once, excluding borders and right-hand padding.
fn all_help_text(ui: &mut LiveUi, width: u16, height: u16) -> String {
    draw(ui, width, height);
    let area = ui::overlay_area(ratatui::layout::Rect::new(0, 0, width, height)).unwrap();
    let start = usize::from(area.y + 1) * usize::from(width) + usize::from(area.x + 1);
    let rows = ui.key_help.as_ref().unwrap().rendered_rows;
    let mut text = String::new();
    for offset in 0..rows {
        ui.key_help.as_mut().unwrap().scroll.offset = offset;
        let screen = draw(ui, width, height);
        let row = screen
            .chars()
            .skip(start)
            .take(usize::from(area.width - 2))
            .collect::<String>();
        text.push_str(row.trim_end());
    }
    text
}

#[tokio::test]
async fn rebound_browse_enter_keeps_fixed_detail_activation() {
    let mut ui = configured(r#"{"prompt.submit":["ctrl+enter"],"transcript.browse":["enter"]}"#);
    let BackendEvent::ToolCall(call) = BackendEvent::from_projection_value(&serde_json::json!({
        "type": "tool.call", "call_id": "edit-one", "name": "edit",
        "arguments": {"path": "file.txt", "edits": [{"oldText": "old\n", "newText": "new\n"}]}
    }))
    .unwrap() else {
        panic!("tool call")
    };
    let id = ui.state.transcript.observe_tool_call(call);
    let BackendEvent::ToolResult(result) =
        BackendEvent::from_projection_value(&serde_json::json!({
            "type": "tool.result", "message_entry_id": null, "call_id": "edit-one", "name": "edit",
            "output": "Applied 1 edit", "is_error": false
        }))
        .unwrap()
    else {
        panic!("tool result")
    };
    ui.state.transcript.observe_tool_result(*result);
    draw(&mut ui, 80, 24);
    ui.browse_selected = Some(id);
    let (writer, mut received) = mpsc::channel(8);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert!(ui.detail_view.is_open());
    assert!(received.try_recv().is_err());
}
