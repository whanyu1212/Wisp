use super::*;
use crossterm::event::MouseButton;
use image::ImageEncoder;
use ratatui::backend::TestBackend;
use serde_json::{Value, json};
use std::time::Instant;
use wisp_protocol::commands::LiveBrowserViewport;

const LIMIT: usize = MAX_APPLICATION_FRAME_BYTES;

async fn docked_browser(
    writer: &mpsc::Sender<WriterMessage>,
    connection: &ConnectionInfo,
) -> (LiveUi, Terminal<TestBackend>, LiveBrowserViewport) {
    let mut ui = LiveUi {
        mouse_enabled: true,
        ..LiveUi::default()
    };
    let mut terminal = Terminal::new(TestBackend::new(180, 36)).unwrap();
    let initial = LiveBrowserViewport {
        width: 600,
        height: 300,
        device_scale_factor: 1,
    };
    show_frame(&mut ui, writer, initial, [0, 100, 200]).await;
    ui.draw(&mut terminal, connection).unwrap();
    let initial = ui
        .side_panel
        .desired_browser_geometry(ui.cell_size)
        .unwrap();
    show_frame(&mut ui, writer, initial, [0, 100, 200]).await;
    ui.draw(&mut terminal, connection).unwrap();
    (ui, terminal, initial)
}

fn mouse(kind: MouseEventKind, column: u16, row: u16) -> Input {
    Input::Mouse(MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    })
}

async fn show_frame(
    ui: &mut LiveUi,
    writer: &mpsc::Sender<WriterMessage>,
    geometry: LiveBrowserViewport,
    color: [u8; 3],
) {
    use base64::Engine;
    let mut png = Vec::new();
    image::codecs::png::PngEncoder::new(&mut png)
        .write_image(&color, 1, 1, image::ExtendedColorType::Rgb8)
        .unwrap();
    let event = BackendEvent::from_projection_value(&json!({
        "type": "browser.viewport.updated",
        "timestamp": "2026-11-20T12:34:56Z",
        "live_png": base64::engine::general_purpose::STANDARD.encode(png),
        "live_device_scale": geometry.device_scale_factor,
        "viewport_width": geometry.width,
        "viewport_height": geometry.height,
        "url": "https://example.test/",
    }))
    .unwrap();
    ui.dispatch(UiAction::BackendEvent(event), writer, LIMIT)
        .await
        .unwrap();
}

fn next_command(commands: &mut mpsc::Receiver<WriterMessage>) -> Value {
    let WriterMessage::Frame { payload, .. } = commands.try_recv().unwrap() else {
        panic!("expected browser command");
    };
    serde_json::from_slice(&payload).unwrap()
}

#[tokio::test]
async fn drag_resizes_after_release_paint() {
    let (writer, mut commands) = mpsc::channel(8);
    let connection = ConnectionInfo {
        backend_version: "test".into(),
    };
    let (mut ui, mut terminal, initial) = docked_browser(&writer, &connection).await;
    let left = MouseButton::Left;
    let now = Instant::now();
    let original = ui
        .transcript_row_cache
        .browser_image_preview
        .as_ref()
        .unwrap()
        .key
        .clone();
    let border = ui.mouse_frame.as_ref().unwrap().panel_border.unwrap();
    ui.handle_input(
        mouse(MouseEventKind::Down(left), border.x, border.y),
        &writer,
        LIMIT,
    )
    .await
    .unwrap();
    assert!(ui.side_panel.resizing());

    // An already-running frame can finish while the border is held.
    show_frame(&mut ui, &writer, initial, [200, 100, 0]).await;
    for step in [8, 16] {
        ui.handle_input(
            mouse(MouseEventKind::Drag(left), border.x - step, border.y),
            &writer,
            LIMIT,
        )
        .await
        .unwrap();
        ui.draw(&mut terminal, &connection).unwrap();
        assert_eq!(
            ui.mouse_frame.as_ref().unwrap().panel_border.unwrap().x,
            border.x - step
        );
        assert_eq!(
            ui.transcript_row_cache
                .browser_image_preview
                .as_ref()
                .unwrap()
                .key,
            original
        );
        let image = ui.side_panel.viewport_image_rect().unwrap();
        assert!(
            ui.side_panel
                .map_viewport_point(&ui.state, image.x, image.y)
                .is_none()
        );
        ui.poll_browser_viewport(now, &writer, LIMIT).await.unwrap();
        assert!(commands.try_recv().is_err());
    }

    ui.handle_input(
        mouse(MouseEventKind::Up(left), border.x - 24, border.y),
        &writer,
        LIMIT,
    )
    .await
    .unwrap();
    ui.poll_browser_viewport(now, &writer, LIMIT).await.unwrap();
    assert!(commands.try_recv().is_err());
    ui.draw(&mut terminal, &connection).unwrap();
    assert_eq!(
        ui.mouse_frame.as_ref().unwrap().panel_border.unwrap().x,
        border.x - 24
    );
    let image = ui.side_panel.viewport_image_rect().unwrap();
    ui.handle_input(
        mouse(MouseEventKind::Down(left), image.x, image.y),
        &writer,
        LIMIT,
    )
    .await
    .unwrap();
    assert!(commands.try_recv().is_err());
    let final_geometry = ui
        .side_panel
        .desired_browser_geometry(ui.cell_size)
        .unwrap();
    assert_ne!(initial, final_geometry);
    ui.poll_browser_viewport(now, &writer, LIMIT).await.unwrap();
    let resize = next_command(&mut commands);
    assert_eq!(resize["action"], "live_resize");
    assert_eq!(
        resize["viewport"],
        serde_json::to_value(final_geometry).unwrap()
    );

    show_frame(&mut ui, &writer, final_geometry, [100, 200, 0]).await;
    let finished = BackendEvent::from_projection_value(&json!({
        "type": "rpc.command.finished", "timestamp": "2026-11-20T12:34:57Z",
        "command_type": "browser_interact", "command_id": resize["id"], "ok": true,
    }))
    .unwrap();
    ui.dispatch(UiAction::BackendEvent(finished), &writer, LIMIT)
        .await
        .unwrap();
    assert!(
        ui.side_panel
            .map_viewport_point(&ui.state, image.x, image.y)
            .is_none()
    );
    ui.draw(&mut terminal, &connection).unwrap();
    let image = ui.side_panel.viewport_image_rect().unwrap();
    assert!(
        ui.side_panel
            .map_viewport_point(&ui.state, image.x, image.y)
            .is_some()
    );
    ui.poll_browser_viewport(now + LIVE_BROWSER_RESIZE_INTERVAL, &writer, LIMIT)
        .await
        .unwrap();
    assert_eq!(next_command(&mut commands)["action"], "live_frame");
}

#[tokio::test]
async fn unchanged_drag_skips_browser_resize() {
    let (writer, mut commands) = mpsc::channel(8);
    let connection = ConnectionInfo {
        backend_version: "test".into(),
    };
    let (mut ui, mut terminal, initial) = docked_browser(&writer, &connection).await;
    let border = ui.mouse_frame.as_ref().unwrap().panel_border.unwrap();
    let left = MouseButton::Left;
    for (kind, column) in [
        (MouseEventKind::Down(left), border.x),
        (MouseEventKind::Drag(left), border.x - 12),
        (MouseEventKind::Up(left), border.x),
    ] {
        ui.handle_input(mouse(kind, column, border.y), &writer, LIMIT)
            .await
            .unwrap();
    }
    ui.draw(&mut terminal, &connection).unwrap();
    assert_eq!(
        ui.side_panel.desired_browser_geometry(ui.cell_size),
        Some(initial)
    );
    let image = ui.side_panel.viewport_image_rect().unwrap();
    assert!(
        ui.side_panel
            .map_viewport_point(&ui.state, image.x, image.y)
            .is_some()
    );
    ui.poll_browser_viewport(Instant::now(), &writer, LIMIT)
        .await
        .unwrap();
    assert_eq!(next_command(&mut commands)["action"], "live_frame");
}

#[tokio::test]
async fn focus_loss_ends_drag() {
    let (writer, mut commands) = mpsc::channel(8);
    let connection = ConnectionInfo {
        backend_version: "test".into(),
    };
    let (mut ui, mut terminal, initial) = docked_browser(&writer, &connection).await;
    let border = ui.mouse_frame.as_ref().unwrap().panel_border.unwrap();
    let left = MouseButton::Left;
    for (kind, column) in [
        (MouseEventKind::Down(left), border.x),
        (MouseEventKind::Drag(left), border.x - 12),
    ] {
        ui.handle_input(mouse(kind, column, border.y), &writer, LIMIT)
            .await
            .unwrap();
    }
    ui.draw(&mut terminal, &connection).unwrap();

    ui.handle_input(Input::Focus(false), &writer, LIMIT)
        .await
        .unwrap();
    assert!(!ui.side_panel.resizing());
    assert!(ui.render_pending);
    ui.poll_browser_viewport(Instant::now(), &writer, LIMIT)
        .await
        .unwrap();
    assert!(commands.try_recv().is_err());

    ui.handle_input(
        mouse(MouseEventKind::Up(left), border.x - 20, border.y),
        &writer,
        LIMIT,
    )
    .await
    .unwrap();
    ui.draw(&mut terminal, &connection).unwrap();
    assert_eq!(
        ui.mouse_frame.as_ref().unwrap().panel_border.unwrap().x,
        border.x - 12
    );
    let final_geometry = ui
        .side_panel
        .desired_browser_geometry(ui.cell_size)
        .unwrap();
    assert_ne!(final_geometry, initial);
    ui.poll_browser_viewport(Instant::now(), &writer, LIMIT)
        .await
        .unwrap();
    let resize = next_command(&mut commands);
    assert_eq!(resize["action"], "live_resize");
    assert_eq!(
        resize["viewport"],
        serde_json::to_value(final_geometry).unwrap()
    );
}

#[tokio::test]
async fn occlusion_mid_drag_upgrades_kitty_viewport_to_half_blocks() {
    use crate::terminal_images::ImageProtocol;
    use base64::Engine;

    let (writer, _commands) = mpsc::channel(8);
    let connection = ConnectionInfo {
        backend_version: "test".into(),
    };
    let mut ui = LiveUi {
        mouse_enabled: true,
        image_protocol: ImageProtocol::KittyUnicode,
        ..LiveUi::default()
    };
    let mut terminal = Terminal::new(TestBackend::new(180, 36)).unwrap();
    let mut png = Vec::new();
    image::codecs::png::PngEncoder::new(&mut png)
        .write_image(&[10, 120, 220], 1, 1, image::ExtendedColorType::Rgb8)
        .unwrap();
    let event = BackendEvent::from_projection_value(&json!({
        "type": "browser.viewport.updated",
        "timestamp": "2026-11-20T12:34:56Z",
        "live_png": base64::engine::general_purpose::STANDARD.encode(png),
        "live_device_scale": 1,
        "viewport_width": 600,
        "viewport_height": 300,
        "url": "https://example.test/",
    }))
    .unwrap();
    ui.dispatch(UiAction::BackendEvent(event), &writer, LIMIT)
        .await
        .unwrap();
    ui.draw(&mut terminal, &connection).unwrap();

    // Unoccluded KittyUnicode frame skips PNG -> RGBA decode.
    let key = ui
        .transcript_row_cache
        .browser_image_preview
        .as_ref()
        .unwrap()
        .key
        .clone();
    assert!(ui.image_cache.get(&key).unwrap().rgba.is_empty());

    // Holding a border drag and opening KeyHelp mid-drag must decode RGBA on demand
    // so uncovered cells paint half-blocks instead of blanking out.
    let border = ui.mouse_frame.as_ref().unwrap().panel_border.unwrap();
    ui.handle_input(
        mouse(MouseEventKind::Down(MouseButton::Left), border.x, border.y),
        &writer,
        LIMIT,
    )
    .await
    .unwrap();
    assert!(ui.side_panel.resizing());
    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::CONTROL)),
        &writer,
        LIMIT,
    )
    .await
    .unwrap();
    assert!(ui.key_help.is_some());

    ui.draw(&mut terminal, &connection).unwrap();
    assert!(!ui.image_cache.get(&key).unwrap().rgba.is_empty());
    let image = ui.side_panel.viewport_image_rect().unwrap();
    let popup = ui.mouse_frame.as_ref().unwrap().popup.unwrap();
    let uncovered_col = (image.x..image.right())
        .find(|&x| !popup.contains(ratatui::layout::Position::new(x, image.y)))
        .expect("at least one uncovered viewport cell");
    assert_eq!(
        terminal.backend().buffer()[(uncovered_col, image.y)].symbol(),
        "▀"
    );
}
