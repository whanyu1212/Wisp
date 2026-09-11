//! Conversation layering and modal input regressions.

use super::*;
use ratatui::{backend::TestBackend, buffer::Buffer, layout::Rect};
use reducer::{ActiveCommand, ActiveCommandType, InteractionStatus, MessageContentKind};
use serde_json::json;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{
    ConnectionCatalogSnapshot, ConnectionMethodSnapshot, ConnectionProviderSnapshot,
};

const KINDS: [OverlayKind; 9] = [
    OverlayKind::PromptHistory,
    OverlayKind::Discovery,
    OverlayKind::Context,
    OverlayKind::Help,
    OverlayKind::Model,
    OverlayKind::Connection,
    OverlayKind::SessionTree,
    OverlayKind::Session,
    OverlayKind::Detail,
];

fn base() -> LiveUi {
    let mut ui = LiveUi {
        state: UiState::new("fake".into(), None, None),
        ..LiveUi::default()
    };
    ui.editor.insert_paste("keep this\ndraft 👩‍💻");
    ui.editor
        .handle_key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
    ui.state
        .transcript
        .append_prompt("background prompt".into());
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"background row\n".repeat(60));
    ui
}

fn catalog(provider: &str) -> ConnectionCatalogSnapshot {
    ConnectionCatalogSnapshot {
        providers: vec![ConnectionProviderSnapshot {
            id: provider.into(),
            label: provider.into(),
            methods: vec![ConnectionMethodSnapshot {
                provider: provider.into(),
                label: "API key".into(),
                kind: "api_key".into(),
                source: "stored".into(),
                environment_variable: None,
                oauth_expires_at: None,
                has_stored_credential: true,
            }],
        }],
    }
}

fn session(id: &str) -> reducer::SessionSummary {
    reducer::SessionSummary {
        session_id: id.into(),
        session_path: format!("/{id}.jsonl"),
        name: Some(id.into()),
        updated_at: "2026-01-02T03:04:05Z".into(),
        entry_count: 1,
    }
}

fn tree(id: &str) -> reducer::SessionTreePage {
    reducer::SessionTreePage {
        session: None,
        active_leaf_id: None,
        total_node_count: 2,
        nodes: vec![reducer::SessionTreeNode {
            entry_id: id.into(),
            parent_id: None,
            created_at: "2026-01-02T03:04:05Z".into(),
            kind: reducer::SessionTreeNodeKind::Message,
            role: Some("user".into()),
            preview: id.into(),
            preview_truncated: false,
        }],
        truncated: true,
        next_after_entry_id: Some(id.into()),
    }
}

fn open(ui: &mut LiveUi, kind: OverlayKind) {
    match kind {
        OverlayKind::PromptHistory => {
            ui.prompt_history.record("earlier prompt".into());
            ui.open_prompt_history();
        }
        OverlayKind::Discovery => ui.discovery_view = Some(DiscoveryView::skills(None)),
        OverlayKind::Context => ui.context_view = Some(context_view::ContextView::default()),
        OverlayKind::Help => ui.command_help = Some(Help::default()),
        OverlayKind::Model => {
            let mut picker = ModelPicker::loading();
            picker.update_catalog(model_picker::tests::catalog());
            ui.model_picker = Some(picker);
        }
        OverlayKind::Connection => {
            ui.connection_panel = Some(ConnectionPanel::new(catalog("openai")))
        }
        OverlayKind::SessionTree => {
            ui.session_tree_picker = Some(SessionTreePicker::new(tree("entry-1")))
        }
        OverlayKind::Session => {
            ui.session_picker = Some(SessionPicker::new(
                vec![session("one"), session("two")],
                None,
            ))
        }
        OverlayKind::Detail => {
            let target = ui
                .state
                .transcript
                .observe_tool_call(tool_cards::ToolCallInput {
                    call_id: "read-1".into(),
                    name: "read".into(),
                    arguments: json!({"path":"README.md"}),
                    detail_source: tool_detail::ToolDetailSource::None,
                });
            let presentation = ToolDetailPresentation {
                kind: tool_detail::DetailPresentationKind::Read,
                title: "README.md".into(),
                summary: "retained detail".into(),
                additions: 0,
                deletions: 0,
                rows: vec![],
                truncated: false,
            };
            ui.detail_view.open(target, &presentation);
            ui.state.history.active_exact_detail = Some(reducer::ActiveExactDetail {
                target,
                presentation,
            });
        }
    }
}

fn draw(ui: &mut LiveUi, terminal: &mut Terminal<TestBackend>) {
    ui.draw(
        terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
            protocol_version: LIVE_RPC_PROTOCOL_VERSION,
            event_schema_version: EVENT_SCHEMA_VERSION,
        },
    )
    .unwrap();
}

fn text(buffer: &Buffer) -> String {
    buffer.content.iter().map(|cell| cell.symbol()).collect()
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}
fn ctrl_c() -> Input {
    Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL))
}
fn command(receiver: &mut mpsc::Receiver<WriterMessage>) -> serde_json::Value {
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().expect("one command") else {
        panic!("frame")
    };
    serde_json::from_slice(&payload).unwrap()
}

#[tokio::test]
async fn every_overlay_preserves_background_cells_draft_and_viewport_on_close() {
    for kind in KINDS {
        let mut ui = base();
        open(&mut ui, kind);
        let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
        // Render the same background independently; opening detail can add a transcript card.
        let mut baseline = Terminal::new(TestBackend::new(100, 32)).unwrap();
        let mut viewport = ui.transcript_viewport.clone();
        let mut cache = TranscriptRowCache::default();
        baseline
            .draw(|frame| {
                ui::render(
                    frame,
                    &ui.state,
                    &mut viewport,
                    &mut cache,
                    &ui.editor,
                    &ConnectionInfo {
                        backend_version: "test".into(),
                        protocol_version: 6,
                        event_schema_version: 37,
                    },
                    ui.notice.as_deref(),
                )
            })
            .unwrap();
        let draft = ui.editor.clone();
        draw(&mut ui, &mut terminal);
        let popup = ui::overlay_area(terminal.backend().buffer().area).unwrap();
        for y in 0..32 {
            for x in 0..100 {
                if !popup.contains((x, y).into()) {
                    assert_eq!(
                        terminal.backend().buffer()[(x, y)],
                        baseline.backend().buffer()[(x, y)],
                        "{kind:?} at {x},{y}"
                    );
                }
            }
        }
        assert_eq!(ui.transcript_viewport, viewport, "{kind:?}");
        let (writer, mut receiver) = mpsc::channel(16);
        ui.handle_input(key(KeyCode::Esc), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.active_overlay(), None, "{kind:?}");
        draw(&mut ui, &mut terminal);
        assert_eq!(ui.editor, draft, "{kind:?}");
        assert_eq!(ui.transcript_viewport, viewport, "{kind:?}");
        // Closing a connection panel intentionally publishes a footer notice.
        baseline
            .draw(|frame| {
                ui::render(
                    frame,
                    &ui.state,
                    &mut viewport,
                    &mut cache,
                    &ui.editor,
                    &ConnectionInfo {
                        backend_version: "test".into(),
                        protocol_version: 6,
                        event_schema_version: 37,
                    },
                    ui.notice.as_deref(),
                )
            })
            .unwrap();
        assert_eq!(
            terminal.backend().buffer(),
            baseline.backend().buffer(),
            "{kind:?}"
        );
        assert!(receiver.try_recv().is_err());
    }
}

#[test]
fn popup_clear_is_opaque_and_geometry_stays_bounded() {
    for (width, height) in [(29, 7), (30, 8), (30, 10), (30, 11), (80, 24), (160, 50)] {
        let area = Rect::new(0, 0, width, height);
        let mut ui = base();
        open(&mut ui, OverlayKind::Help);
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        draw(&mut ui, &mut terminal);
        if let Some(popup) = ui::overlay_area(area) {
            assert_eq!(area.intersection(popup), popup);
            assert!((30..=100).contains(&popup.width));
            assert!((8..=28).contains(&popup.height));
            // Render the popup alone to prove that none of the underlying cells survive inside it.
            let mut isolated = Terminal::new(TestBackend::new(width, height)).unwrap();
            isolated
                .draw(|frame| {
                    commands::render_help(
                        frame,
                        popup,
                        ui.command_help.as_ref().unwrap(),
                        None,
                        false,
                        None,
                    )
                })
                .unwrap();
            for y in popup.y..popup.bottom() {
                for x in popup.x..popup.right() {
                    assert_eq!(
                        terminal.backend().buffer()[(x, y)],
                        isolated.backend().buffer()[(x, y)]
                    );
                }
            }
        } else {
            assert!(text(terminal.backend().buffer()).contains("terminal too"));
            assert!(ui.rendered_overlay.is_none());
        }
    }
}

#[tokio::test]
async fn streaming_updates_background_without_changing_scroll_intent_or_eating_input() {
    for scrolled in [false, true] {
        let mut ui = base();
        ui.state.current_command = Some(ActiveCommand {
            id: "prompt-1".into(),
            command_type: ActiveCommandType::Prompt,
        });
        ui.state.view_status = ViewStatus::Running;
        ui.state.interaction_status = InteractionStatus::Running;
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        draw(&mut ui, &mut terminal);
        let (writer, mut receiver) = mpsc::channel(16);
        if scrolled {
            ui.handle_input(key(KeyCode::PageUp), &writer, 8192)
                .await
                .unwrap();
            draw(&mut ui, &mut terminal);
        }
        let draft = ui.editor.clone();
        let before = ui.transcript_viewport.clone();
        open(&mut ui, OverlayKind::Help);
        draw(&mut ui, &mut terminal);
        for input in [
            key(KeyCode::Char('x')),
            Input::Paste("must not enter composer".into()),
            key(KeyCode::Enter),
        ] {
            ui.handle_input(input, &writer, 8192).await.unwrap();
        }
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::MessageDelta {
                turn: 1,
                delta: "NEWBG\n".into(),
                content_kind: MessageContentKind::Text,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        draw(&mut ui, &mut terminal);
        assert_eq!(ui.editor, draft);
        assert_eq!(ui.transcript_viewport.follows_tail(), !scrolled);
        if scrolled {
            assert!(ui.transcript_viewport.has_unseen_output());
        } else {
            assert!(text(terminal.backend().buffer()).contains("NEWBG"));
        }
        let updated = ui.transcript_viewport.clone();
        ui.handle_input(key(KeyCode::Esc), &writer, 8192)
            .await
            .unwrap();
        draw(&mut ui, &mut terminal);
        assert_eq!(ui.transcript_viewport, updated);
        assert_eq!(ui.editor, draft);
        assert!(receiver.try_recv().is_err());
        if !scrolled {
            assert_ne!(updated, before);
        }
    }
}

#[tokio::test]
async fn decisions_preempt_every_overlay_even_before_the_next_frame() {
    for kind in KINDS {
        for trust in [false, true] {
            for height in [8, 10, 11, 24] {
                let mut ui = base();
                open(&mut ui, kind);
                ui.state.current_command = Some(ActiveCommand {
                    id: "prompt-1".into(),
                    command_type: ActiveCommandType::Prompt,
                });
                ui.state.view_status = ViewStatus::Running;
                let mut terminal = Terminal::new(TestBackend::new(80, height)).unwrap();
                draw(&mut ui, &mut terminal);
                let (writer, mut receiver) = mpsc::channel(16);
                let event = if trust {
                    BackendEvent::TrustRequested {
                        request_id: "trust-1".into(),
                        project_path: "/project".into(),
                    }
                } else {
                    BackendEvent::ToolApprovalRequested(PendingApproval {
                        call_id: "call-1".into(),
                        name: "read".into(),
                        arguments: json!({}),
                        detail_source: tool_detail::ToolDetailSource::None,
                        safety: "read".into(),
                    })
                };
                let draft = ui.editor.clone();
                ui.dispatch(UiAction::BackendEvent(event), &writer, 8192)
                    .await
                    .unwrap();
                assert_eq!(ui.active_overlay(), None);
                ui.handle_input(key(KeyCode::Char('y')), &writer, 8192)
                    .await
                    .unwrap();
                ui.handle_input(Input::Paste("not a secret".into()), &writer, 8192)
                    .await
                    .unwrap();
                assert!(receiver.try_recv().is_err());
                assert_eq!(ui.editor, draft);
                draw(&mut ui, &mut terminal);
                assert!(text(terminal.backend().buffer()).contains(if trust {
                    "trust required"
                } else {
                    "approval required"
                }));
                ui.handle_input(key(KeyCode::Char('y')), &writer, 8192)
                    .await
                    .unwrap();
                assert_eq!(
                    command(&mut receiver)["type"],
                    if trust { "trust" } else { "approval" }
                );
            }
        }
    }
}

#[tokio::test]
async fn session_replacement_navigation_and_resize_require_the_new_choice_to_be_drawn() {
    let mut ui = base();
    open(&mut ui, OverlayKind::Session);
    let (writer, mut receiver) = mpsc::channel(16);
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    for input in [key(KeyCode::Enter), key(KeyCode::Down), key(KeyCode::Enter)] {
        ui.handle_input(input, &writer, 8192).await.unwrap();
    }
    assert!(receiver.try_recv().is_err());
    draw(&mut ui, &mut terminal);
    ui.apply_effects(
        vec![UiEffect::ShowSessionPicker {
            sessions: vec![session("replacement")],
            selected_session_id: None,
        }],
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    draw(&mut ui, &mut terminal);
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    terminal.backend_mut().resize(29, 7);
    terminal.autoresize().unwrap();
    draw(&mut ui, &mut terminal);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    terminal.backend_mut().resize(80, 24);
    terminal.autoresize().unwrap();
    draw(&mut ui, &mut terminal);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(command(&mut receiver)["session_id"], "replacement");
}

#[tokio::test]
async fn connection_refresh_cannot_retarget_disconnect_before_redraw_and_secret_d_is_literal() {
    let mut ui = base();
    open(&mut ui, OverlayKind::Connection);
    let (writer, mut receiver) = mpsc::channel(16);
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    draw(&mut ui, &mut terminal);
    ui.apply_effects(
        vec![UiEffect::ConnectionCatalogUpdated(catalog("anthropic"))],
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Char('d')), &writer, 8192)
        .await
        .unwrap();
    assert!(receiver.try_recv().is_err());
    draw(&mut ui, &mut terminal);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    for c in "odd-secret".chars() {
        ui.handle_input(key(KeyCode::Char(c)), &writer, 8192)
            .await
            .unwrap();
    }
    assert_eq!(
        ui.connection_panel.as_ref().unwrap().pending_api_key(),
        Some(("anthropic", "odd-secret"))
    );
    assert!(!text(terminal.backend().buffer()).contains("odd-secret"));
    draw(&mut ui, &mut terminal);
    assert!(!text(terminal.backend().buffer()).contains("odd-secret"));
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.connection_panel.is_none());
    assert!(receiver.try_recv().is_err());
    assert!(!ui.editor.text().contains("secret"));
    assert!(ui.prompt_history.is_empty());
}

#[tokio::test]
async fn closed_tree_does_not_reopen_on_late_page_and_ctrl_c_keeps_its_existing_meaning() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = base();
    open(&mut ui, OverlayKind::SessionTree);
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    ui.apply_effects(
        vec![UiEffect::ShowSessionTreePage {
            page: tree("entry-2"),
            append: true,
        }],
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.session_tree_picker.is_none());
    assert!(receiver.try_recv().is_err());
    for kind in [
        OverlayKind::Session,
        OverlayKind::SessionTree,
        OverlayKind::Detail,
    ] {
        let mut ui = base();
        open(&mut ui, kind);
        assert_eq!(
            ui.handle_input(ctrl_c(), &writer, 8192).await.unwrap(),
            LoopControl::Exit
        );
    }
    let mut ui = base();
    open(&mut ui, OverlayKind::Help);
    assert_eq!(
        ui.handle_input(ctrl_c(), &writer, 8192).await.unwrap(),
        LoopControl::Continue
    );
    assert!(ui.command_help.is_none());
}

#[tokio::test]
async fn ignored_paste_cannot_leave_a_picker_waiting_forever_for_a_redraw() {
    for kind in [
        OverlayKind::Session,
        OverlayKind::SessionTree,
        OverlayKind::Model,
        OverlayKind::Discovery,
    ] {
        let mut ui = base();
        open(&mut ui, kind);
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        draw(&mut ui, &mut terminal);
        let (writer, mut receiver) = mpsc::channel(16);
        assert!(!ui.render_pending);
        ui.handle_input(Input::Paste("ignored".into()), &writer, 8192)
            .await
            .unwrap();
        assert!(
            ui.render_pending,
            "invalidated selections need a scheduled frame: {kind:?}"
        );
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert!(receiver.try_recv().is_err());
        draw(&mut ui, &mut terminal);
        assert_eq!(ui.rendered_overlay, Some(kind));
    }
}

#[tokio::test]
async fn cursor_belongs_to_the_focused_surface_and_returns_to_the_exact_draft_position() {
    let mut ui = base();
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    draw(&mut ui, &mut terminal);
    let position = terminal.backend_mut().get_cursor_position().unwrap();
    open(&mut ui, OverlayKind::Help);
    draw(&mut ui, &mut terminal);
    let mut hidden = terminal.backend().clone();
    hidden.hide_cursor().unwrap();
    assert_eq!(
        terminal.backend(),
        &hidden,
        "read-only modal must hide the composer cursor"
    );
    let (writer, _receiver) = mpsc::channel(16);
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui, &mut terminal);
    let mut shown = terminal.backend().clone();
    shown.show_cursor().unwrap();
    assert_eq!(terminal.backend(), &shown);
    assert_eq!(
        terminal.backend_mut().get_cursor_position().unwrap(),
        position
    );
    ui.open_prompt_history();
    draw(&mut ui, &mut terminal);
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    assert!(
        ui::overlay_area(terminal.backend().buffer().area)
            .unwrap()
            .contains(cursor)
    );
    assert_ne!(cursor, position);
    let mut shown = terminal.backend().clone();
    shown.show_cursor().unwrap();
    assert_eq!(terminal.backend(), &shown);
}

#[tokio::test]
async fn a_device_login_can_finish_while_suppressed_and_decision_keys_do_not_cancel_it() {
    let mut ui = base();
    open(&mut ui, OverlayKind::Connection);
    ui.connection_panel
        .as_mut()
        .unwrap()
        .begin_device_code("openai-codex".into());
    ui.state.current_command = Some(ActiveCommand {
        id: "prompt-1".into(),
        command_type: ActiveCommandType::Prompt,
    });
    ui.state.view_status = ViewStatus::Running;
    let (writer, mut receiver) = mpsc::channel(16);
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    draw(&mut ui, &mut terminal);
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
    ui.apply_effects(
        vec![
            UiEffect::ShowDeviceCode(wisp_protocol::events::DeviceCodeChallenge {
                provider: "openai-codex".into(),
                verification_uri: "https://example.invalid/device".into(),
                user_code: "CODE".into(),
            }),
            UiEffect::FinishDeviceCode,
        ],
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(ui.active_overlay(), None);
    draw(&mut ui, &mut terminal);
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(command(&mut receiver)["type"], "trust");
    assert!(
        receiver.try_recv().is_err(),
        "decision escape must not dispatch device-code cancellation"
    );
    assert!(ui.connection_panel.is_some());
    // Simulate the decision settling; the retained panel must show its current picker mode.
    ui.state.view_status = ViewStatus::Idle;
    ui.state.pending_trust_request_id = None;
    ui.state.current_command = None;
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(
        ui.connection_panel
            .as_ref()
            .unwrap()
            .pending_api_key()
            .is_none()
    );
    draw(&mut ui, &mut terminal);
    assert!(text(terminal.backend().buffer()).contains("API key"));
    assert!(!text(terminal.backend().buffer()).contains("CODE"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(
        !ui.connection_panel
            .as_ref()
            .unwrap()
            .key_activates(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::NONE)),
        "the completed login returns to an actionable picker after redraw"
    );
}

#[tokio::test]
async fn wide_characters_at_popup_edges_do_not_damage_the_popup_or_restored_transcript() {
    let (writer, _receiver) = mpsc::channel(16);
    for width in [80, 81, 82] {
        let mut ui = base();
        ui.state
            .transcript
            .append_message_delta(1, &format!("{}\n", "界".repeat(45)).repeat(40));
        let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
        draw(&mut ui, &mut terminal);
        let original = terminal.backend().buffer().clone();
        open(&mut ui, OverlayKind::Help);
        draw(&mut ui, &mut terminal);
        let popup = ui::overlay_area(terminal.backend().buffer().area).unwrap();
        let mut isolated = Terminal::new(TestBackend::new(width, 24)).unwrap();
        isolated
            .draw(|frame| {
                commands::render_help(
                    frame,
                    popup,
                    ui.command_help.as_ref().unwrap(),
                    None,
                    false,
                    None,
                )
            })
            .unwrap();
        for y in popup.y..popup.bottom() {
            for x in popup.x..popup.right() {
                assert_eq!(
                    terminal.backend().buffer()[(x, y)],
                    isolated.backend().buffer()[(x, y)],
                    "popup edge at {x},{y}, terminal width {width}"
                );
            }
        }
        ui.handle_input(key(KeyCode::Esc), &writer, 8192)
            .await
            .unwrap();
        draw(&mut ui, &mut terminal);
        // TestBackend retains old values under a wide glyph; terminals overwrite those
        // trailing cells when drawing the glyph. Compare every visible cell instead.
        for y in original.area.y..original.area.bottom() {
            let mut x = original.area.x;
            while x < original.area.right() {
                let expected = &original[(x, y)];
                assert_eq!(
                    &terminal.backend().buffer()[(x, y)],
                    expected,
                    "restored cell at {x},{y}, terminal width {width}"
                );
                x += expected.symbol().width().max(1) as u16;
            }
        }
    }
}
