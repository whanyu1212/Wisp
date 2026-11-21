//! The top bar's title and side-panel tabs, painted by the real frame and clicked
//! through the real input routing.

use super::*;
use crate::side_panel::PanelMode;
use crossterm::event::MouseButton;
use ratatui::{backend::TestBackend, buffer::Buffer, style::Modifier};

/// Wide enough to dock the side panel.
const DOCKED: u16 = 200;
/// Too narrow to dock it, so the panel opens as an overlay.
const NARROW: u16 = 100;
const HEIGHT: u16 = 30;

fn ui() -> LiveUi {
    let mut ui = LiveUi {
        mouse_enabled: true,
        ..LiveUi::default()
    };
    ui.state
        .transcript
        .append_prompt("fix-login-redirect\nand keep the session".into());
    ui
}

fn draw(ui: &mut LiveUi, width: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, HEIGHT)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    terminal.backend().buffer().clone()
}

fn row_text(buffer: &Buffer, y: u16) -> String {
    (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect()
}

/// The column where `label` starts on the top bar's row.
fn tab_column(buffer: &Buffer, label: &str) -> u16 {
    let row = row_text(buffer, 0);
    let byte = row
        .find(&format!(" {label} "))
        .unwrap_or_else(|| panic!("no {label} tab in {row:?}"));
    u16::try_from(row[..byte].chars().count()).unwrap() + 1
}

/// Whether the `label` tab is painted as the active one.
fn tab_active(ui: &LiveUi, buffer: &Buffer, label: &str) -> bool {
    let cell = &buffer[(tab_column(buffer, label), 0)];
    cell.modifier.contains(Modifier::BOLD) && cell.fg == ui.palette().primary
}

fn click(x: u16, y: u16) -> Input {
    Input::Mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    })
}

async fn click_tab(ui: &mut LiveUi, width: u16, label: &str) {
    let (writer, _receiver) = mpsc::channel(16);
    let buffer = draw(ui, width);
    ui.handle_input(click(tab_column(&buffer, label), 0), &writer, 8192)
        .await
        .unwrap();
}

/// A panel closed on either terminal: hidden when docked, never opened when narrow.
fn closed(width: u16) -> LiveUi {
    let mut ui = ui();
    draw(&mut ui, width);
    if ui.side_panel.docked() {
        ui.side_panel.toggle();
    }
    assert!(!ui.side_panel.visible());
    ui
}

#[test]
fn closed_panel_shows_bar() {
    for width in [NARROW, DOCKED] {
        let mut ui = closed(width);
        let buffer = draw(&mut ui, width);
        let row = row_text(&buffer, 0);
        assert!(row.starts_with(" fix-login-redirect "), "{row:?}");
        assert!(row.ends_with(" Work   Browser  "), "{row:?}");
        assert!(!tab_active(&ui, &buffer, "Work"));
        assert!(!tab_active(&ui, &buffer, "Browser"));
        // The conversation starts beneath the bar.
        assert!(!row.contains("and keep the session"));
    }
}

#[test]
fn active_tab_follows_panel() {
    let mut docked = ui();
    let buffer = draw(&mut docked, DOCKED);
    assert!(tab_active(&docked, &buffer, "Work"));
    assert!(!tab_active(&docked, &buffer, "Browser"));
    docked.side_panel.toggle_browser();
    let buffer = draw(&mut docked, DOCKED);
    assert!(!tab_active(&docked, &buffer, "Work"));
    assert!(tab_active(&docked, &buffer, "Browser"));
    docked.side_panel.toggle();
    let buffer = draw(&mut docked, DOCKED);
    assert!(!tab_active(&docked, &buffer, "Browser"));

    let mut narrow = closed(NARROW);
    narrow.side_panel.toggle();
    let buffer = draw(&mut narrow, NARROW);
    assert!(tab_active(&narrow, &buffer, "Work"));
    assert!(!tab_active(&narrow, &buffer, "Browser"));
}

#[tokio::test]
async fn inactive_tab_opens_panel() {
    for width in [NARROW, DOCKED] {
        let mut ui = closed(width);
        click_tab(&mut ui, width, "Browser").await;
        assert!(ui.side_panel.visible(), "width {width}");
        assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
        assert_eq!(ui.side_panel.overlay_open(), width == NARROW);
        click_tab(&mut ui, width, "Work").await;
        assert!(ui.side_panel.visible(), "width {width}");
        assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
        let buffer = draw(&mut ui, width);
        assert!(tab_active(&ui, &buffer, "Work"));
    }
}

#[tokio::test]
async fn active_tab_closes_panel() {
    for (width, expanded) in [
        (NARROW, false),
        (DOCKED, false),
        (NARROW, true),
        (DOCKED, true),
    ] {
        let mut ui = closed(width);
        click_tab(&mut ui, width, "Work").await;
        if expanded {
            ui.side_panel.toggle_expanded();
        }
        assert!(ui.side_panel.visible());
        click_tab(&mut ui, width, "Work").await;
        assert!(
            !ui.side_panel.visible(),
            "width {width}, expanded {expanded}"
        );
        assert!(!ui.side_panel.expanded());
        let buffer = draw(&mut ui, width);
        assert!(!tab_active(&ui, &buffer, "Work"));
    }
}

#[test]
fn long_title_keeps_tabs() {
    let mut ui = LiveUi::default();
    ui.state
        .transcript
        .append_prompt(format!("{}\u{1b}]52;c;secret\u{7}", "x".repeat(120)));
    let mut terminal = Terminal::new(TestBackend::new(30, 9)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    let row = row_text(terminal.backend().buffer(), 0);
    assert!(row.ends_with(" Work   Browser  "), "{row:?}");
    assert!(row.starts_with(" xxxxxxxx"), "{row:?}");
    assert!(!row.contains('\u{1b}'));
    assert_eq!(row.chars().count(), 30);
}

#[tokio::test]
async fn blocked_tab_click_ignored() {
    let (writer, _receiver) = mpsc::channel(16);
    for blocker in ["approval", "popup"] {
        let mut ui = closed(NARROW);
        if blocker == "approval" {
            ui.dispatch(
                UiAction::BackendEvent(BackendEvent::ToolApprovalRequested(PendingApproval {
                    call_id: "approval".into(),
                    name: "bash".into(),
                    arguments: serde_json::json!({}),
                    detail_source: tool_detail::ToolDetailSource::None,
                    safety: "command".into(),
                    grants_allowed: true,
                    session_only_yolo: false,
                })),
                &writer,
                8192,
            )
            .await
            .unwrap();
        } else {
            ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
        }
        let buffer = draw(&mut ui, NARROW);
        ui.handle_input(click(tab_column(&buffer, "Browser"), 0), &writer, 8192)
            .await
            .unwrap();
        assert!(!ui.side_panel.visible(), "{blocker}");
        assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork, "{blocker}");
    }
}

#[tokio::test]
async fn panel_starts_below_bar() {
    let (writer, _receiver) = mpsc::channel(16);
    let mut ui = ui();
    let buffer = draw(&mut ui, DOCKED);
    let border = ui.mouse_frame.as_ref().unwrap().panel_border.unwrap();
    assert_eq!((border.y, border.bottom()), (1, HEIGHT));
    // The panel's surface begins on the row under the bar.
    let panel_x = DOCKED - side_panel::auto_width(DOCKED) + 3;
    assert_ne!(buffer[(panel_x, 0)].bg, buffer[(panel_x, 1)].bg);
    // A press on the bar above the border neither resizes the panel nor selects text.
    ui.handle_input(click(border.x, 0), &writer, 8192)
        .await
        .unwrap();
    assert!(!ui.side_panel.resizing());
    assert!(ui.transcript_selection.is_none());

    ui.side_panel.toggle_expanded();
    draw(&mut ui, DOCKED);
    let popup = ui.mouse_frame.as_ref().unwrap().popup.unwrap();
    assert!(popup.y >= 1, "{popup:?}");
}
