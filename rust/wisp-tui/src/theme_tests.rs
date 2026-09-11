//! Theme selection never owns runtime state; persistence is tested in isolated homes.

use super::*;
use ratatui::{
    backend::TestBackend,
    buffer::Buffer,
    style::{Color, Modifier},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

struct TestHome(PathBuf);

impl TestHome {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        loop {
            let path = std::env::temp_dir().join(format!(
                "wisp-theme-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("create isolated theme home: {error}"),
            }
        }
    }
    fn path(&self) -> PathBuf {
        self.0.join(".wisp/tui.json")
    }
    fn store(&self) -> ThemePreferences {
        ThemePreferences::at(self.path())
    }
    fn write(&self, bytes: &[u8]) {
        fs::create_dir_all(self.path().parent().unwrap()).unwrap();
        fs::write(self.path(), bytes).unwrap();
    }
    fn document(&self) -> Value {
        serde_json::from_slice(&fs::read(self.path()).unwrap()).unwrap()
    }
}

impl Drop for TestHome {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
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

fn ui(home: &TestHome) -> LiveUi {
    LiveUi {
        theme: home.store().load(),
        theme_preferences: Some(home.store()),
        ..LiveUi::default()
    }
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

fn text(buffer: &Buffer) -> String {
    buffer.content.iter().map(|cell| cell.symbol()).collect()
}

#[test]
fn catalog_is_ordered_resolvable_and_monochrome_keeps_all_roles_achromatic() {
    assert_eq!(
        theme::themes()
            .iter()
            .map(|theme| theme.slug.as_str())
            .collect::<Vec<_>>(),
        [
            "vapor", "orchid", "ember", "storm", "grove", "wave", "paper", "dawn"
        ]
    );
    assert_eq!(theme::default_theme().name, "wisp");
    assert_eq!(theme::paper_theme().name, "wisp-light");
    for theme in theme::themes() {
        assert_eq!(
            theme::resolve(&theme.slug.to_uppercase()).unwrap().name,
            theme.name
        );
        assert_eq!(theme::named(&theme.name).unwrap().slug, theme.slug);
        let color = theme.palette(false);
        let gray = theme.palette(true);
        let channels = |palette: Palette| {
            [
                palette.background,
                palette.foreground,
                palette.primary,
                palette.secondary,
                palette.accent,
                palette.success,
                palette.warning,
                palette.error,
                palette.surface,
                palette.panel,
                palette.muted,
                palette.addition,
                palette.addition_background,
                palette.deletion,
                palette.deletion_background,
            ]
        };
        for (index, (original, mono)) in channels(color).into_iter().zip(channels(gray)).enumerate()
        {
            let Color::Rgb(r, g, b) = original else {
                panic!("generated RGB")
            };
            let value = (0.2126 * f64::from(r) + 0.7152 * f64::from(g) + 0.0722 * f64::from(b))
                .round() as u8;
            let Color::Rgb(r, g, b) = mono else {
                panic!("monochrome RGB")
            };
            assert_eq!(r, g);
            assert_eq!(g, b);
            if [0, 8, 9, 12, 14].contains(&index) {
                assert_eq!(
                    mono,
                    Color::Rgb(value, value, value),
                    "backgrounds retain Textual's conversion"
                );
            }
        }
        for foreground in [
            gray.foreground,
            gray.primary,
            gray.secondary,
            gray.accent,
            gray.success,
            gray.error,
            gray.muted,
        ] {
            for background in [gray.background, gray.surface] {
                assert!(theme::contrast_ratio(foreground, background) >= 4.5);
            }
        }
        assert!(theme::contrast_ratio(gray.warning, gray.panel) >= 4.5);
        assert!(theme::contrast_ratio(gray.addition, gray.addition_background) >= 4.5);
        assert!(theme::contrast_ratio(gray.deletion, gray.deletion_background) >= 4.5);
        assert!(gray.selection().add_modifier.contains(Modifier::REVERSED));
        assert_eq!(color.selection().fg, Some(color.background));
        assert_eq!(color.selection().bg, Some(color.primary));
    }
    assert!(theme::resolve("not-a-theme").is_none());
    assert!(
        theme::named("paper").is_none(),
        "stored names, not command aliases"
    );
}

#[test]
fn preferences_round_trip_dark_history_and_preserve_unrelated_fields() {
    let home = TestHome::new();
    let store = home.store();
    assert_eq!(store.load().active.name, "wisp");
    home.write(
        br#"{"unrelated":{"keep":[1,2,3]},"theme":"wisp-wave","last_dark_theme":"wisp-wave"}"#,
    );
    let mut selection = store.load();
    selection.select(theme::resolve("dawn").unwrap());
    store.save(selection).unwrap();
    assert_eq!(home.document()["unrelated"], json!({"keep":[1,2,3]}));
    assert_eq!(home.document()["theme"], "wisp-dawn");
    assert_eq!(home.document()["last_dark_theme"], "wisp-wave");
    let mut reloaded = store.load();
    reloaded.toggle();
    assert_eq!(reloaded.active.slug, "wave");
    reloaded.toggle();
    assert_eq!(reloaded.active.slug, "paper");
    assert_eq!(
        fs::read_dir(home.path().parent().unwrap()).unwrap().count(),
        1,
        "no staged file remains"
    );
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(home.path()).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn unusable_preferences_fall_back_and_unreadable_bytes_are_not_overwritten() {
    let home = TestHome::new();
    for contents in [
        r#"{"theme":"dracula"}"#,
        r#"{"theme":42}"#,
        "[]",
        "{bad json",
        "",
    ] {
        home.write(contents.as_bytes());
        assert_eq!(home.store().load().active.slug, "vapor");
        home.store().save(ThemeSelection::default()).unwrap();
        assert_eq!(home.document()["theme"], "wisp");
    }
    for contents in [vec![0xff, 0xfe], vec![b' '; 70 * 1024]] {
        home.write(&contents);
        assert_eq!(home.store().load().active.slug, "vapor");
        assert!(home.store().save(ThemeSelection::default()).is_err());
        assert_eq!(fs::read(home.path()).unwrap(), contents);
    }
    home.write(br#"{"theme":"wisp-orchid","last_dark_theme":"wisp-light"}"#);
    assert_eq!(home.store().load().last_dark.slug, "orchid");
    home.write(br#"{"theme":"wisp-dawn","last_dark_theme":"invalid"}"#);
    assert_eq!(home.store().load().last_dark.slug, "vapor");
}

#[test]
fn nonregular_preference_files_fail_without_waiting_for_a_writer() {
    let home = TestHome::new();
    fs::create_dir_all(home.path().parent().unwrap()).unwrap();
    assert!(
        std::process::Command::new("mkfifo")
            .arg(home.path())
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(home.store().load().active.slug, "vapor");
    assert!(home.store().save(ThemeSelection::default()).is_err());
    use std::os::unix::fs::FileTypeExt;
    assert!(
        fs::symlink_metadata(home.path())
            .unwrap()
            .file_type()
            .is_fifo()
    );
}

#[tokio::test]
async fn preview_cancel_and_toggle_do_not_commit_or_disturb_draft_or_viewport() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("keep this\ndraft 👩‍💻");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"**background** `code`\n".repeat(100));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::PageUp), &writer, 8192)
        .await
        .unwrap();
    let before = draw(&mut ui, 80, 24);
    let viewport = ui.transcript_viewport.clone();
    let editor = ui.editor.clone();
    let generation = ui.state.transcript.generation();
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    let preview = draw(&mut ui, 80, 24);
    assert_ne!(preview, before);
    assert_eq!(
        ui.palette(),
        theme::resolve("orchid").unwrap().palette(false)
    );
    ui.handle_input(ctrl('t'), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme.active.slug, "vapor");
    assert!(!home.path().exists(), "preview and Ctrl-T must not persist");
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(draw(&mut ui, 80, 24), before);
    assert_eq!(ui.transcript_viewport, viewport);
    assert_eq!(ui.editor, editor);
    assert_eq!(ui.state.transcript.generation(), generation);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn command_and_picker_commit_without_rpc_and_selection_survives_reload() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("/theme wave");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.theme.active.slug, "wave");
    assert_eq!(home.document()["theme"], "wisp-wave");
    assert!(ui.editor.text().is_empty());
    ui.handle_input(ctrl('t'), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme.active.slug, "paper");
    ui.handle_input(ctrl('t'), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme.active.slug, "wave");
    ui.editor.insert_paste("/theme");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui, 30, 8);
    ui.handle_input(key(KeyCode::End), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.theme.active.slug, "wave", "unpainted selection");
    assert!(text(&draw(&mut ui, 30, 8)).contains("Dawn"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(home.store().load().active.slug, "dawn");
    assert_eq!(home.store().load().last_dark.slug, "wave");
    assert!(ui.theme_picker.is_none());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn invalid_commands_stay_local_but_multiline_theme_text_remains_a_prompt() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    for command in ["/theme invalid", "/theme paper extra"] {
        ui.editor.clear();
        ui.editor.insert_paste(command);
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.editor.text(), command);
        assert_eq!(ui.theme.active.slug, "vapor");
        assert!(receiver.try_recv().is_err());
    }
    ui.editor.clear();
    let prompt = "/theme paper\nexplain this command";
    ui.editor.insert_paste(prompt);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("prompt")
    };
    assert_eq!(
        serde_json::from_slice::<Value>(&payload).unwrap()["prompt"],
        prompt
    );
    ui.editor.insert_paste("/theme paper");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.theme.active.slug, "paper",
        "theme remains local during a run"
    );
    assert!(receiver.try_recv().is_err(), "must not turn into steering");
}

#[tokio::test]
async fn save_failure_keeps_live_theme_and_existing_preferences_with_a_visible_warning() {
    let home = TestHome::new();
    home.write(&[0xff, 0xfe]);
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("/theme paper");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.theme.active.slug, "paper");
    assert!(ui.notice.as_ref().unwrap().contains("could not save"));
    assert_eq!(fs::read(home.path()).unwrap(), [0xff, 0xfe]);
    assert!(text(&draw(&mut ui, 80, 24)).contains("could not save"));
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn trust_and_new_workflow_displacement_roll_back_uncommitted_preview() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    for trust in [true, false] {
        let mut ui = ui(&home);
        ui.editor.insert_paste("draft");
        ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
        draw(&mut ui, 80, 24);
        ui.handle_input(key(KeyCode::End), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.palette(), theme::resolve("dawn").unwrap().palette(false));
        if trust {
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
        } else {
            ui.apply_effects(
                vec![UiEffect::ShowConnectionPanel(
                    ui.state.connection_catalog.clone(),
                )],
                &writer,
                8192,
            )
            .await
            .unwrap();
        }
        assert!(ui.theme_picker.is_none());
        assert_eq!(ui.palette(), theme::default_theme().palette(false));
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.theme.active.slug, "vapor");
        assert_eq!(ui.editor.text(), "draft");
        assert!(!home.path().exists());
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn theme_toggles_preserve_unsendable_approval_trust_and_cancellation_notices() {
    for kind in ["approval", "trust", "cancelling"] {
        let home = TestHome::new();
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui(&home);
        ui.state.view_status = ViewStatus::Running;
        ui.state.current_command = Some(reducer::ActiveCommand {
            id: "prompt-1".into(),
            command_type: reducer::ActiveCommandType::Prompt,
        });
        ui.state.cancel_requested = kind == "cancelling";
        let event = if kind == "trust" {
            BackendEvent::TrustRequested {
                request_id: "trust-1".into(),
                project_path: "/project".into(),
            }
        } else {
            BackendEvent::ToolApprovalRequested(PendingApproval {
                call_id: "call-1".into(),
                name: "bash".into(),
                arguments: json!({}),
                detail_source: reducer::ToolDetailSource::None,
                safety: "command".into(),
            })
        };
        ui.dispatch(UiAction::BackendEvent(event), &writer, 1)
            .await
            .unwrap();
        if kind != "cancelling" {
            ui.handle_input(key(KeyCode::Esc), &writer, 1)
                .await
                .unwrap();
        }
        assert!(ui.unsendable_current_response(), "{kind}");
        let notice = ui.notice.clone();
        assert!(notice.as_ref().unwrap().contains("Esc/Ctrl-C"));
        ui.handle_input(ctrl('t'), &writer, 1).await.unwrap();
        assert_eq!(ui.theme.active.slug, "paper");
        assert_eq!(ui.notice, notice, "{kind}");
        assert!(ui.unsendable_current_response());
        assert!(text(&draw(&mut ui, 100, 24)).contains("Esc/Ctrl-C"));
        assert_eq!(
            ui.handle_input(key(KeyCode::Esc), &writer, 1)
                .await
                .unwrap(),
            LoopControl::Exit
        );
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn opening_and_cancelling_preview_preserves_the_latest_runtime_notice() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.notice = Some("Queued work could not be restored.".into());
    ui.editor.insert_paste("/theme");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.notice.as_deref(),
        Some("Queued work could not be restored.")
    );
    draw(&mut ui, 80, 24);
    ui.apply_effects(
        vec![UiEffect::Notice("A newer backend warning.".into())],
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.notice.as_deref(), Some("A newer backend warning."));
    assert!(!home.path().exists());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn buffered_workflow_displaces_theme_before_enter_can_commit() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("keep this draft");
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui, 80, 24);
    let (sender, mut events) = mpsc::channel(16);
    sender
        .send(QueuedEvent {
            event: BackendEvent::TrustRequested {
                request_id: "new-trust".into(),
                project_path: "/project".into(),
            },
            _wire_bytes: Arc::new(Semaphore::new(1)).acquire_owned().await.unwrap(),
        })
        .await
        .unwrap();
    ui.handle_received_input(key(KeyCode::Enter), &mut events, &writer, 8192)
        .await
        .unwrap();
    assert!(ui.theme_picker.is_none());
    assert_eq!(ui.theme.active.slug, "vapor");
    assert_eq!(ui.editor.text(), "keep this draft");
    assert!(!home.path().exists());
    assert!(
        receiver.try_recv().is_err(),
        "Enter belongs to the displaced theme, not trust or draft"
    );
}

#[tokio::test]
async fn stale_device_detail_and_tree_results_do_not_displace_a_theme_preview() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    let stale_detail = ui.state.transcript.append_prompt("old".into());
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    let preview = ui.palette();
    for effect in [
        UiEffect::ShowDeviceCode(wisp_protocol::events::DeviceCodeChallenge {
            provider: "old-provider".into(),
            verification_uri: "https://example.test".into(),
            user_code: "OLD-CODE".into(),
        }),
        UiEffect::OpenExactDetail(stale_detail),
        UiEffect::ShowSessionTreePage {
            append: true,
            page: reducer::SessionTreePage {
                session: None,
                active_leaf_id: None,
                total_node_count: 0,
                nodes: vec![],
                truncated: false,
                next_after_entry_id: None,
            },
        },
    ] {
        ui.apply_effects(vec![effect], &writer, 8192).await.unwrap();
        assert!(ui.theme_picker.is_some());
        assert_eq!(ui.palette(), preview);
    }
    let mut panel = ConnectionPanel::new(ui.state.connection_catalog.clone());
    panel.begin_device_code("new-provider".into());
    ui.connection_panel = Some(panel);
    ui.apply_effects(
        vec![UiEffect::ShowDeviceCode(
            wisp_protocol::events::DeviceCodeChallenge {
                provider: "old-provider".into(),
                verification_uri: "https://example.test".into(),
                user_code: "OLD".into(),
            },
        )],
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(
        ui.theme_picker.is_some(),
        "wrong provider challenge is not presentation"
    );
    assert!(!home.path().exists());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn recoloring_during_streaming_preserves_semantic_caches_and_monochrome_is_consistent() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.state.transcript.start_message(1);
    ui.state.transcript.append_message_delta(
        1,
        "# Heading\n```rust\nfn main() { let text = \"hello\"; }\n```\n",
    );
    let generation = ui.state.transcript.generation();
    for selected in theme::themes() {
        ui.theme.select(selected);
        for no_color in [false, true] {
            ui.no_color = no_color;
            let frame = draw(&mut ui, 80, 24);
            assert!(text(&frame).contains("Heading"));
            assert_eq!(frame[(79, 0)].bg, selected.palette(no_color).background);
            if no_color {
                for cell in &frame.content {
                    for color in [cell.fg, cell.bg] {
                        let Color::Rgb(r, g, b) = color else {
                            panic!("explicit theme color: {color:?}")
                        };
                        assert_eq!(r, g);
                        assert_eq!(g, b);
                    }
                }
            }
        }
    }
    assert_eq!(ui.state.transcript.generation(), generation);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageDelta {
            turn: 1,
            delta: "stream continues".into(),
            content_kind: reducer::MessageContentKind::Text,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(text(&draw(&mut ui, 80, 24)).contains("stream continues"));
    assert!(receiver.try_recv().is_err());
}
