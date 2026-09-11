//! Reference semantics and real input/render/reducer integration.

use super::*;
use crate::{
    Input, LiveUi, LoopControl, WriterMessage,
    reducer::{BackendEvent, UiAction},
    ui::ConnectionInfo,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use tokio::sync::mpsc;
use wisp_protocol::events::ProjectFileEntry;

fn editor(text: &str) -> PromptEditor {
    let mut editor = PromptEditor::default();
    editor.insert_paste(text);
    editor
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn snapshot(paths: &[(&str, ProjectFileKind)]) -> Arc<ProjectFileSnapshot> {
    Arc::new(ProjectFileSnapshot {
        generation: 1,
        entries: paths
            .iter()
            .map(|(path, kind)| ProjectFileEntry {
                path: (*path).into(),
                kind: *kind,
            })
            .collect(),
        truncated: false,
    })
}

fn frame(receiver: &mut mpsc::Receiver<WriterMessage>) -> Value {
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("expected JSONL command");
    };
    serde_json::from_slice(&payload).unwrap()
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

async fn load(
    ui: &mut LiveUi,
    writer: &mpsc::Sender<WriterMessage>,
    id: &str,
    snapshot: Arc<ProjectFileSnapshot>,
) {
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ProjectFilesReported {
            command_id: id.into(),
            snapshot: Ok(snapshot),
        }),
        writer,
        8192,
    )
    .await
    .unwrap();
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: "get_project_files".into(),
            ok: true,
            error: None,
        }),
        writer,
        8192,
    )
    .await
    .unwrap();
}

#[test]
fn reference_format_matches_shared_python_cases() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/tui_file_references.json"
    ))
    .unwrap();
    for case in cases.as_array().unwrap() {
        assert_eq!(
            format_reference(case["path"].as_str().unwrap()),
            case["reference"]
        );
    }
}

#[test]
fn token_detection_preserves_boundaries_quotes_multiline_and_suffixes() {
    for text in [
        "",
        "email@example.com",
        "prefix@file",
        "done @file ",
        "@\"some file\" next",
    ] {
        assert!(reference(&editor(text)).is_none(), "{text}");
    }
    for (text, query) in [
        ("@", ""),
        ("Explain @run", "run"),
        ("first line\n@资料", "资料"),
        ("@\"example fi", "example fi"),
        ("@\"a \\\"quote", "a \"quote"),
        ("@\"example file.py\"", "example file.py"),
    ] {
        assert_eq!(reference(&editor(text)).unwrap().query, query);
    }
    let mut edit = editor("prefix @runner.rs suffix");
    for _ in 0.."ner.rs suffix".len() {
        edit.handle_key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
    }
    let token = reference(&edit).unwrap();
    assert_eq!(token.query, "run");
    assert_eq!(&edit.text()[token.range.clone()], "@runner.rs");
    assert!(edit.replace_range(token.range, "@src/runner.rs").changed);
    assert_eq!(edit.text(), "prefix @src/runner.rs suffix");
}

#[test]
fn range_replacement_rejects_limits_and_invalid_boundaries_atomically() {
    let mut edit = editor("資料 @x");
    let before = edit.clone();
    assert!(edit.replace_range(1..2, "bad").rejected_limit);
    assert_eq!(edit, before);
    assert!(
        edit.replace_range(0..0, &"x".repeat(crate::prompt_editor::MAX_PROMPT_BYTES))
            .rejected_limit
    );
    assert!(
        edit.replace_range(0..0, &"\n".repeat(crate::prompt_editor::MAX_PROMPT_LINES))
            .rejected_limit
    );
    assert_eq!(edit, before);
}

#[test]
fn fuzzy_matching_is_smart_case_bounded_and_stable() {
    assert!(score("src/FooBar.rs", "FB", true).is_some());
    assert!(score("src/foobar.rs", "FB", true).is_none());
    assert!(score("src/FooBar.rs", "fb", false).is_some());
    assert!(score("src/资料.rs", "资料", false).is_some());
    assert!(score("src/main.rs", "missing", false).is_none());
    let mut picker = FilePicker::default();
    picker.sync_editor(&editor("@"));
    let data = Arc::new(ProjectFileSnapshot {
        generation: 1,
        entries: (0..10_000)
            .map(|i| ProjectFileEntry {
                path: format!("file{i:05}.rs"),
                kind: ProjectFileKind::File,
            })
            .collect(),
        truncated: true,
    });
    picker.sync_snapshot(Some(&data));
    assert_eq!(picker.rows.len(), RESULT_LIMIT);
    assert_eq!(picker.rows[0], 0);
    picker.sync_editor(&editor("@9999"));
    assert_eq!(picker.rows, vec![9999]);
    picker.sync_editor(&editor(&format!("@{}", "x".repeat(QUERY_BYTES_LIMIT + 1))));
    assert!(picker.rows.is_empty(), "never silently truncate the query");
}

#[test]
fn tree_navigation_uses_only_snapshot_entries_and_keeps_query() {
    let mut picker = FilePicker::default();
    picker.sync_editor(&editor("@runner"));
    let data = snapshot(&[
        ("src", ProjectFileKind::Directory),
        ("src/agent", ProjectFileKind::Directory),
        ("src/agent/runner.rs", ProjectFileKind::File),
        ("tests", ProjectFileKind::Directory),
    ]);
    picker.sync_snapshot(Some(&data));
    assert_eq!(picker.rows, vec![2]);
    picker.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
    assert!(picker.tree);
    assert_eq!(picker.rows, vec![0, 1, 2, 3]);
    assert_eq!(picker.rows[picker.selected], 2);
    picker.rendered = Some(2);
    picker.handle_key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
    assert_eq!(picker.rows[picker.selected], 1);
    picker.rendered = Some(1);
    picker.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(picker.rows, vec![0, 1, 3]);
    picker.handle_key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
    assert!(!picker.tree);
    assert_eq!(picker.rows, vec![2]);
    assert_eq!(picker.context.as_ref().unwrap().query, "runner");
}

#[test]
fn fuzzy_directory_selection_inserts_the_same_reference_as_textual() {
    let mut picker = FilePicker::default();
    picker.sync_editor(&editor("@sr"));
    picker.sync_snapshot(Some(&snapshot(&[("src", ProjectFileKind::Directory)])));
    picker.rendered = Some(0);
    let PickerAction::Replace(range, text) =
        picker.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE))
    else {
        panic!("expected local directory reference");
    };
    assert_eq!(range, 0..3);
    assert_eq!(text, "@src/");
    picker.sync_editor(&editor("@src/"));
    assert_eq!(
        picker.rows,
        vec![0],
        "match the displayed directory spelling"
    );
}

#[tokio::test]
async fn oversized_valid_reports_fail_recoverably_without_retaining_paths() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("@"),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    let fixtures: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/rpc_project_files.json"
    ))
    .unwrap();
    let mut report = fixtures["report"].clone();
    report["command_id"] = id.clone().into();
    report["entries"] = (0..1000)
        .map(|index| {
            json!({
                "path": format!("file-{index:04}-{}", "x".repeat(1200)), "kind": "file",
            })
        })
        .collect();
    let projected = BackendEvent::from_projection_value(&report).unwrap();
    let BackendEvent::ProjectFilesReported { snapshot, .. } = &projected else {
        panic!("project report");
    };
    assert!(snapshot.as_ref().unwrap_err().contains("1 MiB"));
    ui.dispatch(UiAction::BackendEvent(projected), &writer, 8192)
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
    assert!(ui.state.project_files.snapshot.is_none());
    assert!(!ui.state.project_files.loading());
    assert!(ui.file_picker.rows.is_empty());
    assert!(draw(&mut ui, 80, 24).contains("1 MiB"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@");
    assert!(receiver.try_recv().is_err(), "no uncontrolled retry");
}

#[tokio::test]
async fn picker_works_during_a_run_and_preserves_modified_follow_up_submission() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("active prompt"),
        ..LiveUi::default()
    };
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    frame(&mut receiver);
    let active = ui.state.current_command.clone();
    ui.handle_input(Input::Paste("inspect @file".into()), &writer, 8192)
        .await
        .unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("file.rs", ProjectFileKind::File)]),
    )
    .await;
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "inspect @file.rs ");
    assert!(
        receiver.try_recv().is_err(),
        "plain Enter selects, not steers"
    );
    assert_eq!(ui.state.current_command, active);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("file.rs", ProjectFileKind::File)]),
    )
    .await;
    assert!(ui.file_picker.is_open());
    let write = tokio::spawn(async move {
        let WriterMessage::Frame { payload, ack, .. } = receiver.recv().await.unwrap() else {
            panic!("queued command");
        };
        ack.unwrap().send(Ok(())).unwrap();
        serde_json::from_slice::<Value>(&payload).unwrap()
    });
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let command = write.await.unwrap();
    assert_eq!(command["type"], "follow_up");
    assert_eq!(command["content"], "inspect @file.rs ");
    assert_eq!(ui.state.current_command, active);
    assert!(!ui.file_picker.is_open());
}

#[tokio::test]
async fn short_multiline_draft_and_empty_or_limited_results_keep_controls_reachable() {
    let (writer, mut receiver) = mpsc::channel(16);
    let draft = "line1\nline2\nline3\nline4\nline5\n@";
    let mut ui = LiveUi {
        editor: editor(draft),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    let empty = Arc::new(ProjectFileSnapshot {
        generation: 1,
        entries: vec![],
        truncated: true,
    });
    load(&mut ui, &writer, &id, empty).await;
    assert!(draw(&mut ui, 30, 8).contains("limited"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), draft);
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert!(!ui.file_picker.is_open());
    ui.handle_input(key(KeyCode::Tab), &writer, 8192)
        .await
        .unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("file.rs", ProjectFileKind::File)]),
    )
    .await;
    assert!(draw(&mut ui, 30, 8).contains("@ files: fuzzy"));
    assert!(ui.file_picker.rendered.is_some());
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.editor.text().ends_with("@file.rs "));
}

#[tokio::test]
async fn insertion_requires_paint_preserves_draft_and_never_submits_in_the_same_action() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("Explain @example"),
        ..LiveUi::default()
    };
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let request = frame(&mut receiver);
    assert_eq!(request["type"], "get_project_files");
    assert_eq!(ui.editor.text(), "Explain @example");
    let paths = snapshot(&[("src/example file.py", ProjectFileKind::File)]);
    load(&mut ui, &writer, request["id"].as_str().unwrap(), paths).await;
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "Explain @example", "unpainted report");
    assert!(draw(&mut ui, 80, 24).contains("src/example file.py"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "Explain @\"src/example file.py\" ");
    assert!(!ui.file_picker.is_open());
    assert!(receiver.try_recv().is_err(), "insertion is local");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let submitted = frame(&mut receiver);
    assert_eq!(submitted["type"], "prompt");
    assert_eq!(submitted["prompt"], "Explain @\"src/example file.py\" ");
}

#[tokio::test]
async fn resize_changed_query_and_policy_invalidation_retire_the_rendered_selection() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("@"),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[
            ("one", ProjectFileKind::File),
            ("two", ProjectFileKind::File),
        ]),
    )
    .await;
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@");
    draw(&mut ui, 80, 24);
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@");
    draw(&mut ui, 80, 24);
    ui.handle_input(Input::Paste("one".into()), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@one");
    draw(&mut ui, 80, 24);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::ProjectFilesInvalidated(2)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert_eq!(frame(&mut receiver)["type"], "get_project_files");
    assert!(ui.file_picker.snapshot.is_none());
    assert!(ui.file_picker.rows.is_empty());
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@one");
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn buffered_invalidation_precedes_file_activation_even_with_queued_input() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("@"),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("old-authority", ProjectFileKind::File)]),
    )
    .await;
    draw(&mut ui, 80, 24);
    let (event_tx, mut events) = mpsc::channel(crate::EVENT_CHANNEL_CAPACITY);
    let budget = Arc::new(tokio::sync::Semaphore::new(crate::EVENT_CHANNEL_CAPACITY));
    for index in 0..crate::EVENT_CHANNEL_CAPACITY {
        event_tx
            .send(crate::QueuedEvent {
                event: if index + 1 == crate::EVENT_CHANNEL_CAPACITY {
                    BackendEvent::ProjectFilesInvalidated(2)
                } else {
                    BackendEvent::Other {
                        event_type: format!("unrelated-{index}"),
                    }
                },
                _wire_bytes: budget.clone().acquire_owned().await.unwrap(),
            })
            .await
            .unwrap();
    }
    for _ in 0..2 {
        ui.handle_received_input(key(KeyCode::Enter), &mut events, &writer, 8192)
            .await
            .unwrap();
    }
    assert!(events.is_empty());
    assert_eq!(ui.editor.text(), "@");
    assert!(ui.file_picker.snapshot.is_none());
    assert_eq!(frame(&mut receiver)["type"], "get_project_files");
    assert!(
        receiver.try_recv().is_err(),
        "neither key may submit stale content"
    );
    assert_eq!(budget.available_permits(), crate::EVENT_CHANNEL_CAPACITY);
}

#[tokio::test]
async fn escape_closes_first_tab_reopens_and_late_reports_do_not_reopen() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("@file"),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    assert_eq!(
        ui.handle_input(key(KeyCode::Esc), &writer, 8192)
            .await
            .unwrap(),
        LoopControl::Continue
    );
    assert_eq!(ui.editor.text(), "@file");
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("file", ProjectFileKind::File)]),
    )
    .await;
    assert!(!ui.file_picker.is_open());
    assert!(ui.file_picker.snapshot.is_none());
    assert!(receiver.try_recv().is_err());
    ui.handle_input(key(KeyCode::Tab), &writer, 8192)
        .await
        .unwrap();
    assert!(ui.file_picker.is_open());
    assert_eq!(frame(&mut receiver)["type"], "get_project_files");
}

#[tokio::test]
async fn popup_keeps_transcript_geometry_and_remains_reachable_at_short_sizes() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi::default();
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"background output\n".repeat(100));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::PageUp), &writer, 8192)
        .await
        .unwrap();
    let viewport = ui.transcript_viewport.clone();
    ui.handle_input(Input::Paste("@file".into()), &writer, 8192)
        .await
        .unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("file", ProjectFileKind::File)]),
    )
    .await;
    draw(&mut ui, 80, 24);
    assert_eq!(ui.transcript_viewport, viewport);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageDelta {
            turn: 1,
            delta: "continued output\n".into(),
            content_kind: crate::reducer::MessageContentKind::Text,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(!ui.transcript_viewport.follows_tail());
    for (width, height) in [(80, 24), (30, 8), (120, 40)] {
        assert!(draw(&mut ui, width, height).contains("@ files: fuzzy"));
        assert!(ui.file_picker.rendered.is_some());
    }
    draw(&mut ui, 29, 7);
    assert!(ui.file_picker.rendered.is_none());
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@file");
}

#[tokio::test]
async fn trust_and_modals_preempt_files_without_leaking_input_or_retained_paths() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("@file"),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 8192).await.unwrap();
    let id = frame(&mut receiver)["id"].as_str().unwrap().to_owned();
    load(
        &mut ui,
        &writer,
        &id,
        snapshot(&[("file", ProjectFileKind::File)]),
    )
    .await;
    draw(&mut ui, 80, 24);
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(ui.prompt_history_view.is_some());
    assert!(!ui.file_picker.is_open());
    assert!(ui.file_picker.snapshot.is_none());
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Tab), &writer, 8192)
        .await
        .unwrap();
    frame(&mut receiver);
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
    assert!(!ui.file_picker.is_open());
    assert!(!ui.state.project_files.is_open());
    assert_eq!(ui.editor.text(), "@file");
}

#[tokio::test]
async fn frame_limit_failure_is_recoverable_without_submitting_or_losing_draft() {
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = LiveUi {
        editor: editor("@file"),
        ..LiveUi::default()
    };
    ui.handle_input(Input::Redraw, &writer, 1).await.unwrap();
    assert!(ui.state.project_files.error.is_some());
    assert!(!ui.state.project_files.loading());
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Enter), &writer, 1)
        .await
        .unwrap();
    assert_eq!(ui.editor.text(), "@file");
    assert!(receiver.try_recv().is_err());
}

#[test]
fn real_project_discovery_fixture_projects_to_reducer_events() {
    let fixtures: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/rpc_project_files.json"
    ))
    .unwrap();
    assert!(matches!(
        BackendEvent::from_projection_value(&fixtures["report"]).unwrap(),
        BackendEvent::ProjectFilesReported { .. }
    ));
    assert_eq!(
        BackendEvent::from_projection_value(&fixtures["invalidated"]).unwrap(),
        BackendEvent::ProjectFilesInvalidated(2)
    );
    let mut invalid = fixtures["report"].clone();
    invalid["entries"][0]["path"] = json!("\u{1b}]52;clipboard");
    assert!(BackendEvent::from_projection_value(&invalid).is_err());
}
