//! Live input routing regressions: commands never become queued prompt text.
use super::*;
use ratatui::backend::TestBackend;
use reducer::{ActiveCommand, ActiveCommandType, AgentMode, InteractionStatus};

fn ui(draft: &str, active: bool) -> LiveUi {
    let mut ui = LiveUi::default();
    ui.state.command_catalog = Some(commands::tests::catalog().into());
    ui.editor.insert_paste(draft);
    if active {
        ui.state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        ui.state.view_status = ViewStatus::Running;
        ui.state.interaction_status = InteractionStatus::Running;
    }
    ui
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
fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}
fn frame(receiver: &mut mpsc::Receiver<WriterMessage>) -> serde_json::Value {
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("expected frame")
    };
    serde_json::from_slice(&payload).unwrap()
}

#[tokio::test]
async fn permissions_require_a_confirmed_snapshot_and_painted_choice() {
    use wisp_protocol::{commands::PermissionMode, events::PermissionState};
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("/permissions", false);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let request = frame(&mut receiver);
    assert_eq!(request["type"], "get_permissions");
    ui.handle_input(key(KeyCode::Char('2')), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    let id = request["id"].as_str().unwrap().to_string();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::PermissionsReported {
            command_id: id.clone(),
            permissions: Ok(Arc::new(PermissionState {
                mode: PermissionMode::Ask,
                saved_mode: None,
                project_path: Some("/project".into()),
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
            command_type: "get_permissions".into(),
            ok: true,
            error: None,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Char('2')), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    let painted = draw(&mut ui, 80, 24);
    assert!(painted.contains("Project permissions"));
    assert!(painted.contains("1  Ask before changes"));
    assert!(painted.contains("2  YOLO"));
    ui.handle_input(key(KeyCode::Char('2')), &writer, 8192)
        .await
        .unwrap();
    let change = frame(&mut receiver);
    assert_eq!(change["type"], "set_permissions");
    assert_eq!(change["mode"], "yolo");
    assert_eq!(
        ui.state.permissions.snapshot.as_ref().unwrap().mode,
        PermissionMode::Ask
    );
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: change["id"].as_str().unwrap().into(),
            command_type: "set_permissions".into(),
            ok: false,
            error: Some("Could not save".into()),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(
        ui.state.permissions.snapshot.as_ref().unwrap().mode,
        PermissionMode::Ask
    );
    assert!(draw(&mut ui, 80, 24).contains("Could not save"));
}

#[tokio::test]
async fn permission_gate_keys_send_only_the_displayed_scopes() {
    for (choice, approved, scope) in [
        ('1', true, None),
        ('2', true, Some("tool_session")),
        ('3', true, Some("all_project")),
        ('4', false, None),
    ] {
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui("", true);
        ui.state.view_status = ViewStatus::WaitingForApproval;
        ui.state.pending_approval = Some(PendingApproval {
            call_id: "call".into(),
            name: "bash".into(),
            arguments: serde_json::json!({"command":"echo hi"}),
            safety: "command".into(),
            detail_source: tool_detail::ToolDetailSource::None,
        });
        if approved {
            ui.handle_input(key(KeyCode::Char(choice)), &writer, 8192)
                .await
                .unwrap();
            assert!(receiver.try_recv().is_err());
        }
        let painted = draw(&mut ui, 30, 8);
        for label in [
            "1 Allow once",
            "2 Allow tool this session",
            "3 YOLO (saved)",
            "4 Deny",
        ] {
            assert!(painted.contains(label), "{painted}");
        }
        ui.handle_input(key(KeyCode::Char(choice)), &writer, 8192)
            .await
            .unwrap();
        let response = frame(&mut receiver);
        assert_eq!(response["type"], "approval");
        assert_eq!(response["approved"], approved);
        assert_eq!(
            response.get("scope").and_then(serde_json::Value::as_str),
            scope
        );
    }
}

#[tokio::test]
async fn skill_browser_inserts_without_submitting_and_catalog_changes_require_a_redraw() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui("/skills", true);
    ui.state.skills.snapshot = Some(Arc::new(commands::tests::skills()));
    let active = ui.state.current_command.clone();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let request = frame(&mut receiver);
    assert_eq!(request["type"], "get_skills");
    assert!(ui.discovery_view.is_some());
    assert!(ui.editor.text().is_empty());
    ui.editor.insert_paste("keep my draft");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep my draft");
    draw(&mut ui, 80, 24);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::SkillCatalogUpdated(Ok(Arc::new(
            commands::tests::skills(),
        )))),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep my draft");
    draw(&mut ui, 29, 7);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep my draft");
    draw(&mut ui, 80, 24);
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "keep my draft");
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.discovery_view.is_none());
    assert_eq!(ui.editor.text(), "/skill:review keep my draft");
    assert_eq!(ui.state.current_command, active);
    assert!(receiver.try_recv().is_err());
    let sent = tokio::spawn(async move {
        let WriterMessage::Frame { payload, ack, .. } = receiver.recv().await.unwrap() else {
            panic!("expected queued skill invocation");
        };
        ack.unwrap().send(Ok(())).unwrap();
        serde_json::from_slice::<serde_json::Value>(&payload).unwrap()
    });
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let steer = sent.await.unwrap();
    assert_eq!(steer["type"], "steer");
    assert_eq!(steer["content"], "/skill:review keep my draft");
}

#[tokio::test]
async fn discovery_views_coalesce_refreshes_and_yield_to_decisions() {
    for command in ["/skills", "/mcp"] {
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui(command, true);
        let active = ui.state.current_command.clone();
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(
            frame(&mut receiver)["type"],
            if command == "/skills" {
                "get_skills"
            } else {
                "get_mcp_status"
            }
        );
        ui.handle_input(key(KeyCode::Char('r')), &writer, 8192)
            .await
            .unwrap();
        assert!(receiver.try_recv().is_err());
        ui.editor.insert_paste("draft");
        ui.handle_input(Input::Paste("ignored".into()), &writer, 8192)
            .await
            .unwrap();
        ui.handle_input(
            Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert!(ui.discovery_view.is_none());
        assert!(!ui.state.cancel_requested);
        assert_eq!(ui.state.current_command, active);
        assert_eq!(ui.editor.text(), "draft");
        ui.discovery_view = Some(DiscoveryView::mcp());
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::TrustRequested {
                request_id: "t".into(),
                project_path: "/project".into(),
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert!(ui.discovery_view.is_none());
        assert_eq!(ui.editor.text(), "draft");
        assert_eq!(ui.state.view_status, ViewStatus::WaitingForTrust);
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn undersized_discovery_requests_fail_locally_without_ending_the_prompt() {
    for action in [UiAction::LoadSkills, UiAction::LoadMcpStatus] {
        let (writer, mut receiver) = mpsc::channel(8);
        let mut ui = ui("draft", true);
        ui.dispatch(action, &writer, 1).await.unwrap();
        assert!(!ui.state.skills.loading());
        assert!(!ui.state.mcp.loading());
        assert!(ui.state.skills.error.is_some() || ui.state.mcp.error.is_some());
        assert_eq!(ui.state.current_command.as_ref().unwrap().id, "prompt-1");
        assert_eq!(ui.editor.text(), "draft");
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn skill_insertion_overflow_preserves_browser_and_draft() {
    let (writer, mut receiver) = mpsc::channel(8);
    let mut ui = ui("", false);
    ui.state.skills.snapshot = Some(Arc::new(commands::tests::skills()));
    ui.discovery_view = Some(DiscoveryView::skills(ui.state.skills.snapshot.as_deref()));
    ui.editor
        .insert_paste(&"x".repeat(prompt_editor::MAX_PROMPT_BYTES));
    let original = ui.editor.text().to_owned();
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), original);
    assert!(ui.discovery_view.is_some());
    assert!(ui.notice.is_some());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn context_during_prompt_is_cached_and_dismissal_preserves_ownership() {
    let (writer, mut receiver) = mpsc::channel(8);
    let mut ui = ui("/context", true);
    let active = ui.state.current_command.clone();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.context_view.is_some());
    assert!(receiver.try_recv().is_err());
    assert!(draw(&mut ui, 30, 8).contains("Context"));
    ui.editor.insert_paste("preserved draft");
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.context_view.is_none());
    assert_eq!(ui.state.current_command, active);
    assert_eq!(ui.editor.text(), "preserved draft");
    assert!(receiver.try_recv().is_err());
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "trust-1".into(),
            project_path: "/project".into(),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.context_view.is_none());
}

#[tokio::test]
async fn compact_preflight_preserves_input_and_bare_command_cancels_its_own_id() {
    let (writer, mut receiver) = mpsc::channel(8);
    let mut ui = ui("/compact", false);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/compact");
    assert!(receiver.try_recv().is_err());
    ui.state.selected_session = Some(reducer::SessionIdentity {
        session_id: "session-1".into(),
        session_path: "/session-1.jsonl".into(),
        session_name: None,
    });
    ui.handle_input(key(KeyCode::Enter), &writer, 1)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/compact");
    assert!(ui.state.current_command.is_none());
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let command = frame(&mut receiver);
    assert_eq!(command["type"], "compact");
    assert!(command.get("instructions").is_none());
    assert!(ui.editor.text().is_empty());
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let cancel = frame(&mut receiver);
    assert_eq!(cancel["type"], "cancel");
    assert_eq!(cancel["target_id"], command["id"]);
}

#[tokio::test]
async fn partial_enter_completes_then_exact_enter_opens_help() {
    let (writer, mut receiver) = mpsc::channel(8);
    let mut ui = ui("/he", false);
    assert!(draw(&mut ui, 30, 8).contains("/help"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/help");
    assert!(ui.command_help.is_none());
    assert!(receiver.try_recv().is_err());
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.command_help.is_some());
    assert_eq!(frame(&mut receiver)["type"], "get_commands");
    assert!(draw(&mut ui, 30, 8).contains("Commands"));
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandCatalogReported {
            command_id: "get_commands-1".into(),
            catalog: commands::tests::catalog().into(),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: "get_commands-1".into(),
            command_type: "get_commands".into(),
            ok: true,
            error: None,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.command_help.is_none());
}

#[tokio::test]
async fn hidden_or_changed_completion_cannot_fill_and_escape_dismisses_only_menu() {
    let (writer, mut receiver) = mpsc::channel(8);
    let mut ui = ui("/mo", true);
    draw(&mut ui, 30, 8);
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/mo");
    draw(&mut ui, 29, 7);
    ui.handle_input(key(KeyCode::Tab), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/mo");
    draw(&mut ui, 30, 8);
    ui.apply_effects(vec![UiEffect::CommandCatalogChanged], &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Tab), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "/mo");
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert!(!ui.state.cancel_requested);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn supported_invalid_and_unsupported_commands_never_enter_active_queues() {
    for text in [
        "/new",
        "/resume",
        "/name hi",
        "/clone",
        "/tree",
        "/unrevert",
        "/connect",
        "/model",
        "/provider",
        "/plan",
        "/build",
        "/compact guidance",
        "/missing",
        "/quit extra",
    ] {
        for modifiers in [KeyModifiers::NONE, KeyModifiers::ALT] {
            let (writer, mut receiver) = mpsc::channel(8);
            let mut ui = ui(text, true);
            assert_eq!(
                ui.handle_input(
                    Input::Key(KeyEvent::new(KeyCode::Enter, modifiers)),
                    &writer,
                    8192
                )
                .await
                .unwrap(),
                LoopControl::Continue
            );
            assert_eq!(ui.editor.text(), text);
            assert!(ui.notice.is_some(), "{text}");
            assert!(receiver.try_recv().is_err(), "{text}");
        }
    }
}

#[tokio::test]
async fn quit_exits_active_run_and_help_ctrl_c_only_dismisses() {
    for text in ["/quit", "/exit", ":q"] {
        let (writer, mut receiver) = mpsc::channel(8);
        let mut ui = ui(text, true);
        assert_eq!(
            ui.handle_input(key(KeyCode::Enter), &writer, 8192)
                .await
                .unwrap(),
            LoopControl::Exit
        );
        assert!(receiver.try_recv().is_err());
    }
    let (writer, _) = mpsc::channel(8);
    let mut ui = ui("/help", true);
    ui.command_help = Some(Help::default());
    assert_eq!(
        ui.handle_input(
            Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
            &writer,
            8192
        )
        .await
        .unwrap(),
        LoopControl::Continue
    );
    assert!(ui.command_help.is_none());
    assert!(!ui.state.cancel_requested);
}

#[tokio::test]
async fn mode_acknowledgement_controls_header_and_draft_and_ctrl_c_remains_available() {
    for ok in [true, false] {
        let (writer, mut receiver) = mpsc::channel(8);
        let mut ui = ui("/plan", false);
        ui.state.mode_confirmed = true;
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        let command = frame(&mut receiver);
        assert_eq!(command["mode"], "plan");
        assert_eq!(ui.editor.text(), "/plan");
        assert_eq!(ui.state.mode, AgentMode::Build);
        assert_eq!(
            ui.handle_input(
                Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
                &writer,
                8192
            )
            .await
            .unwrap(),
            LoopControl::Exit
        );
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: command["id"].as_str().unwrap().into(),
                command_type: "configure".into(),
                ok,
                error: None,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        assert_eq!(ui.editor.text(), if ok { "" } else { "/plan" });
        let rendered = draw(&mut ui, 80, 18);
        assert!(
            rendered.contains(if ok { "plan" } else { "build" }),
            "{rendered}"
        );
    }
}

#[tokio::test]
async fn incoming_trust_dismisses_help_and_completion_without_answering() {
    let (writer, mut receiver) = mpsc::channel(8);
    let mut ui = ui("/he", false);
    draw(&mut ui, 80, 24);
    ui.command_help = Some(Help::default());
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::TrustRequested {
            request_id: "trust-1".into(),
            project_path: "/workspace".into(),
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.command_help.is_none());
    assert!(
        ui.completion
            .view(
                ui.state.command_catalog.as_deref(),
                ui.state.skills.snapshot.as_deref()
            )
            .is_none()
    );
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    assert_eq!(ui.editor.text(), "/he");
}

#[tokio::test]
async fn tool_browsing_hides_completion_until_composer_regains_focus() {
    let (writer, _) = mpsc::channel(8);
    let mut ui = ui("/mo", false);
    let event = BackendEvent::from_projection_value(&serde_json::json!({
        "type": "tool.call", "call_id": "tool-1", "name": "edit", "arguments": {"path": "README.md", "edits": [{"oldText": "old", "newText": "new"}]}
    }))
    .unwrap();
    ui.dispatch(UiAction::BackendEvent(event), &writer, 8192)
        .await
        .unwrap();
    let event = BackendEvent::from_projection_value(&serde_json::json!({
        "type": "tool.result", "message_entry_id": null, "call_id": "tool-1", "name": "edit", "output": "Applied 1 edit", "is_error": false
    })).unwrap();
    ui.dispatch(UiAction::BackendEvent(event), &writer, 8192)
        .await
        .unwrap();
    assert!(draw(&mut ui, 80, 24).contains("Describe model"));
    ui.handle_input(key(KeyCode::F(6)), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.browse_selected.is_some());
    assert!(!draw(&mut ui, 80, 24).contains("Describe model"));
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert!(draw(&mut ui, 80, 24).contains("Describe model"));
}
