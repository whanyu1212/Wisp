use super::*;
use crate::{
    PendingApproval, ViewStatus,
    reducer::{ActiveCommand, ActiveCommandType, InteractionStatus, MessageContentKind},
    tool_detail::ToolDetailSource,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::backend::TestBackend;
use serde_json::json;
use std::sync::Arc;
use tokio::sync::Semaphore;

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}
fn connection() -> ConnectionInfo {
    ConnectionInfo {
        backend_version: "test".into(),
        protocol_version: 6,
        event_schema_version: 37,
    }
}
fn active_ui() -> LiveUi {
    let mut ui = LiveUi::default();
    ui.state.view_status = ViewStatus::Running;
    ui.state.interaction_status = InteractionStatus::Running;
    ui.state.current_command = Some(ActiveCommand {
        id: "prompt-1".into(),
        command_type: ActiveCommandType::Prompt,
    });
    ui
}
fn approval(id: &str) -> UiAction {
    UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
        call_id: id.into(),
        name: "shell".into(),
        arguments: json!({}),
        detail_source: ToolDetailSource::None,
        safety: "command".into(),
    }))
}
fn draw(ui: &mut LiveUi) {
    ui.draw(
        &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
        &connection(),
    )
    .unwrap();
}
fn delta(text: &str) -> BackendEvent {
    BackendEvent::MessageDelta {
        turn: 1,
        delta: text.into(),
        content_kind: MessageContentKind::Text,
    }
}
async fn queued(event: BackendEvent, budget: &Arc<Semaphore>) -> QueuedEvent {
    QueuedEvent {
        event,
        _wire_bytes: budget.clone().acquire_owned().await.unwrap(),
    }
}

impl InterruptSource for mpsc::Receiver<()> {
    async fn receive(&mut self) -> Result<(), Error> {
        self.recv().await.ok_or(Error::ReaderStopped)
    }
}

#[tokio::test(start_paused = true)]
async fn external_interrupt_includes_reserved_completion_but_not_later_output() {
    for reserved_resolution in [false, true] {
        let (writer, mut commands) = mpsc::channel(16);
        let mut ui = active_ui();
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::MessageStarted { turn: 1 }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        ui.dispatch(approval("old"), &writer, 8192).await.unwrap();
        draw(&mut ui);
        let budget = Arc::new(Semaphore::new(64));
        let (events_tx, mut events) = mpsc::channel(64);
        for _ in 0..63 {
            events_tx
                .send(queued(delta("x"), &budget).await)
                .await
                .unwrap();
        }
        let resolution = queued(
            BackendEvent::CommandFinished {
                command_id: "prompt-1".into(),
                command_type: "prompt".into(),
                ok: true,
                error: None,
            },
            &budget,
        )
        .await;
        let reserved = if reserved_resolution {
            Some((events_tx.clone().reserve_owned().await.unwrap(), resolution))
        } else {
            events_tx.send(resolution).await.unwrap();
            None
        };
        let producer = tokio::spawn(async move {
            if let Some((slot, resolution)) = reserved {
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                slot.send(resolution);
            }
            for _ in 0..2048 {
                if events_tx
                    .send(queued(delta("y"), &budget).await)
                    .await
                    .is_err()
                {
                    break;
                }
            }
        });
        let (signal_tx, mut signals) = mpsc::channel(1);
        signal_tx.send(()).await.unwrap();
        let (input_tx, mut inputs) = mpsc::channel(16);
        input_tx
            .send(Input::Error(std::io::Error::other("test stop")))
            .await
            .unwrap();
        let mut ready_event = None;
        let mut reader = None;
        let mut writer_outcome = None;
        let result = run_with_interrupts(
            &mut ui,
            &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
            &connection(),
            Sources {
                events: &mut events,
                ready_event: &mut ready_event,
                inputs: &mut inputs,
                reader: &mut reader,
                writer: &mut writer_outcome,
            },
            &writer,
            8192,
            &mut signals,
        )
        .await;

        assert!(matches!(result, Ok(Exit::User)));
        while let Ok(message) = commands.try_recv() {
            let WriterMessage::Frame { payload, .. } = message else {
                panic!("unexpected close")
            };
            let command: serde_json::Value = serde_json::from_slice(&payload).unwrap();
            assert_eq!(
                command["type"], "get_session_stats",
                "unexpected command: {command}"
            );
        }
        assert!(
            !producer.is_finished(),
            "later output must not extend the interrupt barrier"
        );
        assert!(ui.state.latest_assistant_text().unwrap().len() <= 127);
        producer.abort();
        let _ = producer.await;
    }
}

#[tokio::test]
async fn external_interrupt_waits_for_admitted_completion_before_acting() {
    for decision in [
        approval("old"),
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "old".into(),
            project_path: "/project".into(),
        }),
    ] {
        let (writer, mut commands) = mpsc::channel(16);
        let mut ui = active_ui();
        ui.dispatch(decision, &writer, 8192).await.unwrap();
        draw(&mut ui);
        let budget = Arc::new(Semaphore::new(64));
        let (events_tx, mut events) = mpsc::channel(64);
        let mut ready_event = Some(queued(delta("held"), &budget).await);
        for _ in 0..16 {
            events_tx
                .send(queued(delta("prefix"), &budget).await)
                .await
                .unwrap();
        }
        events_tx
            .send(
                queued(
                    BackendEvent::CommandFinished {
                        command_id: "prompt-1".into(),
                        command_type: "prompt".into(),
                        ok: true,
                        error: None,
                    },
                    &budget,
                )
                .await,
            )
            .await
            .unwrap();
        let (signal_tx, mut signals) = mpsc::channel(1);
        signal_tx.send(()).await.unwrap();
        let (input_tx, mut inputs) = mpsc::channel(16);
        input_tx
            .send(Input::Paste("must wait for the signal".into()))
            .await
            .unwrap();
        let mut reader = None;
        let mut writer_outcome = None;
        let result = run_with_interrupts(
            &mut ui,
            &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
            &connection(),
            Sources {
                events: &mut events,
                ready_event: &mut ready_event,
                inputs: &mut inputs,
                reader: &mut reader,
                writer: &mut writer_outcome,
            },
            &writer,
            8192,
            &mut signals,
        )
        .await;

        assert!(matches!(result, Ok(Exit::User)));
        while let Ok(message) = commands.try_recv() {
            let WriterMessage::Frame { payload, .. } = message else {
                panic!("unexpected close")
            };
            let command: serde_json::Value = serde_json::from_slice(&payload).unwrap();
            assert_eq!(
                command["type"], "get_session_stats",
                "unexpected command: {command}"
            );
        }
        assert!(ui.current_decision_context().is_none());
        assert!(ui.editor.text().is_empty());
        assert!(ready_event.is_none());
        assert_eq!(budget.available_permits(), 64);
    }
}

#[tokio::test]
async fn replacement_approval_cannot_use_a_key_captured_before_redraw() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(approval("old"), &writer, 8192).await.unwrap();
    draw(&mut ui);
    let pending = PendingInput::capture(key(KeyCode::Char('y')), &ui, 16);
    ui.dispatch(approval("replacement"), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui);
    pending.apply(&mut ui, &writer, 8192).await.unwrap();
    assert!(commands.try_recv().is_err());
    assert_eq!(
        ui.state.pending_approval.as_ref().unwrap().call_id,
        "replacement"
    );
    PendingInput::capture(key(KeyCode::Char('y')), &ui, 0)
        .apply(&mut ui, &writer, 8192)
        .await
        .unwrap();
    let WriterMessage::Frame { payload, .. } = commands.try_recv().unwrap() else {
        panic!("approval frame")
    };
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&payload).unwrap()["call_id"],
        "replacement"
    );
}

#[tokio::test]
async fn first_paint_does_not_authorize_an_already_captured_decision_or_picker_key() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(approval("unseen"), &writer, 8192)
        .await
        .unwrap();
    let pending = PendingInput::capture(key(KeyCode::Char('y')), &ui, 16);
    draw(&mut ui);
    pending.apply(&mut ui, &writer, 8192).await.unwrap();
    assert!(commands.try_recv().is_err());

    let mut ui = LiveUi {
        theme_picker: Some(crate::ThemePicker::new(crate::theme::default_theme())),
        ..LiveUi::default()
    };
    let pending = PendingInput::capture(key(KeyCode::Enter), &ui, 16);
    draw(&mut ui);
    pending.apply(&mut ui, &writer, 8192).await.unwrap();
    assert!(
        ui.theme_picker.is_some(),
        "the newly painted theme must not be applied"
    );
}

#[tokio::test]
async fn ordinary_typing_and_paste_survive_unrelated_workflow_revision_changes() {
    for input in [key(KeyCode::Char('x')), Input::Paste("pasted draft".into())] {
        let (writer, _commands) = mpsc::channel(16);
        let mut ui = active_ui();
        let expected = match &input {
            Input::Paste(text) => text.clone(),
            _ => "x".into(),
        };
        let pending = PendingInput::capture(input, &ui, 16);
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::MessageCompleted {
                turn: 1,
                content: "tool output".into(),
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        draw(&mut ui);
        pending.apply(&mut ui, &writer, 8192).await.unwrap();
        assert_eq!(ui.editor.text(), expected);
    }
}

#[tokio::test]
async fn pending_browse_activation_cannot_fall_through_to_the_editor() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = LiveUi::default();
    ui.editor.insert_paste("must not submit");
    let call = BackendEvent::from_projection_value(&json!({"type":"tool.call", "call_id":"browse", "name":"edit", "arguments":{"path":"file", "edits":[{"oldText":"old", "newText":"new"}]}})).unwrap();
    ui.dispatch(UiAction::BackendEvent(call), &writer, 8192)
        .await
        .unwrap();
    let result = BackendEvent::from_projection_value(&json!({"type":"tool.result", "call_id":"browse", "name":"edit", "output":"Applied 1 edit", "is_error":false})).unwrap();
    ui.dispatch(UiAction::BackendEvent(result), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui);
    ui.enter_or_cycle_browse();
    assert!(ui.browse_selected.is_some());
    draw(&mut ui);
    let pending = PendingInput::capture(key(KeyCode::Enter), &ui, 16);
    // This is the selection transition dispatch performs when streaming output
    // moves the selected card out of the visible viewport.
    ui.browse_selected = None;
    draw(&mut ui);
    pending.apply(&mut ui, &writer, 8192).await.unwrap();
    assert!(commands.try_recv().is_err());
    assert_eq!(ui.editor.text(), "must not submit");
}

#[tokio::test(start_paused = true)]
async fn saturated_fifo_gets_a_paint_after_eight_events_and_does_not_starve_input() {
    let (writer, _commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageStarted { turn: 1 }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let budget = Arc::new(Semaphore::new(64));
    let (events_tx, mut events) = mpsc::channel(64);
    for _ in 0..64 {
        events_tx
            .send(queued(delta("x"), &budget).await)
            .await
            .unwrap();
    }
    let (inputs_tx, mut inputs) = mpsc::channel(16);
    inputs_tx.send(Input::Paste("draft".into())).await.unwrap();
    inputs_tx
        .send(Input::Error(std::io::Error::other("test stop")))
        .await
        .unwrap();
    let (_reader_tx, reader_rx) = oneshot::channel();
    let (_writer_tx, writer_rx) = oneshot::channel();
    let mut reader = Some(reader_rx);
    let mut writer_outcome = Some(writer_rx);
    let mut ready_event = None;
    let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
    let result = run(
        &mut ui,
        &mut terminal,
        &connection(),
        Sources {
            events: &mut events,
            ready_event: &mut ready_event,
            inputs: &mut inputs,
            reader: &mut reader,
            writer: &mut writer_outcome,
        },
        &writer,
        8192,
    )
    .await;
    assert!(matches!(result, Err(Error::Io(_))));
    assert_eq!(ui.editor.text(), "draft");
    assert_eq!(
        ui.state.latest_assistant_text(),
        Some("x".repeat(64).as_str())
    );
    assert!(terminal.backend().to_string().contains("xxxxxxxx"));
    assert!(!terminal.backend().to_string().contains("xxxxxxxxx"));
    assert_eq!(budget.available_permits(), 64);
}

#[tokio::test(start_paused = true)]
async fn later_events_do_not_extend_the_captured_input_prefix() {
    let (writer, _commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageStarted { turn: 1 }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let budget = Arc::new(Semaphore::new(64));
    let (events_tx, mut events) = mpsc::channel(64);
    for _ in 0..64 {
        events_tx
            .send(queued(delta("x"), &budget).await)
            .await
            .unwrap();
    }
    let producer = tokio::spawn(async move {
        for _ in 0..2048 {
            if events_tx
                .send(queued(delta("y"), &budget).await)
                .await
                .is_err()
            {
                break;
            }
        }
    });
    let (inputs_tx, mut inputs) = mpsc::channel(16);
    inputs_tx.send(Input::Paste("draft".into())).await.unwrap();
    inputs_tx
        .send(Input::Error(std::io::Error::other("test stop")))
        .await
        .unwrap();
    let (_reader_tx, reader_rx) = oneshot::channel();
    let (_writer_tx, writer_rx) = oneshot::channel();
    let mut reader = Some(reader_rx);
    let mut writer_outcome = Some(writer_rx);
    let mut ready_event = None;
    let result = run(
        &mut ui,
        &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
        &connection(),
        Sources {
            events: &mut events,
            ready_event: &mut ready_event,
            inputs: &mut inputs,
            reader: &mut reader,
            writer: &mut writer_outcome,
        },
        &writer,
        8192,
    )
    .await;
    assert!(matches!(result, Err(Error::Io(_))));
    assert_eq!(ui.editor.text(), "draft");
    assert!(
        !producer.is_finished(),
        "input must complete while output is still being produced"
    );
    assert!(ui.state.latest_assistant_text().unwrap().len() <= 128);
    producer.abort();
    let _ = producer.await;
}

#[tokio::test(start_paused = true)]
async fn eof_drains_the_admitted_fifo_without_activating_pending_input() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageStarted { turn: 1 }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let budget = Arc::new(Semaphore::new(64));
    let (events_tx, mut events) = mpsc::channel(64);
    for text in ["first ", "second ", "last"] {
        events_tx
            .send(queued(delta(text), &budget).await)
            .await
            .unwrap();
    }
    drop(events_tx);
    let (inputs_tx, mut inputs) = mpsc::channel(16);
    inputs_tx.send(key(KeyCode::Enter)).await.unwrap();
    let (reader_tx, reader_rx) = oneshot::channel();
    reader_tx.send(Ok(ReaderTermination::Eof)).unwrap();
    let (_writer_tx, writer_rx) = oneshot::channel();
    let mut reader = Some(reader_rx);
    let mut writer_outcome = Some(writer_rx);
    let mut ready_event = None;
    let result = run(
        &mut ui,
        &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
        &connection(),
        Sources {
            events: &mut events,
            ready_event: &mut ready_event,
            inputs: &mut inputs,
            reader: &mut reader,
            writer: &mut writer_outcome,
        },
        &writer,
        8192,
    )
    .await;
    assert!(matches!(result, Ok(Exit::Eof)));
    assert_eq!(ui.state.latest_assistant_text(), Some("first second last"));
    assert!(commands.try_recv().is_err());
    assert_eq!(budget.available_permits(), 64);
}

#[tokio::test(start_paused = true)]
async fn fatal_writer_outcome_preempts_dispatch_and_preserves_the_held_event_for_accounting() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = LiveUi::default();
    let budget = Arc::new(Semaphore::new(64));
    let (_events_tx, mut events) = mpsc::channel(64);
    let mut ready_event = Some(
        queued(
            BackendEvent::Diagnostic("must not dispatch".into()),
            &budget,
        )
        .await,
    );
    let (inputs_tx, mut inputs) = mpsc::channel(16);
    inputs_tx
        .send(Input::Paste("must not apply".into()))
        .await
        .unwrap();
    let (_reader_tx, reader_rx) = oneshot::channel();
    let (writer_tx, writer_rx) = oneshot::channel();
    writer_tx.send(Err(Error::WriterStallTimeout)).unwrap();
    let mut reader = Some(reader_rx);
    let mut writer_outcome = Some(writer_rx);
    let result = run(
        &mut ui,
        &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
        &connection(),
        Sources {
            events: &mut events,
            ready_event: &mut ready_event,
            inputs: &mut inputs,
            reader: &mut reader,
            writer: &mut writer_outcome,
        },
        &writer,
        8192,
    )
    .await;
    assert!(matches!(result, Err(Error::WriterStallTimeout)));
    assert!(ui.editor.text().is_empty());
    assert!(commands.try_recv().is_err());
    assert_eq!(ready_event.as_ref().unwrap()._wire_bytes.num_permits(), 1);
    assert_eq!(budget.available_permits(), 63);
    drop(ready_event);
    assert_eq!(budget.available_permits(), 64);
}

#[tokio::test]
async fn replacement_model_catalog_cannot_retarget_enter_after_redraw() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = LiveUi::default();
    ui.dispatch(UiAction::OpenModelPicker, &writer, 8192)
        .await
        .unwrap();
    for refresh in 0..2 {
        let WriterMessage::Frame { payload, .. } = commands.try_recv().unwrap() else {
            panic!("catalog request")
        };
        let command: serde_json::Value = serde_json::from_slice(&payload).unwrap();
        let id = command["id"].as_str().unwrap().to_owned();
        let pending = (refresh == 1).then(|| PendingInput::capture(key(KeyCode::Enter), &ui, 16));
        let mut catalog = crate::model_picker::tests::catalog();
        if refresh == 1 {
            catalog.providers[1].available = false;
        }
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::ModelCatalogReported {
                command_id: id.clone(),
                catalog,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: id,
                command_type: "get_model_catalog".into(),
                ok: true,
                error: None,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        draw(&mut ui);
        if let Some(pending) = pending {
            assert!(!ui.state.model_catalog.as_ref().unwrap().providers[1].available);
            pending.apply(&mut ui, &writer, 8192).await.unwrap();
            assert!(commands.try_recv().is_err());
            assert!(ui.model_picker.is_some());
        } else {
            ui.dispatch(UiAction::LoadModelCatalog, &writer, 8192)
                .await
                .unwrap();
            draw(&mut ui);
        }
    }
}

#[tokio::test]
async fn replacement_trust_request_cannot_use_a_key_captured_before_redraw() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "old".into(),
            project_path: "/old".into(),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui);
    let pending = PendingInput::capture(key(KeyCode::Char('y')), &ui, 16);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "new".into(),
            project_path: "/new".into(),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    draw(&mut ui);
    pending.apply(&mut ui, &writer, 8192).await.unwrap();
    assert!(commands.try_recv().is_err());
    assert_eq!(ui.state.pending_trust_request_id.as_deref(), Some("new"));
    PendingInput::capture(key(KeyCode::Char('n')), &ui, 0)
        .apply(&mut ui, &writer, 8192)
        .await
        .unwrap();
    let WriterMessage::Frame { payload, .. } = commands.try_recv().unwrap() else {
        panic!("denial frame")
    };
    let command: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(command["request_id"], "new");
    assert_eq!(command["trusted"], false);
}

#[tokio::test(start_paused = true)]
async fn idle_loop_observes_writer_failure_without_another_command() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = LiveUi::default();
    let (_events_tx, mut events) = mpsc::channel(64);
    let (_inputs_tx, mut inputs) = mpsc::channel(16);
    let (_reader_tx, reader_rx) = oneshot::channel();
    let (writer_tx, writer_rx) = oneshot::channel();
    let failure = tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        writer_tx.send(Err(Error::WriterStallTimeout)).unwrap();
    });
    let mut reader = Some(reader_rx);
    let mut writer_outcome = Some(writer_rx);
    let mut ready_event = None;
    let before = Instant::now();
    let result = run(
        &mut ui,
        &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
        &connection(),
        Sources {
            events: &mut events,
            ready_event: &mut ready_event,
            inputs: &mut inputs,
            reader: &mut reader,
            writer: &mut writer_outcome,
        },
        &writer,
        8192,
    )
    .await;
    assert!(matches!(result, Err(Error::WriterStallTimeout)));
    assert_eq!(Instant::now() - before, std::time::Duration::from_secs(2));
    assert!(commands.try_recv().is_err());
    failure.await.unwrap();
}

#[tokio::test]
async fn rebound_submit_does_not_let_enter_activate_a_completion_first_painted_while_pending() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = LiveUi {
        bindings: crate::Bindings::from_json(r#"{"prompt.submit":["ctrl+enter"]}"#).unwrap(),
        ..LiveUi::default()
    };
    ui.editor.insert_paste("/the");
    ui.completion.sync(&ui.editor);
    let pending = PendingInput::capture(key(KeyCode::Enter), &ui, 16);
    draw(&mut ui);
    assert!(ui.completion.rendered_selection().is_some());
    pending.apply(&mut ui, &writer, 8192).await.unwrap();
    assert_eq!(ui.editor.text(), "/the");
    assert!(commands.try_recv().is_err());
    PendingInput::capture(key(KeyCode::Enter), &ui, 0)
        .apply(&mut ui, &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/theme ");
}

#[tokio::test]
async fn a_stale_denial_cannot_become_text_after_approval_or_trust_finishes() {
    for decision in [
        approval("old"),
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "old".into(),
            project_path: "/project".into(),
        }),
    ] {
        let (writer, mut commands) = mpsc::channel(16);
        let mut ui = active_ui();
        ui.editor.insert_paste("next draft");
        ui.dispatch(decision, &writer, 8192).await.unwrap();
        draw(&mut ui);
        let pending = PendingInput::capture(key(KeyCode::Char('n')), &ui, 16);
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: "prompt-1".into(),
                command_type: "prompt".into(),
                ok: false,
                error: Some("RPC command cancelled: requested by user".into()),
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert!(ui.current_decision_context().is_none());
        assert!(ui.editor_editable());
        while commands.try_recv().is_ok() {}
        draw(&mut ui);
        pending.apply(&mut ui, &writer, 8192).await.unwrap();
        assert_eq!(ui.editor.text(), "next draft");
        assert!(commands.try_recv().is_err());
    }
}

#[tokio::test(start_paused = true)]
async fn input_waits_for_a_reserved_event_that_has_not_been_published() {
    let (writer, mut commands) = mpsc::channel(16);
    let mut ui = active_ui();
    ui.dispatch(approval("old"), &writer, 8192).await.unwrap();
    draw(&mut ui);
    let budget = Arc::new(Semaphore::new(64));
    let (events_tx, mut events) = mpsc::channel(64);
    let reserved = events_tx.reserve_owned().await.unwrap();
    assert_eq!(events.len(), 0);
    assert_eq!(events.capacity(), 63);
    let publisher = tokio::spawn(async move {
        // The main task captures input before this simulated decode finishes.
        tokio::task::yield_now().await;
        let UiAction::BackendEvent(event) = approval("replacement") else {
            unreachable!()
        };
        reserved.send(queued(event, &budget).await);
    });
    let (inputs_tx, mut inputs) = mpsc::channel(16);
    inputs_tx.send(key(KeyCode::Char('y'))).await.unwrap();
    inputs_tx
        .send(Input::Error(std::io::Error::other("test stop")))
        .await
        .unwrap();
    let (_reader_tx, reader_rx) = oneshot::channel();
    let (_writer_tx, writer_rx) = oneshot::channel();
    let mut reader = Some(reader_rx);
    let mut writer_outcome = Some(writer_rx);
    let mut ready_event = None;
    let result = run(
        &mut ui,
        &mut Terminal::new(TestBackend::new(100, 24)).unwrap(),
        &connection(),
        Sources {
            events: &mut events,
            ready_event: &mut ready_event,
            inputs: &mut inputs,
            reader: &mut reader,
            writer: &mut writer_outcome,
        },
        &writer,
        8192,
    )
    .await;
    assert!(matches!(result, Err(Error::Io(_))));
    assert!(
        commands.try_recv().is_err(),
        "input must not approve the old decision before the reserved event arrives"
    );
    assert_eq!(
        ui.state.pending_approval.as_ref().unwrap().call_id,
        "replacement"
    );
    publisher.await.unwrap();
}
