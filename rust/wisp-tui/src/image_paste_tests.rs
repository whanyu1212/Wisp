//! Attaching images to the composer: clipboard images, pasted paths, chips and removal.

use super::*;
use crate::clipboard::ClipboardImage;
use base64::Engine as _;
use reducer::{ActiveCommand, ActiveCommandType};
use tokio::io::{AsyncReadExt, duplex};
use transcript::TranscriptRole;

fn ui_with_clipboard() -> (LiveUi, Arc<std::sync::Mutex<clipboard::TestClipboardState>>) {
    let (clipboard, state) = clipboard::test_clipboard();
    (
        LiveUi {
            clipboard,
            ..LiveUi::default()
        },
        state,
    )
}

fn red_pixels(width: usize, height: usize) -> ClipboardImage {
    ClipboardImage {
        width,
        height,
        rgba: [208, 16, 16, 255].repeat(width * height),
    }
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    let buffer = terminal.backend().buffer();
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn ctrl_v() -> Input {
    Input::Key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::CONTROL))
}

async fn send(ui: &mut LiveUi, input: Input) {
    let (writer, _received) = mpsc::channel(8);
    ui.handle_input(input, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
}

#[tokio::test]
async fn clipboard_image_becomes_a_chip() {
    let (mut ui, clipboard) = ui_with_clipboard();
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(red_pixels(3, 2));

    send(&mut ui, ctrl_v()).await;

    assert_eq!(ui.images.chip_labels(), ["Image 1 · 3×2"]);
    assert_eq!(ui.editor.text(), "");
    let screen = draw(&mut ui, 80, 24);
    assert!(screen.contains("[Image 1 · 3×2]"), "{screen}");
    assert!(
        ui.notice
            .as_deref()
            .unwrap()
            .starts_with("Attached Image 1")
    );
}

#[tokio::test]
async fn text_clipboard_still_pastes_text() {
    let (mut ui, clipboard) = ui_with_clipboard();
    clipboard
        .lock()
        .unwrap()
        .pastes
        .push_back(Ok("plain words".into()));

    send(&mut ui, ctrl_v()).await;

    assert!(ui.images.is_empty());
    assert_eq!(ui.editor.text(), "plain words");
    assert!(!draw(&mut ui, 80, 24).contains("[Image"));
}

#[tokio::test]
async fn backspace_removes_images_only_when_empty() {
    let (mut ui, clipboard) = ui_with_clipboard();
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .extend([red_pixels(1, 1), red_pixels(2, 2)]);
    send(&mut ui, ctrl_v()).await;
    send(&mut ui, ctrl_v()).await;
    send(&mut ui, Input::Paste("hi".into())).await;

    send(&mut ui, key(KeyCode::Backspace)).await;
    assert_eq!(ui.editor.text(), "h");
    assert_eq!(ui.images.len(), 2);

    send(&mut ui, key(KeyCode::Backspace)).await;
    send(&mut ui, key(KeyCode::Backspace)).await;
    assert_eq!(ui.editor.text(), "");
    assert_eq!(ui.images.chip_labels(), ["Image 1 · 1×1"]);
    assert!(!draw(&mut ui, 80, 24).contains("Image 2"));
}

#[tokio::test]
async fn pasted_image_path_attaches_the_file() {
    let directory = std::env::temp_dir().join(format!("wisp-paste-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let image = directory.join("shot.png");
    let png = composer_images::AttachedImage::from_rgba(1, 1, &[0, 0, 0, 255]).unwrap();
    std::fs::write(
        &image,
        base64::engine::general_purpose::STANDARD
            .decode(png.wire_value().as_str().unwrap())
            .unwrap(),
    )
    .unwrap();
    let notes = directory.join("notes.txt");
    std::fs::write(&notes, "text").unwrap();
    let (mut ui, _clipboard) = ui_with_clipboard();

    send(&mut ui, Input::Paste(image.display().to_string())).await;
    send(&mut ui, Input::Paste(notes.display().to_string())).await;

    assert_eq!(ui.images.chip_labels(), ["Image 1 · shot.png"]);
    assert_eq!(ui.editor.text(), notes.display().to_string());
    std::fs::remove_dir_all(&directory).unwrap();
}

#[tokio::test]
async fn text_only_model_refuses_images() {
    let (mut ui, clipboard) = ui_with_clipboard();
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(red_pixels(1, 1));
    ui.state.model_catalog = Some(catalog_with_images(false));

    send(&mut ui, ctrl_v()).await;

    assert!(ui.images.is_empty());
    assert_eq!(
        ui.notice.as_deref(),
        Some("Could not attach the image: text-model does not accept images.")
    );
}

pub(crate) fn catalog_with_images(
    image_input: bool,
) -> wisp_protocol::events::ModelCatalogSnapshot {
    serde_json::from_value(serde_json::json!({
        "selection": {
            "provider": "test",
            "model": "text-model",
            "effective_model": "text-model",
            "catalog_model": "text-model",
            "effort": null,
            "image_input": image_input,
            "max_request_image_bytes": if image_input { 24_000_000 } else { 0 },
        },
        "providers": [],
    }))
    .unwrap()
}

async fn attach_red_pixels(
    ui: &mut LiveUi,
    clipboard: &Arc<std::sync::Mutex<clipboard::TestClipboardState>>,
) {
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(red_pixels(3, 2));
    send(ui, ctrl_v()).await;
}

#[tokio::test]
async fn image_only_prompt_sends_the_image() {
    let (mut ui, clipboard) = ui_with_clipboard();
    attach_red_pixels(&mut ui, &clipboard).await;
    let encoded = ui.images.wire_images();
    let (writer, mut received) = mpsc::channel(8);

    ui.handle_input(key(KeyCode::Enter), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();

    let WriterMessage::Frame { payload, .. } = received.try_recv().unwrap() else {
        panic!("prompt frame")
    };
    let sent: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert_eq!(sent["type"], "prompt");
    assert_eq!(sent["prompt"], "");
    assert_eq!(sent["images"], serde_json::json!(encoded));
    assert!(ui.images.is_empty());
    let entry = ui
        .state
        .transcript
        .entries()
        .iter()
        .rfind(|entry| entry.role == TranscriptRole::User)
        .unwrap();
    assert_eq!(entry.display_content(), "[image 3×2 png]");
    assert_eq!(entry.content, "");
    assert!(!draw(&mut ui, 80, 24).contains("[Image 1"));
}

#[tokio::test]
async fn text_prompt_sends_no_images_field() {
    let (mut ui, _clipboard) = ui_with_clipboard();
    ui.editor.insert_paste("just words");
    let (writer, mut received) = mpsc::channel(8);

    ui.handle_input(key(KeyCode::Enter), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();

    let WriterMessage::Frame { payload, .. } = received.try_recv().unwrap() else {
        panic!("prompt frame")
    };
    let sent: serde_json::Value = serde_json::from_slice(&payload).unwrap();
    assert!(sent.get("images").is_none(), "{sent}");
}

#[tokio::test]
async fn model_switch_blocks_sending_images() {
    let (mut ui, clipboard) = ui_with_clipboard();
    attach_red_pixels(&mut ui, &clipboard).await;
    ui.editor.insert_paste("describe it");
    ui.state.model_catalog = Some(catalog_with_images(false));
    let (writer, mut received) = mpsc::channel(8);

    ui.handle_input(key(KeyCode::Enter), &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();

    assert!(received.try_recv().is_err());
    assert_eq!(ui.images.len(), 1);
    assert_eq!(ui.editor.text(), "describe it");
    assert!(
        ui.notice
            .as_deref()
            .unwrap()
            .starts_with("Cannot send the attached images")
    );
}

#[tokio::test]
async fn oversized_frame_keeps_the_draft() {
    let (mut ui, clipboard) = ui_with_clipboard();
    attach_red_pixels(&mut ui, &clipboard).await;
    let (writer, mut received) = mpsc::channel(8);

    // A frame limit too small for the encoded image.
    ui.handle_input(key(KeyCode::Enter), &writer, 64)
        .await
        .unwrap();

    assert!(received.try_recv().is_err());
    assert_eq!(ui.images.len(), 1);
    assert!(
        ui.notice
            .as_deref()
            .unwrap()
            .contains("exceeding the negotiated 64-byte limit")
    );
}

#[tokio::test]
async fn steering_sends_attached_images() {
    let (writer_client, mut writer_server) = duplex(64 * 1024);
    let (writer_tx, writer_rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
    let writer = tokio::spawn(writer_task(writer_client, writer_rx));
    let (mut ui, clipboard) = ui_with_clipboard();
    ui.state.view_status = ViewStatus::Running;
    ui.state.interaction_status = InteractionStatus::Running;
    ui.state.current_command = Some(ActiveCommand {
        id: "prompt-1".into(),
        command_type: ActiveCommandType::Prompt,
    });
    attach_red_pixels(&mut ui, &clipboard).await;
    let encoded = ui.images.wire_images();
    ui.editor.insert_paste("and this");

    ui.handle_input(
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)),
        &writer_tx,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();

    let mut frame = vec![0; 64 * 1024];
    let frame_len = writer_server.read(&mut frame).await.unwrap();
    let sent: serde_json::Value = serde_json::from_slice(&frame[..frame_len - 1]).unwrap();
    assert_eq!(sent["type"], "steer");
    assert_eq!(sent["content"], "and this");
    assert_eq!(sent["images"], serde_json::json!(encoded));
    assert!(ui.images.is_empty());
    assert!(ui.editor.text().is_empty());
    drop(writer_tx);
    writer.await.unwrap().unwrap();
}

fn reported_image() -> serde_json::Value {
    serde_json::json!({
        "media_type": "image/jpeg",
        "sha256": "a".repeat(64),
        "byte_size": 288,
        "width": 1280,
        "height": 720,
    })
}

#[test]
fn history_shows_image_lines() {
    let messages = [serde_json::json!({
        "entry_id": "entry-1", "role": "user", "content": "what is this?",
        "content_truncated": false, "images": [reported_image()],
    })];

    let projected = crate::history::project_chronological_history(messages.iter()).unwrap();

    let entry = &projected.transcript.entries()[0];
    assert_eq!(entry.content, "what is this?");
    assert_eq!(
        entry.display_content(),
        "what is this?\n[image 1280×720 jpeg]"
    );
}

#[test]
fn injected_message_shows_image_lines() {
    let mut state = UiState::unconfigured();
    let event = BackendEvent::from_projection_value(&serde_json::json!({
        "type": "queue.message.injected",
        "kind": "steering",
        "content": "",
        "images": [reported_image(), reported_image()],
    }))
    .unwrap();

    reducer::reduce(
        &mut state,
        UiAction::BackendEvent(event),
        &mut SequentialCommandIds::default(),
    )
    .unwrap();

    let entry = state.transcript.entries().last().unwrap();
    assert_eq!(
        entry.display_content(),
        "[image 1280×720 jpeg]\n[image 1280×720 jpeg]"
    );
}

/// Writes the composer with chips, and a sent image message, as HTML for review.
#[tokio::test]
#[ignore = "visual review only; writes HTML to WISP_VISUAL_DIR"]
async fn render_image_review_pages() {
    let directory = std::path::PathBuf::from(std::env::var("WISP_VISUAL_DIR").unwrap());
    for theme_name in ["wisp", "wisp-light"] {
        for (width, height) in [(36u16, 20u16), (120, 24)] {
            let (mut ui, clipboard) = ui_with_clipboard();
            ui.theme.select(crate::theme::named(theme_name).unwrap());
            ui.state.transcript.append_prompt_with_display(
                String::new(),
                Some("what changed here?\n[image 1280×720 png]".into()),
            );
            for size in 1..=6 {
                clipboard
                    .lock()
                    .unwrap()
                    .image_pastes
                    .push_back(red_pixels(size * 320, size * 180));
                send(&mut ui, ctrl_v()).await;
            }
            ui.editor.insert_paste("compare these");
            let mut terminal =
                Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
            ui.draw(
                &mut terminal,
                &ConnectionInfo {
                    backend_version: "test".into(),
                },
            )
            .unwrap();
            let buffer = terminal.backend().buffer();
            let mut html = String::from(
                "<html><head><meta charset='utf-8'></head><body style='margin:0;background:#000'><pre style='font:14px/1.2 Menlo,monospace;margin:0'>",
            );
            for y in 0..buffer.area.height {
                for x in 0..buffer.area.width {
                    let cell = &buffer[(x, y)];
                    let color = |color: ratatui::style::Color, fallback: &str| match color {
                        ratatui::style::Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
                        _ => fallback.to_owned(),
                    };
                    let symbol = match cell.symbol() {
                        "<" => "&lt;",
                        ">" => "&gt;",
                        "&" => "&amp;",
                        other => other,
                    };
                    html.push_str(&format!(
                        "<span style='color:{};background:{}'>{symbol}</span>",
                        color(cell.fg, "#ccc"),
                        color(cell.bg, "#000")
                    ));
                }
                html.push('\n');
            }
            html.push_str("</pre></body></html>");
            std::fs::write(directory.join(format!("{theme_name}-{width}.html")), html).unwrap();
        }
    }
}

#[tokio::test]
async fn refused_clipboard_image_falls_back_to_text() {
    let (mut ui, clipboard) = ui_with_clipboard();
    ui.state.model_catalog = Some(catalog_with_images(false));
    {
        let mut state = clipboard.lock().unwrap();
        state.image_pastes.push_back(red_pixels(1, 1));
        state.pastes.push_back(Ok("cell text".into()));
    }

    send(&mut ui, ctrl_v()).await;

    assert!(ui.images.is_empty());
    assert_eq!(ui.editor.text(), "cell text");
    assert!(
        ui.notice
            .as_deref()
            .unwrap()
            .starts_with("Pasted the clipboard text")
    );
}

#[tokio::test]
async fn refused_image_path_pastes_as_text() {
    let directory = std::env::temp_dir().join(format!("wisp-refused-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let image = directory.join("logo.png");
    let png = composer_images::AttachedImage::from_rgba(1, 1, &[0, 0, 0, 255]).unwrap();
    std::fs::write(
        &image,
        base64::engine::general_purpose::STANDARD
            .decode(png.wire_value().as_str().unwrap())
            .unwrap(),
    )
    .unwrap();
    let (mut ui, _clipboard) = ui_with_clipboard();
    ui.state.model_catalog = Some(catalog_with_images(false));

    send(&mut ui, Input::Paste(image.display().to_string())).await;

    assert!(ui.images.is_empty());
    assert_eq!(ui.editor.text(), image.display().to_string());
    assert!(
        ui.notice
            .as_deref()
            .unwrap()
            .starts_with("Pasted the path as text")
    );
    std::fs::remove_dir_all(&directory).unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn unreadable_path_pastes_as_text() {
    use std::os::unix::fs::PermissionsExt;
    let directory = std::env::temp_dir().join(format!("wisp-unreadable-{}", std::process::id()));
    std::fs::create_dir_all(&directory).unwrap();
    let locked = directory.join("locked.png");
    std::fs::write(&locked, b"\x89PNG\r\n\x1a\n").unwrap();
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
    let (mut ui, _clipboard) = ui_with_clipboard();

    send(&mut ui, Input::Paste(locked.display().to_string())).await;

    assert!(ui.images.is_empty());
    assert_eq!(ui.editor.text(), locked.display().to_string());
    std::fs::remove_dir_all(&directory).unwrap();
}

fn split_tone_image(width: usize, height: usize) -> ClipboardImage {
    let mut rgba = Vec::with_capacity(width * height * 4);
    for y in 0..height {
        let color = if y < height / 2 {
            [220, 20, 20, 255]
        } else {
            [20, 40, 220, 255]
        };
        for _ in 0..width {
            rgba.extend_from_slice(&color);
        }
    }
    ClipboardImage {
        width,
        height,
        rgba,
    }
}

fn encode_png_rgba(width: u32, height: u32, rgba: &[u8]) -> Vec<u8> {
    use image::ImageEncoder as _;
    let mut png = Vec::new();
    let encoder = image::codecs::png::PngEncoder::new(&mut png);
    encoder
        .write_image(rgba, width, height, image::ExtendedColorType::Rgba8)
        .unwrap();
    png
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(bytes);
    let mut hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

fn draw_buffer(ui: &mut LiveUi, width: u16, height: u16) -> ratatui::buffer::Buffer {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    terminal.backend().buffer().clone()
}

fn buffer_lines(buffer: &ratatui::buffer::Buffer) -> Vec<String> {
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect()
}

#[tokio::test]
async fn pasted_prompt_renders_half_block_thumbnail_below_image_label() {
    let (mut ui, clipboard) = ui_with_clipboard();
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(split_tone_image(32, 32));
    send(&mut ui, ctrl_v()).await;
    ui.editor.insert_paste("check shot");
    send(&mut ui, key(KeyCode::Enter)).await;

    let buffer = draw_buffer(&mut ui, 80, 24);
    let lines = buffer_lines(&buffer);
    let label_row = lines
        .iter()
        .position(|line| line.contains("[image 32×32 png]"))
        .unwrap_or_else(|| panic!("missing image label in:\n{}", lines.join("\n")));

    assert!(lines[label_row + 1].contains('▀'), "{}", lines.join("\n"));
    let thumb_x = lines[label_row + 1]
        .chars()
        .take_while(|c| *c != '▀')
        .count() as u16;
    let first_cell = &buffer[(thumb_x, (label_row + 1) as u16)];
    let second_cell = &buffer[(thumb_x, (label_row + 2) as u16)];
    assert_ne!(first_cell.fg, second_cell.bg);
}

#[tokio::test]
async fn stored_history_and_corrupt_tool_images_render_and_degrade_cleanly() {
    let dir = std::env::temp_dir().join(format!("wisp-stored-preview-{}", std::process::id()));
    let images_dir = dir.join("images");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&images_dir).unwrap();

    let clip = split_tone_image(32, 32);
    let png = encode_png_rgba(32, 32, &clip.rgba);
    let valid_sha = sha256_hex(&png);
    std::fs::write(images_dir.join(&valid_sha), &png).unwrap();

    // Tampered file (wrong digest) for the tool result so it must degrade without blank rows.
    let bad_sha = "f".repeat(64);
    let mut bad_png = png.clone();
    bad_png[0] ^= 0xff;
    std::fs::write(images_dir.join(&bad_sha), &bad_png).unwrap();

    let messages = [
        serde_json::json!({
            "entry_id": "entry-user",
            "role": "user",
            "content": "inspect these",
            "content_truncated": false,
            "images": [{
                "media_type": "image/png",
                "sha256": valid_sha,
                "byte_size": png.len(),
                "width": 32,
                "height": 32,
            }],
        }),
        serde_json::json!({
            "entry_id": "entry-tool",
            "role": "tool",
            "tool_call_id": "read-bad",
            "tool_name": "read",
            "tool_arguments": {"path": "tampered.png"},
            "content": "Image file tampered.png",
            "content_truncated": false,
            "images": [{
                "media_type": "image/png",
                "sha256": bad_sha,
                "byte_size": bad_png.len(),
                "width": 32,
                "height": 32,
            }],
        }),
    ];

    let projected = crate::history::project_chronological_history(messages.iter()).unwrap();
    let mut ui = LiveUi::default();
    ui.state.transcript = projected.transcript;
    ui.state.selected_session = Some(reducer::SessionIdentity {
        session_id: "s1".into(),
        session_path: dir.join("session.jsonl").display().to_string(),
        session_name: None,
    });

    let buffer = draw_buffer(&mut ui, 80, 24);
    let lines = buffer_lines(&buffer);
    let joined = lines.join("\n");
    let user_label = lines
        .iter()
        .position(|line| line.contains("[image 32×32 png]") && !line.contains("Read"))
        .unwrap_or_else(|| panic!("missing user image label:\n{joined}"));
    assert!(lines[user_label + 1].contains('▀'), "{joined}");

    let tool_row = lines
        .iter()
        .position(|line| line.contains("Read") && line.contains("[image 32×32 png]"))
        .unwrap_or_else(|| panic!("missing tool card row:\n{joined}"));
    // Corrupt stored image must emit no half-block or orphan connector row under Read.
    assert!(!lines[tool_row + 1].contains('▀'), "{joined}");
    assert!(!lines[tool_row + 1].contains('└'), "{joined}");

    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn overlay_occludes_thumbnail_without_leaking_half_blocks() {
    let (mut ui, clipboard) = ui_with_clipboard();
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(split_tone_image(288, 128));
    send(&mut ui, ctrl_v()).await;
    send(&mut ui, key(KeyCode::Enter)).await;

    let unoccluded = draw_buffer(&mut ui, 80, 24);
    assert!(
        buffer_lines(&unoccluded)
            .iter()
            .any(|line| line.contains('▀'))
    );

    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    let occluded = draw_buffer(&mut ui, 80, 24);
    let popup = ui::overlay_area(ratatui::layout::Rect::new(0, 0, 80, 24)).unwrap();
    let mut overlaps = false;
    for y in popup.top()..popup.bottom() {
        for x in popup.left()..popup.right() {
            if unoccluded[(x, y)].symbol() == "▀" {
                overlaps = true;
            }
            assert_ne!(
                occluded[(x, y)].symbol(),
                "▀",
                "half-block leaked into overlay at ({x}, {y})"
            );
        }
    }
    assert!(overlaps, "test overlay did not overlap thumbnail");
}

struct RecordingBackend {
    inner: ratatui::backend::TestBackend,
    events: Vec<String>,
}

impl RecordingBackend {
    fn new(width: u16, height: u16) -> Self {
        Self {
            inner: ratatui::backend::TestBackend::new(width, height),
            events: Vec::new(),
        }
    }

    fn take_events(&mut self) -> Vec<String> {
        std::mem::take(&mut self.events)
    }
}

impl ratatui::backend::Backend for RecordingBackend {
    type Error = std::convert::Infallible;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a ratatui::buffer::Cell)>,
    {
        self.inner.draw(content)
    }

    fn hide_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.hide_cursor()
    }

    fn show_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.show_cursor()
    }

    fn get_cursor_position(&mut self) -> Result<ratatui::layout::Position, Self::Error> {
        self.inner.get_cursor_position()
    }

    fn set_cursor_position<P: Into<ratatui::layout::Position>>(
        &mut self,
        position: P,
    ) -> Result<(), Self::Error> {
        self.inner.set_cursor_position(position)
    }

    fn clear(&mut self) -> Result<(), Self::Error> {
        self.inner.clear()
    }

    fn clear_region(&mut self, clear_type: ratatui::backend::ClearType) -> Result<(), Self::Error> {
        self.inner.clear_region(clear_type)
    }

    fn append_lines(&mut self, n: u16) -> Result<(), Self::Error> {
        self.inner.append_lines(n)
    }

    fn size(&self) -> Result<ratatui::layout::Size, Self::Error> {
        self.inner.size()
    }

    fn window_size(&mut self) -> Result<ratatui::backend::WindowSize, Self::Error> {
        self.inner.window_size()
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        self.events.push("flush".into());
        self.inner.flush()
    }
}

impl terminal::FrameBackend for RecordingBackend {
    fn begin_frame(&mut self) -> std::io::Result<()> {
        self.events.push("begin".into());
        Ok(())
    }

    fn end_frame(&mut self) -> std::io::Result<()> {
        self.events.push("end".into());
        Ok(())
    }

    fn into_io_error(error: std::convert::Infallible) -> std::io::Error {
        match error {}
    }

    fn write_control(&mut self, _bytes: &[u8]) -> std::io::Result<()> {
        Ok(())
    }

    fn write_frame_bytes(&mut self, bytes: &[u8]) -> std::io::Result<()> {
        self.events
            .push(format!("bytes:{}", String::from_utf8_lossy(bytes)));
        Ok(())
    }

    fn suspend(&mut self) -> std::io::Result<terminal::Suspended> {
        self.inner.suspend()
    }

    fn resume(&mut self, suspended: terminal::Suspended) -> std::io::Result<()> {
        self.inner.resume(suspended)
    }
}

#[tokio::test]
async fn kitty_protocol_recording_backend_emits_sync_updates_occludes_scrolls_and_resizes() {
    let (mut ui, clipboard) = ui_with_clipboard();
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(split_tone_image(288, 96));
    send(&mut ui, ctrl_v()).await;
    send(&mut ui, key(KeyCode::Enter)).await;
    // Append extra lines after the prompt so scrolling up unpins and crops the thumbnail.
    let long_reply = (0..12)
        .map(|i| format!("reply line {i}"))
        .collect::<Vec<_>>()
        .join("\n");
    ui.state.transcript.complete_message(1, long_reply);

    let mut terminal = Terminal::new(RecordingBackend::new(80, 20)).unwrap();
    let conn = ConnectionInfo {
        backend_version: "test".into(),
    };

    // Frame 1: emits Kitty placement inside synchronized begin..flush..bytes..end.
    ui.draw(&mut terminal, &conn).unwrap();
    let f1 = terminal.backend_mut().take_events();
    assert_eq!(f1.first().map(String::as_str), Some("begin"));
    assert_eq!(f1.last().map(String::as_str), Some("end"));
    let f1_bytes = f1
        .iter()
        .find_map(|event| event.strip_prefix("bytes:"))
        .expect("expected Kitty payload");
    assert!(f1_bytes.starts_with("\x1b7") && f1_bytes.ends_with("\x1b8"));
    assert!(f1_bytes.contains("a=T,f=32,") && f1_bytes.contains(",q=2,"));

    // Frame 2 (unchanged): emits zero graphics bytes.
    ui.draw(&mut terminal, &conn).unwrap();
    let f2 = terminal.backend_mut().take_events();
    assert!(
        !f2.iter().any(|event| event.starts_with("bytes:")),
        "{f2:?}"
    );

    // Frame 3: scroll up to top, then down 3 rows so RowPosition::Image { index: 0, row: 1 } is top.
    ui.transcript_viewport.reduce(
        crate::transcript_view::TranscriptViewAction::ScrollLines(-30),
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    ui.transcript_viewport.reduce(
        crate::transcript_view::TranscriptViewAction::ScrollLines(3),
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    ui.draw(&mut terminal, &conn).unwrap();
    let f3 = terminal.backend_mut().take_events();
    let f3_bytes = f3
        .iter()
        .find_map(|event| event.strip_prefix("bytes:"))
        .expect("expected scroll deletion+re-emit");
    assert!(
        f3_bytes.contains("\x1b_Ga=d,d=i,i=1,q=2\x1b\\"),
        "{f3_bytes}"
    );
    assert!(f3_bytes.contains("a=T,f=32,"), "{f3_bytes}");

    // Frame 4 (`Input::Redraw` resize invalidation): forces delete + re-emit at same viewport.
    send(&mut ui, Input::Redraw).await;
    ui.draw(&mut terminal, &conn).unwrap();
    let f4 = terminal.backend_mut().take_events();
    let f4_bytes = f4
        .iter()
        .find_map(|event| event.strip_prefix("bytes:"))
        .expect("expected redraw deletion+re-emit");
    assert!(
        f4_bytes.contains("\x1b_Ga=d,d=i,i=2,q=2\x1b\\"),
        "{f4_bytes}"
    );
    assert!(f4_bytes.contains("a=T,f=32,"), "{f4_bytes}");

    // Frame 5 (overlay opens over thumbnail): deletes active Kitty placement.
    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    ui.draw(&mut terminal, &conn).unwrap();
    let f5 = terminal.backend_mut().take_events();
    let f5_bytes = f5
        .iter()
        .find_map(|event| event.strip_prefix("bytes:"))
        .expect("expected occlusion deletion");
    assert!(
        f5_bytes.contains("\x1b_Ga=d,d=i,i=3,q=2\x1b\\"),
        "{f5_bytes}"
    );
    assert!(!f5_bytes.contains("a=T,"), "{f5_bytes}");
    assert!(!ui.image_placements.has_active_kitty());
}

#[tokio::test]
async fn overlay_occludes_tool_card_thumbnail_on_matching_background() {
    let dir = std::env::temp_dir().join(format!("wisp-tool-overlay-{}", std::process::id()));
    let images_dir = dir.join("images");
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&images_dir).unwrap();

    let clip = split_tone_image(288, 128);
    let png = encode_png_rgba(288, 128, &clip.rgba);
    let sha = sha256_hex(&png);
    std::fs::write(images_dir.join(&sha), &png).unwrap();

    let mut ui = LiveUi::default();
    ui.state.selected_session = Some(reducer::SessionIdentity {
        session_id: "s-tool-occ".into(),
        session_path: dir.join("session.jsonl").display().to_string(),
        session_name: None,
    });
    for event in [
        serde_json::json!({
            "type": "tool.call",
            "call_id": "read-occ-1",
            "name": "read",
            "arguments": {"path": "shot.png"}
        }),
        serde_json::json!({
            "type": "tool.result",
            "message_entry_id": null,
            "call_id": "read-occ-1",
            "name": "read",
            "output": "shot.png",
            "is_error": false,
            "images": [{
                "media_type": "image/png",
                "sha256": sha,
                "byte_size": png.len(),
                "width": 288,
                "height": 128,
            }]
        }),
    ] {
        match BackendEvent::from_projection_value(&event).unwrap() {
            BackendEvent::ToolCall(call) => {
                ui.state.transcript.observe_tool_call(call);
            }
            BackendEvent::ToolResult(result) => {
                ui.state.transcript.observe_tool_result(*result);
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    // Half-block over default solid theme (where palette.base() == palette.overlay()).
    let unoccluded = draw_buffer(&mut ui, 80, 24);
    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    let occluded = draw_buffer(&mut ui, 80, 24);
    let popup = ui::overlay_area(ratatui::layout::Rect::new(0, 0, 80, 24)).unwrap();
    let mut overlaps = false;
    for y in popup.top()..popup.bottom() {
        for x in popup.left()..popup.right() {
            if unoccluded[(x, y)].symbol() == "▀" {
                overlaps = true;
            }
            assert_ne!(
                occluded[(x, y)].symbol(),
                "▀",
                "tool-card half-block leaked into overlay at ({x}, {y})"
            );
        }
    }
    assert!(overlaps, "test overlay did not overlap tool thumbnail");

    // Kitty placement covering blank overlay cells must also be deleted.
    ui.key_help = None;
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    let mut terminal = Terminal::new(RecordingBackend::new(80, 24)).unwrap();
    let conn = ConnectionInfo {
        backend_version: "test".into(),
    };
    ui.draw(&mut terminal, &conn).unwrap();
    let emit_events = terminal.backend_mut().take_events();
    assert!(emit_events.iter().any(|ev| ev.contains("a=T,f=32,")));

    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    ui.draw(&mut terminal, &conn).unwrap();
    let occ_events = terminal.backend_mut().take_events();
    let occ_bytes = occ_events
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty delete on tool-card overlay occlusion");
    assert!(occ_bytes.contains("a=d,d=i,i=1,q=2"), "{occ_bytes}");
    assert!(!occ_bytes.contains("a=T,"), "{occ_bytes}");

    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test]
async fn redraw_while_key_help_open_reemits_kitty_placement() {
    let (mut ui, clipboard) = ui_with_clipboard();
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    clipboard
        .lock()
        .unwrap()
        .image_pastes
        .push_back(split_tone_image(32, 32));
    send(&mut ui, ctrl_v()).await;
    send(&mut ui, key(KeyCode::Enter)).await;

    // On an 80×50 screen, key_help overlay sits at y=11..39 while top prompt stays unoccluded.
    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    let mut terminal = Terminal::new(RecordingBackend::new(80, 50)).unwrap();
    let conn = ConnectionInfo {
        backend_version: "test".into(),
    };

    ui.draw(&mut terminal, &conn).unwrap();
    let f1 = terminal.backend_mut().take_events();
    assert!(
        f1.iter()
            .any(|ev| ev.contains("a=T,f=32,") && ev.contains(",i=1,"))
    );

    send(&mut ui, Input::Redraw).await;
    assert!(ui.key_help.is_some());
    ui.draw(&mut terminal, &conn).unwrap();
    let f2 = terminal.backend_mut().take_events();
    let f2_bytes = f2
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty delete + re-emit after Input::Redraw while key_help is open");
    assert!(
        f2_bytes.contains("\x1b_Ga=d,d=i,i=1,q=2\x1b\\"),
        "{f2_bytes}"
    );
    assert!(
        f2_bytes.contains("a=T,f=32,") && f2_bytes.contains(",i=2,"),
        "{f2_bytes}"
    );
}

#[tokio::test]
async fn iterm2_and_sixel_recording_backend_mark_hidden_cells_and_reemit_after_occlusion() {
    for (protocol, needle) in [
        (
            terminal_images::ImageProtocol::ITerm2,
            "\x1b]1337;File=inline=1;",
        ),
        (terminal_images::ImageProtocol::Sixel, "\x1bP0;1q"),
    ] {
        let (mut ui, clipboard) = ui_with_clipboard();
        ui.image_protocol = protocol;
        clipboard
            .lock()
            .unwrap()
            .image_pastes
            .push_back(split_tone_image(288, 128));
        send(&mut ui, ctrl_v()).await;
        send(&mut ui, key(KeyCode::Enter)).await;

        let mut terminal = Terminal::new(RecordingBackend::new(80, 24)).unwrap();
        let conn = ConnectionInfo {
            backend_version: "test".into(),
        };

        ui.draw(&mut terminal, &conn).unwrap();
        let f1 = terminal.backend_mut().take_events();
        let f1_bytes = f1
            .iter()
            .find_map(|ev| ev.strip_prefix("bytes:"))
            .expect("expected native image payload");
        assert!(f1_bytes.starts_with("\x1b7") && f1_bytes.ends_with("\x1b8"));
        assert!(f1_bytes.contains(needle), "{f1_bytes}");
        assert!(
            terminal
                .backend()
                .inner
                .buffer()
                .content
                .iter()
                .any(|cell| cell.modifier.contains(ratatui::style::Modifier::HIDDEN)),
            "expected HIDDEN cells for {protocol:?}"
        );

        // Unchanged second frame emits nothing.
        ui.draw(&mut terminal, &conn).unwrap();
        let f2 = terminal.backend_mut().take_events();
        assert!(!f2.iter().any(|ev| ev.starts_with("bytes:")), "{f2:?}");

        // Overlay occludes placement; uncovered left edge falls back to half-blocks.
        ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
        ui.draw(&mut terminal, &conn).unwrap();
        let f3 = terminal.backend_mut().take_events();
        assert!(!f3.iter().any(|ev| ev.starts_with("bytes:")), "{f3:?}");

        // Dismissing overlay re-emits the native placement cleanly.
        ui.key_help = None;
        ui.draw(&mut terminal, &conn).unwrap();
        let f4 = terminal.backend_mut().take_events();
        let f4_bytes = f4
            .iter()
            .find_map(|ev| ev.strip_prefix("bytes:"))
            .expect("expected re-emit after dismissing overlay");
        assert!(f4_bytes.contains(needle), "{f4_bytes}");
    }
}
