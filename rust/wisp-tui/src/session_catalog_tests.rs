//! Asynchronous picker integration: stale results never replace painted identities.

use super::*;
use ratatui::{Terminal, backend::TestBackend};
use reducer::{CatalogNavigation, SessionSummary};
use serde_json::{Value, json};

fn ui() -> LiveUi {
    let mut ui = LiveUi {
        state: UiState::new("fake".into(), None, None),
        ..LiveUi::default()
    };
    ui.editor.insert_paste("keep draft 👩‍💻");
    ui.state.transcript.append_prompt("background".into());
    ui
}
fn command(receiver: &mut mpsc::Receiver<WriterMessage>) -> Value {
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("frame")
    };
    serde_json::from_slice(&payload).unwrap()
}
fn summary(id: &str) -> SessionSummary {
    SessionSummary {
        session_id: id.into(),
        session_path: format!("/{id}.jsonl"),
        name: Some("duplicate".into()),
        updated_at: "now".into(),
        entry_count: 1,
    }
}
async fn complete(ui: &mut LiveUi, writer: &mpsc::Sender<WriterMessage>, id: &str, session: &str) {
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::SessionsReported {
            command_id: id.into(),
            sessions: vec![summary(session)],
            navigation: CatalogNavigation::default(),
            selected_session: None,
        }),
        writer,
        8192,
    )
    .await
    .unwrap();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: "get_sessions".into(),
            ok: true,
            error: None,
        }),
        writer,
        8192,
    )
    .await
    .unwrap();
}
fn draw(ui: &mut LiveUi) {
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
            protocol_version: LIVE_RPC_PROTOCOL_VERSION,
        },
    )
    .unwrap();
}
fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

#[tokio::test]
async fn rapid_queries_discard_old_pages_preserve_draft_and_require_paint() {
    let mut ui = ui();
    draw(&mut ui);
    let viewport = ui.transcript_viewport.clone();
    let draft = ui.editor.clone();
    let (writer, mut receiver) = mpsc::channel(16);
    ui.session_picker = Some(SessionPicker::loading());
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    let first = command(&mut receiver);
    ui.handle_input(Input::Paste("first".into()), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(Input::Paste("last".into()), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    assert!(receiver.try_recv().is_err());
    complete(&mut ui, &writer, first["id"].as_str().unwrap(), "wrong").await;
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    let latest = command(&mut receiver);
    assert_eq!(latest["query"], "last");
    complete(&mut ui, &writer, latest["id"].as_str().unwrap(), "right").await;
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    assert_eq!(ui.editor, draft);
    assert_eq!(ui.transcript_viewport, viewport);
    draw(&mut ui);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let selection = command(&mut receiver);
    assert_eq!(selection["type"], "select_session");
    assert_eq!(selection["session_id"], "right");
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: selection["id"].as_str().unwrap().into(),
            command_type: "select_session".into(),
            ok: false,
            error: Some("Session deleted; refresh".into()),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.session_picker.as_ref().is_some_and(|p| !p.selecting));
    assert_eq!(ui.editor, draft);
    assert_eq!(ui.transcript_viewport, viewport);
}

#[tokio::test]
async fn dismissed_request_cannot_reopen_or_overwrite_a_new_picker() {
    let mut ui = ui();
    let (writer, mut receiver) = mpsc::channel(16);
    ui.session_picker = Some(SessionPicker::loading());
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    let first = command(&mut receiver);
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    complete(&mut ui, &writer, first["id"].as_str().unwrap(), "old").await;
    assert!(ui.session_picker.is_none());
    ui.session_picker = Some(SessionPicker::loading());
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    let second = command(&mut receiver);
    complete(&mut ui, &writer, first["id"].as_str().unwrap(), "old").await;
    complete(&mut ui, &writer, second["id"].as_str().unwrap(), "new").await;
    draw(&mut ui);
    assert_eq!(
        ui.session_picker.as_ref().unwrap().rendered_selection(),
        Some("new")
    );
}

#[tokio::test]
async fn buffered_result_does_not_swallow_search_edits_or_retarget_reopened_picker() {
    let mut ui = ui();
    let (writer, mut receiver) = mpsc::channel(16);
    ui.session_picker = Some(SessionPicker::loading());
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    let first = command(&mut receiver);
    let input = event_loop::PendingInput::capture(Input::Paste("kept query".into()), &ui, 0);
    complete(&mut ui, &writer, first["id"].as_str().unwrap(), "one").await;
    input.apply(&mut ui, &writer, 8192).await.unwrap();
    assert_eq!(
        ui.session_picker
            .as_ref()
            .unwrap()
            .editing_identity()
            .unwrap()
            .1,
        "kept query"
    );
    let old_input = event_loop::PendingInput::capture(Input::Paste("wrong".into()), &ui, 0);
    ui.session_picker = Some(SessionPicker::loading());
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    old_input.apply(&mut ui, &writer, 8192).await.unwrap();
    assert_eq!(
        ui.session_picker
            .as_ref()
            .unwrap()
            .editing_identity()
            .unwrap()
            .1,
        ""
    );
}

#[tokio::test]
async fn oversized_search_command_is_recoverable() {
    let mut ui = ui();
    let (writer, mut receiver) = mpsc::channel(16);
    ui.session_picker = Some(SessionPicker::loading());
    ui.poll_session_catalog(&writer, 10).await.unwrap();
    assert!(receiver.try_recv().is_err());
    assert!(ui.state.session_operation.is_none());
    assert!(ui.state.input_ready);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.poll_session_catalog(&writer, 8192).await.unwrap();
    assert_eq!(command(&mut receiver)["type"], json!("get_sessions"));
}
