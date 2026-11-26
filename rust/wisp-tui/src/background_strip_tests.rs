//! The background strip under the composer, and the ways it focuses the side
//! panel's background processes: Down from an empty composer, a click, and `/ps`.

use super::*;
use crossterm::event::{MouseButton, MouseEventKind};
use ratatui::{backend::TestBackend, buffer::Buffer};

/// Docks the side panel; narrower terminals open it as an overlay.
const DOCKED: u16 = 200;
const NARROW: u16 = 100;
const HEIGHT: u16 = 30;

fn ui_with(draft: &str, processes: &[(&str, &str)]) -> LiveUi {
    let mut ui = LiveUi {
        mouse_enabled: true,
        ..LiveUi::default()
    };
    ui.editor.insert_paste(draft);
    ui.state
        .transcript
        .append_prompt("start the servers".into());
    ui.state
        .transcript
        .complete_message(1, "Both are running.".into());
    list_processes(&mut ui, processes);
    ui
}

/// A session with no transcript yet, whose welcome menu takes Down.
fn fresh_session(processes: &[(&str, &str)]) -> LiveUi {
    let mut ui = LiveUi {
        mouse_enabled: true,
        ..LiveUi::default()
    };
    list_processes(&mut ui, processes);
    ui
}

/// Replaces the running list as a `get_state` reply does.
fn list_processes(ui: &mut LiveUi, processes: &[(&str, &str)]) {
    let processes: Vec<_> = processes
        .iter()
        .map(|(id, command)| {
            serde_json::json!({"process_id": id, "command": command, "started_at_ms": 0})
        })
        .collect();
    let event = reducer::BackendEvent::from_projection_value(&serde_json::json!({
        "type": "rpc.state", "command_id": "get_state-1",
        "timestamp": "2026-11-15T09:00:00Z",
        "state": {
            "provider": "fake", "auto_compaction_enabled": true,
            "steering_mode": "one_at_a_time", "follow_up_mode": "one_at_a_time",
            "pending_steering_count": 0, "pending_follow_up_count": 0,
            "background_processes": processes
        }
    }))
    .unwrap();
    reducer::reduce(
        &mut ui.state,
        UiAction::BackendEvent(event),
        &mut SequentialCommandIds::default(),
    )
    .unwrap();
}

fn two_running(draft: &str) -> LiveUi {
    ui_with(
        draft,
        &[
            ("server", "npm run dev\u{1b}]0;owned\u{7}"),
            ("tests", "cargo watch -x test"),
        ],
    )
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    terminal.backend().buffer().clone()
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

/// The strip's row, if one is painted.
fn strip_row(buffer: &Buffer) -> Option<(u16, String)> {
    rows(buffer)
        .into_iter()
        .enumerate()
        .find(|(_, row)| row.contains("● 2 background"))
        .map(|(y, row)| (u16::try_from(y).unwrap(), row))
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

async fn press(ui: &mut LiveUi, input: Input, width: u16) {
    let (writer, _receiver) = mpsc::channel(32);
    ui.handle_input(input, &writer, 8192).await.unwrap();
    draw(ui, width, HEIGHT);
}

#[test]
fn strip_sits_under_composer() {
    let mut ui = two_running("");
    let buffer = draw(&mut ui, NARROW, HEIGHT);
    let (y, row) = strip_row(&buffer).expect("strip painted");
    assert!(row.contains("● 2 background · npm run dev"), "{row}");
    assert!(row.contains(", cargo watch -x test"), "{row}");
    assert!(row.contains("↓ open"), "{row}");
    // Command text is untrusted: the escape sequence is neutralized.
    assert!(!row.contains('\u{1b}') && !row.contains('\u{7}'), "{row:?}");
    // The footer is 2 rows at this size; the strip is directly above it.
    assert_eq!(y, HEIGHT - 3, "{:#?}", rows(&buffer));
}

#[test]
fn docked_panel_replaces_strip() {
    let mut ui = two_running("");
    let buffer = draw(&mut ui, DOCKED, HEIGHT);
    assert_eq!(strip_row(&buffer), None, "{:#?}", rows(&buffer));
    // The panel beside the conversation lists the same processes.
    let rows = rows(&buffer);
    assert!(
        rows.iter()
            .any(|row| row.contains("2 ● cargo watch -x test")),
        "{rows:#?}"
    );
}

#[test]
fn strip_sits_under_running_composer() {
    let mut ui = two_running("");
    ui.state.view_status = ViewStatus::Running;
    let buffer = draw(&mut ui, NARROW, HEIGHT);
    let (y, _) = strip_row(&buffer).expect("strip painted");
    assert_eq!(y, HEIGHT - 3, "{:#?}", rows(&buffer));
    // The run's status line stays above the composer, which sits above the strip.
    let status = rows(&buffer)
        .iter()
        .position(|row| row.contains("Working"))
        .expect("status line");
    assert!(status + 2 < usize::from(y), "{:#?}", rows(&buffer));
}

#[test]
fn strip_hidden_without_processes() {
    let mut idle = ui_with("", &[]);
    let buffer = draw(&mut idle, NARROW, HEIGHT);
    assert!(
        !rows(&buffer).iter().any(|row| row.contains("background")),
        "{:#?}",
        rows(&buffer)
    );
    // A short terminal gives the row back to the transcript and composer.
    let mut short = two_running("");
    assert!(strip_row(&draw(&mut short, NARROW, 11)).is_none());
}

#[test]
fn strip_clips_long_commands() {
    let long = "x".repeat(300);
    let mut ui = ui_with("", &[("server", &long), ("tests", "cargo test")]);
    let buffer = draw(&mut ui, 60, HEIGHT);
    let (_, row) = strip_row(&buffer).expect("strip painted");
    assert!(row.contains('…'), "{row}");
    assert!(row.trim_end().ends_with("↓ open"), "{row}");
}

#[test]
fn draft_drops_down_hint() {
    let mut ui = two_running("half a prompt");
    let (_, row) = strip_row(&draw(&mut ui, NARROW, HEIGHT)).expect("strip painted");
    // Down edits the draft, and `panel.background` is unbound by default.
    assert!(!row.contains("open"), "{row}");
}

#[tokio::test]
async fn down_focuses_first_process() {
    for width in [DOCKED, NARROW] {
        let mut ui = two_running("");
        draw(&mut ui, width, HEIGHT);
        press(&mut ui, key(KeyCode::Down), width).await;
        assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
        // Focus starts on the first process, so Enter opens its peek in place.
        press(&mut ui, key(KeyCode::Enter), width).await;
        assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
        let rows = rows(&draw(&mut ui, width, HEIGHT));
        let peek = rows
            .iter()
            .position(|row| row.contains("│ reading…"))
            .expect("peek painted");
        assert!(rows[peek - 1].contains("1 ● npm run dev"), "{rows:#?}");
        assert_eq!(ui.editor.text(), "");
    }
}

#[tokio::test]
async fn hidden_panel_hides_again() {
    let mut ui = two_running("");
    draw(&mut ui, DOCKED, HEIGHT);
    ui.side_panel.toggle();
    draw(&mut ui, DOCKED, HEIGHT);
    assert!(!ui.side_panel.docked());
    press(&mut ui, key(KeyCode::Down), DOCKED).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    press(&mut ui, key(KeyCode::Esc), DOCKED).await;
    // The user's Ctrl+P stands once focus leaves the processes.
    assert!(!ui.side_panel.docked());
    // F8 still shows the panel for good, as before.
    press(&mut ui, key(KeyCode::F(8)), DOCKED).await;
    press(&mut ui, key(KeyCode::Esc), DOCKED).await;
    assert!(ui.side_panel.docked());
}

#[tokio::test]
async fn down_with_draft_edits() {
    let mut ui = two_running("first line\nsecond line");
    draw(&mut ui, NARROW, HEIGHT);
    press(&mut ui, key(KeyCode::Down), NARROW).await;
    assert_eq!(ui.active_overlay(), None);
    // Without processes, Down from an empty composer also stays put.
    let mut idle = ui_with("", &[]);
    draw(&mut idle, NARROW, HEIGHT);
    press(&mut idle, key(KeyCode::Down), NARROW).await;
    assert_eq!(idle.active_overlay(), None);
}

#[tokio::test]
async fn click_focuses_and_esc_keeps_draft() {
    let mut ui = two_running("keep me");
    let buffer = draw(&mut ui, NARROW, HEIGHT);
    let (y, _) = strip_row(&buffer).expect("strip painted");
    let click = Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 4,
        row: y,
        modifiers: KeyModifiers::NONE,
    });
    press(&mut ui, click, NARROW).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    press(&mut ui, key(KeyCode::Esc), NARROW).await;
    assert_eq!(ui.active_overlay(), None);
    assert_eq!(ui.editor.text(), "keep me");
}

#[tokio::test]
async fn ps_focuses_or_reports_none() {
    let mut ui = two_running("/ps");
    draw(&mut ui, NARROW, HEIGHT);
    press(&mut ui, key(KeyCode::Enter), NARROW).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    assert_eq!(ui.editor.text(), "");

    let mut idle = ui_with("/ps", &[]);
    draw(&mut idle, NARROW, HEIGHT);
    press(&mut idle, key(KeyCode::Enter), NARROW).await;
    assert_eq!(idle.active_overlay(), None);
    assert_eq!(
        idle.notice.as_deref(),
        Some("No background processes are running.")
    );
}

#[tokio::test]
async fn bound_key_focuses_with_draft() {
    let mut ui = two_running("half a prompt");
    ui.bindings = Bindings::from_json(r#"{"panel.background":["F7"]}"#).unwrap();
    let (_, row) = strip_row(&draw(&mut ui, NARROW, HEIGHT)).expect("strip painted");
    assert!(row.trim_end().ends_with("F7 open"), "{row}");
    press(&mut ui, key(KeyCode::F(7)), NARROW).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    assert_eq!(ui.editor.text(), "half a prompt");
}

#[tokio::test]
async fn welcome_menu_keeps_down() {
    let mut ui = fresh_session(&[("server", "npm run dev")]);
    let buffer = draw(&mut ui, NARROW, HEIGHT);
    let row = rows(&buffer)
        .into_iter()
        .find(|row| row.contains("● 1 background"))
        .expect("strip painted");
    // The welcome menu moves on Down, so the strip offers no Down.
    assert!(!row.contains("open"), "{row}");
    press(&mut ui, key(KeyCode::Down), NARROW).await;
    assert_eq!(ui.active_overlay(), None);
    assert_eq!(ui.welcome_selected, 1);
}

#[tokio::test]
async fn hide_survives_width_change() {
    let mut ui = two_running("");
    draw(&mut ui, DOCKED, HEIGHT);
    ui.side_panel.toggle();
    draw(&mut ui, NARROW, HEIGHT);
    press(&mut ui, key(KeyCode::Down), NARROW).await;
    assert_eq!(ui.active_overlay(), Some(OverlayKind::SidePanel));
    // Widening turns the overlay into the docked panel, which the user had hidden.
    draw(&mut ui, DOCKED, HEIGHT);
    assert!(!ui.side_panel.docked());

    // Closing the overlay with Ctrl+P keeps the hide too.
    draw(&mut ui, NARROW, HEIGHT);
    press(&mut ui, key(KeyCode::Down), NARROW).await;
    let ctrl_p = Input::Key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
    press(&mut ui, ctrl_p, NARROW).await;
    assert_eq!(ui.active_overlay(), None);
    draw(&mut ui, DOCKED, HEIGHT);
    assert!(!ui.side_panel.docked());
}

#[tokio::test]
async fn bound_key_reports_none() {
    let mut ui = ui_with("", &[]);
    ui.bindings = Bindings::from_json(r#"{"panel.background":["F7"]}"#).unwrap();
    draw(&mut ui, NARROW, HEIGHT);
    press(&mut ui, key(KeyCode::F(7)), NARROW).await;
    assert_eq!(ui.active_overlay(), None);
    assert_eq!(
        ui.notice.as_deref(),
        Some("No background processes are running.")
    );
}
