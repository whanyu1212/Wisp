//! The resize pointer over the docked side panel's border, driven through the real
//! input routing and paint. The test backend discards control bytes, so these tests
//! observe the shape the UI last sent and whether a change requested a paint.

use super::*;
use crate::pointer_shape::{PointerShape, PointerShapeState};
use crossterm::event::MouseButton;
use ratatui::{backend::TestBackend, layout::Rect};

/// Wide enough to dock the side panel.
const WIDTH: u16 = 200;
const HEIGHT: u16 = 30;
const LIMIT: usize = 8192;

fn ui(supported: bool) -> LiveUi {
    LiveUi {
        mouse_enabled: true,
        pointer_shape: PointerShapeState::new(supported),
        ..LiveUi::default()
    }
}

fn draw(ui: &mut LiveUi) {
    let mut terminal = Terminal::new(TestBackend::new(WIDTH, HEIGHT)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
}

fn mouse(kind: MouseEventKind, column: u16, row: u16) -> Input {
    Input::Mouse(MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    })
}

async fn send(ui: &mut LiveUi, input: Input) {
    let (writer, _receiver) = mpsc::channel(16);
    ui.handle_input(input, &writer, LIMIT).await.unwrap();
}

/// Paints the docked panel and returns where its border is.
fn docked(ui: &mut LiveUi) -> Rect {
    draw(ui);
    ui.mouse_frame.as_ref().unwrap().panel_border.unwrap()
}

#[tokio::test]
async fn hover_sets_resize_once() {
    let mut ui = ui(true);
    let border = docked(&mut ui);
    send(&mut ui, mouse(MouseEventKind::Moved, border.x, 5)).await;
    assert!(ui.render_pending);
    draw(&mut ui);
    assert_eq!(ui.pointer_shape.sent(), PointerShape::Resize);

    // Further motion along the border asks for nothing more.
    for column in [border.x, border.x + 1, border.x] {
        send(&mut ui, mouse(MouseEventKind::Moved, column, 6)).await;
        assert!(!ui.render_pending, "{column}");
    }

    send(&mut ui, mouse(MouseEventKind::Moved, border.x - 1, 6)).await;
    assert!(ui.render_pending);
    draw(&mut ui);
    assert_eq!(ui.pointer_shape.sent(), PointerShape::Default);
}

#[tokio::test]
async fn drag_keeps_resize_until_release() {
    let left = MouseButton::Left;
    let mut ui = ui(true);
    let border = docked(&mut ui);
    send(&mut ui, mouse(MouseEventKind::Down(left), border.x, 5)).await;
    draw(&mut ui);
    assert_eq!(ui.pointer_shape.sent(), PointerShape::Resize);

    // The panel stops at its widest, so the pointer leaves the border behind.
    send(&mut ui, mouse(MouseEventKind::Drag(left), 2, 5)).await;
    draw(&mut ui);
    let border = ui.mouse_frame.as_ref().unwrap().panel_border.unwrap();
    assert!(!border.contains((2, 5).into()));
    assert_eq!(ui.pointer_shape.sent(), PointerShape::Resize);

    send(&mut ui, mouse(MouseEventKind::Up(left), 2, 5)).await;
    assert!(ui.render_pending);
    draw(&mut ui);
    assert_eq!(ui.pointer_shape.sent(), PointerShape::Default);
}

#[tokio::test]
async fn focus_loss_resets() {
    let mut ui = ui(true);
    let border = docked(&mut ui);
    send(&mut ui, mouse(MouseEventKind::Moved, border.x, 5)).await;
    draw(&mut ui);
    send(&mut ui, Input::Focus(false)).await;
    assert!(ui.render_pending);
    draw(&mut ui);
    assert_eq!(ui.pointer_shape.sent(), PointerShape::Default);
}

#[tokio::test]
async fn covered_border_resets() {
    for cover in ["help", "picker", "expanded"] {
        let mut ui = ui(true);
        let border = docked(&mut ui);
        send(&mut ui, mouse(MouseEventKind::Moved, border.x, 5)).await;
        draw(&mut ui);
        assert_eq!(ui.pointer_shape.sent(), PointerShape::Resize, "{cover}");

        match cover {
            "help" => {
                send(
                    &mut ui,
                    Input::Key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::CONTROL)),
                )
                .await;
            }
            "picker" => {
                ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
                ui.render_pending = true;
            }
            _ => {
                ui.side_panel.toggle_expanded();
                ui.render_pending = true;
            }
        }
        // The paint that shows the cover takes the arrow away without further motion.
        draw(&mut ui);
        assert_eq!(ui.pointer_shape.sent(), PointerShape::Default, "{cover}");
        send(&mut ui, mouse(MouseEventKind::Moved, border.x, 6)).await;
        assert!(!ui.render_pending, "{cover}");
    }
}

#[tokio::test]
async fn unsupported_sends_nothing() {
    let left = MouseButton::Left;
    let mut ui = ui(false);
    let border = docked(&mut ui);
    // Hovering the border does not even ask for a paint.
    send(&mut ui, mouse(MouseEventKind::Moved, border.x, 5)).await;
    assert!(!ui.render_pending);
    for input in [
        mouse(MouseEventKind::Moved, border.x, 5),
        mouse(MouseEventKind::Down(left), border.x, 5),
        mouse(MouseEventKind::Drag(left), 2, 5),
    ] {
        send(&mut ui, input).await;
        draw(&mut ui);
        assert_eq!(ui.pointer_shape.sent(), PointerShape::Default);
    }
    assert!(ui.side_panel.resizing());
}
