//! Live browser viewport pane integration, rendering, occlusion, and protocol tests.

use super::*;
use crate::side_panel::PanelMode;
use image::ImageEncoder;
use ratatui::buffer::CellDiffOption;
use ratatui::layout::Rect;
use sha2::Digest;

struct TempSessionDir(std::path::PathBuf);

impl TempSessionDir {
    fn new(tag: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "wisp-browser-viewport-{tag}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("images")).unwrap();
        Self(dir)
    }

    fn session_path(&self) -> String {
        self.0.join("session.jsonl").display().to_string()
    }

    fn images_dir(&self) -> std::path::PathBuf {
        self.0.join("images")
    }
}

impl Drop for TempSessionDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct RecordingBackend {
    inner: ratatui::backend::TestBackend,
    events: Vec<String>,
    drawn_coords: Vec<(u16, u16)>,
}

impl RecordingBackend {
    fn new(width: u16, height: u16) -> Self {
        Self {
            inner: ratatui::backend::TestBackend::new(width, height),
            events: Vec::new(),
            drawn_coords: Vec::new(),
        }
    }

    fn take_events(&mut self) -> Vec<String> {
        std::mem::take(&mut self.events)
    }

    fn take_drawn_coords(&mut self) -> Vec<(u16, u16)> {
        std::mem::take(&mut self.drawn_coords)
    }
}

impl ratatui::backend::Backend for RecordingBackend {
    type Error = std::convert::Infallible;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a ratatui::buffer::Cell)>,
    {
        let cells: Vec<_> = content.collect();
        self.drawn_coords
            .extend(cells.iter().map(|&(x, y, _)| (x, y)));
        self.inner.draw(cells.into_iter())
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

fn conn() -> ConnectionInfo {
    ConnectionInfo {
        backend_version: "test".into(),
    }
}

fn write_split_tone_png(images_dir: &std::path::Path, width: u32, height: u32) -> (String, usize) {
    let mut rgba = Vec::with_capacity((width * height * 4) as usize);
    for y in 0..height {
        let color = if y < height / 2 {
            [220u8, 24, 24, 255]
        } else {
            [24u8, 60, 220, 255]
        };
        for _ in 0..width {
            rgba.extend_from_slice(&color);
        }
    }
    let mut png = Vec::new();
    image::codecs::png::PngEncoder::new(&mut png)
        .write_image(&rgba, width, height, image::ExtendedColorType::Rgba8)
        .unwrap();
    let digest = format!("{:x}", sha2::Sha256::digest(&png));
    let len = png.len();
    std::fs::write(images_dir.join(&digest), &png).unwrap();
    (digest, len)
}

async fn push_backend_event(ui: &mut LiveUi, payload: serde_json::Value) {
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

async fn run_command(ui: &mut LiveUi, command: Command) {
    let (writer, _receiver) = mpsc::channel(8);
    ui.handle_command(command, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
}

async fn send_input(ui: &mut LiveUi, input: Input) {
    let (writer, _receiver) = mpsc::channel(8);
    ui.handle_input(input, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
}

fn viewport_event(
    timestamp: &str,
    image: Option<(&str, usize, u32, u32)>,
    url: Option<&str>,
    title: Option<&str>,
    source_tool: Option<&str>,
) -> serde_json::Value {
    serde_json::json!({
        "type": "browser.viewport.updated",
        "timestamp": timestamp,
        "image": image.map(|(sha, byte_size, w, h)| serde_json::json!({
            "media_type": "image/png",
            "sha256": sha,
            "byte_size": byte_size,
            "width": w,
            "height": h,
        })),
        "url": url,
        "title": title,
        "viewport_width": 1280,
        "viewport_height": 720,
        "source_tool": source_tool,
        "call_id": "call-browser-1",
    })
}

fn buffer_text(buffer: &ratatui::buffer::Buffer) -> String {
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn draw_text(ui: &mut LiveUi, width: u16, height: u16) -> (ratatui::buffer::Buffer, String) {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let buf = terminal.backend().buffer().clone();
    let text = buffer_text(&buf);
    (buf, text)
}

fn ui_with_session(dir: &TempSessionDir) -> LiveUi {
    let mut ui = LiveUi::default();
    ui.state.workspace_cwd = Some(dir.0.display().to_string());
    ui.state.selected_session = Some(reducer::SessionIdentity {
        session_id: "sess-browser".into(),
        session_path: dir.session_path(),
        session_name: Some("Browser Session".into()),
    });
    ui
}

#[tokio::test]
async fn docked_auto_switch_manual_tab_and_session_reset() {
    let dir = TempSessionDir::new("docked");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = ui_with_session(&dir);

    // Warm the docked width before the first browser event arrives.
    let (_buf, initial) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(!initial.contains("Work · Browser"));

    // First image-bearing frame auto-switches docked side panel to Browser.
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://example.com/app"),
            Some("Example Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    let (buf, screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen.contains("Work · Browser"), "{screen}");
    assert!(screen.contains("Example Page"), "{screen}");
    assert!(screen.contains("12:34:56Z"), "{screen}");
    assert!(screen.contains("https://example.com/app"), "{screen}");
    assert!(screen.contains("1280×720 · playwright"), "{screen}");
    let panel_rect = ui.side_panel.split(buf.area).1;
    let has_pixels = (panel_rect.top()..panel_rect.bottom())
        .any(|y| (panel_rect.left()..panel_rect.right()).any(|x| buf[(x, y)].symbol() == "▀"));
    assert!(has_pixels, "expected half-block viewport in side panel");

    // Click the "Work" tab on the Work · Browser row inside the panel body to return to LiveWork.
    let work_pos = (panel_rect.top()..panel_rect.bottom())
        .find_map(|y| {
            let row_text: String = (panel_rect.left()..panel_rect.right())
                .map(|x| buf[(x, y)].symbol())
                .collect();
            row_text
                .find("Work · Browser")
                .map(|dx| (panel_rect.left() + dx as u16, y))
        })
        .expect("Work · Browser row in panel");
    assert_eq!(
        ui.side_panel.click(work_pos.0, work_pos.1),
        side_panel::OverlayKey::Moved
    );
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);

    // Subsequent frame updates respect the user's manual switch back to LiveWork.
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:00Z",
            Some((&sha, len, 256, 128)),
            Some("https://example.com/next"),
            Some("Second Page"),
            Some("mcp__playwright__browser_click"),
        ),
    )
    .await;
    let (_buf, live_screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(live_screen.contains("Work · Browser"), "{live_screen}");
    assert!(live_screen.contains("Browser Session"), "{live_screen}");

    // Workspace change clears browser state, returns to LiveWork, and re-arms first-frame auto-switch.
    let next_cwd = dir.0.join("other-repo");
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.workspace",
            "cwd": next_cwd.display().to_string(),
            "git_branch": "main",
        }),
    )
    .await;
    let (_buf, reset_screen) = draw_text(&mut ui, 160, 36);
    assert!(ui.state.browser_viewport.is_none());
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(!reset_screen.contains("Work · Browser"), "{reset_screen}");

    // Next session's first image frame auto-switches again.
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:36:00Z",
            Some((&sha, len, 256, 128)),
            Some("https://example.com/rearmed"),
            Some("Rearmed Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;
    let (_buf, rearmed_screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(rearmed_screen.contains("Rearmed Page"), "{rearmed_screen}");
}

#[tokio::test]
async fn pre_frame_browser_command_stays_open_and_toggles() {
    let dir = TempSessionDir::new("preframe");
    let mut ui = ui_with_session(&dir);
    let _ = draw_text(&mut ui, 160, 36);

    run_command(&mut ui, Command::Browser).await;
    let (_buf, screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen.contains("No browser frame yet."), "{screen}");

    // Second frame keeps Browser open rather than snapping back to LiveWork.
    let (_buf, second_screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(second_screen.contains("No browser frame yet."));

    // Running /browser again toggles back to LiveWork on a docked panel.
    run_command(&mut ui, Command::Browser).await;
    let (_buf, toggled_back) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(!toggled_back.contains("Work · Browser"));
}

#[tokio::test]
async fn narrow_terminal_does_not_auto_pop_overlay_and_browser_command_opens_without_occlusion() {
    let dir = TempSessionDir::new("narrow");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = ui_with_session(&dir);

    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://narrow.test"),
            Some("Narrow Page"),
            Some("mcp__playwright__browser_snapshot"),
        ),
    )
    .await;

    // Narrow 100×28 terminal must not auto-pop SidePanel overlay.
    let (_buf, narrow) = draw_text(&mut ui, 100, 28);
    assert!(!ui.side_panel.overlay_open());
    assert!(!narrow.contains("Narrow Page"), "{narrow}");

    // `/browser` opens SidePanel overlay in Browser mode without self-occlusion (HalfBlock).
    run_command(&mut ui, Command::Browser).await;
    let (overlay_buf, overlay_screen) = draw_text(&mut ui, 100, 28);
    assert!(ui.side_panel.overlay_open());
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(overlay_screen.contains("Narrow Page"), "{overlay_screen}");
    assert!(
        overlay_buf.content.iter().any(|cell| cell.symbol() == "▀"),
        "expected unoccluded half-block preview inside SidePanel overlay"
    );

    // Focus inside narrow `/browser` starts on the "Browser" tab (Enter stays in Browser),
    // and Tab moves focus to "Work" where Enter switches back to LiveWork once painted.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);

    let _ = draw_text(&mut ui, 100, 28);
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)),
    )
    .await;
    let _ = draw_text(&mut ui, 100, 28);
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(ui.side_panel.overlay_open());

    // Switch back to Browser for the Kitty overlay check.
    run_command(&mut ui, Command::Browser).await;
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);

    // Switching to Kitty inside the SidePanel overlay emits native frame bytes (no self-occlusion).
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    let mut terminal = Terminal::new(RecordingBackend::new(100, 28)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let events = terminal.backend_mut().take_events();
    let bytes = events
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty payload in narrow SidePanel overlay");
    assert!(bytes.contains("a=T,f=32,"), "{bytes}");

    // `b` switches tab inside overlay to LiveWork; `Esc` closes overlay.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(ui.side_panel.overlay_open());

    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(!ui.side_panel.overlay_open());
}

#[tokio::test]
async fn hostile_browser_metadata_is_sanitized() {
    let dir = TempSessionDir::new("sanitize");
    let mut ui = ui_with_session(&dir);
    run_command(&mut ui, Command::Browser).await;

    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            None,
            Some("https://safe.test/\x1b]52;c;c2VjcmV0\x07path"),
            Some("Safe\x1b[31mTitle\u{202E}Here"),
            Some("mcp__play\x1b[1mwright__click"),
        ),
    )
    .await;

    let (_buf, screen) = draw_text(&mut ui, 160, 36);
    assert!(
        screen.contains("Safe\u{FFFD}[31mTitle\u{FFFD}Here"),
        "{screen}"
    );
    assert!(
        screen.contains("https://safe.test/\u{FFFD}]52;c;c2VjcmV0\u{FFFD}path"),
        "{screen}"
    );
    assert!(
        screen.contains("1280×720 · play\u{FFFD}[1mwright"),
        "{screen}"
    );
    assert!(
        screen.contains("Metadata updated (no screenshot returned)."),
        "{screen}"
    );
    assert!(!screen.contains('\x1b'));
    assert!(!screen.contains('\x07'));
    assert!(!screen.contains('\u{202E}'));
}

#[tokio::test]
async fn key_help_occludes_docked_and_f8_focused_kitty_browser_viewport() {
    let dir = TempSessionDir::new("occlusion");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 288, 160);
    let mut ui = ui_with_session(&dir);
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 288, 160)),
            Some("https://occlusion.test"),
            Some("Occlusion Test"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    // 148×36 ensures centered key_help popup (x=24..124) overlaps docked side panel (x=104..148).
    let mut terminal = Terminal::new(RecordingBackend::new(148, 36)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let f1 = terminal.backend_mut().take_events();
    assert!(
        f1.iter().any(|ev| ev.contains("a=T,f=32,")),
        "expected initial Kitty emit: {f1:?}"
    );

    // Open key_help over docked Browser panel -> deletes Kitty placement.
    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    ui.draw(&mut terminal, &conn()).unwrap();
    let f2 = terminal.backend_mut().take_events();
    let f2_bytes = f2
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty delete on key_help occlusion");
    assert!(f2_bytes.contains("a=d,d=i,"), "{f2_bytes}");
    assert!(!f2_bytes.contains("a=T,"), "{f2_bytes}");

    // Dismiss key_help -> re-emits Kitty placement.
    ui.key_help = None;
    ui.draw(&mut terminal, &conn()).unwrap();
    let f3 = terminal.backend_mut().take_events();
    assert!(f3.iter().any(|ev| ev.contains("a=T,f=32,")), "{f3:?}");

    // Focus the docked panel (`F8` style: docked() && overlay_open()) -> keeps placement active,
    // then opening key_help still occludes and deletes it cleanly.
    ui.side_panel.focus();
    ui.draw(&mut terminal, &conn()).unwrap();
    let _f4 = terminal.backend_mut().take_events();
    assert!(ui.image_placements.has_active_kitty());

    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    ui.draw(&mut terminal, &conn()).unwrap();
    let f5 = terminal.backend_mut().take_events();
    let f5_bytes = f5
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty delete when key_help covers F8-focused panel");
    assert!(f5_bytes.contains("a=d,d=i,"), "{f5_bytes}");
    assert!(!f5_bytes.contains("a=T,"), "{f5_bytes}");
}

#[tokio::test]
async fn native_protocols_use_cell_diff_option_and_clear_on_tab_switch() {
    let dir = TempSessionDir::new("diff-option");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);

    for (protocol, needle) in [
        (terminal_images::ImageProtocol::Kitty, "a=T,f=32,"),
        (
            terminal_images::ImageProtocol::ITerm2,
            "\x1b]1337;File=inline=1;",
        ),
        (terminal_images::ImageProtocol::Sixel, "\x1bP0;1q"),
    ] {
        let mut ui = ui_with_session(&dir);
        ui.image_protocol = protocol;
        push_backend_event(
            &mut ui,
            viewport_event(
                "2026-11-20T12:34:56Z",
                Some((&sha, len, 256, 128)),
                Some("https://diff.test"),
                Some("Diff Option Test"),
                Some("mcp__playwright__browser_navigate"),
            ),
        )
        .await;

        let mut terminal = Terminal::new(RecordingBackend::new(160, 36)).unwrap();

        // Frame 1: native bytes emitted, AlwaysUpdate cells, zero Modifier::HIDDEN.
        ui.draw(&mut terminal, &conn()).unwrap();
        let f1 = terminal.backend_mut().take_events();
        let _ = terminal.backend_mut().take_drawn_coords();
        let f1_bytes = f1
            .iter()
            .find_map(|ev| ev.strip_prefix("bytes:"))
            .unwrap_or_else(|| panic!("expected native payload for {protocol:?}"));
        assert!(f1_bytes.contains(needle), "{f1_bytes}");
        let buf1 = terminal.backend().inner.buffer();
        let mut image_coords = Vec::new();
        for y in 0..buf1.area.height {
            for x in 0..buf1.area.width {
                if buf1[(x, y)].diff_option == CellDiffOption::AlwaysUpdate {
                    image_coords.push((x, y));
                }
            }
        }
        assert!(
            !image_coords.is_empty(),
            "expected AlwaysUpdate on first frame for {protocol:?}"
        );
        assert!(
            !buf1
                .content
                .iter()
                .any(|c| c.modifier.contains(ratatui::style::Modifier::HIDDEN)),
            "unexpected Modifier::HIDDEN for {protocol:?}"
        );

        // Frame 2 (unchanged): emits zero graphics bytes and skips every image cell in diff.
        ui.draw(&mut terminal, &conn()).unwrap();
        let f2 = terminal.backend_mut().take_events();
        let f2_drawn = terminal.backend_mut().take_drawn_coords();
        assert!(
            !f2.iter().any(|ev| ev.starts_with("bytes:")),
            "unexpected bytes on steady frame for {protocol:?}: {f2:?}"
        );
        for &(x, y) in &image_coords {
            assert!(
                !f2_drawn.contains(&(x, y)),
                "steady frame should skip drawing image cell ({x}, {y}) for {protocol:?}"
            );
        }

        // Switching to LiveWork reverts every cell back to CellDiffOption::None and deletes Kitty.
        ui.side_panel.toggle_browser();
        ui.draw(&mut terminal, &conn()).unwrap();
        let f3 = terminal.backend_mut().take_events();
        let f3_drawn = terminal.backend_mut().take_drawn_coords();
        if protocol == terminal_images::ImageProtocol::Kitty {
            let f3_bytes = f3
                .iter()
                .find_map(|ev| ev.strip_prefix("bytes:"))
                .expect("expected Kitty delete when switching back to Work");
            assert!(f3_bytes.contains("a=d,d=i,"), "{f3_bytes}");
        }
        for &(x, y) in &image_coords {
            assert!(
                f3_drawn.contains(&(x, y)),
                "switching back to LiveWork should redraw previously skipped cell ({x}, {y}) for {protocol:?}"
            );
        }
        let buf3 = terminal.backend().inner.buffer();
        assert!(
            buf3.content
                .iter()
                .all(|c| c.diff_option == CellDiffOption::None),
            "expected all cells reverted to CellDiffOption::None for {protocol:?}"
        );
    }
}

#[tokio::test]
async fn border_resize_updates_geometry_and_corrupt_image_shows_fallback() {
    let dir = TempSessionDir::new("resize-corrupt");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 640, 320);
    let mut ui = ui_with_session(&dir);
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 640, 320)),
            Some("https://resize.test"),
            Some("Resize Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    let always_update_bounds = |buf: &ratatui::buffer::Buffer| -> (u16, u16, u16, u16) {
        let mut min_x = u16::MAX;
        let mut max_x = 0;
        let mut min_y = u16::MAX;
        let mut max_y = 0;
        for y in 0..buf.area.height {
            for x in 0..buf.area.width {
                if buf[(x, y)].diff_option == CellDiffOption::AlwaysUpdate {
                    min_x = min_x.min(x);
                    max_x = max_x.max(x);
                    min_y = min_y.min(y);
                    max_y = max_y.max(y);
                }
            }
        }
        assert!(
            min_x <= max_x && min_y <= max_y,
            "no AlwaysUpdate image bbox"
        );
        (min_x, max_x - min_x + 1, min_y, max_y - min_y + 1)
    };

    let mut terminal = Terminal::new(RecordingBackend::new(180, 40)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let initial_width = ui.side_panel.split(Rect::new(0, 0, 180, 40)).1.width;
    let initial_bounds = always_update_bounds(terminal.backend().inner.buffer());
    let _ = terminal.backend_mut().take_events();

    // Widen docked side panel via border drag (from column 128 leftwards to 108).
    let now = std::time::Instant::now();
    ui.side_panel.press_border(now, 0);
    let border_col = 180 - initial_width;
    ui.side_panel.continue_resize(
        crossterm::event::MouseEventKind::Drag(crossterm::event::MouseButton::Left),
        border_col - 20,
        now,
    );
    ui.side_panel.continue_resize(
        crossterm::event::MouseEventKind::Up(crossterm::event::MouseButton::Left),
        border_col - 20,
        now,
    );

    ui.draw(&mut terminal, &conn()).unwrap();
    let widened_width = ui.side_panel.split(Rect::new(0, 0, 180, 40)).1.width;
    assert!(
        widened_width > initial_width,
        "{widened_width} vs {initial_width}"
    );
    let widened_bounds = always_update_bounds(terminal.backend().inner.buffer());
    assert!(
        widened_bounds.1 > initial_bounds.1,
        "{widened_bounds:?} vs {initial_bounds:?}"
    );
    let f2 = terminal.backend_mut().take_events();
    let f2_bytes = f2
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected delete + re-emit on border resize");
    assert!(
        f2_bytes.contains("a=d,d=i,") && f2_bytes.contains("a=T,f=32,"),
        "{f2_bytes}"
    );

    // Corrupt/missing stored image falls back cleanly while preserving header metadata.
    let _ = std::fs::remove_file(dir.images_dir().join(&sha));
    ui.image_cache = terminal_images::ImageCache::default();
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:10Z",
            Some((
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                64,
                1280,
                720,
            )),
            Some("https://broken.test"),
            Some("Broken Page"),
            Some("mcp__playwright__browser_snapshot"),
        ),
    )
    .await;
    let (_buf, broken_screen) = draw_text(&mut ui, 180, 40);
    assert!(broken_screen.contains("Broken Page"), "{broken_screen}");
    assert!(
        broken_screen.contains("https://broken.test"),
        "{broken_screen}"
    );
    assert!(
        broken_screen.contains("Preview unavailable."),
        "{broken_screen}"
    );
}

#[tokio::test]
async fn fresh_and_new_sessions_resolve_frames_from_workspace_images_dir() {
    let dir = TempSessionDir::new("fresh-workspace-dir");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = LiveUi::default();
    assert!(ui.state.selected_session.is_none());

    // Startup `rpc.workspace` reports the store's shared `images` directory before any session is persisted.
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.workspace",
            "cwd": dir.0.display().to_string(),
            "git_branch": null,
            "session_images_dir": dir.images_dir().display().to_string(),
        }),
    )
    .await;

    let _ = draw_text(&mut ui, 160, 36);
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://fresh.test"),
            Some("Fresh Session Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    let (buf, screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen.contains("Fresh Session Page"), "{screen}");
    assert!(!screen.contains("Preview unavailable."), "{screen}");
    let panel_rect = ui.side_panel.split(buf.area).1;
    assert!(
        (panel_rect.top()..panel_rect.bottom()).any(|y| {
            (panel_rect.left()..panel_rect.right()).any(|x| buf[(x, y)].symbol() == "▀")
        }),
        "expected rendered half-block pixels during fresh session's first run"
    );

    // `/new` clears `selected_session` & `browser_viewport` while preserving `default_session_images_dir`.
    run_command(&mut ui, Command::Session(commands::SessionCommand::New)).await;
    let new_cmd_id = match &ui.state.session_operation {
        Some(reducer::SessionOperation::CreatingSession { command_id }) => command_id.clone(),
        other => panic!("expected CreatingSession operation, got {other:?}"),
    };
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.command.finished",
            "command_id": new_cmd_id,
            "command_type": "new_session",
            "ok": true,
            "error": null,
        }),
    )
    .await;
    assert!(ui.state.selected_session.is_none());
    assert!(ui.state.browser_viewport.is_none());
    let _ = draw_text(&mut ui, 160, 36);

    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:00Z",
            Some((&sha, len, 256, 128)),
            Some("https://after-new.test"),
            Some("After New Session"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;
    let (buf2, screen2) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen2.contains("After New Session"), "{screen2}");
    assert!(!screen2.contains("Preview unavailable."), "{screen2}");
    assert!(
        (panel_rect.top()..panel_rect.bottom()).any(|y| {
            (panel_rect.left()..panel_rect.right()).any(|x| buf2[(x, y)].symbol() == "▀")
        }),
        "expected rendered half-block pixels after /new"
    );
}
