//! The peek under a background process in the side panel: it reads the process's
//! output from the backend, and its keys open the process's card or stop it.

use super::*;
use crossterm::event::{MouseButton, MouseEventKind};
use ratatui::{backend::TestBackend, buffer::Buffer};

const WIDTH: u16 = 100;
const HEIGHT: u16 = 40;

/// A session whose agent started `npm run dev` as process `server`.
fn server_running() -> LiveUi {
    running(&[("server", "npm run dev")])
}

/// A session whose agent started these processes, by id and command, in order.
fn running(processes: &[(&str, &str)]) -> LiveUi {
    let mut ui = LiveUi::default();
    ui.state.transcript.append_prompt("start the server".into());
    for (process_id, command) in processes {
        let call_id = format!("call-{process_id}");
        backend(
            &mut ui,
            serde_json::json!({
                "type": "tool.call", "call_id": call_id, "name": "bash",
                "arguments": {"operation": "start", "command": command}
            }),
        );
        backend(
            &mut ui,
            serde_json::json!({
                "type": "tool.result", "call_id": call_id, "name": "bash",
                "output": "", "is_error": false, "process_id": process_id,
                "process_state": "running", "process_started_at_ms": 0
            }),
        );
    }
    ui
}

fn process_ended(ui: &mut LiveUi, process_id: &str, command: &str) {
    backend(
        ui,
        serde_json::json!({
            "type": "process.ended", "timestamp": "2026-11-15T09:00:00Z",
            "process_id": process_id, "command": command, "state": "completed",
            "exit_code": 0, "duration_ms": 1000, "notify": false
        }),
    );
}

fn backend(ui: &mut LiveUi, event: serde_json::Value) {
    let event = reducer::BackendEvent::from_projection_value(&event).unwrap();
    reducer::reduce(
        &mut ui.state,
        UiAction::BackendEvent(event),
        &mut SequentialCommandIds::default(),
    )
    .unwrap();
}

fn draw(ui: &mut LiveUi) -> Vec<String> {
    draw_at(ui, HEIGHT)
}

fn draw_at(ui: &mut LiveUi, height: u16) -> Vec<String> {
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    rows(terminal.backend().buffer())
}

fn rows(buffer: &Buffer) -> Vec<String> {
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect()
        })
        .collect()
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

/// Presses keys, then lets the panel report its peek as a frame does.
async fn press(ui: &mut LiveUi, writer: &mpsc::Sender<WriterMessage>, codes: &[KeyCode]) {
    for code in codes {
        ui.handle_input(key(*code), writer, 8192).await.unwrap();
        draw(ui);
        ui.sync_side_panel(writer, 8192).await.unwrap();
    }
}

fn sent(receiver: &mut mpsc::Receiver<WriterMessage>) -> Vec<serde_json::Value> {
    let mut commands = Vec::new();
    while let Ok(message) = receiver.try_recv() {
        if let WriterMessage::Frame { payload, .. } = message {
            commands.push(serde_json::from_slice(&payload).unwrap());
        }
    }
    commands
}

fn of_type<'a>(commands: &'a [serde_json::Value], kind: &str) -> Vec<&'a serde_json::Value> {
    commands
        .iter()
        .filter(|command| command["type"] == kind)
        .collect()
}

fn output_reply(command_id: &str, state: &str, text: &str) -> serde_json::Value {
    serde_json::json!({
        "type": "rpc.process_output", "command_id": command_id,
        "timestamp": "2026-11-15T09:00:00Z",
        "process": {
            "process_id": "server", "command": "npm run dev", "state": state,
            "exit_code": if state == "running" { None } else { Some(0) },
            "started_at_ms": 0, "ended_at_ms": null, "stopped_by_user": false,
            "notify": false
        },
        "chunks": [{"stream": "stdout", "text": text}],
        "omitted_bytes": 0
    })
}

/// Opens the panel on the process and its peek, and returns the output read sent.
async fn open_peek(
    ui: &mut LiveUi,
    writer: &mpsc::Sender<WriterMessage>,
    receiver: &mut mpsc::Receiver<WriterMessage>,
) -> String {
    ui.focus_background_processes();
    draw(ui);
    press(ui, writer, &[KeyCode::Enter]).await;
    let commands = sent(receiver);
    let reads = of_type(&commands, "get_process_output");
    assert_eq!(reads.len(), 1, "{commands:#?}");
    assert_eq!(reads[0]["process_id"], "server");
    reads[0]["id"].as_str().unwrap().to_owned()
}

#[tokio::test]
async fn peek_shows_latest_output() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    let read = open_peek(&mut ui, &writer, &mut receiver).await;
    backend(
        &mut ui,
        output_reply(
            &read,
            "running",
            "compiling\n\u{1b}[32mready\u{1b}[0m on :3000\n",
        ),
    );

    let rows = draw(&mut ui);
    let at = rows
        .iter()
        .position(|row| row.contains("1 ● npm run dev"))
        .expect("process row");
    assert!(rows[at + 1].contains("│ compiling"), "{rows:#?}");
    assert!(rows[at + 2].contains("│ ready on :3000"), "{rows:#?}");
    assert!(rows[at + 3].contains("x stop"), "{rows:#?}");
    assert!(!rows.iter().any(|row| row.contains('\u{1b}')), "{rows:#?}");
}

#[tokio::test]
async fn poll_reads_until_process_ends() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    let read = open_peek(&mut ui, &writer, &mut receiver).await;

    // One read at a time: a tick while the first is in flight sends nothing.
    ui.poll_process_peek(&writer, 8192).await.unwrap();
    assert!(sent(&mut receiver).is_empty());

    backend(&mut ui, output_reply(&read, "running", "one\n"));
    backend(
        &mut ui,
        serde_json::json!({"type": "rpc.command.finished", "command_id": read,
            "command_type": "get_process_output", "ok": true}),
    );
    ui.poll_process_peek(&writer, 8192).await.unwrap();
    let commands = sent(&mut receiver);
    let again = of_type(&commands, "get_process_output");
    assert_eq!(again.len(), 1, "{commands:#?}");

    // Once a reply shows the process ended, the peek stops reading and says how.
    let read = again[0]["id"].as_str().unwrap().to_owned();
    backend(&mut ui, output_reply(&read, "completed", "one\ndone\n"));
    backend(
        &mut ui,
        serde_json::json!({"type": "rpc.command.finished", "command_id": read,
            "command_type": "get_process_output", "ok": true}),
    );
    ui.poll_process_peek(&writer, 8192).await.unwrap();
    assert!(sent(&mut receiver).is_empty());
    let rows = draw(&mut ui);
    assert!(
        rows.iter().any(|row| row.contains("│ finished")),
        "{rows:#?}"
    );
    assert!(
        rows.iter().any(|row| row.contains("✓ npm run dev")),
        "{rows:#?}"
    );
}

#[tokio::test]
async fn stop_asks_first() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    open_peek(&mut ui, &writer, &mut receiver).await;

    // Any key but `y` keeps the process running.
    press(&mut ui, &writer, &[KeyCode::Char('x')]).await;
    assert!(
        draw(&mut ui)
            .iter()
            .any(|row| row.contains("Stop npm run dev?")),
        "{:#?}",
        draw(&mut ui)
    );
    press(&mut ui, &writer, &[KeyCode::Char('n')]).await;
    assert!(of_type(&sent(&mut receiver), "cancel_process").is_empty());

    press(&mut ui, &writer, &[KeyCode::Char('x'), KeyCode::Char('y')]).await;
    let commands = sent(&mut receiver);
    let stops = of_type(&commands, "cancel_process");
    assert_eq!(stops.len(), 1, "{commands:#?}");
    assert_eq!(stops[0]["process_id"], "server");
}

#[tokio::test]
async fn esc_closes_peek_first() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    open_peek(&mut ui, &writer, &mut receiver).await;

    press(&mut ui, &writer, &[KeyCode::Esc]).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    assert!(ui.state.background_processes.peek().is_none());
    assert!(!draw(&mut ui).iter().any(|row| row.contains("Enter card")));

    press(&mut ui, &writer, &[KeyCode::Esc]).await;
    assert_eq!(ui.active_overlay(), None);
}

#[tokio::test]
async fn enter_opens_process_card() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    let card = ui
        .state
        .transcript
        .process_card("server")
        .expect("start card");
    open_peek(&mut ui, &writer, &mut receiver).await;

    press(&mut ui, &writer, &[KeyCode::Enter]).await;

    assert_eq!(ui.active_overlay(), None);
    assert!(ui.notice.is_none(), "{:?}", ui.notice);
    assert!(ui.transcript_row_cache.fold().is_expanded(card));
    assert_eq!(
        ui.transcript_viewport
            .scrolled_top()
            .map(|top| top.entry_id),
        Some(card)
    );
}

#[tokio::test]
async fn rebuild_reads_again() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    open_peek(&mut ui, &writer, &mut receiver).await;

    // A transcript rebuild closes the reducer's peek while the panel keeps it open
    // on a process still listed; the next frame reads it again.
    reducer::reduce(
        &mut ui.state,
        UiAction::PeekProcess(None),
        &mut SequentialCommandIds::default(),
    )
    .unwrap();
    draw(&mut ui);
    ui.sync_side_panel(&writer, 8192).await.unwrap();

    let commands = sent(&mut receiver);
    assert_eq!(
        of_type(&commands, "get_process_output").len(),
        1,
        "{commands:#?}"
    );
    assert_eq!(
        ui.state
            .background_processes
            .peek()
            .map(|peek| peek.process_id.as_str()),
        Some("server")
    );
}

#[tokio::test]
async fn missing_card_says_so() {
    let (writer, _receiver) = mpsc::channel(32);
    let mut ui = LiveUi::default();
    ui.open_from_panel(side_panel::Target::Process("gone".into()), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.notice.as_deref(),
        Some("That process's card is not in the transcript.")
    );
}

#[tokio::test]
async fn peek_closes_when_unlisted() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    open_peek(&mut ui, &writer, &mut receiver).await;

    // The backend's list no longer has the process, as after a session change.
    backend(
        &mut ui,
        serde_json::json!({
            "type": "rpc.state", "command_id": "get_state-9",
            "timestamp": "2026-11-15T09:00:00Z",
            "state": {
                "provider": "fake", "auto_compaction_enabled": true,
                "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
                "pending_steering_count": 0, "pending_follow_up_count": 0,
                "background_processes": []
            }
        }),
    );
    draw(&mut ui);
    ui.sync_side_panel(&writer, 8192).await.unwrap();

    assert!(ui.state.background_processes.peek().is_none());
    assert!(!draw(&mut ui).iter().any(|row| row.contains("Enter card")));
}

#[tokio::test]
async fn stop_follows_peek() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = running(&[("a", "make a"), ("b", "make b"), ("c", "make c")]);
    draw(&mut ui);
    ui.focus_background_processes();
    draw(&mut ui);
    press(&mut ui, &writer, &[KeyCode::Down, KeyCode::Char('x')]).await;
    assert!(draw(&mut ui).iter().any(|row| row.contains("Stop make b?")));

    // `a` ends while the stop waits, so `b` and `c` move up a row.
    process_ended(&mut ui, "a", "make a");
    draw(&mut ui);
    ui.sync_side_panel(&writer, 8192).await.unwrap();
    press(&mut ui, &writer, &[KeyCode::Char('y')]).await;

    let commands = sent(&mut receiver);
    let stops = of_type(&commands, "cancel_process");
    assert_eq!(stops.len(), 1, "{commands:#?}");
    assert_eq!(stops[0]["process_id"], "b");
}

#[tokio::test]
async fn ended_peek_keeps_focus() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = running(&[("a", "make a"), ("b", "make b")]);
    process_ended(&mut ui, "a", "make a");
    process_ended(&mut ui, "b", "make b");
    draw(&mut ui);
    ui.focus_background_processes();
    draw(&mut ui);
    press(&mut ui, &writer, &[KeyCode::Enter]).await;
    let commands = sent(&mut receiver);
    let read = of_type(&commands, "get_process_output")[0]["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut reply = output_reply(&read, "completed", "built a\n");
    reply["process"]["process_id"] = "a".into();
    reply["process"]["command"] = "make a".into();
    backend(&mut ui, reply);

    // The peek stays under `a`, and Esc closes it, not the panel.
    let rows = draw(&mut ui);
    let at = rows
        .iter()
        .position(|row| row.contains("✓ make a"))
        .unwrap_or_else(|| panic!("{rows:#?}"));
    assert!(rows[at + 1].contains("│ built a"), "{rows:#?}");
    press(&mut ui, &writer, &[KeyCode::Esc]).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    assert!(ui.state.background_processes.peek().is_none());
}

#[tokio::test]
async fn click_away_leaves_panel() {
    let (writer, mut receiver) = mpsc::channel(32);
    let mut ui = server_running();
    ui.mouse_enabled = true;
    open_peek(&mut ui, &writer, &mut receiver).await;

    let click = Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 1,
        row: 1,
        modifiers: KeyModifiers::NONE,
    });
    ui.handle_input(click, &writer, 8192).await.unwrap();
    draw(&mut ui);

    // Typing goes to the composer, not to the peek's keys.
    assert_eq!(ui.active_overlay(), None);
    press(&mut ui, &writer, &[KeyCode::Char('x'), KeyCode::Char('y')]).await;
    assert_eq!(ui.editor.text(), "xy");
    assert!(of_type(&sent(&mut receiver), "cancel_process").is_empty());
}

#[tokio::test]
async fn low_peek_scrolls_into_view() {
    let (writer, _receiver) = mpsc::channel(32);
    let processes: Vec<_> = (1..=8)
        .map(|n| (format!("p{n}"), format!("job {n}")))
        .collect();
    let processes: Vec<_> = processes
        .iter()
        .map(|(id, command)| (id.as_str(), command.as_str()))
        .collect();
    let mut ui = running(&processes);
    let height = 22;
    draw_at(&mut ui, height);
    ui.focus_background_processes();
    draw_at(&mut ui, height);
    ui.handle_input(key(KeyCode::End), &writer, 8192)
        .await
        .unwrap();
    draw_at(&mut ui, height);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();

    let rows = draw_at(&mut ui, height);
    assert!(
        rows.iter().any(|row| row.contains("8 ● job 8")),
        "{rows:#?}"
    );
    assert!(
        rows.iter().any(|row| row.contains("Enter card")),
        "{rows:#?}"
    );
}
