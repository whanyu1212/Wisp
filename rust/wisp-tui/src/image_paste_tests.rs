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
