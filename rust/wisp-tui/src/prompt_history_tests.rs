//! Live recall tests exercise real input routing, submission effects, and rendering.

use super::*;
use ratatui::backend::TestBackend;

fn ui(draft: &str) -> LiveUi {
    let mut ui = LiveUi {
        state: UiState::new("fake".into(), None, None),
        ..LiveUi::default()
    };
    ui.editor.insert_paste(draft);
    ui
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn ctrl(character: char) -> Input {
    Input::Key(KeyEvent::new(
        KeyCode::Char(character),
        KeyModifiers::CONTROL,
    ))
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> String {
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
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}

fn frame(receiver: &mut mpsc::Receiver<WriterMessage>) -> serde_json::Value {
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("expected command");
    };
    serde_json::from_slice(&payload).unwrap()
}

fn finished(id: &str, kind: &str, ok: bool) -> UiAction {
    UiAction::BackendEvent(BackendEvent::CommandFinished {
        command_id: id.into(),
        command_type: kind.into(),
        ok,
        error: None,
    })
}

#[tokio::test]
async fn recall_preserves_draft_on_close_and_restores_exact_text_without_submitting() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("original\ndraft 👩‍💻");
    ui.editor
        .handle_key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
    let before = ui.editor.clone();
    ui.state.transcript.append_exchange("transcript".into());
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"older output\n".repeat(80));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::PageUp), &writer, 8192)
        .await
        .unwrap();
    assert!(!ui.transcript_viewport.follows_tail());
    let viewport = ui.transcript_viewport.clone();
    ui.prompt_history.record("other".into());
    let prompt = "  Straße\n/skill:review e\u{301} 👩‍💻  ";
    ui.prompt_history.record(prompt.into());
    for close in [key(KeyCode::Esc), ctrl('c'), ctrl('r')] {
        ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
        assert!(draw(&mut ui, 80, 24).contains("Prompt history"));
        ui.handle_input(Input::Paste("STRASSE".into()), &writer, 8192)
            .await
            .unwrap();
        ui.handle_input(close, &writer, 8192).await.unwrap();
        assert!(ui.prompt_history_view.is_none());
        assert_eq!(ui.editor, before);
        assert_eq!(ui.transcript_viewport, viewport);
        assert!(!ui.state.cancel_requested);
    }
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    draw(&mut ui, 80, 24);
    ui.handle_input(Input::Paste("STRASSE".into()), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.editor, before,
        "query changes invalidate the rendered choice"
    );
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.prompt_history_view.is_none());
    assert_eq!(ui.editor.text(), prompt);
    assert_eq!(ui.editor.cursor_offset(), prompt.len());
    assert_eq!(ui.transcript_viewport, viewport);
    assert!(
        receiver.try_recv().is_err(),
        "restoring must not send a command"
    );
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let command = frame(&mut receiver);
    assert_eq!(command["type"], "prompt");
    assert_eq!(command["prompt"], prompt);
}

#[tokio::test]
async fn history_command_is_local_and_invalid_arguments_preserve_input() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("/history extra");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.prompt_history_view.is_none());
    assert_eq!(ui.editor.text(), "/history extra");
    assert_eq!(ui.notice.as_deref(), Some("Usage: /history"));
    ui.editor.restore_prompt("/history");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.editor.text().is_empty());
    assert!(draw(&mut ui, 80, 24).contains("No prompts submitted in this TUI run."));
    assert!(ui.prompt_history.is_empty());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn hidden_or_changed_selection_cannot_restore_and_new_entries_preserve_identity() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.prompt_history.record("oldest".into());
    ui.prompt_history.record("newest".into());
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "draft");
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "draft");
    draw(&mut ui, 80, 24);
    ui.apply_effects(
        vec![UiEffect::RecordPromptHistory("just accepted".into())],
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "draft");
    draw(&mut ui, 80, 24);
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "draft");
    draw(&mut ui, 29, 7);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "draft");
    assert!(draw(&mut ui, 30, 8).contains("Prompt history"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "oldest");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn ordinary_prompts_record_after_dispatch_and_local_rejections_do_not_record() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("/skill:review original");
    ui.handle_input(key(KeyCode::Enter), &writer, 1)
        .await
        .unwrap();
    assert!(ui.prompt_history.is_empty());
    assert_eq!(ui.editor.text(), "/skill:review original");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(frame(&mut receiver)["prompt"], "/skill:review original");
    assert_eq!(
        ui.prompt_history.entry(0).unwrap().prompt,
        "/skill:review original"
    );
    // Transcript hydration and session replacement do not seed or erase process history.
    ui.state = UiState::new("other".into(), None, None);
    ui.state
        .transcript
        .append_prompt("persisted transcript only".into());
    ui.apply_effects(vec![UiEffect::ReplaceTranscript], &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.prompt_history.search(""), [0]);
    assert!(LiveUi::default().prompt_history.is_empty());
    drop(receiver);
    ui.editor.restore_prompt("writer closed");
    assert!(
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .is_err()
    );
    assert_eq!(ui.prompt_history.search(""), [0]);
    assert_eq!(ui.editor.text(), "writer closed");
}

#[tokio::test]
async fn queued_recall_waits_for_backend_acceptance_and_failure_preempts_the_browser() {
    for modifiers in [KeyModifiers::NONE, KeyModifiers::ALT] {
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui("active prompt");
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        frame(&mut receiver);
        ui.editor.insert_paste("/skill:review queued");
        let write = tokio::spawn(async move {
            let WriterMessage::Frame { payload, ack, .. } = receiver.recv().await.unwrap() else {
                panic!("queued frame")
            };
            ack.unwrap().send(Ok(())).unwrap();
            (
                receiver,
                serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            )
        });
        ui.handle_input(
            Input::Key(KeyEvent::new(KeyCode::Enter, modifiers)),
            &writer,
            8192,
        )
        .await
        .unwrap();
        let (mut receiver, command) = write.await.unwrap();
        let id = command["id"].as_str().unwrap();
        let kind = command["type"].as_str().unwrap();
        assert_eq!(
            kind,
            if modifiers == KeyModifiers::ALT {
                "follow_up"
            } else {
                "steer"
            }
        );
        assert!(
            ui.prompt_history.search("queued").is_empty(),
            "writer ACK is not admission"
        );
        ui.dispatch(finished(id, "wrong", true), &writer, 8192)
            .await
            .unwrap();
        ui.dispatch(finished("wrong-id", kind, true), &writer, 8192)
            .await
            .unwrap();
        assert!(ui.prompt_history.search("queued").is_empty());
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: if kind == "steer" {
                    vec!["/skill:review queued".into()]
                } else {
                    vec![]
                },
                follow_up: if kind == "follow_up" {
                    vec!["/skill:review queued".into()]
                } else {
                    vec![]
                },
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert!(ui.prompt_history.search("queued").is_empty());
        ui.dispatch(finished(id, kind, true), &writer, 8192)
            .await
            .unwrap();
        let accepted = ui.prompt_history.search("queued");
        assert_eq!(accepted.len(), 1);
        assert_eq!(
            ui.prompt_history.entry(accepted[0]).unwrap().prompt,
            "/skill:review queued"
        );
        ui.dispatch(finished(id, kind, true), &writer, 8192)
            .await
            .unwrap();
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::QueueUpdated {
                steering: vec![],
                follow_up: vec![],
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert_eq!(ui.prompt_history.search("queued"), accepted);
        // Restore the accepted text during an active run, then explicitly submit it again.
        ui.editor.insert_paste("newer draft");
        ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
        ui.handle_input(Input::Paste("queued".into()), &writer, 8192)
            .await
            .unwrap();
        draw(&mut ui, 80, 24);
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.editor.text(), "/skill:review queued");
        assert!(receiver.try_recv().is_err());
        let write = tokio::spawn(async move {
            let WriterMessage::Frame { payload, ack, .. } = receiver.recv().await.unwrap() else {
                panic!("queued frame")
            };
            ack.unwrap().send(Ok(())).unwrap();
            (
                receiver,
                serde_json::from_slice::<serde_json::Value>(&payload).unwrap(),
            )
        });
        ui.handle_input(
            Input::Key(KeyEvent::new(KeyCode::Enter, modifiers)),
            &writer,
            8192,
        )
        .await
        .unwrap();
        let (_receiver, command) = write.await.unwrap();
        ui.editor.insert_paste("keep newer draft");
        ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
        draw(&mut ui, 80, 24);
        ui.dispatch(
            finished(command["id"].as_str().unwrap(), kind, false),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert!(ui.prompt_history_view.is_none());
        assert_eq!(
            ui.prompt_history.search("queued"),
            accepted,
            "rejection does not promote a duplicate"
        );
        assert_eq!(ui.editor.text(), "keep newer draft");
        assert!(ui.retry_deferred_queue_recovery());
        assert_eq!(ui.editor.text(), "/skill:review queued\nkeep newer draft");
    }
}

#[tokio::test]
async fn trust_and_other_modals_take_priority_over_history() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("draft");
    ui.prompt_history.record("retained".into());
    ui.command_help = Some(Help::default());
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    assert!(ui.prompt_history_view.is_none());
    ui.command_help = None;
    ui.connection_panel = Some(ConnectionPanel::new(ui.state.connection_catalog.clone()));
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    assert!(ui.prompt_history_view.is_none());
    ui.connection_panel = None;
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    ui.apply_effects(
        vec![UiEffect::ShowConnectionPanel(
            ui.state.connection_catalog.clone(),
        )],
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.prompt_history_view.is_none());
    assert_eq!(ui.editor.text(), "draft");
    ui.connection_panel = None;
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "trust".into(),
            project_path: "/project".into(),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.prompt_history_view.is_none());
    ui.handle_input(ctrl('r'), &writer, 8192).await.unwrap();
    assert!(ui.prompt_history_view.is_none());
    assert_eq!(ui.editor.text(), "draft");
    assert!(receiver.try_recv().is_err());
}
