//! The Browser view's start page: the address field, recent addresses, and the
//! untrusted-project explanation shown while no live page is open.

use super::*;
use crate::reducer::{ActiveCommand, ActiveCommandType, BrowserAddresses};
use crate::side_panel::PanelMode;
use crossterm::event::MouseButton;
use ratatui::{backend::TestBackend, buffer::Buffer};

const WIDTH: u16 = 160;
const HEIGHT: u16 = 36;
const RECENTS: [&str; 3] = [
    "https://github.com/whanyu1212/Wisp/pulls",
    "http://localhost:4321/guide/tui",
    "https://linear.app/queryverse/issue/HY-560",
];
const PLACEHOLDER: &str = "[Search or enter address";

fn ui_with(report: Option<(bool, &[&str])>) -> LiveUi {
    let mut ui = LiveUi {
        mouse_enabled: true,
        ..LiveUi::default()
    };
    ui.state.browser_addresses = report.map(|(trusted, addresses)| BrowserAddresses {
        trusted,
        addresses: addresses
            .iter()
            .map(|&address| address.to_owned())
            .collect(),
    });
    ui.editor.replace_range(0..0, "draft for the agent");
    ui
}

fn draw(ui: &mut LiveUi) -> (Buffer, Vec<String>) {
    draw_rows(ui, HEIGHT)
}

/// Draw on a terminal `height` rows tall and [`WIDTH`] columns wide.
fn draw_rows(ui: &mut LiveUi, height: u16) -> (Buffer, Vec<String>) {
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    let buffer = terminal.backend().buffer().clone();
    let rows = (0..height)
        .map(|y| (0..WIDTH).map(|x| buffer[(x, y)].symbol()).collect())
        .collect();
    (buffer, rows)
}

/// The column and row where `text` is painted.
fn find(rows: &[String], text: &str) -> Option<(u16, u16)> {
    rows.iter().enumerate().find_map(|(y, row)| {
        let byte = row.find(text)?;
        Some((
            u16::try_from(row[..byte].chars().count()).unwrap(),
            u16::try_from(y).unwrap(),
        ))
    })
}

/// Apply `input` and return the commands it sent.
async fn send(ui: &mut LiveUi, input: Input) -> Vec<serde_json::Value> {
    let (writer, mut receiver) = mpsc::channel(32);
    ui.handle_input(input, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    let mut commands = Vec::new();
    while let Ok(message) = receiver.try_recv() {
        if let WriterMessage::Frame { payload, .. } = message {
            commands.push(serde_json::from_slice(&payload).unwrap());
        }
    }
    commands
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn left_click(column: u16, row: u16) -> Input {
    Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column,
        row,
        modifiers: KeyModifiers::NONE,
    })
}

async fn type_text(ui: &mut LiveUi, text: &str) -> Vec<serde_json::Value> {
    let mut commands = Vec::new();
    for ch in text.chars() {
        commands.extend(send(ui, key(KeyCode::Char(ch))).await);
    }
    commands
}

/// Open the Browser view from its top-bar tab, returning what the click sent.
async fn open_from_tab(ui: &mut LiveUi) -> Vec<serde_json::Value> {
    let (_, rows) = draw(ui);
    let (column, row) = find(&rows, " Browser ").expect("Browser tab");
    send(ui, left_click(column + 1, row)).await
}

fn live_open(commands: &[serde_json::Value]) -> Vec<&str> {
    commands
        .iter()
        .filter(|command| command["action"] == "live_open")
        .map(|command| command["url"].as_str().unwrap())
        .collect()
}

#[tokio::test]
async fn start_page_lists_recents() {
    let hostile = "https://evil.test/\u{1b}]52;c;c2VjcmV0\u{7}x";
    let mut addresses = RECENTS.to_vec();
    addresses.push(hostile);
    let mut ui = ui_with(Some((true, &addresses)));
    open_from_tab(&mut ui).await;

    let (buffer, rows) = draw(&mut ui);
    let screen = rows.join("\n");
    let (toolbar_col, toolbar_row) = find(&rows, "[‹] [›] [↻]").expect("toolbar");
    assert_eq!(
        find(&rows, PLACEHOLDER).map(|(_, row)| row),
        Some(toolbar_row)
    );
    assert_eq!(
        buffer[(toolbar_col + 1, toolbar_row)].fg,
        ui.palette().muted,
        "Back has nothing to go back to"
    );
    let (_, heading) = find(&rows, "Recent").expect("Recent heading");
    let shown: Vec<u16> = [
        "github.com/whanyu1212/Wisp/pulls",
        "http://localhost:4321/guide/tui",
        "linear.app/queryverse/issue/HY-560",
        "evil.test/\u{FFFD}]52;c;c2VjcmV0\u{FFFD}x",
    ]
    .iter()
    .map(|text| {
        find(&rows, text)
            .unwrap_or_else(|| panic!("{text} in {screen}"))
            .1
    })
    .collect();
    assert_eq!(shown, (heading + 1..heading + 5).collect::<Vec<_>>());
    assert!(!screen.contains("https://"), "{screen}");
    assert!(!screen.contains('\u{1b}'));
    assert!(
        screen.contains("Enter open · ↑↓ recent · Esc back to prompt"),
        "{screen}"
    );
}

#[tokio::test]
async fn close_button_hides_browser_without_leaving_interact_first() {
    for expanded in [false, true] {
        let mut ui = ui_with(Some((true, &[])));
        open_from_tab(&mut ui).await;
        if expanded {
            ui.side_panel.toggle_expanded();
        }
        ui.side_panel.enter_browser_interact();
        let (_, rows) = draw(&mut ui);
        let (column, row) = rows
            .iter()
            .enumerate()
            .find_map(|(y, line)| {
                let (x, _) = find(std::slice::from_ref(line), "[Close]")?;
                ui.side_panel.contains(x, y as u16).then_some((x, y as u16))
            })
            .expect("browser close button inside the panel");
        assert!(send(&mut ui, left_click(column + 2, row)).await.is_empty());
        assert!(
            !ui.side_panel.visible(),
            "expanded={expanded}, click=({column}, {row}), layer={:?}, rendered={:?}, header={}",
            ui.mouse_layer(),
            ui.rendered_overlay,
            rows[usize::from(row)]
        );
        assert!(!ui.side_panel.browser_interacting());
    }
}

#[tokio::test]
async fn no_recents_no_heading() {
    for report in [None, Some((true, &[][..]))] {
        let mut ui = ui_with(report);
        open_from_tab(&mut ui).await;
        let (_, rows) = draw(&mut ui);
        let screen = rows.join("\n");
        assert!(screen.contains(PLACEHOLDER), "{screen}");
        assert!(!screen.contains("Recent"), "{screen}");
        assert!(
            screen.contains("Enter open · Esc back to prompt"),
            "{screen}"
        );
    }
}

#[tokio::test]
async fn tab_focuses_address_field() {
    let mut ui = ui_with(None);
    let commands = open_from_tab(&mut ui).await;

    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(ui.browser_address_active());
    assert_eq!(ui.editor.text(), "draft for the agent");
    assert_eq!(commands.len(), 1);
    assert_eq!(commands[0]["type"], "get_browser_addresses");
    // Painting and the panel's sync ask for nothing more.
    let (writer, mut receiver) = mpsc::channel(8);
    draw(&mut ui);
    ui.sync_side_panel(&writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    draw(&mut ui);
    let asked_again = std::iter::from_fn(|| receiver.try_recv().ok()).any(|message| {
        matches!(message, WriterMessage::Frame { payload, .. }
            if serde_json::from_slice::<serde_json::Value>(&payload).unwrap()["type"]
                == "get_browser_addresses")
    });
    assert!(!asked_again);
    assert!(type_text(&mut ui, "ab").await.is_empty());
    assert_eq!(ui.side_panel.browser_address_editor().unwrap().text(), "ab");
    assert_eq!(ui.editor.text(), "draft for the agent");
}

#[tokio::test]
async fn enter_sends_text_as_typed() {
    for text in ["example.com", "rust ratatui docs"] {
        let mut ui = ui_with(None);
        open_from_tab(&mut ui).await;
        type_text(&mut ui, text).await;

        let commands = send(&mut ui, key(KeyCode::Enter)).await;

        assert_eq!(live_open(&commands), [text]);
        assert!(!ui.browser_address_active());
        assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
        assert_eq!(ui.editor.text(), "draft for the agent");
    }
}

#[tokio::test]
async fn blank_enter_sends_nothing() {
    for text in ["", "   "] {
        let mut ui = ui_with(None);
        open_from_tab(&mut ui).await;
        type_text(&mut ui, text).await;

        assert!(send(&mut ui, key(KeyCode::Enter)).await.is_empty());
        assert!(ui.browser_address_active());
        assert!(ui.notice.is_none());
    }
}

#[tokio::test]
async fn arrows_select_recents() {
    let mut ui = ui_with(Some((true, &RECENTS)));
    open_from_tab(&mut ui).await;

    // Up from the field stays there; Down stops at the last row.
    for (code, selected) in [
        (KeyCode::Up, None),
        (KeyCode::Down, Some(0)),
        (KeyCode::Down, Some(1)),
        (KeyCode::Up, Some(0)),
        (KeyCode::Up, None),
        (KeyCode::Down, Some(0)),
        (KeyCode::Down, Some(1)),
        (KeyCode::Down, Some(2)),
        (KeyCode::Down, Some(2)),
        (KeyCode::Up, Some(1)),
    ] {
        assert!(send(&mut ui, key(code)).await.is_empty());
        assert_eq!(ui.side_panel.browser_recent(), selected, "{code:?}");
    }
    let (_, rows) = draw(&mut ui);
    assert!(find(&rows, "▸ http://localhost:4321/guide/tui").is_some());

    let commands = send(&mut ui, key(KeyCode::Enter)).await;
    assert_eq!(live_open(&commands), [RECENTS[1]]);
}

#[tokio::test]
async fn typing_leaves_recents() {
    let mut ui = ui_with(Some((true, &RECENTS)));
    open_from_tab(&mut ui).await;
    send(&mut ui, key(KeyCode::Down)).await;

    type_text(&mut ui, "x").await;

    assert_eq!(ui.side_panel.browser_recent(), None);
    assert_eq!(ui.side_panel.browser_address_editor().unwrap().text(), "x");
}

#[tokio::test]
async fn click_recent_opens_it() {
    let mut ui = ui_with(Some((true, &RECENTS)));
    open_from_tab(&mut ui).await;
    let (_, rows) = draw(&mut ui);
    let (column, row) = find(&rows, "linear.app/queryverse").unwrap();

    let commands = send(&mut ui, left_click(column, row)).await;

    assert_eq!(live_open(&commands), [RECENTS[2]]);
}

/// The most recent addresses the backend reports, `site00.test` first.
fn full_recents() -> Vec<String> {
    (0..20)
        .map(|index| format!("https://site{index:02}.test/page"))
        .collect()
}

/// A terminal too short for every recent address under the toolbar.
const SHORT: u16 = 24;
const FOOTER: &str = "Enter open · ↑↓ recent · Esc back to prompt";

/// The recent addresses painted under the heading, top to bottom.
fn shown_recents(rows: &[String]) -> Vec<String> {
    let (column, heading) = find(rows, "Recent").expect("Recent heading");
    rows[usize::from(heading) + 1..]
        .iter()
        .map(|row| {
            row.chars()
                .skip(usize::from(column) + 2)
                .collect::<String>()
        })
        .take_while(|text| text.starts_with("site"))
        .map(|text| text.split_whitespace().next().unwrap().to_owned())
        .collect()
}

#[tokio::test]
async fn recents_follow_selection() {
    let recents = full_recents();
    let addresses: Vec<&str> = recents.iter().map(String::as_str).collect();
    let mut ui = ui_with(Some((true, &addresses)));
    open_from_tab(&mut ui).await;
    let (_, rows) = draw_rows(&mut ui, SHORT);
    let fits = shown_recents(&rows).len();
    assert!((1..20).contains(&fits), "{}", rows.join("\n"));

    for _ in 0..20 {
        send(&mut ui, key(KeyCode::Down)).await;
    }
    let (_, rows) = draw_rows(&mut ui, SHORT);
    let screen = rows.join("\n");
    assert!(find(&rows, "▸ site19.test/page").is_some(), "{screen}");
    assert!(find(&rows, "[‹] [›] [↻]").is_some(), "{screen}");
    assert!(find(&rows, FOOTER).is_some(), "{screen}");
    let shown = shown_recents(&rows);
    assert_eq!(shown.len(), fits, "{screen}");
    assert_eq!(shown.last().map(String::as_str), Some("site19.test/page"));

    for _ in 0..19 {
        send(&mut ui, key(KeyCode::Up)).await;
    }
    let (_, rows) = draw_rows(&mut ui, SHORT);
    assert!(find(&rows, "▸ site00.test/page").is_some());
    assert!(find(&rows, "[‹] [›] [↻]").is_some());
    assert!(find(&rows, FOOTER).is_some());

    for _ in 0..19 {
        send(&mut ui, key(KeyCode::Down)).await;
    }
    let commands = send(&mut ui, key(KeyCode::Enter)).await;
    assert_eq!(live_open(&commands), [recents[19].as_str()]);
}

#[tokio::test]
async fn click_scrolled_recent() {
    let recents = full_recents();
    let addresses: Vec<&str> = recents.iter().map(String::as_str).collect();
    let mut ui = ui_with(Some((true, &addresses)));
    open_from_tab(&mut ui).await;
    for _ in 0..20 {
        send(&mut ui, key(KeyCode::Down)).await;
    }
    let (_, rows) = draw_rows(&mut ui, SHORT);
    let top = shown_recents(&rows)[0].clone();
    assert_ne!(top, "site00.test/page", "the list did not scroll");
    let (column, row) = find(&rows, &top).unwrap();

    let commands = send(&mut ui, left_click(column, row)).await;

    assert_eq!(live_open(&commands), [format!("https://{top}")]);
}

#[tokio::test]
async fn tall_panel_shows_every_recent() {
    let recents = full_recents();
    let addresses: Vec<&str> = recents.iter().map(String::as_str).collect();
    let mut ui = ui_with(Some((true, &addresses)));
    open_from_tab(&mut ui).await;
    let every: Vec<String> = recents
        .iter()
        .map(|address| address["https://".len()..].to_owned())
        .collect();

    for _ in 0..20 {
        send(&mut ui, key(KeyCode::Down)).await;
    }
    let (_, rows) = draw_rows(&mut ui, 60);

    assert_eq!(shown_recents(&rows), every);
}

#[tokio::test]
async fn escape_returns_to_composer() {
    for code in [KeyCode::Esc, KeyCode::Char('c')] {
        let mut ui = ui_with(None);
        open_from_tab(&mut ui).await;
        let modifiers = if code == KeyCode::Esc {
            KeyModifiers::NONE
        } else {
            KeyModifiers::CONTROL
        };

        send(&mut ui, Input::Key(KeyEvent::new(code, modifiers))).await;

        assert!(!ui.browser_address_active());
        assert!(ui.side_panel.visible());
        assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
        type_text(&mut ui, "!").await;
        assert_eq!(ui.editor.text(), "draft for the agent!");
        let (_, rows) = draw(&mut ui);
        assert!(find(&rows, "Ctrl+L address").is_some());

        // Ctrl+L focuses the field again.
        send(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::CONTROL)),
        )
        .await;
        assert!(ui.browser_address_active());
    }
}

#[tokio::test]
async fn untrusted_project_explains() {
    let mut ui = ui_with(None);
    open_from_tab(&mut ui).await;
    assert!(ui.browser_address_active());

    // The report arrives after the field took focus.
    let (writer, _receiver) = mpsc::channel(8);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::BrowserAddressesReported(BrowserAddresses {
            trusted: false,
            addresses: Vec::new(),
        })),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let (_, rows) = draw(&mut ui);
    let screen = rows.join("\n");

    assert!(screen.contains("This project is not trusted."), "{screen}");
    assert!(!screen.contains(PLACEHOLDER), "{screen}");
    assert!(!ui.browser_address_active());
    type_text(&mut ui, "a").await;
    assert_eq!(ui.editor.text(), "draft for the agenta");
    for control in [
        side_panel::BrowserControl::Address,
        side_panel::BrowserControl::Recent(0),
    ] {
        ui.handle_browser_control(control, &writer, MAX_APPLICATION_FRAME_BYTES)
            .await
            .unwrap();
        assert!(!ui.side_panel.browser_address_focused());
    }

    // Reopening asks again, and still gives the field no focus.
    send(&mut ui, key(KeyCode::Esc)).await;
    ui.side_panel.close();
    let commands = open_from_tab(&mut ui).await;
    assert_eq!(commands[0]["type"], "get_browser_addresses");
    assert!(!ui.side_panel.browser_address_focused());
}

#[tokio::test]
async fn history_buttons_are_inert() {
    let mut ui = ui_with(None);
    open_from_tab(&mut ui).await;
    send(&mut ui, key(KeyCode::Esc)).await;
    let (_, rows) = draw(&mut ui);
    let (column, row) = find(&rows, "[‹] [›] [↻]").unwrap();

    for offset in [1, 5, 9] {
        assert!(
            send(&mut ui, left_click(column + offset, row))
                .await
                .is_empty()
        );
        assert!(ui.notice.is_none());
    }
    assert!(ui.state.browser_viewport.is_none());
}

#[tokio::test]
async fn run_blocks_start_page_enter() {
    let mut ui = ui_with(None);
    open_from_tab(&mut ui).await;
    type_text(&mut ui, "example.com").await;
    ui.state.view_status = ViewStatus::Running;

    let commands = send(&mut ui, key(KeyCode::Enter)).await;

    assert!(live_open(&commands).is_empty());
    assert_eq!(
        ui.notice.as_deref(),
        Some("Wait for the active run to finish before controlling the browser.")
    );
    assert!(ui.browser_address_active());
}

/// Apply a backend event shaped as the wire sends it.
async fn backend_event(ui: &mut LiveUi, payload: serde_json::Value) {
    let event = BackendEvent::from_projection_value(&payload).unwrap();
    let (writer, _receiver) = mpsc::channel(8);
    ui.dispatch(
        UiAction::BackendEvent(event),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
}

fn approval_requested() -> serde_json::Value {
    serde_json::json!({
        "type": "tool.approval.requested",
        "timestamp": "2026-11-20T12:35:10Z",
        "call_id": "call-1",
        "name": "bash",
        "arguments": {"command": "ls"},
        "safety": "command",
    })
}

fn trust_requested() -> serde_json::Value {
    serde_json::json!({
        "type": "trust.requested",
        "timestamp": "2026-11-20T12:35:10Z",
        "request_id": "trust-1",
        "project_path": "/project",
    })
}

/// The start page's field is focused with a draft when a run asks for a decision.
async fn start_page_awaiting(request: serde_json::Value) -> LiveUi {
    let mut ui = ui_with(Some((true, &RECENTS)));
    open_from_tab(&mut ui).await;
    type_text(&mut ui, "ab").await;
    backend_event(&mut ui, request).await;
    // Approving needs the prompt on screen.
    draw(&mut ui);
    ui
}

#[tokio::test]
async fn decision_keys_answer_decision() {
    for (request, key_char, kind, field, answer) in [
        (approval_requested(), 'y', "approval", "approved", true),
        (approval_requested(), 'n', "approval", "approved", false),
        (trust_requested(), 'y', "trust", "trusted", true),
        (trust_requested(), 'n', "trust", "trusted", false),
    ] {
        let mut ui = start_page_awaiting(request).await;
        assert!(!ui.browser_address_active());

        let commands = send(&mut ui, key(KeyCode::Char(key_char))).await;

        assert_eq!(commands.len(), 1, "{kind} {key_char}: {commands:?}");
        assert_eq!(commands[0]["type"], kind);
        assert_eq!(commands[0][field], answer);
        // The field gave up focus and took none of the keys.
        assert!(!ui.side_panel.browser_address_focused());
        assert_eq!(ui.editor.text(), "draft for the agent");
    }
}

#[tokio::test]
async fn decision_enter_opens_nothing() {
    for (request, kind) in [
        (approval_requested(), "approval"),
        (trust_requested(), "trust"),
    ] {
        let mut ui = start_page_awaiting(request).await;
        // Down highlights the decision's first choice, not a recent address.
        send(&mut ui, key(KeyCode::Down)).await;
        draw(&mut ui);

        let commands = send(&mut ui, key(KeyCode::Enter)).await;

        assert!(live_open(&commands).is_empty(), "{commands:?}");
        assert_eq!(commands.len(), 1, "{commands:?}");
        assert_eq!(commands[0]["type"], kind);
        assert_eq!(ui.side_panel.browser_recent(), None);
    }
}

#[tokio::test]
async fn decision_outranks_focused_field() {
    // Focus left on the field, as if no event had dismissed it, still yields to the
    // decision.
    let mut ui = ui_with(None);
    open_from_tab(&mut ui).await;
    ui.state.view_status = ViewStatus::WaitingForTrust;
    ui.state.pending_trust_request_id = Some("trust-1".into());
    assert!(ui.side_panel.browser_address_focused());
    assert!(!ui.browser_address_active());

    let commands = send(&mut ui, key(KeyCode::Char('n'))).await;

    assert_eq!(commands.len(), 1, "{commands:?}");
    assert_eq!(commands[0]["type"], "trust");
    assert_eq!(commands[0]["trusted"], false);
}

#[tokio::test]
async fn decision_keeps_field_unfocused() {
    let mut ui = ui_with(None);
    open_from_tab(&mut ui).await;
    send(&mut ui, key(KeyCode::Esc)).await;
    ui.state.view_status = ViewStatus::WaitingForTrust;
    ui.state.pending_trust_request_id = Some("trust-1".into());
    let (writer, _receiver) = mpsc::channel(8);

    ui.begin_browser_start(&writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    assert!(!ui.side_panel.browser_address_focused());

    // Ctrl+L leaves the field alone too.
    send(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::CONTROL)),
    )
    .await;
    assert!(!ui.side_panel.browser_address_focused());
}

#[tokio::test]
async fn run_keeps_field_focused() {
    let mut ui = ui_with(None);
    open_from_tab(&mut ui).await;
    type_text(&mut ui, "ab").await;
    ui.state.view_status = ViewStatus::Running;
    ui.state.current_command = Some(ActiveCommand {
        id: "prompt-1".into(),
        command_type: ActiveCommandType::Prompt,
    });

    backend_event(
        &mut ui,
        serde_json::json!({
            "type": "tool.call",
            "call_id": "read-1",
            "name": "read",
            "arguments": {"path": "README.md"},
        }),
    )
    .await;
    draw(&mut ui);

    assert!(ui.browser_address_active());
    type_text(&mut ui, "c").await;
    assert_eq!(
        ui.side_panel.browser_address_editor().unwrap().text(),
        "abc"
    );
}

#[tokio::test]
async fn panel_key_b_opens_start_page() {
    let mut ui = ui_with(None);
    draw(&mut ui);
    ui.side_panel.focus();
    draw(&mut ui);

    let commands = send(&mut ui, key(KeyCode::Char('b'))).await;

    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(ui.browser_address_active());
    assert_eq!(commands[0]["type"], "get_browser_addresses");
}
